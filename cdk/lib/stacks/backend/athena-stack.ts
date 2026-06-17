import * as cdk from 'aws-cdk-lib';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sam from 'aws-cdk-lib/aws-sam';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { DataLakeStack } from './data-lake-stack';
import { GlueCatalogStack } from './glue-catalog-stack';
import { DynamoDBStack } from './dynamodb-stack';

export interface AthenaStackProps extends cdk.StackProps {
  projectName: string;
  dataLakeStack: DataLakeStack;
  glueCatalogStack: GlueCatalogStack;
  dynamodbStack: DynamoDBStack;
  vpc: ec2.Vpc;
  /** Additional IAM role/user ARNs to register as LF admins (e.g. human admin roles, SSO roles).
   *  This stack's CfnDataLakeSettings is the authoritative last-writer; any principal that must
   *  survive a CDK redeploy must be listed here. */
  additionalLakeFormationAdmins?: string[];
}

/**
 * Athena Stack
 * Creates Amazon Athena workgroup and federated data source connectors
 * Enables unified SQL queries across DynamoDB and S3 historical data
 */
export class AthenaStack extends cdk.Stack {
  public readonly workgroup: athena.CfnWorkGroup;
  public readonly workgroupName: string;
  public readonly athenaExecutionRole: iam.Role;
  public readonly dynamodbConnectorFunction: lambda.IFunction;
  public readonly dynamodbCatalog: athena.CfnDataCatalog;
  public readonly spillBucket: s3.Bucket;
  public readonly spillEncryptionKey: kms.IKey;

  constructor(scope: Construct, id: string, props: AthenaStackProps) {
    super(scope, id, props);

    // IAM role for Athena query execution
    this.athenaExecutionRole = new iam.Role(this, 'AthenaExecutionRole', {
      assumedBy: new iam.ServicePrincipal('athena.amazonaws.com'),
      description: 'Role for Athena to execute federated queries',
    });

    // Grant Athena workgroup and query execution permissions
    this.athenaExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'athena:StartQueryExecution',
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StopQueryExecution',
          'athena:GetWorkGroup',
          'athena:GetDataCatalog',
          'athena:GetDatabase',
          'athena:GetTableMetadata',
          'athena:ListDataCatalogs',
          'athena:ListDatabases',
          'athena:ListTableMetadata',
          'athena:ListWorkGroups',
        ],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.projectName}-workgroup`,
          `arn:aws:athena:${this.region}:${this.account}:datacatalog/*`,
        ],
      })
    );

    // Grant Lambda invoke permissions for federated queries
    this.athenaExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [
          `arn:aws:lambda:${this.region}:${this.account}:function:${props.projectName}-ddb-connector`,
        ],
      })
    );

    // Grant access to S3 buckets
    props.dataLakeStack.athenaResultsBucket.grantReadWrite(this.athenaExecutionRole);
    props.dataLakeStack.artifactsBucket.grantRead(this.athenaExecutionRole);

    // Grant access to S3 Tables for analytics queries
    this.athenaExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3tables:GetTable',
          's3tables:GetTableMetadata',
          's3tables:GetNamespace',
          's3tables:ListTables',
          's3tables:GetTableData',
        ],
        resources: [props.dataLakeStack.tableBucketArn, `${props.dataLakeStack.tableBucketArn}/*`],
      })
    );

    // Grant access to Glue Data Catalog
    this.athenaExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'glue:GetDatabase',
          'glue:GetDatabases',
          'glue:GetTable',
          'glue:GetTables',
          'glue:GetPartition',
          'glue:GetPartitions',
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:database/${props.glueCatalogStack.dynamodbDatabase.ref}`,
          `arn:aws:glue:${this.region}:${this.account}:table/${props.glueCatalogStack.dynamodbDatabase.ref}/*`,
          // S3 Tables (Iceberg) metadata is managed automatically, Glue catalog permissions via S3 Tables API
        ],
      })
    );

    // Grant access to DynamoDB (insuranceTable removed from DynamoDBStack)

    // Athena workgroup
    this.workgroupName = `${props.projectName}-workgroup`;
    this.workgroup = new athena.CfnWorkGroup(this, 'SemanticLayerWorkgroup', {
      name: this.workgroupName,
      description: 'Workgroup for semantic layer queries',
      state: 'ENABLED',
      recursiveDeleteOption: true,
      workGroupConfiguration: {
        resultConfiguration: {
          outputLocation: `s3://${props.dataLakeStack.athenaResultsBucket.bucketName}/query-results/`,
          encryptionConfiguration: {
            encryptionOption: 'SSE_S3',
          },
        },
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: true,
        bytesScannedCutoffPerQuery: 100000000000, // 100 GB
        engineVersion: {
          selectedEngineVersion: 'AUTO',
        },
      },
    });

    // ============================================================================
    // DynamoDB Connector for Athena Federated Queries
    // Deployed via AWS Serverless Application Repository
    // ============================================================================

    const connectorName = `${props.projectName}-ddb-connector`;

    // Create KMS key for encryption
    const encryptionKey = new kms.Key(this, 'ConnectorEncryptionKey', {
      description: `KMS key for Athena DynamoDB Connector spill bucket`,
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.spillEncryptionKey = encryptionKey;

    // Create spill bucket for query results that exceed Lambda memory
    this.spillBucket = new s3.Bucket(this, 'SpillBucket', {
      bucketName: `${connectorName}-spill`,
      encryptionKey: encryptionKey,
      bucketKeyEnabled: true, // Reduce KMS costs
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Deny unencrypted object uploads
    this.spillBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'DenyUnencryptedObjectUploads',
        effect: iam.Effect.DENY,
        principals: [new iam.AnyPrincipal()],
        actions: ['s3:PutObject'],
        resources: [this.spillBucket.arnForObjects('*')],
        conditions: {
          StringNotEquals: {
            's3:x-amz-server-side-encryption': 'aws:kms',
          },
        },
      })
    );

    // Create IAM role for the connector Lambda function
    const connectorRole = new iam.Role(this, 'DynamoDBConnectorRole', {
      roleName: connectorName,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Role for Athena DynamoDB connector Lambda',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // Grant KMS permissions
    encryptionKey.grantEncryptDecrypt(connectorRole);
    connectorRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['kms:GenerateRandom'],
        resources: ['*'], // GenerateRandom does not use account-specific resources
      })
    );

    // Grant S3 permissions
    connectorRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListAllMyBuckets'],
        resources: ['*'],
      })
    );
    this.spillBucket.grantReadWrite(connectorRole);

    // Grant DynamoDB permissions to all tables in the DynamoDB stack
    const allTables = [
      props.dynamodbStack.adminCodesTable,
      props.dynamodbStack.coverageProductsTable,
      props.dynamodbStack.coveragesTable,
      props.dynamodbStack.financialActivitiesTable,
      props.dynamodbStack.financialStatementsTable,
      props.dynamodbStack.holdingsTable,
      props.dynamodbStack.investProductsTable,
      props.dynamodbStack.metadataTable,
      props.dynamodbStack.partiesTable,
      props.dynamodbStack.policyProductsTable,
      props.dynamodbStack.relationsTable,
      props.dynamodbStack.ridersTable,
      props.dynamodbStack.typeCodesTable,
    ];

    allTables.forEach((table) => {
      table.grantReadData(connectorRole);
    });

    // Glue schema discovery — scoped to DynamoDB database
    connectorRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'athena:GetQueryExecution',
          'glue:GetTableVersions',
          'glue:GetPartitions',
          'glue:GetTables',
          'glue:GetTableVersion',
          'glue:GetDatabases',
          'glue:GetTable',
          'glue:GetPartition',
          'glue:GetDatabase',
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:database/${props.glueCatalogStack.dynamodbDatabase.ref}`,
          `arn:aws:glue:${this.region}:${this.account}:table/${props.glueCatalogStack.dynamodbDatabase.ref}/*`,
          `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.projectName}-workgroup`,
        ],
      })
    );
    // DynamoDB schema discovery — these actions do not support resource-level restrictions
    connectorRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'dynamodb:ListTables',
          'dynamodb:DescribeTable',
          'dynamodb:ListSchemas',
          'dynamodb:Scan',
        ],
        resources: ['*'],
      })
    );

    // Deploy Athena DynamoDB Connector from Serverless Application Repository
    const connectorApp = new sam.CfnApplication(this, 'DynamoDBConnectorApp', {
      location: {
        applicationId: `arn:aws:serverlessrepo:${this.region}:292517598671:applications/AthenaDynamoDBConnector`,
        semanticVersion: '2026.4.1', // Latest version as of deployment
      },
      parameters: {
        LambdaRole: connectorRole.roleArn,
        AthenaCatalogName: connectorName,
        DisableSpillEncryption: 'false',
        SpillBucket: this.spillBucket.bucketName,
        KMSKeyId: encryptionKey.keyId,
        LambdaMemory: '3008',
        LambdaTimeout: '900',
      },
    });

    // Reference the Lambda function created by SAM
    this.dynamodbConnectorFunction = lambda.Function.fromFunctionName(
      this,
      'DynamoDBConnectorFunction',
      connectorName
    );

    // Create Athena Data Catalog pointing to the connector
    this.dynamodbCatalog = new athena.CfnDataCatalog(this, 'DynamoDBCatalog', {
      name: 'dynamodb_catalog',
      description: 'Athena catalog for DynamoDB tables',
      type: 'LAMBDA',
      parameters: {
        function: `arn:aws:lambda:${this.region}:${this.account}:function:${connectorName}`,
      },
    });

    // Ensure catalog is created after the SAM application
    this.dynamodbCatalog.node.addDependency(connectorApp);

    // Create Lambda permission for Athena to invoke the connector
    const lambdaPermission = new lambda.CfnPermission(this, 'AthenaInvokePermission', {
      functionName: connectorName,
      action: 'lambda:InvokeFunction',
      principal: 'athena.amazonaws.com',
    });

    // Ensure permission is created after the SAM application
    lambdaPermission.node.addDependency(connectorApp);

    // ============================================================================
    // Lake Formation: register athenaExecutionRole as LF admin
    //
    // lakeformation:GrantPermissions on a federated sub-catalog (Catalog.Id pointing
    // to an S3 Tables sub-catalog path) fails via AwsCustomResource with
    // "Insufficient Glue permissions" regardless of IAM or LF admin status on the
    // calling Lambda. The reliable workaround is to register athenaExecutionRole
    // directly as an LF admin so it can query the s3tablescatalog federated catalog
    // without needing an explicit per-catalog grant.
    //
    // This CfnDataLakeSettings supersedes DataLakeStack's — must carry forward ALL prior admins:
    //   CDK bootstrap roles + DataLake singleton role + athenaExecutionRole.
    //
    // lakeformation:GrantPermissions on a federated sub-catalog (Catalog.Id = sub-catalog path)
    // performs an internal Glue validation that fails via AwsCustomResource regardless of IAM
    // or LF admin status. The reliable workaround is to register athenaExecutionRole as an LF
    // admin so it can access the s3tablescatalog federated catalog without a separate grant.

    // S3 Tables access is controlled via IAM permissions (lines 82-93) and LF admin status below.
    // Lake Formation CfnPermissions do NOT work with S3 Tables namespaces as they are not
    // traditional Glue databases. Attempting to grant LF permissions on S3 Tables namespaces
    // results in "Database not found" errors.

    const lfAdmins: lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty[] = [
      {
        dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}`,
      },
      {
        dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}`,
      },
      { dataLakePrincipalIdentifier: props.dataLakeStack.lfGrantSingletonRoleArn },
      ...(props.additionalLakeFormationAdmins ?? []).map((arn) => ({
        dataLakePrincipalIdentifier: arn,
      })),
    ];

    new lakeformation.CfnDataLakeSettings(this, 'AthenaDataLakeAdminSettings', {
      admins: lfAdmins,
    });

    // LF permission: athenaExecutionRole — DESCRIBE on DynamoDB database
    new lakeformation.CfnPermissions(this, 'AthenaLFDynamoDBDatabasePermissions', {
      dataLakePrincipal: { dataLakePrincipalIdentifier: this.athenaExecutionRole.roleArn },
      resource: {
        databaseResource: {
          catalogId: this.account,
          name: props.glueCatalogStack.dynamodbDatabase.ref,
        },
      },
      permissions: ['DESCRIBE'],
    });

    // LF permission: athenaExecutionRole — SELECT + DESCRIBE on all DynamoDB tables
    new lakeformation.CfnPermissions(this, 'AthenaLFDynamoDBTablePermissions', {
      dataLakePrincipal: { dataLakePrincipalIdentifier: this.athenaExecutionRole.roleArn },
      resource: {
        tableResource: {
          catalogId: this.account,
          databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
          tableWildcard: {},
        },
      },
      permissions: ['SELECT', 'DESCRIBE'],
    });

    // Apply cdk-nag suppressions for Athena connector spill bucket
    // S1: Server access logging disabled on spill bucket — logging is not required for temporary query spill data
    NagSuppressions.addResourceSuppressions(this.spillBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Spill bucket stores temporary intermediate data from Athena DynamoDB connector queries; access logging is not required for ephemeral operational data',
      },
    ]);

    // Outputs
    new cdk.CfnOutput(this, 'AthenaWorkgroupName', {
      value: this.workgroup.name,
      description: 'Athena workgroup name',
      exportName: `${props.projectName}-athena-workgroup`,
    });

    new cdk.CfnOutput(this, 'AthenaResultsLocation', {
      value: `s3://${props.dataLakeStack.athenaResultsBucket.bucketName}/query-results/`,
      description: 'Athena query results location',
    });

    new cdk.CfnOutput(this, 'DynamoDBCatalogName', {
      value: this.dynamodbCatalog.name,
      description: 'Athena catalog for DynamoDB tables',
      exportName: `${props.projectName}-dynamodb-catalog`,
    });

    new cdk.CfnOutput(this, 'DynamoDBConnectorFunctionArn', {
      value: this.dynamodbConnectorFunction.functionArn,
      description: 'Athena DynamoDB connector Lambda function ARN',
    });

    new cdk.CfnOutput(this, 'SpillBucketName', {
      value: this.spillBucket.bucketName,
      description: 'S3 bucket for Athena connector spill data',
    });

    new cdk.CfnOutput(this, 'AthenaExecutionRoleArn', {
      value: this.athenaExecutionRole.roleArn,
      description: 'Athena execution role ARN',
      exportName: `${props.projectName}-athena-role`,
    });
  }
}
