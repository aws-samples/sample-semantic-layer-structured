import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as path from 'path';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { DataLakeStack } from './data-lake-stack';
import { ZeroEtlStack } from './zeroetl';

export interface NormalizedViewsStackProps extends cdk.StackProps {
  projectName: string;
  dataLakeStack: DataLakeStack;
  zeroEtlStack: ZeroEtlStack;
  refreshIntervalHours?: number; // default 6
}

export class NormalizedViewsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: NormalizedViewsStackProps) {
    super(scope, id, props);

    const refreshHours = props.refreshIntervalHours ?? 6;
    const bucketName = props.dataLakeStack.tableBucketName;
    const artifactsBucket = props.dataLakeStack.artifactsBucket;

    // ── IAM role ────────────────────────────────────────────────────────────
    const jobRole = new iam.Role(this, 'GlueMVJobRole', {
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole'),
      ],
      inlinePolicies: {
        S3TablesPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                's3tables:GetTableBucket',
                's3tables:ListNamespaces',
                's3tables:GetNamespace',
                's3tables:CreateNamespace',
                's3tables:ListTables',
                's3tables:GetTable',
                's3tables:PutTableMaintenanceConfiguration',
                // Data access — required when lakeformation-enabled=false (IAM-direct / hybrid mode)
                's3tables:GetTableData',
                's3tables:PutTableData',
                's3tables:GetTableMetadataLocation',
                's3tables:UpdateTableMetadataLocation',
                's3tables:CreateTable',
                's3tables:DeleteTable',
              ],
              resources: [
                `arn:aws:s3tables:${this.region}:${this.account}:bucket/${bucketName}`,
                `arn:aws:s3tables:${this.region}:${this.account}:bucket/${bucketName}/*`,
              ],
            }),
            // AWSGlueServiceRole only permits s3://aws-glue-* — grant explicit read on artifacts bucket
            new iam.PolicyStatement({
              actions: ['s3:GetObject', 's3:ListBucket'],
              resources: [artifactsBucket.bucketArn, `${artifactsBucket.bucketArn}/*`],
            }),
          ],
        }),
        LakeFormationPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['lakeformation:GetDataAccess'],
              resources: ['*'],
            }),
          ],
        }),
        GlueDataCatalogPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                'glue:GetCatalog',
                'glue:GetCatalogs',
                'glue:GetTable',
                'glue:GetTables',
                'glue:CreateTable',
                'glue:UpdateTable',
                'glue:GetDatabase',
                'glue:GetDatabases',
              ],
              resources: [
                `arn:aws:glue:${this.region}:${this.account}:catalog`,
                `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
                `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/${bucketName}`,
                `arn:aws:glue:${this.region}:${this.account}:database/*`,
                `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
              ],
            }),
          ],
        }),
      },
    });

    // PassRole on itself (required for MV auto-refresh)
    jobRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [jobRole.roleArn],
      })
    );

    // Apply cdk-nag suppressions for Glue MV job role
    // IAM4: AWSGlueServiceRole is required for Glue jobs to access the Glue Data Catalog
    NagSuppressions.addResourceSuppressions(jobRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'AWSGlueServiceRole is the standard AWS managed policy required for Glue jobs to access the Glue Data Catalog and perform automated schema/table management',
        appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSGlueServiceRole'],
      },
    ]);

    // ── Pre-create 'normalized' namespace so LF grant can reference it ───────
    // Lake Formation cannot grant permissions on a database that doesn't exist yet.
    // The Glue job creates the namespace at runtime, so we also create it here
    // during CDK deploy via a custom resource (idempotent — ConflictException is ignored).
    const tableBucketArn = `arn:aws:s3tables:${this.region}:${this.account}:bucket/${bucketName}`;

    const createNormalizedNs = new cr.AwsCustomResource(this, 'CreateNormalizedNamespace', {
      onCreate: {
        service: 'S3Tables',
        action: 'createNamespace',
        parameters: {
          tableBucketARN: tableBucketArn,
          namespace: ['normalized'],
        },
        physicalResourceId: cr.PhysicalResourceId.of('normalized-namespace'),
        ignoreErrorCodesMatching: 'ConflictException',
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['s3tables:CreateNamespace', 's3tables:GetNamespace'],
          resources: [tableBucketArn],
        }),
      ]),
    });

    // ── Lake Formation grants on source zetl_* namespaces ───────────────────
    // All known zetl_* namespaces — each Zero-ETL redeployment creates new UUIDs.
    // LF grants must cover every namespace the Glue job may read from (job picks newest).
    // Add new entries here after redeployments; old entries are harmless.
    const ZETL_NAMESPACES: string[] = [
      // Batch 4 (redeploy 2026-04-01, semantic-layer-dev)
      'zetl_0fcf456c_5821_4739_b594_32fb42d80a73',
      'zetl_14335b80_396f_4c30_913c_444902e5cb10',
      'zetl_16c76f57_3905_45da_93ed_3c1d4b72f196',
      'zetl_351f3256_dff1_4045_9238_5ec8abcbf944',
      'zetl_7698428a_bd2b_4dc4_90c6_ac47ce2d5871',
      'zetl_8ee52339_f1e1_4d73_99f1_06e79c97b2bd',
      'zetl_946f6685_d7c2_400f_a145_08f37153b24e',
      'zetl_b5a644dd_474a_4b13_bd3a_358d99d4dc9f',
      'zetl_cc86f78a_2d5a_4643_811f_6c6262cf28a4',
      'zetl_e3a58232_3570_4de4_a16b_3410c53b2f2c',
      'zetl_ebdc9373_f923_48e7_af6a_929ecca1a237',
      'zetl_f644c442_482d_440f_a282_b1e5d4de25d9',
    ];

    const s3tCatalogId = `${this.account}:s3tablescatalog/${bucketName}`;

    ZETL_NAMESPACES.forEach((zetlNs, i) => {
      new lakeformation.CfnPermissions(this, `LFSourceGrant${i}`, {
        dataLakePrincipal: { dataLakePrincipalIdentifier: jobRole.roleArn },
        resource: {
          tableResource: {
            catalogId: s3tCatalogId,
            databaseName: zetlNs,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT'],
      });
    });

    // Grant CREATE_TABLE on the target 'normalized' namespace
    const lfNormalizedGrant = new lakeformation.CfnPermissions(this, 'LFNormalizedDbGrant', {
      dataLakePrincipal: { dataLakePrincipalIdentifier: jobRole.roleArn },
      resource: {
        databaseResource: {
          catalogId: s3tCatalogId,
          name: 'normalized',
        },
      },
      permissions: ['CREATE_TABLE', 'DESCRIBE'],
    });
    lfNormalizedGrant.node.addDependency(createNormalizedNs);

    // ── Upload Glue script to artifacts bucket ───────────────────────────────
    new s3deploy.BucketDeployment(this, 'GlueScriptDeploy', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../../../../glue'))],
      destinationBucket: artifactsBucket,
      destinationKeyPrefix: 'glue-scripts',
    });

    // Spark --conf chain for S3Tables catalog
    // warehouse is required by the Glue catalog plugin to initialize (fixes NPE: region must not be null)
    const sparkConf = [
      'spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions',
      'spark.sql.catalog.s3t_catalog=org.apache.iceberg.spark.SparkCatalog',
      'spark.sql.catalog.s3t_catalog.type=glue',
      `spark.sql.catalog.s3t_catalog.glue.id=${this.account}:s3tablescatalog/${bucketName}`,
      `spark.sql.catalog.s3t_catalog.glue.account-id=${this.account}`,
      'spark.sql.catalog.s3t_catalog.glue.lakeformation-enabled=false',
      `spark.sql.catalog.s3t_catalog.glue.region=${this.region}`,
      // client.region is required by LakeFormationAwsClientFactory (S3FileIO with LF enabled)
      // to build table ARNs — glue.region alone is not sufficient
      `spark.sql.catalog.s3t_catalog.client.region=${this.region}`,
      `spark.sql.catalog.s3t_catalog.warehouse=s3://${artifactsBucket.bucketName}/mv-warehouse`,
      'spark.sql.defaultCatalog=s3t_catalog',
      'spark.sql.optimizer.answerQueriesWithMVs.enabled=true',
      'spark.sql.materializedViews.metadataCache.enabled=true',
      'spark.sql.optimizer.incrementalMVRefresh.enabled=true',
      'spark.sql.optimizer.incrementalMVRefresh.deltaThresholdCheckEnabled=false',
    ].join(' --conf ');

    // ── Glue 5.1 job ─────────────────────────────────────────────────────────
    const glueJob = new glue.CfnJob(this, 'NormalizedViewsJob', {
      name: `${props.projectName}-create-normalized-views`,
      role: jobRole.roleArn,
      glueVersion: '5.1',
      command: {
        name: 'glueetl',
        pythonVersion: '3',
        scriptLocation: `s3://${artifactsBucket.bucketName}/glue-scripts/create-normalized-views.py`,
      },
      defaultArguments: {
        '--enable-glue-datacatalog': 'true',
        '--conf': sparkConf,
        '--table_bucket_name': bucketName,
        '--account_id': this.account,
        '--region': this.region,
        '--refresh_hours': String(refreshHours),
      },
      executionProperty: { maxConcurrentRuns: 1 },
      timeout: 120,
    });

    // ── EventBridge schedule trigger ─────────────────────────────────────────
    const rule = new events.Rule(this, 'NormalizedViewsSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.hours(refreshHours)),
      description: `Trigger ${props.projectName} normalized views Glue job every ${refreshHours}h`,
    });
    rule.addTarget(
      new targets.AwsApi({
        service: 'Glue',
        action: 'startJobRun',
        parameters: {
          JobName: glueJob.ref,
          Arguments: {
            '--table_bucket_name': bucketName,
            '--account_id': this.account,
            '--region': this.region,
            '--refresh_hours': String(refreshHours),
          },
        },
      })
    );
  }
}
