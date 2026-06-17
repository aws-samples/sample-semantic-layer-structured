"""
Initial Load: DynamoDB → Firehose → S3 Tables (Iceberg)

Scans every DynamoDB table and sends all existing records through the
corresponding Firehose delivery stream so that Iceberg tables are
populated with the full schema (schema evolution must be enabled on
the streams first).

Usage:
    python scripts/initial_load_to_iceberg.py [--dry-run] [--table holding]

Options:
    --dry-run   Print record counts without sending to Firehose
    --table     Only process one table by its S3 name (e.g. holding, party)
"""

import argparse
import json
import time
import boto3
import logging
from decimal import Decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_NAME = "semantic-layer"

# DynamoDB table name → Firehose delivery stream name
TABLE_MAPPINGS = {
    f"{PROJECT_NAME}-holdings":             ("holding",           f"{PROJECT_NAME}-holding-delivery"),
    f"{PROJECT_NAME}-parties":              ("party",             f"{PROJECT_NAME}-party-delivery"),
    f"{PROJECT_NAME}-coverages":            ("coverage",          f"{PROJECT_NAME}-coverage-delivery"),
    f"{PROJECT_NAME}-financial-activities": ("financialactivity",  f"{PROJECT_NAME}-financialactivity-delivery"),
    f"{PROJECT_NAME}-financial-statements": ("financialstatement", f"{PROJECT_NAME}-financialstatement-delivery"),
    f"{PROJECT_NAME}-relations":            ("relation",          f"{PROJECT_NAME}-relation-delivery"),
    f"{PROJECT_NAME}-policy-products":      ("policyproduct",     f"{PROJECT_NAME}-policyproduct-delivery"),
    f"{PROJECT_NAME}-coverage-products":    ("coverageproduct",   f"{PROJECT_NAME}-coverageproduct-delivery"),
    f"{PROJECT_NAME}-invest-products":      ("investproduct",     f"{PROJECT_NAME}-investproduct-delivery"),
    f"{PROJECT_NAME}-riders":               ("rider",             f"{PROJECT_NAME}-rider-delivery"),
    f"{PROJECT_NAME}-admin-codes":          ("admincode",         f"{PROJECT_NAME}-admincode-delivery"),
    f"{PROJECT_NAME}-type-codes":           ("typecode",          f"{PROJECT_NAME}-typecode-delivery"),
}

FIREHOSE_BATCH_SIZE = 500   # Firehose max records per PutRecordBatch
FIREHOSE_BATCH_BYTES = 4 * 1024 * 1024  # 4 MB safety margin (limit is 4 MB)


def decimal_default(obj):
    """JSON serialiser that handles Decimal from boto3 DynamoDB."""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def scan_dynamodb_table(table_name: str):
    """Yield all items from a DynamoDB table using paginated Scan."""
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    kwargs = {}
    page = 0
    total = 0
    while True:
        response = table.scan(**kwargs)
        items = response.get("Items", [])
        page += 1
        total += len(items)
        logger.info(f"  [{table_name}] page {page}: {len(items)} items (total so far: {total})")
        yield from items

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    logger.info(f"  [{table_name}] scan complete: {total} items")


def items_to_firehose_records(items):
    """Convert DynamoDB items (already deserialised by boto3 resource) to Firehose records."""
    records = []
    for item in items:
        # Add operation metadata to match stream processor format
        item["_operation"] = "insert"
        item["_event_timestamp"] = int(time.time())

        record_str = json.dumps(item, default=decimal_default) + "\n"
        records.append({"Data": record_str.encode("utf-8")})
    return records


def send_to_firehose(firehose_client, stream_name: str, records: list, dry_run: bool) -> int:
    """Send records to Firehose in batches; returns total records sent."""
    if dry_run:
        logger.info(f"  [DRY RUN] would send {len(records)} records to {stream_name}")
        return len(records)

    sent = 0
    batch = []
    batch_bytes = 0

    for record in records:
        record_bytes = len(record["Data"])
        # Flush if adding this record would exceed limits
        if batch and (len(batch) >= FIREHOSE_BATCH_SIZE or batch_bytes + record_bytes > FIREHOSE_BATCH_BYTES):
            _flush_batch(firehose_client, stream_name, batch)
            sent += len(batch)
            batch = []
            batch_bytes = 0

        batch.append(record)
        batch_bytes += record_bytes

    if batch:
        _flush_batch(firehose_client, stream_name, batch)
        sent += len(batch)

    return sent


def _flush_batch(firehose_client, stream_name: str, batch: list):
    """Send one batch and retry on partial failures."""
    for attempt in range(3):
        response = firehose_client.put_record_batch(
            DeliveryStreamName=stream_name,
            Records=batch,
        )
        failed_count = response.get("FailedPutCount", 0)
        if failed_count == 0:
            return

        # Retry only the failed records
        failed_records = [
            batch[i]
            for i, r in enumerate(response["RequestResponses"])
            if "ErrorCode" in r
        ]
        logger.warning(f"  {failed_count} records failed (attempt {attempt+1}/3), retrying...")
        batch = failed_records
        time.sleep(2 ** attempt)

    if batch:
        logger.error(f"  {len(batch)} records permanently failed for stream {stream_name}")


def main():
    parser = argparse.ArgumentParser(description="Initial load: DynamoDB → Iceberg via Firehose")
    parser.add_argument("--dry-run", action="store_true", help="Count records without sending")
    parser.add_argument("--table", help="Only process this S3 table name (e.g. holding)")
    args = parser.parse_args()

    firehose_client = boto3.client("firehose")

    for dynamo_table, (s3_name, stream_name) in TABLE_MAPPINGS.items():
        if args.table and args.table != s3_name:
            continue

        logger.info(f"Processing: {dynamo_table} → {stream_name}")

        try:
            items = list(scan_dynamodb_table(dynamo_table))
        except Exception as e:
            logger.error(f"  Failed to scan {dynamo_table}: {e}")
            continue

        if not items:
            logger.info(f"  No items found in {dynamo_table}, skipping")
            continue

        records = items_to_firehose_records(items)
        sent = send_to_firehose(firehose_client, stream_name, records, args.dry_run)
        logger.info(f"  {'Would send' if args.dry_run else 'Sent'} {sent} records to {stream_name}")

    logger.info("Done.")


if __name__ == "__main__":
    main()
