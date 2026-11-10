import { Match } from 'aws-cdk-lib/assertions';
import { SnowflakeConfig } from '../lib/stacks/backend/athena-stack';
import { buildAthenaTestStack } from './helpers/athena-test-stack';

// Compile-time contract test: AthenaStack must re-export the shared SnowflakeConfig
// type (declared in bin/snowflake-config.ts) so a caller can type its props without
// reaching across into bin/. If the re-export is missing this file fails to compile.
test('AthenaStack re-exports SnowflakeConfig type usable for props', () => {
  const cfg: SnowflakeConfig = {
    account: 'a',
    warehouse: 'w',
    databases: ['d'],
    role: 'r',
    secretName: 's',
  };
  expect(cfg.databases).toEqual(['d']);
});

// Task 3b: prove the AthenaStack synth harness works BEFORE Task 3 depends on it.
// This is the fixture self-test — it only asserts AthenaStack synthesizes and
// produces its always-present core resource (the Athena workgroup). It does NOT
// assert snowflake_catalog: that DataCatalog does not exist until Task 3 builds
// the SAR connector block (gated on the snowflakeConfig prop passed here).
describe('AthenaStack synth harness (buildAthenaTestStack)', () => {
  test('synthesizes without Snowflake and always creates the Athena workgroup', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: false });
    // The workgroup is the core resource AthenaStack unconditionally creates.
    template.hasResourceProperties('AWS::Athena::WorkGroup', {
      Name: 'semantic-layer-workgroup',
    });
    // Sanity: the operational DynamoDB catalog is present, the Snowflake one is not.
    template.hasResourceProperties('AWS::Athena::DataCatalog', {
      Name: 'dynamodb_catalog',
    });
  });
});

// Task 3: the flag-gated Snowflake SAR connector block.
describe('Snowflake SAR connector (Task 3)', () => {
  test('withSnowflake=true registers a per-database LAMBDA data catalog', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: true });
    // Catalog and connector are named from the database (FHIR_DB → fhir_db / fhir-db),
    // not a fixed `snowflake_catalog`, so a second database cannot collide with the first.
    template.hasResourceProperties('AWS::Athena::DataCatalog', {
      Name: 'snowflake_fhir_db',
      Type: 'LAMBDA',
      Parameters: {
        function: Match.stringLikeRegexp('function:semantic-layer-snowflake-fhir-db-connector$'),
      },
    });
  });

  test('withSnowflake=true deploys the AthenaSnowflakeConnector SAR application', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: true });
    template.hasResourceProperties(
      'AWS::Serverless::Application',
      Match.objectLike({
        Location: Match.objectLike({
          ApplicationId:
            'arn:aws:serverlessrepo:us-east-1:292517598671:applications/AthenaSnowflakeConnector',
        }),
      })
    );
  });

  test('withSnowflake=true grants secretsmanager:GetSecretValue scoped to the connector secret', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: true });

    // CDK now owns the secret resource, so the grant is `sfSecret.grantRead(role)`.
    // That renders the resource as a `Ref` to the in-stack CfnSecret rather than the
    // hand-built `…:secret:<name>-*` literal the earlier version asserted, and it
    // emits Action as a two-element array (GetSecretValue + DescribeSecret).
    // Assert against the secret's actual logical ID so the scope is provably the one
    // secret and nothing wider.
    const secretIds = Object.keys(template.findResources('AWS::SecretsManager::Secret'));
    expect(secretIds).toHaveLength(1);
    const secretLogicalId = secretIds[0];

    template.hasResourceProperties(
      'AWS::IAM::Policy',
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith(['secretsmanager:GetSecretValue']),
              Effect: 'Allow',
              Resource: { Ref: secretLogicalId },
            }),
          ]),
        }),
      })
    );

    // Belt-and-suspenders regression guard: scan EVERY IAM policy statement and fail
    // if any statement that grants GetSecretValue is scoped to the wildcard `*`.
    // stringLikeRegexp above proves a scoped grant EXISTS; this proves a wildcard
    // grant does NOT — together they catch a future edit that widens the resource.
    const policies = template.findResources('AWS::IAM::Policy');
    for (const [logicalId, policy] of Object.entries(policies)) {
      const statements: Array<{ Action?: unknown; Resource?: unknown }> =
        policy.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        // Action may be a single string or an array of actions.
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        if (actions.includes('secretsmanager:GetSecretValue')) {
          expect(stmt.Resource).not.toBe('*');
          // Resource may also be an array; ensure `*` is not among the entries.
          if (Array.isArray(stmt.Resource)) {
            expect(stmt.Resource).not.toContain('*');
          }
        }
      }
      // Reference logicalId so lint does not flag the destructured binding as unused.
      void logicalId;
    }
  });

  test('withSnowflake=true creates the credentials secret with a placeholder, not a credential', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: true });
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: 'semantic-layer/snowflake/connector',
    });

    // The whole point of the `.env` + sync-script design: the synthesized template
    // must contain a marker only. If someone "simplifies" this to
    // SecretValue.unsafePlainText(process.env.SNOWFLAKE_PRIVATE_KEY), the real key
    // lands in cdk.out/ and in the CloudFormation-stored template. Assert the stored
    // SecretString is exactly the placeholder document.
    const secrets = template.findResources('AWS::SecretsManager::Secret');
    const secretString = Object.values(secrets)[0].Properties?.SecretString;
    expect(typeof secretString).toBe('string');
    expect(JSON.parse(secretString as string)).toEqual({
      placeholder: 'unset — run `npm run sync:snowflake-secret` from cdk/',
    });
  });

  test('no credential-shaped value appears anywhere in the synthesized template', () => {
    // Belt-and-suspenders on the assertion above: scan the ENTIRE template JSON for the
    // credential key names the connector reads and for PEM/password markers. A future
    // change that threads a credential in through a different property (a Lambda env
    // var, a SAR parameter, an output) would be caught here even though the
    // SecretString itself still looks clean.
    const { template } = buildAthenaTestStack({ withSnowflake: true });
    const json = JSON.stringify(template.toJSON());
    for (const forbidden of [
      'BEGIN PRIVATE KEY',
      'BEGIN RSA PRIVATE KEY',
      'pem_private_key',
      'pem_private_key_passphrase',
      'SNOWFLAKE_PRIVATE_KEY',
      'SNOWFLAKE_PASSWORD',
    ]) {
      expect(json).not.toContain(forbidden);
    }
  });

  test('the connector is NOT attached to a VPC, and asks for no ENI permissions', () => {
    // Placement regression guard, measured live: in this stack's private subnets the
    // connector cold-started in 150–190s and `SELECT COUNT(*)` over 10,969 rows ran 894s
    // before being cancelled, failing against Snowflake's S3 result stage with
    // `NoHttpResponseException`. Out of the VPC the same query takes ~15s and a full scan
    // ~19s. Re-adding SubnetIds/SecurityGroupIds silently restores the slow path — and,
    // worse, cannot be undone by emptying the parameters again (the SAR template hides
    // both behind `Fn::If … AWS::NoValue`, and CloudFormation does not detach a Lambda
    // when `VpcConfig` is absent from an update).
    const { template } = buildAthenaTestStack({ withSnowflake: true });

    const apps = template.findResources('AWS::Serverless::Application');
    const sfApp = Object.values(apps).find((a) =>
      String(a.Properties?.Location?.ApplicationId ?? '').includes('AthenaSnowflakeConnector')
    );
    expect(sfApp).toBeDefined();
    expect(sfApp!.Properties.Parameters.SubnetIds).toBeUndefined();
    expect(sfApp!.Properties.Parameters.SecurityGroupIds).toBeUndefined();

    // No security group carries the connector's old description, i.e. the construct is
    // gone rather than orphaned into the template.
    const sgs = template.findResources('AWS::EC2::SecurityGroup');
    for (const sg of Object.values(sgs)) {
      expect(String(sg.Properties?.GroupDescription ?? '')).not.toContain(
        'Athena Snowflake connector'
      );
    }

    // Out of a VPC the function creates no ENIs, so the `ec2:*NetworkInterface` grants
    // (which can only be scoped to `*`) must not be requested.
    const policies = template.findResources('AWS::IAM::Policy');
    for (const policy of Object.values(policies)) {
      const statements: Array<{ Action?: unknown }> =
        policy.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        expect(actions).not.toContain('ec2:CreateNetworkInterface');
      }
    }
  });

  test('each configured database gets its own connector, catalog and spill prefix', () => {
    const { template } = buildAthenaTestStack({
      snowflakeConfig: {
        account: 'acme-acct',
        warehouse: 'ACME_WH',
        databases: ['FHIR_DB', 'CLINICAL_CSV'],
        role: 'ATHENA_CONNECTOR_ROLE',
        secretName: 'semantic-layer/snowflake/connector',
      },
    });

    // Two catalogs, named from the databases.
    const catalogNames = Object.values(template.findResources('AWS::Athena::DataCatalog')).map(
      (c) => c.Properties?.Name
    );
    expect(catalogNames).toContain('snowflake_fhir_db');
    expect(catalogNames).toContain('snowflake_clinical_csv');

    // Two SAR applications, one per database, with distinct function names and spill
    // prefixes but the SAME spill bucket, secret prefix and execution role — the shared
    // resources must not be duplicated per database.
    const sarApps = Object.values(template.findResources('AWS::Serverless::Application')).filter(
      (a) =>
        String(a.Properties?.Location?.ApplicationId ?? '').includes('AthenaSnowflakeConnector')
    );
    expect(sarApps).toHaveLength(2);
    const params = sarApps.map((a) => a.Properties.Parameters);
    expect(params.map((p) => p.LambdaFunctionName).sort()).toEqual([
      'semantic-layer-snowflake-clinical-csv-connector',
      'semantic-layer-snowflake-fhir-db-connector',
    ]);
    expect(params.map((p) => p.SpillPrefix).sort()).toEqual([
      'athena-spill/clinical_csv',
      'athena-spill/fhir_db',
    ]);
    // Each connection string pins its own database.
    expect(params.map((p) => p.DefaultConnectionString).sort()).toEqual([
      expect.stringContaining('db=CLINICAL_CSV'),
      expect.stringContaining('db=FHIR_DB'),
    ]);

    expect(Object.keys(template.findResources('AWS::SecretsManager::Secret'))).toHaveLength(1);
    // SpillBucket renders as a `{ Ref: … }` object, so compare structurally: both
    // connectors must point at the one shared bucket.
    expect(JSON.stringify(params[0].SpillBucket)).toBe(JSON.stringify(params[1].SpillBucket));
  });

  test('a connector name that violates the SAR pattern fails at synth', () => {
    // The SAR AllowedPattern is ^[a-z0-9-_]{1,64}$. A too-long database name would
    // otherwise fail deep inside the nested stack, where the error names the pattern but
    // not the database that produced it.
    expect(() =>
      buildAthenaTestStack({
        snowflakeConfig: {
          account: 'acme-acct',
          warehouse: 'ACME_WH',
          databases: ['A'.repeat(60)],
          role: 'ATHENA_CONNECTOR_ROLE',
          secretName: 'semantic-layer/snowflake/connector',
        },
      })
    ).toThrow(/does not match the SAR AllowedPattern/);
  });

  test('withSnowflake=false creates no credentials secret', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: false });
    expect(Object.keys(template.findResources('AWS::SecretsManager::Secret'))).toHaveLength(0);
  });

  test('withSnowflake=false creates no Snowflake data catalog', () => {
    const { template } = buildAthenaTestStack({ withSnowflake: false });
    const catalogNames = Object.values(template.findResources('AWS::Athena::DataCatalog')).map(
      (c) => String(c.Properties?.Name ?? '')
    );
    expect(catalogNames.filter((n) => n.startsWith('snowflake'))).toHaveLength(0);
  });
});
