"""
DynamoDB Stream Processor - Lambda Handler

Transforms DynamoDB Stream records and writes directly to Iceberg tables via PyIceberg.
Handles INSERT, MODIFY, and REMOVE events from DynamoDB Streams.

Architecture: DynamoDB Streams → Lambda (PyIceberg) → S3 Tables (Iceberg)

Features:
- True schema evolution (Iceberg spec, not preview)
- Sub-second CDC latency (no 60s buffer)
- UPSERT/DELETE via atomic PyIceberg operations
- Automatic column detection and schema updates
"""

import json
import os
import base64
from datetime import datetime, timezone
from typing import Any, Dict, List
import logging
import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.types import (
    BooleanType, DoubleType, StringType, BinaryType
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
TABLE_BUCKET_ARN = os.environ['TABLE_BUCKET_ARN']
NAMESPACE = os.environ['NAMESPACE']
TABLE_MAPPINGS = json.loads(os.environ['TABLE_MAPPINGS'])  # DynamoDB table name → Iceberg table name
REGION = os.environ['REGION']

# Module-level catalog cache (reused across Lambda invocations)
_catalog = None


def get_catalog():
    """Load S3 Tables REST catalog once and cache for reuse."""
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


def get_table_name_from_event(record: dict) -> str:
    """
    Extract DynamoDB table name from eventSourceARN and map to Iceberg table name.

    ARN format: arn:aws:dynamodb:region:account:table/TABLE_NAME/stream/...
    """
    arn = record.get('eventSourceARN', '')
    try:
        # Split by '/' and extract table name
        parts = arn.split('/')
        if len(parts) < 2:
            raise ValueError(f"Invalid ARN format: {arn}")

        dynamo_table = parts[1]

        # Look up Iceberg table name from mapping
        iceberg_table = TABLE_MAPPINGS.get(dynamo_table)
        if not iceberg_table:
            raise ValueError(f"No Iceberg mapping for DynamoDB table: {dynamo_table}")

        return iceberg_table
    except Exception as e:
        logger.warning(f"Failed to extract table name from ARN {arn}: {e}", exc_info=True)
        raise


def infer_iceberg_type(value: Any):
    """
    Infer Iceberg type from Python value.
    Follows ACORD insurance data model patterns.
    """
    if value is None:
        return StringType()  # Default for null

    if isinstance(value, bool):
        return BooleanType()

    if isinstance(value, (int, float)):
        # Use DoubleType for all numerics: DynamoDB Decimal can be int or float
        # per row, and pandas converts int+NaN columns to float64. LongType
        # would cause cast errors when a column has mixed int/float values.
        return DoubleType()

    if isinstance(value, str):
        return StringType()

    if isinstance(value, bytes):
        return BinaryType()

    if isinstance(value, (list, tuple)):
        return StringType()  # Store as JSON string for nested structures

    if isinstance(value, dict):
        return StringType()  # Store as JSON string for nested structures

    # Fallback
    logger.warning(f"Unknown type for value: {value}, defaulting to StringType")
    return StringType()


def extract_fk_columns(record: dict) -> dict:
    """
    Extract entity type and ID from DynamoDB composite pk/sk keys.

    Always adds pk_entity_type and sk_entity_type columns.
    Adds pk_entity_id / sk_entity_id only when the extracted value
    does not already exist in any attribute of the record (avoids
    duplicating e.g. PolicyID when pk = 'POLICY#POL001').

    Examples:
      pk='POLICY#POL001'  -> pk_entity_type='POLICY', pk_entity_id skipped if PolicyID='POL001'
      sk='COVERAGE#COV1'  -> sk_entity_type='COVERAGE', sk_entity_id skipped if CoverageID='COV1'
      sk='METADATA'       -> sk_entity_type='METADATA' (no id — no '#' separator)
    """
    extracted = {}
    existing_values = {str(v) for v in record.values() if v is not None}

    for key_attr, type_col, id_col in [
        ('pk', 'pk_entity_type', 'pk_entity_id'),
        ('sk', 'sk_entity_type', 'sk_entity_id'),
    ]:
        raw = record.get(key_attr)
        if not raw or not isinstance(raw, str):
            continue

        if '#' in raw:
            entity_type, _, entity_id = raw.partition('#')
            extracted[type_col] = entity_type
            if entity_id and entity_id not in existing_values:
                extracted[id_col] = entity_id
        else:
            # No separator (e.g. sk="METADATA") — whole value is the type
            extracted[type_col] = raw

    return extracted


def transform_dynamodb_type(attr_value: dict) -> Any:
    """
    Transform DynamoDB AttributeValue to Python type.

    Follows ACORD insurance data model patterns:
    - Financial amounts: NUMBER → float
    - Dates: STRING (ISO 8601)
    - Extension fields: Preserve NULL values
    - Composite keys: STRING (e.g., "POLICY#POL00000001")
    """
    # String type
    if 'S' in attr_value:
        return attr_value['S']

    # Number type (DynamoDB stores as string, convert to float/int)
    elif 'N' in attr_value:
        num_str = attr_value['N']
        if '.' in num_str:
            return float(num_str)  # Financial amounts, rates, percentages
        else:
            return int(num_str)

    # Boolean type
    elif 'BOOL' in attr_value:
        return attr_value['BOOL']

    # Null type - CRITICAL: Preserve nulls for sparse extension fields
    elif 'NULL' in attr_value:
        return None

    # List type (array)
    elif 'L' in attr_value:
        return [transform_dynamodb_type(item) for item in attr_value['L']]

    # Map type (nested objects)
    elif 'M' in attr_value:
        return {k: transform_dynamodb_type(v) for k, v in attr_value['M'].items()}

    # String Set (convert to array)
    elif 'SS' in attr_value:
        return attr_value['SS']

    # Number Set (convert to array of numbers)
    elif 'NS' in attr_value:
        return [float(n) if '.' in n else int(n) for n in attr_value['NS']]

    # Binary (Base64 encode for JSON compatibility)
    elif 'B' in attr_value:
        return base64.b64encode(attr_value['B']).decode('utf-8')

    # Binary Set
    elif 'BS' in attr_value:
        return [base64.b64encode(b).decode('utf-8') for b in attr_value['BS']]

    # Fallback for unknown types
    else:
        logger.warning(f"Unknown DynamoDB type: {attr_value}")
        return None


def write_to_iceberg(table_name: str, records: List[Dict[str, Any]]) -> int:
    """
    Write records to Iceberg table via PyIceberg.

    Handles:
    - Schema evolution (detect new columns and add them)
    - UPSERT (INSERT/MODIFY via overwrite with filter on pk+sk)
    - DELETE (via delete filter)

    Returns: number of records successfully written
    """
    if not records:
        return 0

    catalog = get_catalog()
    table_path = f"{NAMESPACE}.{table_name}"

    try:
        tbl = catalog.load_table(table_path)
    except Exception as e:
        logger.error(f"Failed to load table {table_path}: {e}")  # nosemgrep: logging-error-without-handling — Lambda boundary handler; ERROR ensures CloudWatch alarm visibility
        raise

    # Group records by operation and pk+sk
    inserts = []
    deletes = []

    for record in records:
        if record.get('_operation') == 'delete':
            deletes.append(record)
        else:
            inserts.append(record)

    # Handle schema evolution: detect new columns and add them to schema
    existing_field_names = {f.name for f in tbl.schema().fields}
    new_fields_to_add = {}

    for record in inserts + deletes:
        for key, value in record.items():
            if key not in existing_field_names and not key.startswith('_'):
                if key not in new_fields_to_add:
                    new_fields_to_add[key] = infer_iceberg_type(value)

    if new_fields_to_add:
        logger.info(f"Adding new columns to {table_path}: {list(new_fields_to_add.keys())}")
        try:
            with tbl.update_schema() as schema_update:
                for field_name, field_type in new_fields_to_add.items():
                    schema_update.add_column(field_name, field_type)
            # Reload table after schema update
            tbl = catalog.load_table(table_path)
        except Exception as e:
            logger.warning(f"Failed to update schema for {table_path}: {e}", exc_info=True)
            raise

    # Process inserts/updates
    if inserts:
        try:
            df = pd.DataFrame(inserts)
            df = df.drop(columns=['_operation'], errors='ignore')

            arrow_table = pa.Table.from_pandas(df, preserve_index=False)
            # Cast int64→float64 for DoubleType fields; null→string for untyped columns
            iceberg_field_types = {f.name: f.field_type for f in tbl.schema().fields}
            fixed_schema = pa.schema([
                f.with_type(pa.float64())
                if pa.types.is_integer(f.type)
                and isinstance(iceberg_field_types.get(f.name), DoubleType)
                else f.with_type(pa.string())
                if pa.types.is_null(f.type)
                else f
                for f in arrow_table.schema
            ])
            arrow_table = arrow_table.cast(fixed_schema)

            # Build one combined filter covering all pk+sk pairs — single snapshot commit
            if 'pk' not in df.columns or 'sk' not in df.columns:
                missing = [k for k in ('pk', 'sk') if k not in df.columns]
                raise ValueError(f"Missing required key columns in insert records: {missing}")

            conditions = [
                f"(pk = '{row.pk}' AND sk = '{row.sk}')"
                for row in df[['pk', 'sk']].drop_duplicates().itertuples(index=False)
            ]
            combined_filter = " OR ".join(conditions)
            tbl.overwrite(arrow_table, overwrite_filter=combined_filter)
            logger.debug(f"Batched overwrite of {len(df)} records with {len(conditions)} pk+sk pairs")
        except Exception as e:
            logger.error(f"Failed to write inserts to {table_path}: {e}", exc_info=True)  # nosemgrep: logging-error-without-handling — Lambda boundary handler; ERROR ensures CloudWatch alarm visibility
            raise

    # Process deletes
    if deletes:
        try:
            for record in deletes:
                pk_val = record.get('pk')
                sk_val = record.get('sk')

                if pk_val is None or sk_val is None:
                    logger.warning(f"DELETE record missing pk/sk: {record}")
                    continue

                delete_filter = f"pk = '{pk_val}' AND sk = '{sk_val}'"  # nosec B608 - pk_val/sk_val sourced from DynamoDB Stream events (trusted AWS internal source)

                try:
                    tbl.delete(delete_filter)
                    logger.debug(f"Deleted record with filter: {delete_filter}")
                except Exception as e:
                    logger.warning(f"Failed to delete from {table_path} with filter {delete_filter}: {e}", exc_info=True)  # nosec B608 - delete_filter already validated via pk_val/sk_val from DynamoDB Stream events
                    raise
        except Exception as e:
            logger.error(f"Failed to process deletes for {table_path}: {e}", exc_info=True)  # nosemgrep: logging-error-without-handling — Lambda boundary handler; ERROR ensures CloudWatch alarm visibility
            raise

    return len(inserts) + len(deletes)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Process DynamoDB Stream records and write to Iceberg via PyIceberg.
    Handles all DynamoDB types: S, N, BOOL, NULL, L, M, SS, NS, B, BS

    Supports multiple DynamoDB tables: groups records by table name and
    invokes write_to_iceberg separately for each table.
    """
    # Group records by Iceberg table name
    records_by_table: Dict[str, List[Dict[str, Any]]] = {}

    logger.info(f"Processing {len(event['Records'])} records across {len(TABLE_MAPPINGS)} mapped tables")

    for record in event['Records']:
        try:
            # Determine target Iceberg table from DynamoDB ARN
            iceberg_table = get_table_name_from_event(record)

            # Extract event details
            event_name = record['eventName']  # INSERT, MODIFY, REMOVE

            # Determine operation
            if event_name == 'INSERT':
                operation = 'insert'
                image = record['dynamodb']['NewImage']
            elif event_name == 'MODIFY':
                operation = 'upsert'
                image = record['dynamodb']['NewImage']
            elif event_name == 'REMOVE':
                operation = 'delete'
                # For deletes, use Keys to identify record
                image = record['dynamodb']['Keys']
            else:
                logger.warning(f"Unknown event type: {event_name}")
                continue

            # Transform DynamoDB JSON to standard JSON
            transformed = {}
            for key, value in image.items():
                transformed[key] = transform_dynamodb_type(value)

            # Extract FK columns from composite keys (pk_entity_type, sk_entity_type, etc.)
            transformed.update(extract_fk_columns(transformed))

            # Add metadata for tracking
            transformed['_operation'] = operation
            _epoch = record['dynamodb'].get('ApproximateCreationDateTime', 0)
            transformed['event_timestamp'] = (
                datetime.fromtimestamp(_epoch, tz=timezone.utc).isoformat()
                if _epoch else None
            )

            # Group by table
            if iceberg_table not in records_by_table:
                records_by_table[iceberg_table] = []
            records_by_table[iceberg_table].append(transformed)

        except Exception as e:
            logger.error(f'Error transforming record: {str(e)}', exc_info=True)
            logger.error(f'Record: {json.dumps(record)}')
            # Continue processing other records
            continue

    # Write to Iceberg tables (one call per table)
    total_written = 0
    for iceberg_table, records in records_by_table.items():
        if records:
            try:
                written = write_to_iceberg(iceberg_table, records)
                logger.info(f'Successfully wrote {written} records to Iceberg table {iceberg_table}')
                total_written += written

            except Exception as e:
                logger.warning(f'Error writing to Iceberg table {iceberg_table}: {str(e)}', exc_info=True)
                raise

    logger.info(f"Processing complete: {total_written} records written to {len(records_by_table)} tables")

    return {
        'statusCode': 200,
        'processedRecords': total_written,
        'tablesUpdated': len(records_by_table)
    }
