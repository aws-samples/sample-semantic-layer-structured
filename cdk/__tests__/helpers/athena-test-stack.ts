/**
 * Test harness for synthesizing {@link AthenaStack} in isolation.
 *
 * AthenaStack normally depends on three concrete upstream stacks
 * (DataLakeStack, GlueCatalogStack, DynamoDBStack), but its constructor only
 * reads a small, well-defined set of fields from each (see below). DataLakeStack
 * in particular pulls a DockerImageFunction (CodeBuild/ECR) and therefore cannot
 * be instantiated hermetically in a unit test.
 *
 * This harness (Approach A) instead builds the minimal REAL constructs the
 * AthenaStack constructor actually touches — S3 buckets, DynamoDB tables, a Glue
 * database, and a VPC — inside a throwaway fixture stack, then assembles
 * structurally-typed stand-ins that are cast to the concrete upstream-stack
 * types. AthenaStack itself uses only pure CloudFormation constructs
 * (athena.CfnWorkGroup, sam.CfnApplication, IAM/KMS/S3/LakeFormation) plus
 * lambda.Function.fromFunctionName, so it synthesizes with no asset bundling or
 * Docker — the fixture is fully hermetic.
 *
 * Fields the AthenaStack constructor reads (verified against athena-stack.ts):
 *   - dataLakeStack.athenaResultsBucket   (s3.Bucket — grantReadWrite + bucketName)
 *   - dataLakeStack.artifactsBucket       (s3.Bucket — grantRead)
 *   - dataLakeStack.tableBucketArn        (string)
 *   - dataLakeStack.lfGrantSingletonRoleArn (string)
 *   - glueCatalogStack.dynamodbDatabase   (glue.CfnDatabase — reads .ref)
 *   - dynamodbStack.<13 tables>           (dynamodb.Table — grantReadData)
 *   - vpc                                 (ec2.Vpc — currently NOT read, but the
 *                                          prop is required, so a real one is built)
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Template } from 'aws-cdk-lib/assertions';
import { AthenaStack, SnowflakeConfig } from '../../lib/stacks/backend/athena-stack';
import { DataLakeStack } from '../../lib/stacks/backend/data-lake-stack';
import { GlueCatalogStack } from '../../lib/stacks/backend/glue-catalog-stack';
import { DynamoDBStack } from '../../lib/stacks/backend/dynamodb-stack';

/** Shared fake account/region so generated ARNs are concrete (no `${Token}`). */
const TEST_ENV = { account: '123456789012', region: 'us-east-1' } as const;

/** The 13 DynamoDB table fields AthenaStack's `allTables` array reads. */
const DDB_TABLE_FIELDS = [
  'adminCodesTable',
  'coverageProductsTable',
  'coveragesTable',
  'financialActivitiesTable',
  'financialStatementsTable',
  'holdingsTable',
  'investProductsTable',
  'metadataTable',
  'partiesTable',
  'policyProductsTable',
  'relationsTable',
  'ridersTable',
  'typeCodesTable',
] as const;

/**
 * Build the minimal real upstream constructs and assemble the fake upstream
 * stacks AthenaStack needs.
 *
 * @param scope - the fixture stack that owns the real constructs.
 * @returns the three structurally-typed upstream stand-ins cast to the concrete
 *   stack types AthenaStackProps requires.
 */
function buildUpstreamFakes(scope: cdk.Stack): {
  dataLakeStack: DataLakeStack;
  glueCatalogStack: GlueCatalogStack;
  dynamodbStack: DynamoDBStack;
  vpc: ec2.Vpc;
} {
  // Real S3 buckets — AthenaStack calls grantReadWrite/grantRead + reads bucketName.
  // blockPublicAccess: BLOCK_ALL satisfies the CSO deploy-guard's shared scan (these
  // are test fixtures, but the guard rglobs sibling worktrees; harmless hardening).
  const athenaResultsBucket = new s3.Bucket(scope, 'FakeAthenaResultsBucket', {
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  });
  const artifactsBucket = new s3.Bucket(scope, 'FakeArtifactsBucket', {
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  });

  // Real Glue database — AthenaStack reads `.ref`.
  const dynamodbDatabase = new glue.CfnDatabase(scope, 'FakeDynamoDBDatabase', {
    catalogId: TEST_ENV.account,
    databaseInput: { name: 'fake_dynamodb_db' },
  });

  // Real DynamoDB tables — AthenaStack calls grantReadData on each.
  const tables: Record<string, dynamodb.Table> = {};
  for (const field of DDB_TABLE_FIELDS) {
    tables[field] = new dynamodb.Table(scope, `Fake_${field}`, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });
  }

  // Real VPC — the prop is required even though the constructor does not read it.
  const vpc = new ec2.Vpc(scope, 'FakeVpc', { maxAzs: 2 });

  // Structural stand-ins carrying only the fields AthenaStack reads. Cast through
  // `unknown` because we deliberately supply a subset of the concrete stack shape.
  const dataLakeStack = {
    athenaResultsBucket,
    artifactsBucket,
    tableBucketArn: `arn:aws:s3tables:${TEST_ENV.region}:${TEST_ENV.account}:bucket/fake-table-bucket`,
    lfGrantSingletonRoleArn: `arn:aws:iam::${TEST_ENV.account}:role/fake-lf-singleton-role`,
  } as unknown as DataLakeStack;

  const glueCatalogStack = {
    dynamodbDatabase,
  } as unknown as GlueCatalogStack;

  const dynamodbStack = {
    ...tables,
  } as unknown as DynamoDBStack;

  return { dataLakeStack, glueCatalogStack, dynamodbStack, vpc };
}

/** Fixed connector config used when a test only needs "Snowflake on" via `withSnowflake`. */
const DEFAULT_SNOWFLAKE_CONFIG: SnowflakeConfig = {
  account: 'acme-acct',
  warehouse: 'ACME_WH',
  databases: ['FHIR_DB'],
  role: 'ATHENA_CONNECTOR_ROLE',
  secretName: 'semantic-layer/snowflake/connector',
};

/**
 * Synthesize {@link AthenaStack} in a hermetic throwaway app for assertions.
 *
 * @param opts.withSnowflake - when `true`, pass {@link DEFAULT_SNOWFLAKE_CONFIG} so
 *   Task 3's federated `snowflake_catalog` connector block is active; when
 *   `false`, omit `snowflakeConfig` entirely so no Snowflake resources appear.
 *   Ignored when `opts.snowflakeConfig` is supplied (the `'snowflakeConfig' in opts`
 *   key presence takes precedence, so an explicit `undefined` can be threaded through).
 * @param opts.snowflakeConfig - when the key is present, this exact value (including
 *   `undefined`) is passed as the AthenaStack `snowflakeConfig` prop. This lets a
 *   caller feed the real `resolveSnowflakeConfig(...)` output straight through — the
 *   same value `app.ts` computes and hands to `new AthenaStack(...)` — so the test
 *   exercises the resolve→prop data path rather than a hand-built config.
 * @returns the synthesized `AthenaStack` and its `Template` for assertions.
 */
export function buildAthenaTestStack(opts: {
  withSnowflake?: boolean;
  snowflakeConfig?: SnowflakeConfig;
}): {
  stack: cdk.Stack;
  template: Template;
} {
  // `aws:cdk:bundling-stacks: []` disables asset bundling for all stacks, matching
  // the other suites. AthenaStack has no assets today, but this keeps the harness
  // robust if a future change adds one.
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });

  // Fixture stack owns the real upstream constructs AthenaStack reads from.
  const fixtureStack = new cdk.Stack(app, 'AthenaFixtureUpstreamStack', {
    env: TEST_ENV,
  });
  const { dataLakeStack, glueCatalogStack, dynamodbStack, vpc } = buildUpstreamFakes(fixtureStack);

  // When the caller supplies `snowflakeConfig` explicitly (key present, even if
  // `undefined`), thread it through verbatim; otherwise fall back to the boolean
  // `withSnowflake` toggle → the fixed default config.
  const snowflakeConfig: SnowflakeConfig | undefined =
    'snowflakeConfig' in opts
      ? opts.snowflakeConfig
      : opts.withSnowflake
        ? DEFAULT_SNOWFLAKE_CONFIG
        : undefined;

  const stack = new AthenaStack(app, 'AthenaTestStack', {
    env: TEST_ENV,
    projectName: 'semantic-layer',
    dataLakeStack,
    glueCatalogStack,
    dynamodbStack,
    vpc,
    snowflakeConfig,
  });

  return { stack, template: Template.fromStack(stack) };
}
