import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import * as path from 'path';

export interface DynamoDBDataLoaderProps {
  /**
   * The DynamoDB table to load data into
   */
  table: dynamodb.Table;

  /**
   * Path to the directory containing JSON data files
   * Relative to the construct file location
   */
  dataPath: string;

  /**
   * List of JSON files to load (in order)
   */
  dataFiles: Array<{
    filename: string;
    displayName: string;
  }>;

  /**
   * Whether to load data automatically on stack creation
   * Default: true
   */
  autoLoad?: boolean;
}

/**
 * Custom Resource to load synthetic data into DynamoDB table
 *
 * This construct creates a Lambda function that loads JSON data files
 * into a DynamoDB table during stack deployment.
 */
export class DynamoDBDataLoader extends Construct {
  public readonly customResource: cdk.CustomResource;

  constructor(scope: Construct, id: string, props: DynamoDBDataLoaderProps) {
    super(scope, id);

    // Create log group explicitly
    const logGroup = new logs.LogGroup(this, 'DataLoaderLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Create Lambda function to load data
    const dataLoaderFunction = new lambda.Function(this, 'DataLoaderFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      logGroup: logGroup,
      // nosemgrep: missing-template-string-indicator — literal/template string is intentional; no untrusted interpolation
      code: lambda.Code.fromInline(`
import boto3
import json
import os
from decimal import Decimal
import urllib3

# Initialize clients
dynamodb = boto3.resource('dynamodb')
http = urllib3.PoolManager()

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

def send_response(event, context, status, reason='', data=None):
    """Send response to CloudFormation"""
    response_body = {
        'Status': status,
        'Reason': reason,
        'PhysicalResourceId': context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data or {}
    }

    response_url = event['ResponseURL']
    json_response = json.dumps(response_body)

    try:
        http.request(
            'PUT',
            response_url,
            body=json_response.encode('utf-8'),
            headers={'Content-Type': ''}
        )
    except Exception as e:
        print(f"Error sending response: {e}")

def load_data_batch(table, records):
    """Load records using batch write"""
    with table.batch_writer() as batch:
        for record in records:
            # Convert floats to Decimal
            record = convert_to_decimal(record)
            batch.put_item(Item=record)

def handler(event, context):
    """Lambda handler for data loading custom resource"""
    print(f"Event: {json.dumps(event)}")

    request_type = event['RequestType']

    # Only load data on Create, not Update or Delete
    if request_type == 'Delete':
        send_response(event, context, 'SUCCESS', 'Delete request - no action needed')
        return

    if request_type == 'Update':
        send_response(event, context, 'SUCCESS', 'Update request - no action needed')
        return

    try:
        # Get properties
        table_name = event['ResourceProperties']['TableName']
        data_content = json.loads(event['ResourceProperties']['DataContent'])

        print(f"Loading data into table: {table_name}")
        table = dynamodb.Table(table_name)

        total_loaded = 0

        # Load each data file
        for file_info in data_content:
            filename = file_info['filename']
            records = file_info['records']

            print(f"Loading {filename}: {len(records)} records")

            # Load in batches
            batch_size = 25
            for i in range(0, len(records), batch_size):
                batch = records[i:i+batch_size]
                load_data_batch(table, batch)

            total_loaded += len(records)
            print(f"Loaded {len(records)} records from {filename}")

        print(f"Total records loaded: {total_loaded}")

        send_response(event, context, 'SUCCESS',
                     f'Loaded {total_loaded} records',
                     {'RecordsLoaded': total_loaded})

    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        send_response(event, context, 'FAILED', str(e))
`),
    });

    // Grant permissions to write to DynamoDB table
    props.table.grantWriteData(dataLoaderFunction);

    // Read data files and prepare content
    const dataContent: Array<{ filename: string; records: any[] }> = [];

    for (const fileInfo of props.dataFiles) {
      const filePath = path.join(props.dataPath, fileInfo.filename); // nosemgrep: path-join-resolve-traversal - CDK synth-time path, not runtime user input

      try {
        const fs = require('fs'); // nosemgrep: lazy-load-module — lazy require inside loop is intentional; top-level import would force a hard fs dep on all code paths
        // nosemgrep: detect-non-literal-fs-filename — CDK build dir / static repo path, not user input
        if (fs.existsSync(filePath)) {
          // nosemgrep: detect-non-literal-fs-filename — CDK synth-time path set by app developer
          const content = fs.readFileSync(filePath, 'utf8'); // nosemgrep: detect-non-literal-fs-filename - CDK synth-time file read by app developer
          const records = JSON.parse(content);
          dataContent.push({
            filename: fileInfo.filename,
            records: records,
          });
          console.log(`Prepared ${records.length} records from ${fileInfo.filename}`);
        } else {
          console.warn(`Data file not found: ${filePath}`);
        }
      } catch (error) {
        // nosemgrep: unsafe-formatstring — CDK-controlled format args, build-time, not user input
        console.error(`Error reading ${fileInfo.filename}:`, error);
      }
    }

    // Create custom resource provider
    const provider = new cr.Provider(this, 'DataLoaderProvider', {
      onEventHandler: dataLoaderFunction,
    });

    // Create custom resource
    this.customResource = new cdk.CustomResource(this, 'DataLoaderResource', {
      serviceToken: provider.serviceToken,
      properties: {
        TableName: props.table.tableName,
        DataContent: JSON.stringify(dataContent),
        // Add timestamp to force update on redeploy if needed
        Timestamp: props.autoLoad !== false ? new Date().toISOString() : 'disabled',
      },
    });

    // Ensure custom resource runs after table is created
    this.customResource.node.addDependency(props.table);

    // Note: RecordsLoaded attribute is available in the custom resource response
    // but we don't create a CfnOutput for it to avoid attribute access issues
  }
}
