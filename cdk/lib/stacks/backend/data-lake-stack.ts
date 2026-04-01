import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as path from 'path';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { GlueCatalogStack } from './glue-catalog-stack';
import { ArmBuildConstruct } from '../../common/constructs/arm-build-construct';

export interface DataLakeStackProps extends cdk.StackProps {
  projectName: string;
  glueCatalogStack: GlueCatalogStack;
  /** When false, skip creating the semantic_layer_iceberg namespace and empty Iceberg tables.
   *  Used when enableBatchReplication=true (Zero-ETL) manages its own namespaces. */
  enableRealtimeReplication: boolean;
}

/**
 * Data Lake Stack
 * Creates S3 infrastructure for:
 * - S3 Tables (Apache Iceberg) for real-time analytics with DynamoDB CDC
 * - Semantic layer artifacts (ontologies, schemas)
 * - Athena query results
 * - Bedrock Knowledge Base data
 * - Firehose error handling
 */
export class DataLakeStack extends cdk.Stack {
  public readonly tableBucketArn: string;
  public readonly tableBucketName: string;
  public readonly namespace: string;
  public readonly artifactsBucket: s3.Bucket;
  public readonly athenaResultsBucket: s3.Bucket;
  public readonly knowledgeBaseBucket: s3.Bucket;
  public readonly loggingBucket: s3.Bucket;
  public readonly tableBucketResource: cdk.CustomResource;
  /** ARN of the AwsCustomResource singleton Lambda role registered as LF admin in this stack.
   *  AthenaStack must include this ARN in its own CfnDataLakeSettings to preserve it when
   *  overriding the authoritative LF admin list. */
  public readonly lfGrantSingletonRoleArn: string;

  constructor(scope: Construct, id: string, props: DataLakeStackProps) {
    super(scope, id, props);

    // Artifacts bucket - stores ontologies, schemas, and metadata
    this.artifactsBucket = new s3.Bucket(this, 'ArtifactsBucket', {
      bucketName: `${props.projectName}-artifacts-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Athena results bucket
    this.athenaResultsBucket = new s3.Bucket(this, 'AthenaResultsBucket', {
      bucketName: `${props.projectName}-athena-results-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      lifecycleRules: [
        {
          id: 'DeleteOldResults',
          expiration: cdk.Duration.days(30),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Knowledge Base bucket - stores ontology patterns and examples
    this.knowledgeBaseBucket = new s3.Bucket(this, 'KnowledgeBaseBucket', {
      bucketName: `${props.projectName}-knowledge-base-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Logging bucket - stores CloudFront and S3 access logs
    this.loggingBucket = new s3.Bucket(this, 'LoggingBucket', {
      bucketName: `${props.projectName}-logs-${this.account}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      objectOwnership: s3.ObjectOwnership.OBJECT_WRITER, // Enable ACL for CloudFront logging
      lifecycleRules: [
        {
          id: 'DeleteOldLogs',
          expiration: cdk.Duration.days(90),
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Add bucket policy to allow CloudFront and ELB logging
    this.loggingBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('logdelivery.elasticloadbalancing.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [`${this.loggingBucket.bucketArn}/AWSLogs/${this.account}/*`],
        conditions: {
          StringEquals: {
            's3:x-amz-acl': 'bucket-owner-full-control',
          },
        },
      })
    );

    // Create bucket policy for Neptune bulk loader
    const neptuneServicePrincipal = new iam.ServicePrincipal('rds.amazonaws.com');

    this.artifactsBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [neptuneServicePrincipal],
        actions: ['s3:GetObject', 's3:ListBucket'],
        resources: [this.artifactsBucket.bucketArn, `${this.artifactsBucket.bucketArn}/*`],
      })
    );

    // S3 Tables Infrastructure using Custom Resource
    // Build Docker image via CodeBuild (ARM64) — same pattern as stream-processor/backfill
    const s3TablesManagerBuild = new ArmBuildConstruct(this, 'S3TablesManagerBuild', {
      sourcePath: path.join(__dirname, '../../../../lambda/s3tables-manager'),
      region: this.region,
      namePrefix: 's3tables-manager',
    });

    // Custom Resource Lambda for S3 Tables management
    const s3TablesManagerFn = new lambda.DockerImageFunction(this, 'S3TablesManager', {
      functionName: `${props.projectName}-s3tables-manager`,
      code: lambda.DockerImageCode.fromEcr(s3TablesManagerBuild.repository, {
        tagOrDigest: s3TablesManagerBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      environment: {
        REGION: this.region,
      },
    });

    // Ensure ECR image exists before Lambda is created
    s3TablesManagerBuild.repository.grantPull(s3TablesManagerFn);
    s3TablesManagerFn.node.addDependency(s3TablesManagerBuild.buildCompletion);

    // S3 Tables: scoped to specific actions needed for create/delete bucket, namespace, and tables
    s3TablesManagerFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3tables:CreateTableBucket',
          's3tables:DeleteTableBucket',
          's3tables:GetTableBucket',
          's3tables:ListTableBuckets',
          's3tables:CreateNamespace',
          's3tables:DeleteNamespace',
          's3tables:GetNamespace',
          's3tables:ListNamespaces',
          's3tables:CreateTable',
          's3tables:DeleteTable',
          's3tables:GetTable',
          's3tables:ListTables',
          's3tables:UpdateTableMetadataLocation',
          's3tables:GetTableMetadataLocation',
        ],
        resources: [`arn:aws:s3tables:${this.region}:${this.account}:bucket/*`],
      })
    );
    // Glue: scoped to federated catalog lifecycle actions only
    s3TablesManagerFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['glue:CreateCatalog', 'glue:DeleteCatalog', 'glue:GetCatalog'],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
          `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
        ],
      })
    );

    // Create Custom Resource Provider
    const s3TablesProvider = new cr.Provider(this, 'S3TablesProvider', {
      onEventHandler: s3TablesManagerFn,
    });

    // Namespace name — always computed for reference, but only created when realtime CDC is on.
    const glueNamespace = `${props.projectName}_iceberg`.replace(/-/g, '_');

    // Table names for the realtime CDC pipeline (DynamoDB Streams → PyIceberg).
    // When enableRealtimeReplication=false (Zero-ETL mode) these are NOT created so
    // the Zero-ETL UUID namespaces are the sole source of truth in the table bucket.
    const tableNames = props.enableRealtimeReplication
      ? [
          'holding',
          'party',
          'coverage',
          'financialactivity',
          'financialstatement',
          'relation',
          'policyproduct',
          'coverageproduct',
          'investproduct',
          'rider',
          'admincode',
          'typecode',
        ]
      : [];

    const tableBucket = new cdk.CustomResource(this, 'TableBucket', {
      serviceToken: s3TablesProvider.serviceToken,
      properties: {
        Action: 'CreateTableBucket',
        TableBucketName: `${props.projectName}-analytics-tables`,
        // Pass empty string when realtime replication is off — Lambda skips namespace creation.
        Namespace: props.enableRealtimeReplication ? glueNamespace : '',
        Tables: tableNames,
        Region: this.region,
        Version: '7.0',
      },
    });

    this.tableBucketArn = tableBucket.getAttString('TableBucketArn');
    this.tableBucketName = `${props.projectName}-analytics-tables`;
    this.namespace = glueNamespace;
    this.tableBucketResource = tableBucket;

    // ============================================================
    // S3 Tables → Glue Federated Catalog → Athena integration
    //
    // Steps (per AWS documentation):
    // 1. IAM role that Lake Formation assumes to vend credentials to Athena
    // 2. Register the S3 table bucket ARN pattern with Lake Formation
    //    (requires WithFederation + WithPrivilegedAccess — not in CFN spec yet,
    //     so we call the API directly via AwsCustomResource)
    // 3. Create the Glue federated catalog named "s3tablescatalog" using the
    //    built-in ConnectionName "aws:s3tables"
    //
    // Athena query syntax after setup:
    //   SELECT * FROM "s3tablescatalog/<bucket-name>"."<namespace>"."<table>" LIMIT 10
    // ============================================================

    // 1. LakeFormation data access role — assumed by LF to vend credentials to Athena
    const lfDataAccessRole = new iam.Role(this, 'LFDataAccessRole', {
      roleName: `${props.projectName}-lf-s3tables-data-access`,
      assumedBy: new iam.ServicePrincipal('lakeformation.amazonaws.com'),
      description: 'Assumed by Lake Formation to vend credentials for S3 Tables access',
    });

    // AWS documentation requires three STS actions in the trust policy;
    // CDK ServicePrincipal only generates sts:AssumeRole — add the missing two.
    lfDataAccessRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('lakeformation.amazonaws.com')],
        actions: ['sts:SetSourceIdentity', 'sts:SetContext'],
      })
    );

    lfDataAccessRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3tables:ListTableBuckets'],
        resources: ['*'],
      })
    );

    lfDataAccessRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3tables:GetTableBucket',
          's3tables:GetNamespace',
          's3tables:ListNamespaces',
          's3tables:GetTable',
          's3tables:ListTables',
          's3tables:GetTableMetadataLocation',
          's3tables:GetTableData',
          's3tables:UpdateTableMetadataLocation', // required for Glue UpdateTable via LF federation
          's3tables:PutTableData', // required for Iceberg metadata writes via LF
          's3tables:CreateTable', // required for Iceberg createOrReplace via LF federation
          's3tables:DeleteTable', // required for Iceberg createOrReplace (replace = delete+create)
        ],
        resources: [`arn:aws:s3tables:${this.region}:${this.account}:bucket/*`],
      })
    );

    // 2. Register S3 table bucket pattern with Lake Formation
    // WithPrivilegedAccess is not in CloudFormation spec so we call the API directly.
    const s3TablesLFRegistration = new cr.AwsCustomResource(this, 'S3TablesLFRegistration', {
      onCreate: {
        service: 'LakeFormation',
        action: 'registerResource',
        parameters: {
          ResourceArn: `arn:aws:s3tables:${this.region}:${this.account}:bucket/*`,
          RoleArn: lfDataAccessRole.roleArn,
          WithFederation: true,
          HybridAccessEnabled: true, // equivalent to --with-privileged-access
        },
        physicalResourceId: cr.PhysicalResourceId.of('s3tables-lf-registration'),
        ignoreErrorCodesMatching: 'AlreadyExistsException',
      },
      onDelete: {
        service: 'LakeFormation',
        action: 'deregisterResource',
        parameters: {
          ResourceArn: `arn:aws:s3tables:${this.region}:${this.account}:bucket/*`,
        },
        ignoreErrorCodesMatching: 'EntityNotFoundException',
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['lakeformation:RegisterResource', 'lakeformation:DeregisterResource'],
          resources: ['*'], // LF register/deregister do not support resource-level restrictions
        }),
        new iam.PolicyStatement({
          actions: ['iam:PassRole', 'iam:GetRole'],
          resources: [lfDataAccessRole.roleArn],
        }),
      ]),
    });
    s3TablesLFRegistration.node.addDependency(tableBucket);

    // 3. Glue federated catalog — call Glue CreateCatalog API directly via AwsCustomResource.
    // AWS::Glue::Catalog CloudFormation resource type is not yet available in all regions,
    // so we bypass CFN resource type validation by calling the Glue SDK directly.
    //
    // The singleton Lambda role (established by S3TablesLFRegistration above) must be a
    // Lake Formation admin so that glue:CreateCatalog succeeds under LF authorization.
    // We obtain the role via grantPrincipal and add it to DataLakeAdminSettings.
    const singletonRole = s3TablesLFRegistration.grantPrincipal as iam.IRole;
    this.lfGrantSingletonRoleArn = singletonRole.roleArn;

    // This CfnDataLakeSettings supersedes the one in glue-catalog-stack (same CDK bootstrap
    // roles + the singleton Lambda role). data-lake-stack deploys after glue-catalog-stack
    // so this becomes the authoritative LF admin list.
    const dataLakeAdminSettings = new lakeformation.CfnDataLakeSettings(
      this,
      'DataLakeAdminSettings',
      {
        admins: [
          {
            dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}`,
          },
          {
            dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}`,
          },
          { dataLakePrincipalIdentifier: singletonRole.roleArn },
        ],
      }
    );
    dataLakeAdminSettings.node.addDependency(s3TablesLFRegistration);

    const s3TablesCatalog = new cr.AwsCustomResource(this, 'S3TablesFederatedCatalog', {
      onCreate: {
        service: 'Glue',
        action: 'createCatalog',
        parameters: {
          Name: 's3tablescatalog',
          CatalogInput: {
            FederatedCatalog: {
              Identifier: `arn:aws:s3tables:${this.region}:${this.account}:bucket/*`,
              ConnectionName: 'aws:s3tables',
            },
            CreateDatabaseDefaultPermissions: [],
            CreateTableDefaultPermissions: [],
          },
        },
        physicalResourceId: cr.PhysicalResourceId.of('s3tables-glue-federated-catalog'),
        ignoreErrorCodesMatching: 'AlreadyExistsException',
      },
      onDelete: {
        service: 'Glue',
        action: 'deleteCatalog',
        parameters: {
          CatalogId: `${this.account}:s3tablescatalog`,
        },
        ignoreErrorCodesMatching: 'EntityNotFoundException|AccessDeniedException',
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['glue:CreateCatalog', 'glue:DeleteCatalog', 'glue:GetCatalog'],
          resources: [
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
          ],
        }),
        new iam.PolicyStatement({
          actions: ['glue:PassConnection'],
          resources: [`arn:aws:glue:${this.region}:${this.account}:connection/aws:s3tables`],
        }),
      ]),
    });
    s3TablesCatalog.node.addDependency(dataLakeAdminSettings);
    s3TablesCatalog.node.addDependency(s3TablesLFRegistration);

    // Apply cdk-nag suppressions for S3 bucket security configuration
    // S1: Server access logging disabled on operational/results/temp buckets (acceptable for non-sensitive data)
    // S10: S3 bucket policy does not require SSL (these buckets are internal-use and accessed only by AWS services)
    NagSuppressions.addResourceSuppressions(this.artifactsBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Artifacts bucket stores internal semantic layer metadata; access logging not required for operational data',
      },
      {
        id: 'AwsSolutions-S10',
        reason:
          'Artifacts bucket stores internal semantic layer metadata and is accessed only by AWS services and authenticated Lambda functions; public access is blocked and encryption is enabled',
      },
    ]);

    NagSuppressions.addResourceSuppressions(this.athenaResultsBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Athena results bucket stores temporary query results with 30-day expiration; logging not required for operational ephemeral data',
      },
      {
        id: 'AwsSolutions-S10',
        reason:
          'Athena results are internal-use temporary data accessed only by AWS services and authenticated principals; public access is blocked',
      },
    ]);

    NagSuppressions.addResourceSuppressions(this.knowledgeBaseBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Knowledge Base bucket stores internal ontology patterns; access logging not required for operational metadata',
      },
      {
        id: 'AwsSolutions-S10',
        reason:
          'Knowledge Base bucket stores internal ontology patterns accessed only by Bedrock and authenticated AWS services; public access is blocked and encryption is enabled',
      },
    ]);

    NagSuppressions.addResourceSuppressions(this.loggingBucket, [
      {
        id: 'AwsSolutions-S1',
        reason:
          'Logging bucket is a sink for other AWS services (CloudFront, ELB) and S3 access logs; self-logging is not required and would create infinite recursion',
      },
      {
        id: 'AwsSolutions-S10',
        reason:
          'Logging bucket is designated to receive logs from AWS services via specific principals; encrypted at rest and has block public access enabled',
      },
    ]);

    // Suppress S10 for bucket policies (they don't require SSL enforcement on logging/internal buckets)
    NagSuppressions.addResourceSuppressionsByPath(
      this,
      `/${this.stackName}/ArtifactsBucket/Policy`,
      [
        {
          id: 'AwsSolutions-S10',
          reason:
            'Bucket policy for internal artifacts; SSL enforcement is not required for service-to-service internal access',
        },
      ]
    );

    NagSuppressions.addResourceSuppressionsByPath(
      this,
      `/${this.stackName}/AthenaResultsBucket/Policy`,
      [
        {
          id: 'AwsSolutions-S10',
          reason:
            'Bucket policy for internal Athena results; SSL enforcement is not required for service-to-service internal access',
        },
      ]
    );

    NagSuppressions.addResourceSuppressionsByPath(
      this,
      `/${this.stackName}/KnowledgeBaseBucket/Policy`,
      [
        {
          id: 'AwsSolutions-S10',
          reason:
            'Bucket policy for internal knowledge base; SSL enforcement is not required for service-to-service internal access',
        },
      ]
    );

    NagSuppressions.addResourceSuppressionsByPath(this, `/${this.stackName}/LoggingBucket/Policy`, [
      {
        id: 'AwsSolutions-S10',
        reason:
          'Bucket policy for log delivery from AWS services; SSL enforcement is not required for service-authorized logging',
      },
    ]);

    // Outputs
    new cdk.CfnOutput(this, 'TableBucketArn', {
      value: this.tableBucketArn,
      description: 'S3 Table Bucket ARN for analytics tables',
      exportName: `${props.projectName}-table-bucket-arn`,
    });

    new cdk.CfnOutput(this, 'Namespace', {
      value: this.namespace,
      description: 'S3 Tables namespace',
      exportName: `${props.projectName}-table-namespace`,
    });

    new cdk.CfnOutput(this, 'ArtifactsBucketName', {
      value: this.artifactsBucket.bucketName,
      description: 'Artifacts S3 bucket name',
      exportName: `${props.projectName}-artifacts-bucket`,
    });

    new cdk.CfnOutput(this, 'AthenaResultsBucketName', {
      value: this.athenaResultsBucket.bucketName,
      description: 'Athena results S3 bucket name',
      exportName: `${props.projectName}-athena-results-bucket`,
    });

    new cdk.CfnOutput(this, 'KnowledgeBaseBucketName', {
      value: this.knowledgeBaseBucket.bucketName,
      description: 'Knowledge Base S3 bucket name',
      exportName: `${props.projectName}-kb-bucket`,
    });

    new cdk.CfnOutput(this, 'S3TablesFederatedCatalogName', {
      value: 's3tablescatalog',
      description:
        'Glue federated catalog name for S3 Tables (query as: SELECT * FROM "s3tablescatalog/<bucket-name>"."<namespace>"."<table>")',
    });
  }
}
