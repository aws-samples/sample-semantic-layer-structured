"""
DynamoDB → Iceberg Backfill — CloudFormation Custom Resource Lambda

Scans all DynamoDB tables and writes records directly to S3 Tables via PyIceberg.
Tables are pre-initialized with a bootstrap schema by the s3tables-manager Lambda,
so load_table() always succeeds. This Lambda evolves the schema with real DynamoDB
columns and appends all existing records.

Lifecycle:
  CREATE  — always runs the backfill.
  UPDATE  — only re-runs if DataVersion changed; otherwise no-op.
  DELETE  — no-op (data stays in Iceberg).
"""

import json
import logging
import urllib3
import boto3
import os
import pandas as pd
import pyarrow as pa
from decimal import Decimal
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BooleanType, LongType, DoubleType,
    StringType, BinaryType, NestedField
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

http = urllib3.PoolManager()

TABLE_BUCKET_ARN = os.environ['TABLE_BUCKET_ARN']
NAMESPACE = os.environ['NAMESPACE']
REGION = os.environ['REGION']

_catalog = None
_s3tables_client = None


def get_s3tables_client():
    global _s3tables_client
    if _s3tables_client is None:
        _s3tables_client = boto3.client('s3tables', region_name=REGION)
    return _s3tables_client


def get_catalog():
    global _catalog
    if _catalog is None:
        _catalog = load_catalog("s3tables", **{
            "type": "rest",
            "uri": f"https://s3tables.{REGION}.amazonaws.com/iceberg",
            "warehouse": TABLE_BUCKET_ARN,
            "rest.sigv4-enabled": "true",
            "rest.signing-region": REGION,
            "rest.signing-name": "s3tables",
        })
    return _catalog


def infer_iceberg_type(value):
    if isinstance(value, bool):
        return BooleanType()
    if isinstance(value, (int, float)):
        # Use DoubleType for all numerics: DynamoDB Decimal values can be int
        # in one row and float in another. Pandas also converts int columns
        # that contain NaN to float64, so LongType always risks a cast error.
        return DoubleType()
    if isinstance(value, bytes):
        return BinaryType()
    return StringType()


def _build_schema(items: list) -> Schema:
    """
    Build an Iceberg schema from a list of items.
    Uses the first non-None value per field so numeric columns get the right type
    rather than falling back to StringType just because one item had None.
    """
    field_types: dict = {}
    for item in items:
        for key, value in item.items():
            if key not in field_types and value is not None:
                field_types[key] = infer_iceberg_type(value)
    # Columns seen only as None across all items → string
    all_keys = {k for item in items for k in item}
    for key in all_keys:
        if key not in field_types:
            field_types[key] = StringType()
    return Schema(*[
        NestedField(field_id=i, name=name, field_type=ftype, required=False)
        for i, (name, ftype) in enumerate(field_types.items(), start=1)
    ])


def _convert_item(item: dict) -> dict:
    """Convert Decimal and nested types to pandas-compatible Python types."""
    result = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            result[k] = float(v) if v % 1 else int(v)
        elif isinstance(v, (dict, list)):
            result[k] = json.dumps(v, default=str)
        else:
            result[k] = v
    return result


def _backfill_table(dynamo_table_name: str, s3_table_name: str) -> int:
    """Scan one DynamoDB table and write all items to an initialized Iceberg table."""
    dynamodb = boto3.resource('dynamodb')
    ddb_table = dynamodb.Table(dynamo_table_name)

    # Scan all items first so we can build the complete schema in one pass
    all_items = []
    scan_kwargs = {}
    while True:
        response = ddb_table.scan(**scan_kwargs)
        all_items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        scan_kwargs['ExclusiveStartKey'] = last_key

    logger.info(f"  Scanned {len(all_items)} items from {dynamo_table_name}")

    if not all_items:
        logger.info(f"  No items in {dynamo_table_name}, skipping")
        return 0

    converted = [_convert_item(item) for item in all_items]

    catalog = get_catalog()
    table_path = f"{NAMESPACE}.{s3_table_name}"

    # Build the correct schema from actual data (first non-None value per field)
    schema = _build_schema(converted)

    # Drop and recreate the table so schema is always correct for this backfill run.
    # Any prior Iceberg metadata (possibly with wrong types from earlier runs) is cleared.
    logger.info(f"  Dropping {table_path} to ensure clean schema")
    try:
        catalog.drop_table(table_path)
        logger.info(f"  Dropped table via REST catalog")
    except Exception:
        # Table may not be registered; delete at the S3 Tables storage level instead
        try:
            get_s3tables_client().delete_table(
                tableBucketARN=TABLE_BUCKET_ARN,
                namespace=NAMESPACE,
                name=s3_table_name,
            )
            logger.info(f"  Deleted S3 Table via boto3")
        except Exception as _e:
            logger.debug("Ignoring expected error deleting prior S3 Table: %s", _e)  # nosec B110

    iceberg_table = catalog.create_table(table_path, schema=schema)
    logger.info(f"  Created {table_path} with {len(schema.fields)}-field schema")

    # Build a PyArrow table whose schema exactly matches the Iceberg table schema.
    # Pandas converts int columns that contain NaN to float64, which mismatches
    # Iceberg's LongType (int64). We cast column-by-column to the Iceberg-expected
    # PyArrow type, falling back to string on cast failure.
    df = pd.DataFrame(converted)
    arrow_raw = pa.Table.from_pandas(df, preserve_index=False)
    iceberg_pa_schema = iceberg_table.schema().as_arrow()

    columns = {}
    for field in iceberg_pa_schema:
        if field.name in arrow_raw.column_names:
            col = arrow_raw.column(field.name)
            if col.type == field.type:
                columns[field.name] = col
            else:
                try:
                    columns[field.name] = col.cast(field.type)
                except Exception:
                    columns[field.name] = col.cast(pa.string())
        else:
            columns[field.name] = pa.array([None] * len(df), type=field.type)

    arrow_table = pa.table(columns, schema=iceberg_pa_schema)
    logger.info(f"  Writing {len(arrow_table)} rows to {table_path}")
    iceberg_table.append(arrow_table)
    logger.info(f"  Successfully wrote {len(arrow_table)} rows to {table_path}")

    return len(converted)


def handler(event, context):
    """CloudFormation custom resource handler."""
    request_type = event['RequestType']
    logger.info(f"RequestType: {request_type}")

    if request_type == 'Delete':
        _send_response(event, context, 'SUCCESS', 'Delete — no action required')
        return

    if request_type == 'Update':
        old_version = event.get('OldResourceProperties', {}).get('DataVersion', '')
        new_version = event['ResourceProperties'].get('DataVersion', '')
        if old_version == new_version:
            _send_response(event, context, 'SUCCESS',
                           f'Update — DataVersion unchanged ({new_version}), skipping backfill')
            return

    try:
        mappings = json.loads(event['ResourceProperties']['TableMappings'])
        total_sent = 0

        for dynamo_table, s3_table in mappings.items():
            logger.info(f"Backfilling {dynamo_table} → {s3_table}")
            sent = _backfill_table(dynamo_table, s3_table)
            total_sent += sent
            logger.info(f"  {sent} records sent")

        _send_response(event, context, 'SUCCESS',
                       f'Backfilled {total_sent} records across {len(mappings)} tables',
                       {'RecordsSent': total_sent})

    except Exception as exc:
        logger.error(f"Backfill failed: {exc}", exc_info=True)
        _send_response(event, context, 'FAILED', str(exc))


def _send_response(event, context, status: str, reason: str = '', data: dict = None):
    body = json.dumps({
        'Status': status,
        'Reason': reason,
        'PhysicalResourceId': event.get('PhysicalResourceId', context.log_stream_name),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data or {},
    })
    try:
        http.request('PUT', event['ResponseURL'],
                     body=body.encode('utf-8'),
                     headers={'Content-Type': ''})
    except Exception as exc:
        logger.error(f"Failed to send CFN response: {exc}")
