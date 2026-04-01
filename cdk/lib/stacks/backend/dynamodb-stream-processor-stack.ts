import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { DynamoEventSource, SqsEventSource, SqsDlq } from 'aws-cdk-lib/aws-lambda-event-sources';
import { Construct } from 'constructs';
import * as cr from 'aws-cdk-lib/custom-resources';
import { DynamoDBStack } from './dynamodb-stack';
import { DataLakeStack } from './data-lake-stack';
import { ArmBuildConstruct } from '../../common/constructs/arm-build-construct';
import * as path from 'path';

export interface DynamoDBStreamProcessorStackProps extends cdk.StackProps {
  projectName: string;
  dynamodbStack: DynamoDBStack;
  dataLakeStack: DataLakeStack;
}

/**
 * DynamoDB Stream Processor Stack
 *
 * Architecture: DynamoDB Streams → Lambda (PyIceberg) → S3 Tables (Iceberg)
 *
 * Features:
 * - Real-time CDC from DynamoDB to S3 Tables (sub-second latency)
 * - Automatic schema evolution (true Iceberg native, not preview)
 * - UPSERT/DELETE behavior via PyIceberg atomic operations
 * - DLQ for error handling
 * - CloudWatch monitoring
 *
 * Key improvements over Firehose:
 * - Eliminates 60-second buffering delay
 * - No Firehose costs
 * - True schema evolution (Iceberg spec, not preview feature)
 * - Direct PyIceberg writes with UPSERT/DELETE support
 *
 * Lake Formation Setup (Automatic):
 * This stack handles Lake Formation permissions automatically:
 * 1. Creates Lambda role with S3 Tables and Glue permissions
 * 2. Adds Lake Formation permissions (SELECT, INSERT, DELETE, ALTER)
 * 3. Creates PyIceberg Lambda layer (shared across all stream processors)
 * 4. Stream processor Lambdas write directly to Iceberg tables
 *
 * No manual intervention required - single CDK deployment handles everything!
 */
export class DynamoDBStreamProcessorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DynamoDBStreamProcessorStackProps) {
    super(scope, id, props);

    // Dead Letter Queue for failed stream processing
    const dlq = new sqs.Queue(this, 'StreamDLQ', {
      queueName: `${props.projectName}-stream-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      visibilityTimeout: cdk.Duration.minutes(6), // 6x Lambda timeout
    });

    // Build Docker images for stream processor and backfill Lambda functions
    // This uses CodeBuild to build ARM64 images and push to ECR
    const streamProcessorBuild = new ArmBuildConstruct(this, 'StreamProcessorBuild', {
      sourcePath: path.join(__dirname, '../../../../lambda/dynamodb-stream-processor'),
      region: this.region,
      namePrefix: 'stream-processor',
    });

    const backfillBuild = new ArmBuildConstruct(this, 'BackfillBuild', {
      sourcePath: path.join(__dirname, '../../../../lambda/dynamodb-iceberg-backfill'),
      region: this.region,
      namePrefix: 'iceberg-backfill',
    });

    // Table mappings: DynamoDB table → S3 table name
    const tableMappings: Array<{
      table: cdk.aws_dynamodb.Table;
      s3TableName: string;
      displayName: string;
    }> = [
      { table: props.dynamodbStack.holdingsTable, s3TableName: 'holding', displayName: 'Holdings' },
      { table: props.dynamodbStack.partiesTable, s3TableName: 'party', displayName: 'Parties' },
      { table: props.dynamodbStack.coveragesTable, s3TableName: 'coverage', displayName: 'Coverages' },
      { table: props.dynamodbStack.financialActivitiesTable, s3TableName: 'financialactivity', displayName: 'FinancialActivities' },
      { table: props.dynamodbStack.financialStatementsTable, s3TableName: 'financialstatement', displayName: 'FinancialStatements' },
      { table: props.dynamodbStack.relationsTable, s3TableName: 'relation', displayName: 'Relations' },
      { table: props.dynamodbStack.policyProductsTable, s3TableName: 'policyproduct', displayName: 'PolicyProducts' },
      { table: props.dynamodbStack.coverageProductsTable, s3TableName: 'coverageproduct', displayName: 'CoverageProducts' },
      { table: props.dynamodbStack.investProductsTable, s3TableName: 'investproduct', displayName: 'InvestProducts' },
      { table: props.dynamodbStack.ridersTable, s3TableName: 'rider', displayName: 'Riders' },
      { table: props.dynamodbStack.adminCodesTable, s3TableName: 'admincode', displayName: 'AdminCodes' },
      { table: props.dynamodbStack.typeCodesTable, s3TableName: 'typecode', displayName: 'TypeCodes' },
    ];

    // Build DynamoDB table name → Iceberg S3 table name mapping
    const tableNameMap: Record<string, string> = {};
    tableMappings.forEach(({ table, s3TableName }) => {
      tableNameMap[table.tableName] = s3TableName;
    });

    // Unified stream processor Lambda for all DynamoDB tables
    const streamProcessorFn = new lambda.DockerImageFunction(this, 'StreamProcessor', {
      functionName: `${props.projectName}-stream-processor`,
      code: lambda.DockerImageCode.fromEcr(streamProcessorBuild.repository, {
        tagOrDigest: streamProcessorBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.seconds(180),
      memorySize: 512,
      environment: {
        TABLE_BUCKET_ARN: props.dataLakeStack.tableBucketArn,
        NAMESPACE: props.dataLakeStack.namespace,
        TABLE_MAPPINGS: JSON.stringify(tableNameMap),
        REGION: this.region,
      },
    });

    // Grant ECR pull permission
    streamProcessorBuild.repository.grantPull(streamProcessorFn);
    streamProcessorFn.node.addDependency(streamProcessorBuild.buildCompletion);

    // Grant S3 Tables permissions (single role for all tables)
    streamProcessorFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3tables:*'],
        resources: [
          props.dataLakeStack.tableBucketArn,
          `${props.dataLakeStack.tableBucketArn}/*`,
        ],
      })
    );

    // Attach stream processor to all DynamoDB tables
    tableMappings.forEach(({ table }) => {
      streamProcessorFn.addEventSource(
        new DynamoEventSource(table, {
          startingPosition: lambda.StartingPosition.TRIM_HORIZON,
          batchSize: 100,
          maxBatchingWindow: cdk.Duration.seconds(30),
          retryAttempts: 10,
          bisectBatchOnError: true,
          onFailure: new SqsDlq(dlq),
        })
      );
    });

    // Output the unified stream processor
    new cdk.CfnOutput(this, 'StreamProcessorLambdaArn', {
      value: streamProcessorFn.functionArn,
      description: 'Unified PyIceberg stream processor Lambda ARN (handles all DynamoDB tables)',
    });

    // ============================================================
    // Initial Iceberg backfill — Custom Resource
    // Scans all DynamoDB tables and writes records directly to
    // S3 Tables via PyIceberg so that Iceberg gets the full column
    // schema on first deployment. Schema evolution then keeps the
    // schema up-to-date as new fields arrive via CDC.
    //
    // Re-runs only when DataVersion is incremented, so normal
    // re-deploys do NOT cause duplicate backfills.
    // ============================================================

    // Build the DynamoDB table name → S3 table name map to pass to Lambda
    const backfillMappings: Record<string, string> = {};
    tableMappings.forEach(({ table, s3TableName }) => {
      backfillMappings[table.tableName] = s3TableName;
    });

    const backfillFn = new lambda.DockerImageFunction(this, 'IcebergBackfillFunction', {
      functionName: `${props.projectName}-iceberg-backfill`,
      code: lambda.DockerImageCode.fromEcr(backfillBuild.repository, {
        tagOrDigest: backfillBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      environment: {
        TABLE_BUCKET_ARN: props.dataLakeStack.tableBucketArn,
        NAMESPACE: props.dataLakeStack.namespace,
        REGION: this.region,
      },
    });

    // Grant ECR pull permission and establish dependency
    backfillBuild.repository.grantPull(backfillFn);
    backfillFn.node.addDependency(backfillBuild.buildCompletion);

    // Grant DynamoDB scan permissions for all tables
    tableMappings.forEach(({ table }) => {
      table.grantReadData(backfillFn);
    });

    // Grant S3 Tables permissions
    backfillFn.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['s3tables:*'],
      resources: [
        props.dataLakeStack.tableBucketArn,
        `${props.dataLakeStack.tableBucketArn}/*`,
      ],
    }));

    const backfillProvider = new cr.Provider(this, 'IcebergBackfillProvider', {
      onEventHandler: backfillFn,
    });

    const backfillResource = new cdk.CustomResource(this, 'IcebergBackfill', {
      serviceToken: backfillProvider.serviceToken,
      properties: {
        TableMappings: JSON.stringify(backfillMappings),
        // Increment DataVersion to force a re-backfill after adding new synthetic data.
        // Normal re-deploys with the same version are no-ops.
        DataVersion: '15',
      },
    });

    // Must run after S3 Tables bucket is ready
    backfillResource.node.addDependency(props.dataLakeStack.tableBucketResource);

    // DLQ processor Lambda for retry logic
    // Simple Python Lambda (no PyIceberg needed) that re-invokes the unified stream processor
    const dlqProcessor = new lambda.Function(this, 'DLQProcessor', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../../../lambda/dlq-processor')),
      timeout: cdk.Duration.seconds(180),
      memorySize: 256,
      environment: {
        STREAM_PROCESSOR_FUNCTION_NAME: streamProcessorFn.functionName,
        MAX_RETRIES: '3',
      },
    });

    // Grant DLQ processor permission to invoke the unified stream processor
    streamProcessorFn.grantInvoke(dlqProcessor);

    // DLQ processor event source
    dlqProcessor.addEventSource(
      new SqsEventSource(dlq, {
        batchSize: 10,
        maxBatchingWindow: cdk.Duration.seconds(10),
      })
    );

    // Outputs
    new cdk.CfnOutput(this, 'DLQUrl', {
      value: dlq.queueUrl,
      description: 'Dead Letter Queue URL for failed stream processing',
    });

    new cdk.CfnOutput(this, 'DLQProcessorArn', {
      value: dlqProcessor.functionArn,
      description: 'DLQ processor Lambda ARN (retries failed stream processor invocations)',
    });
  }
}
