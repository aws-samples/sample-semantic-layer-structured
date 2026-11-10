import * as cdk from 'aws-cdk-lib';
import * as athena from 'aws-cdk-lib/aws-athena';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sam from 'aws-cdk-lib/aws-sam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import { LfAdminRegistration } from '../../common/constructs/lf-admin-registration';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { DataLakeStack } from './data-lake-stack';
import { GlueCatalogStack } from './glue-catalog-stack';
import { DynamoDBStack } from './dynamodb-stack';
// Reuse the single shared SnowflakeConfig type declared by Task 1 (bin/snowflake-config.ts).
// Re-export it here so callers can type AthenaStack props without reaching into bin/.
import { SnowflakeConfig } from '../../../bin/snowflake-config';

export { SnowflakeConfig };

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
  /** Snowflake connector config; when present, a federated snowflake_catalog is deployed. */
  snowflakeConfig?: SnowflakeConfig;
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
  /** Snowflake connector spill bucket — only created when props.snowflakeConfig is set. */
  public readonly snowflakeSpillBucket?: s3.Bucket;
  /**
   * Athena federated catalogs for Snowflake — one per configured database, empty when
   * props.snowflakeConfig is unset. A connector Lambda pins exactly one `db=` in its
   * connection string, so N databases means N catalogs.
   */
  public readonly snowflakeCatalogs: athena.CfnDataCatalog[] = [];
  /**
   * Names of the Snowflake connector Lambda functions, index-aligned with
   * {@link snowflakeCatalogs}. Consumed cross-stack by AgentCoreStack, which must grant
   * agent roles `lambda:InvokeFunction` on each one.
   */
  public readonly snowflakeConnectorFunctionNames: string[] = [];
  /**
   * Secrets Manager secret holding the Snowflake connector credentials — only when
   * props.snowflakeConfig is set.
   *
   * CDK owns the RESOURCE; the VALUE is written post-deploy by
   * `cdk/scripts/sync-snowflake-secret.ts` from the deployer's local `.env`. The
   * credential is deliberately kept out of the stack: passing it through
   * `SecretValue.unsafePlainText` would embed the plaintext in the synthesized
   * CloudFormation template (on disk in `cdk.out/` and readable in AWS via
   * `cloudformation:GetTemplate`).
   */
  public readonly snowflakeSecret?: secretsmanager.Secret;

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
      // Suffix with the account id so the globally-unique bucket name doesn't
      // collide with a same-named bucket in another AWS account (S3 bucket
      // namespace is global). Matches the `-${this.account}` convention every
      // other bucket in this app uses.
      bucketName: `${connectorName}-spill-${this.account}`,
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
    // Snowflake Connector for Athena Federated Queries (flag-gated)
    // Deployed via AWS Serverless Application Repository when props.snowflakeConfig
    // is supplied. Inert in normal synth until the config is wired in (Task 4).
    // ============================================================================
    if (props.snowflakeConfig) {
      const cfg = props.snowflakeConfig;
      // Shared across every per-database connector: one spill bucket, one credentials
      // secret, one execution role. Only the Lambda, its catalog and its invoke
      // permission are per-database.
      const sfName = `${props.projectName}-snowflake-connector`;

      // Own spill bucket for query results that exceed Lambda memory.
      // SSE-S3 (bucket-managed) so the connector's spill PutObject is never denied; the SAR
      // connector self-encrypts spill payloads at the app layer (DisableSpillEncryption=false),
      // independent of bucket SSE. (DDB connector uses SSE-KMS because that SAR app is passed a
      // KMSKeyId; the Snowflake SAR params in the targeted version expose none.)
      const sfSpillBucket = new s3.Bucket(this, 'SnowflakeSpillBucket', {
        bucketName: `${sfName}-spill`,
        encryption: s3.BucketEncryption.S3_MANAGED,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        enforceSSL: true,
        versioned: false,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
      });
      this.snowflakeSpillBucket = sfSpillBucket;

      // Credentials secret for the connector. Created EMPTY (placeholder value) and
      // populated after deploy by `npm run sync:snowflake-secret`, which reads the
      // deployer's local `.env` and calls PutSecretValue.
      //
      // Why the value is not set here: the only CDK API that accepts a literal string
      // is `SecretValue.unsafePlainText`, which writes it verbatim into the
      // synthesized CloudFormation template. That puts the Snowflake private key in
      // `cdk.out/` on disk and in the stack template stored in CloudFormation, where
      // `cloudformation:GetTemplate` exposes it — a wider audience than
      // `secretsmanager:GetSecretValue` on this one ARN. See
      // cdk/README.md § Snowflake Athena connector.
      //
      // The placeholder is a JSON object rather than a bare string so that anything
      // reading the secret before the sync gets a parseable document with an explicit
      // marker, instead of a JSON parse error that looks like a connector bug.
      const sfSecret = new secretsmanager.Secret(this, 'SnowflakeConnectorSecret', {
        secretName: cfg.secretName,
        description:
          'Snowflake credentials for the Athena federated connector. Value is written ' +
          'post-deploy by cdk/scripts/sync-snowflake-secret.ts from a local .env file.',
        secretObjectValue: {
          // Marker only — overwritten wholesale by the first PutSecretValue.
          placeholder: cdk.SecretValue.unsafePlainText(
            'unset — run `npm run sync:snowflake-secret` from cdk/'
          ),
        },
        // DESTROY (not RETAIN) so a `cdk destroy` followed by a redeploy does not
        // collide with a scheduled-for-deletion secret of the same name. This matches
        // the spill bucket's policy above and the project's from-scratch-redeploy
        // requirement. The credential is reproducible from `.env`, so retaining it
        // buys nothing.
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      this.snowflakeSecret = sfSecret;

      // No rotation: this is a machine credential whose rotation is a Snowflake-side
      // key rollover (ALTER USER … SET RSA_PUBLIC_KEY) followed by re-running the sync
      // script — not something a Secrets Manager rotation Lambda can perform.
      NagSuppressions.addResourceSuppressions(sfSecret, [
        {
          id: 'AwsSolutions-SMG4',
          reason:
            'Snowflake connector credential is rotated out-of-band (Snowflake key rollover + ' +
            're-run of sync-snowflake-secret.ts); no AWS-side rotation Lambda applies.',
        },
      ]);

      // IAM role for the Snowflake connector Lambda function.
      const snowflakeConnectorRole = new iam.Role(this, 'SnowflakeConnectorRole', {
        roleName: sfName,
        assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
        description: 'Role for Athena Snowflake connector Lambda',
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });

      // S3 permissions.
      snowflakeConnectorRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['s3:ListAllMyBuckets'],
          resources: ['*'],
        })
      );
      sfSpillBucket.grantReadWrite(snowflakeConnectorRole);

      // Glue schema discovery — Snowflake connectors created without a Glue connection
      // can still read Glue for federation. Scope to the catalog + workgroup ARNs
      // (mirror the DynamoDB connector role's Glue discovery statement).
      snowflakeConnectorRole.addToPolicy(
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
            `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.projectName}-workgroup`,
          ],
        })
      );

      // Snowflake-specific: read the credentials secret. Scoped to the concrete
      // secretArn of the resource created above rather than a hand-built
      // `…:secret:<name>-*` wildcard string — now that CDK owns the secret, the exact
      // ARN (including Secrets Manager's random suffix) is known at synth time.
      sfSecret.grantRead(snowflakeConnectorRole);

      // NO VPC ATTACHMENT — and therefore no ENI permissions, no security group, and no
      // subnet list. This is a deliberate, live-measured decision, not an omission.
      //
      // The connector's only network dependencies are public endpoints: Snowflake itself,
      // Secrets Manager, STS, and — for result sets past a few MB — Snowflake's own S3
      // result stage in whatever region the account lives in. Left out of a VPC, the
      // function gets Lambda's managed egress and reaches all of them. Placed in this
      // stack's private subnets it reached Snowflake's control plane through the NAT
      // gateway, but the read path was unusable:
      //
      //                              in private subnets        no VPC
      //   cold start                      150–190 s             12.7 s
      //   SELECT COUNT(*) (10,969 rows)   894 s, cancelled      15.2 s
      //   full-table scan                 never completed       19.2 s  (10,969 rows)
      //
      // In-VPC failures surfaced as `NoHttpResponseException … sfc-*-customer-stage.s3.*`
      // and as bare Athena 504s, i.e. as connector bugs rather than as a placement
      // problem. Putting it back would need per-subnet NAT plus an S3 gateway-endpoint
      // route that covers Snowflake's stage bucket region, to buy nothing: the connector
      // holds no inbound surface (Athena invokes it through the Lambda API, not the
      // network) and its credential is an ARN-scoped secret, not a VPC-reachable host.
      //
      // TRAP for anyone reverting this: emptying `SecurityGroupIds`/`SubnetIds` on an
      // EXISTING connector does NOT detach it. The SAR template guards both under
      // `Fn::If … AWS::NoValue`, and CloudFormation treats an absent `VpcConfig` on an
      // update as "leave the attachment alone". Placement only changes when the function
      // is replaced — which the per-database naming below happens to force.

      // One connector Lambda + one Athena catalog per database: the SAR app takes a single
      // `DefaultConnectionString`, which pins exactly one `db=`.
      for (const database of cfg.databases) {
        // SAR requires `^[a-z0-9-_]{1,64}$` for LambdaFunctionName, so fold the (uppercase,
        // underscore-separated) Snowflake database name to lowercase. Two slugs because
        // Athena catalog names allow `_` but not `-`, and Lambda names read better with `-`.
        const sfSlug = database.toLowerCase().replace(/[^a-z0-9]+/g, '_');
        const sfDashSlug = sfSlug.replace(/_/g, '-');
        // Construct-ID fragment: `FHIR_DB` → `FhirDb`. Stable per database, so adding a
        // second database never renames the first one's resources.
        const sfConstructId = sfSlug
          .split('_')
          .filter((part) => part.length > 0)
          .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
          .join('');
        const sfFunctionName = `${props.projectName}-snowflake-${sfDashSlug}-connector`;

        // Fail loudly at synth rather than mid-deploy: CloudFormation rejects the SAR
        // parameter on its AllowedPattern, and the resulting nested-stack error names the
        // pattern but not which database produced it.
        if (!/^[a-z0-9-_]{1,64}$/.test(sfFunctionName)) {
          throw new Error(
            `Snowflake connector name '${sfFunctionName}' (from database '${database}') does not ` +
              'match the SAR AllowedPattern ^[a-z0-9-_]{1,64}$. Shorten projectName or the ' +
              'database name.'
          );
        }

        // Athena-data-catalog federated connection string; credentials come from the secret.
        const connStr =
          `snowflake://jdbc:snowflake://${cfg.account}.snowflakecomputing.com/` +
          `?warehouse=${cfg.warehouse}&db=${database}&role=${cfg.role}&\${${cfg.secretName}}`;

        // casing_mode note: Snowflake identifiers here are uppercase (FHIR_DB.FHIR_R4) and
        // Athena lowercases identifiers by default. The SAR template has no casing
        // parameter, so the connector Lambda env needs casing_mode=UPPER post-deploy —
        // `npm run deploy:snowflake` applies it. Re-apply after ANY change to this SAR app:
        // a nested-stack update rewrites the function's environment map wholesale.
        const sfConnectorApp = new sam.CfnApplication(
          this,
          `SnowflakeConnectorApp${sfConstructId}`,
          {
            location: {
              applicationId: `arn:aws:serverlessrepo:${this.region}:292517598671:applications/AthenaSnowflakeConnector`,
              // Latest published AthenaSnowflakeConnector version, verified against
              // `aws serverlessrepo list-application-versions` on 2026-08-11. The previous
              // value ('2024.44.1') was a placeholder that was never published and made the
              // stack undeployable.
              semanticVersion: '2026.28.1',
            },
            // Unlike the DynamoDB connector block above, no KMSKeyId param is passed here:
            // the AthenaSnowflakeConnector SAR template exposes no KMSKeyId parameter in the
            // version we target and self-encrypts spill (DisableSpillEncryption=false, so the
            // connector encrypts spill with a generated data key).
            parameters: {
              LambdaFunctionName: sfFunctionName,
              DefaultConnectionString: connStr,
              SecretNamePrefix: cfg.secretName,
              SpillBucket: sfSpillBucket.bucketName,
              // Per-database prefix in the shared bucket so one database's spill fragments
              // are never mistaken for another's during cleanup or debugging.
              SpillPrefix: `athena-spill/${sfSlug}`,
              DisableSpillEncryption: 'false',
              LambdaMemory: '3008',
              LambdaTimeout: '900',
              LambdaRoleArn: snowflakeConnectorRole.roleArn,
              // SecurityGroupIds / SubnetIds intentionally absent — see the block comment above.
            },
          }
        );

        // Athena Data Catalog pointing at this database's connector Lambda.
        const sfCatalog = new athena.CfnDataCatalog(this, `SnowflakeCatalog${sfConstructId}`, {
          name: `snowflake_${sfSlug}`,
          description: `Athena catalog for Snowflake database ${database} via SAR connector`,
          type: 'LAMBDA',
          parameters: {
            function: `arn:aws:lambda:${this.region}:${this.account}:function:${sfFunctionName}`,
          },
        });
        // Ensure the catalog is created after the SAM application.
        sfCatalog.node.addDependency(sfConnectorApp);
        this.snowflakeCatalogs.push(sfCatalog);
        this.snowflakeConnectorFunctionNames.push(sfFunctionName);

        // Allow Athena to invoke this connector Lambda.
        const sfLambdaPermission = new lambda.CfnPermission(
          this,
          `AthenaInvokeSnowflake${sfConstructId}`,
          {
            functionName: sfFunctionName,
            action: 'lambda:InvokeFunction',
            principal: 'athena.amazonaws.com',
          }
        );
        sfLambdaPermission.node.addDependency(sfConnectorApp);
      }

      // Grant the Athena execution role invoke on every Snowflake connector Lambda
      // (mirror the DynamoDB connector grant above). The agent-role grants live in
      // AgentCoreStack, which reads `snowflakeConnectorFunctionNames`.
      this.athenaExecutionRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lambda:InvokeFunction'],
          resources: this.snowflakeConnectorFunctionNames.map(
            (name) => `arn:aws:lambda:${this.region}:${this.account}:function:${name}`
          ),
        })
      );

      // cdk-nag: mirror the DynamoDB spill-bucket suppression (S1 — access logging
      // is not required for ephemeral query spill data).
      NagSuppressions.addResourceSuppressions(sfSpillBucket, [
        {
          id: 'AwsSolutions-S1',
          reason:
            'Spill bucket stores temporary intermediate data from Athena Snowflake connector queries; access logging is not required for ephemeral operational data',
        },
      ]);
    }

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

    // Additively register LF admins via a read-modify-write custom resource
    // (CfnDataLakeSettings is a per-account singleton that collides across
    // stacks on a fresh deploy). The additive merge also makes the historical
    // "carry forward ALL prior admins" concern automatic. See LfAdminRegistration.
    const lfAdmins: string[] = [
      `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}`,
      `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}`,
      props.dataLakeStack.lfGrantSingletonRoleArn,
      ...(props.additionalLakeFormationAdmins ?? []),
    ];

    const athenaLfAdmins = new LfAdminRegistration(this, 'AthenaDataLakeAdminSettings', {
      adminArns: lfAdmins,
    });

    // LF permission: athenaExecutionRole — DESCRIBE on DynamoDB database
    const athenaLfDbPerms = new lakeformation.CfnPermissions(
      this,
      'AthenaLFDynamoDBDatabasePermissions',
      {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.athenaExecutionRole.roleArn },
        resource: {
          databaseResource: {
            catalogId: this.account,
            name: props.glueCatalogStack.dynamodbDatabase.ref,
          },
        },
        permissions: ['DESCRIBE'],
      }
    );
    athenaLfDbPerms.node.addDependency(athenaLfAdmins);

    // LF permission: athenaExecutionRole — SELECT + DESCRIBE on all DynamoDB tables
    const athenaLfTablePerms = new lakeformation.CfnPermissions(
      this,
      'AthenaLFDynamoDBTablePermissions',
      {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.athenaExecutionRole.roleArn },
        resource: {
          tableResource: {
            catalogId: this.account,
            databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT', 'DESCRIBE'],
      }
    );
    athenaLfTablePerms.node.addDependency(athenaLfAdmins);

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
