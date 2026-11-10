import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as path from 'path';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { LfAdminRegistration } from '../../common/constructs/lf-admin-registration';
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

    // ── Lake Formation grants on source zetl_* namespaces (DYNAMIC) ──────────
    // Each Zero-ETL redeployment creates a NEW zetl_<uuid> namespace, and the
    // Glue MV job reads from the NEWEST one per source table. A hardcoded list
    // of namespace ids (the previous approach) inevitably goes stale: the next
    // redeploy's namespace is never in the list, so the MV job hits
    // "Principal does not have any privilege" and fails (observed 2026-06-12).
    //
    // Instead, discover every current zetl_* namespace at DEPLOY time and grant
    // the job role SELECT/DESCRIBE on each. Re-runs on every deploy (poll
    // counter in physicalResourceId) so a fresh Zero-ETL namespace is always
    // covered without editing this file.
    const s3tCatalogId = `${this.account}:s3tablescatalog/${bucketName}`;

    // Provider-backed custom resource: at every deploy, discover every current
    // zetl_* namespace and grant the MV job role SELECT/DESCRIBE on each table.
    // This replaces a hardcoded namespace list that went stale on each Zero-ETL
    // redeploy (the newest namespace was never granted → MV job "Principal does
    // not have any privilege"). The Lambda just does the work and returns; the
    // cr.Provider handles the CloudFormation response protocol.
    const grantZetlLfFn = new lambda.Function(this, 'GrantZetlLfFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(5),
      code: lambda.Code.fromInline(
        [
          'import time',
          'import boto3',
          '',
          '# The LF admin registration this resource depends on completes seconds before',
          '# this handler runs, and the first grant can still see the pre-registration',
          '# state. Retry AccessDenied with backoff for ~2 min before declaring failure.',
          'RETRY_BUDGET_S = 120',
          '',
          'def _grant(lf, role_arn, catalog_id, ns, name):',
          '    deadline = time.time() + RETRY_BUDGET_S',
          '    delay = 5',
          '    while True:',
          '        try:',
          '            lf.grant_permissions(',
          '                Principal={"DataLakePrincipalIdentifier": role_arn},',
          '                Resource={"Table": {"CatalogId": catalog_id, "DatabaseName": ns, "Name": name}},',
          '                Permissions=["SELECT", "DESCRIBE"])',
          '            return',
          '        except lf.exceptions.AccessDeniedException:',
          '            if time.time() + delay > deadline:',
          '                raise',
          '            time.sleep(delay)',
          '            delay = min(delay * 2, 30)',
          '',
          'def handler(event, context):',
          '    if event["RequestType"] == "Delete":',
          '        return {"PhysicalResourceId": "grant-zetl-lf"}',
          '    p = event["ResourceProperties"]',
          '    bucket_arn, catalog_id, role_arn = p["TableBucketArn"], p["CatalogId"], p["RoleArn"]',
          '    s3t = boto3.client("s3tables"); lf = boto3.client("lakeformation")',
          '    granted, failed = 0, []',
          '    for page in s3t.get_paginator("list_namespaces").paginate(tableBucketARN=bucket_arn):',
          '        for entry in page["namespaces"]:',
          '            ns = entry["namespace"][0]',
          '            if not ns.startswith("zetl_"):',
          '                continue',
          '            for t in s3t.list_tables(tableBucketARN=bucket_arn, namespace=ns).get("tables", []):',
          '                try:',
          '                    _grant(lf, role_arn, catalog_id, ns, t["name"])',
          '                    granted += 1',
          '                except Exception as e:',
          '                    failed.append(ns + "." + t["name"] + ": " + str(e))',
          '                    if isinstance(e, lf.exceptions.AccessDeniedException):',
          '                        break  # same denial for every table; do not burn the retry budget 24x',
          '    print("granted " + str(granted) + " table permissions")',
          '    if failed:',
          '        # Fail the deploy: a silently-skipped grant leaves the MV job unable',
          '        # to see its source tables, and every scheduled run then fails.',
          '        raise RuntimeError(str(len(failed)) + " zetl LF grant(s) failed; first: " + failed[0])',
          '    return {"PhysicalResourceId": "grant-zetl-lf", "Data": {"granted": granted}}',
        ].join('\n')
      ),
    });
    grantZetlLfFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3tables:ListNamespaces', 's3tables:ListTables', 's3tables:GetTable'],
        resources: ['*'],
      })
    );
    // GetDataAccess is NOT optional here even though this Lambda never vends
    // credentials: on the S3 Tables federated catalog, GrantPermissions resolves
    // the target table through Lake Formation's own data-access path, and without
    // it every grant fails with the misleading "Insufficient Glue permissions to
    // access table" — identical to the error for missing glue:GetTable. Bisected
    // live on 2026-11-05: LF admin + glue reads alone still fail; adding
    // GetDataAccess is what flips the grants to success.
    grantZetlLfFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['lakeformation:GrantPermissions', 'lakeformation:GetDataAccess'],
        resources: ['*'],
      })
    );
    // LF GrantPermissions reads the target through the Glue catalog; without
    // these every grant fails "Insufficient Glue permissions to access table".
    grantZetlLfFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['glue:GetCatalog', 'glue:GetDatabase', 'glue:GetTable'],
        resources: ['*'],
      })
    );
    // The grantor holds no grantable permissions on the zetl_* tables (Zero-ETL
    // creates them), so it can only grant as an LF admin. See LfAdminRegistration.
    const grantZetlLfAdmin = new LfAdminRegistration(this, 'GrantZetlLfAdminSettings', {
      adminArns: [grantZetlLfFn.role!.roleArn],
    });

    const grantZetlProvider = new cr.Provider(this, 'GrantZetlLfProvider', {
      onEventHandler: grantZetlLfFn,
    });
    const grantZetlLf = new cdk.CustomResource(this, 'GrantZetlLfPermissions', {
      serviceToken: grantZetlProvider.serviceToken,
      properties: {
        TableBucketArn: tableBucketArn,
        CatalogId: s3tCatalogId,
        RoleArn: jobRole.roleArn,
        // Re-run discovery+grant on every deploy so new zetl_* namespaces are covered.
        Nonce: Date.now().toString(),
      },
    });
    grantZetlLf.node.addDependency(grantZetlLfAdmin);

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
        // pyiceberg is needed to capture/restore Iceberg column doc strings +
        // table description across the delete/recreate refresh. S3Tables
        // federation does NOT durably persist Glue Comments/Description (it
        // reconciles them back from the Iceberg schema), so the curated
        // metadata_agent descriptions must be re-applied directly to the
        // Iceberg schema via pyiceberg — mirroring the agent's own write path.
        '--additional-python-modules': 'pyiceberg==0.7.1',
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
