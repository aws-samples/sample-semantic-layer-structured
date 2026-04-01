"""
Load Complete Synthetic Data into DynamoDB
Loads all 12 tables into a single DynamoDB table using batch operations

Usage:
    python3 load_to_dynamodb.py [table_name] [region]

    table_name: Optional. Default is 'semantic-layer-insurance-data'
    region: Optional. Default is 'us-east-1'

Example:
    python3 load_to_dynamodb.py semantic-layer-insurance-data us-east-1
"""

import boto3
import json
from decimal import Decimal
import os
import sys
from datetime import datetime

# Configuration (can be overridden by command-line arguments)
AWS_REGION = sys.argv[2] if len(sys.argv) > 2 else 'us-east-1'
TABLE_NAME = sys.argv[1] if len(sys.argv) > 1 else 'semantic-layer-insurance-data'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, '..')
DATA_DIR = os.path.join(PROJECT_ROOT, 'data', 'complete_synthetic_data')

def convert_to_decimal(obj):
    """Convert float values to Decimal for DynamoDB"""
    if isinstance(obj, list):
        return [convert_to_decimal(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, float):
        return Decimal(str(obj))
    else:
        return obj

def create_table_if_not_exists(dynamodb):
    """Create DynamoDB table if it doesn't exist"""
    try:
        table = dynamodb.Table(TABLE_NAME)
        table.load()
        print(f"✓ Table '{TABLE_NAME}' already exists")
        return table
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        print(f"Creating table '{TABLE_NAME}'...")

        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {'AttributeName': 'pk', 'KeyType': 'HASH'},   # Partition key
                {'AttributeName': 'sk', 'KeyType': 'RANGE'}   # Sort key
            ],
            AttributeDefinitions=[
                {'AttributeName': 'pk', 'AttributeType': 'S'},
                {'AttributeName': 'sk', 'AttributeType': 'S'}
            ],
            BillingMode='PAY_PER_REQUEST'  # On-demand billing
        )

        # Wait for table to be created
        print("Waiting for table to be created...")
        table.meta.client.get_waiter('table_exists').wait(TableName=TABLE_NAME)
        print(f"✓ Table '{TABLE_NAME}' created successfully\n")
        return table

def load_data_file(table, filename, display_name):
    """Load a single JSON file into DynamoDB"""
    filepath = os.path.join(DATA_DIR, filename)

    if not os.path.exists(filepath):
        print(f"⚠ File not found: {filepath}")
        return 0

    print(f"Loading {display_name}...")

    with open(filepath, 'r', encoding='utf-8') as f:
        records = json.load(f)

    total_records = len(records)
    loaded_count = 0
    error_count = 0

    # Batch write (25 items at a time is DynamoDB limit)
    with table.batch_writer() as batch:
        for i, record in enumerate(records):
            try:
                # Convert floats to Decimal
                record = convert_to_decimal(record)
                batch.put_item(Item=record)
                loaded_count += 1

                # Progress indicator
                if (i + 1) % 25 == 0:
                    print(f"  Progress: {i + 1}/{total_records} records")

            except Exception as e:
                error_count += 1
                print(f"  ⚠ Error loading record {i + 1}: {str(e)}")

    print(f"  ✓ Loaded {loaded_count}/{total_records} records")
    if error_count > 0:
        print(f"  ⚠ {error_count} errors occurred")
    print()

    return loaded_count

def main():
    """Main function to load all data into DynamoDB"""
    print("=" * 80)
    print("DYNAMODB DATA LOADER")
    print("=" * 80)
    print(f"Target Table: {TABLE_NAME}")
    print(f"Region: {AWS_REGION}")
    print(f"Data Directory: {DATA_DIR}")
    print("=" * 80)
    print()

    # Initialize DynamoDB
    try:
        dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
        print(f"✓ Connected to DynamoDB in {AWS_REGION}\n")
    except Exception as e:
        print(f"✗ Error connecting to DynamoDB: {str(e)}")
        return

    # Create table if needed
    table = create_table_if_not_exists(dynamodb)

    # Define files to load (in order)
    files_to_load = [
        ('type_codes.json', 'Type Codes'),
        ('admin_codes.json', 'Admin Codes'),
        ('policy_products.json', 'Policy Products'),
        ('coverage_products.json', 'Coverage Products'),
        ('invest_products.json', 'Investment Products'),
        ('parties.json', 'Parties'),
        ('coverages.json', 'Coverages'),
        ('holdings.json', 'Holdings'),
        ('financial_activities.json', 'Financial Activities'),
        ('financial_statements.json', 'Financial Statements'),
        ('riders.json', 'Riders'),
        ('relations.json', 'Relations'),
    ]

    # Load data
    start_time = datetime.now()
    total_loaded = 0

    for filename, display_name in files_to_load:
        count = load_data_file(table, filename, display_name)
        total_loaded += count

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # Summary
    print("=" * 80)
    print("LOADING COMPLETE")
    print("=" * 80)
    print(f"Total records loaded: {total_loaded:,}")
    print(f"Time taken: {duration:.2f} seconds")
    print(f"Average rate: {total_loaded / duration:.0f} records/second")
    print()
    print(f"✓ Data successfully loaded into table: {TABLE_NAME}")
    print("=" * 80)

if __name__ == "__main__":
    main()
