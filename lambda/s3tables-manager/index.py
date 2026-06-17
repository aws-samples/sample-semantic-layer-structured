"""
S3 Tables Manager - Custom Resource Lambda Handler

Creates and manages S3 Tables infrastructure:
- Table Bucket
- Namespace
- Iceberg Tables — initialized via PyIceberg REST catalog (not boto3 only)

WHY PyIceberg for table creation:
  boto3 s3tables.create_table(format='ICEBERG') creates the S3 Table storage
  object but does NOT commit Iceberg metadata. A table without metadata returns
  "invalid_metadata_location" when PyIceberg REST catalog calls load_table().

  Using catalog.create_table() via the S3 Tables REST catalog endpoint performs
  both steps in one call: it creates the S3 Table AND commits the initial
  Iceberg metadata. This means load_table() works immediately, Lake Formation
  wildcard permissions cover the new table, and Glue gets a valid entry.
"""

import json
import boto3
import logging
from typing import Any, Dict

from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import StringType, NestedField
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3tables_client = boto3.client('s3tables')
glue = boto3.client('glue')

# Minimal bootstrap schema — pk + sk are present on every insurance table.
# The backfill Lambda evolves this schema with the real columns from DynamoDB.
BOOTSTRAP_SCHEMA = Schema(
    NestedField(field_id=1, name='pk', field_type=StringType(), required=False),
    NestedField(field_id=2, name='sk', field_type=StringType(), required=False),
)


def _get_catalog(table_bucket_arn: str, region: str):
    return load_catalog("s3tables", **{
        "type": "rest",
        "uri": f"https://s3tables.{region}.amazonaws.com/iceberg",
        "warehouse": table_bucket_arn,
        "rest.sigv4-enabled": "true",
        "rest.signing-region": region,
        "rest.signing-name": "s3tables",
    })


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """CloudFormation Custom Resource handler for S3 Tables."""
    request_type = event['RequestType']
    properties = event['ResourceProperties']

    table_bucket_name = properties.get('TableBucketName')
    namespace = properties.get('Namespace')
    tables = properties.get('Tables', [])
    region = properties.get('Region')

    logger.info(f"RequestType={request_type} bucket={table_bucket_name} namespace={namespace}")

    try:
        if request_type == 'Create':
            result = _create(table_bucket_name, namespace, tables, region)
            return {'PhysicalResourceId': result['TableBucketArn'], 'Data': result}

        elif request_type == 'Update':
            physical_resource_id = event['PhysicalResourceId']
            result = _update(physical_resource_id, table_bucket_name, namespace, tables, region)
            return {'PhysicalResourceId': physical_resource_id, 'Data': result}

        elif request_type == 'Delete':
            physical_resource_id = event['PhysicalResourceId']
            _delete(physical_resource_id, namespace, tables)
            return {'PhysicalResourceId': physical_resource_id}

        else:
            raise ValueError(f"Unknown request type: {request_type}")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)  # nosemgrep: logging-error-without-handling — top-level Lambda handler; ERROR ensures CloudWatch alarm visibility
        raise


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def _create(table_bucket_name: str, namespace: str, tables: list, region: str) -> Dict:
    logger.info("Creating S3 Tables infrastructure...")

    # 1. Table Bucket
    table_bucket_arn = _ensure_table_bucket(table_bucket_name)

    # 2. Namespace + tables — skipped when namespace is empty (Zero-ETL / batch mode).
    if namespace:
        _ensure_namespace(table_bucket_arn, namespace)

        # 3. Initialize each table via PyIceberg REST catalog
        catalog = _get_catalog(table_bucket_arn, region)
        for table_name in tables:
            _initialize_iceberg_table(catalog, namespace, table_name, table_bucket_arn)

    return {
        'TableBucketArn': table_bucket_arn,
        'Namespace': namespace,
        'TableCount': str(len(tables)),
    }


# ---------------------------------------------------------------------------
# UPDATE — recreate any tables that are in bad/uninitialized state
# ---------------------------------------------------------------------------

def _update(
    table_bucket_arn: str,
    table_bucket_name: str,
    namespace: str,
    tables: list,
    region: str,
) -> Dict:
    logger.info("Updating S3 Tables infrastructure...")

    catalog = _get_catalog(table_bucket_arn, region)

    for table_name in tables:
        table_path = f"{namespace}.{table_name}"
        needs_init = False

        try:
            catalog.load_table(table_path)
            logger.info(f"  {table_name}: already initialized, skipping")
            continue
        except (NoSuchTableError, Exception) as e:
            logger.info(f"  {table_name}: not loadable ({type(e).__name__}), will reinitialize")
            needs_init = True

        if needs_init:
            # Drop the S3 Table at the boto3 level to clear corrupted state
            _drop_s3table_boto3(table_bucket_arn, namespace, table_name)
            # Recreate via PyIceberg REST catalog (creates + initializes in one step)
            _initialize_iceberg_table(catalog, namespace, table_name, table_bucket_arn)

    return {
        'TableBucketArn': table_bucket_arn,
        'Namespace': namespace,
        'TableCount': str(len(tables)),
    }


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def _delete(table_bucket_arn: str, namespace: str, tables: list) -> None:
    logger.info("Deleting S3 Tables infrastructure...")

    # Delete Glue tables (best-effort)
    for table_name in tables:
        try:
            glue.delete_table(DatabaseName=namespace, Name=table_name)
            logger.info(f"  Deleted Glue table: {table_name}")
        except glue.exceptions.EntityNotFoundException:
            pass
        except Exception as e:
            logger.warning(f"  Could not delete Glue table {table_name}: {e}")

    # Delete S3 Tables
    for table_name in tables:
        _drop_s3table_boto3(table_bucket_arn, namespace, table_name)

    # Delete Namespace
    try:
        s3tables_client.delete_namespace(
            tableBucketARN=table_bucket_arn, namespace=[namespace]
        )
        logger.info(f"Deleted namespace: {namespace}")
    except s3tables_client.exceptions.NotFoundException:
        pass
    except Exception as e:
        logger.warning(f"Could not delete namespace {namespace}: {e}")

    # Delete Table Bucket
    try:
        s3tables_client.delete_table_bucket(tableBucketARN=table_bucket_arn)
        logger.info(f"Deleted table bucket: {table_bucket_arn}")
    except s3tables_client.exceptions.NotFoundException:
        pass
    except Exception as e:
        logger.warning(f"Could not delete table bucket {table_bucket_arn}: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_table_bucket(table_bucket_name: str) -> str:
    try:
        resp = s3tables_client.create_table_bucket(name=table_bucket_name)
        arn = resp['arn']
        logger.info(f"Created table bucket: {arn}")
        return arn
    except s3tables_client.exceptions.ConflictException:
        buckets = s3tables_client.list_table_buckets().get('tableBuckets', [])
        for b in buckets:
            if b['name'] == table_bucket_name:
                logger.info(f"Table bucket already exists: {b['arn']}")
                return b['arn']
        raise Exception(f"Could not find table bucket: {table_bucket_name}")


def _ensure_namespace(table_bucket_arn: str, namespace: str) -> None:
    try:
        s3tables_client.create_namespace(
            tableBucketARN=table_bucket_arn, namespace=[namespace]
        )
        logger.info(f"Created namespace: {namespace}")
    except s3tables_client.exceptions.ConflictException:
        logger.info(f"Namespace already exists: {namespace}")


def _initialize_iceberg_table(catalog, namespace: str, table_name: str, table_bucket_arn: str) -> None:
    """
    Create an Iceberg table via the REST catalog.

    This single call:
      1. Creates the S3 Table storage object (if not already present)
      2. Commits the initial Iceberg metadata (making load_table() work)
      3. Registers a valid Glue table entry automatically

    The backfill Lambda will evolve the schema with real columns from DynamoDB.
    """
    table_path = f"{namespace}.{table_name}"
    try:
        catalog.create_table(table_path, schema=BOOTSTRAP_SCHEMA)
        logger.info(f"  Initialized Iceberg table: {table_path}")
    except TableAlreadyExistsError:
        logger.info(f"  Iceberg table already initialized: {table_path}")
    except Exception as e:
        logger.error(f"  Failed to initialize {table_path}: {e}", exc_info=True)
        raise


def _drop_s3table_boto3(table_bucket_arn: str, namespace: str, table_name: str) -> None:
    """Delete an S3 Table at the storage level via boto3."""
    try:
        s3tables_client.delete_table(
            tableBucketARN=table_bucket_arn,
            namespace=namespace,
            name=table_name,
        )
        logger.info(f"  Deleted S3 Table: {namespace}.{table_name}")
    except s3tables_client.exceptions.NotFoundException:
        logger.info(f"  S3 Table not found (already deleted): {namespace}.{table_name}")
    except Exception as e:
        logger.warning(f"  Could not delete S3 Table {namespace}.{table_name}: {e}")
