/**
 * Feature-flag matrix tests.
 *
 * These assert that the `enableSemanticRag` and `enableAcordSampleData` flags
 * actually gate the resources they claim to. The two flags are wired as stack
 * props (`BedrockKnowledgeBaseStack.enableSemanticRag`,
 * `DynamoDBStack.loadSyntheticData`), so we instantiate each stack directly with
 * both prop values and assert on the synthesized CloudFormation via
 * `Template.fromStack` — the same hermetic pattern as chat-gateway.test.ts.
 *
 * Why not shell out to `cdk synth --context <flag>=false`?
 *  - cdk.json hardcodes both flags to `true` in its `context` block. (An earlier
 *    version of this comment claimed a CLI `--context` cannot override a key
 *    already present in cdk.json; that is WRONG — verified 2026-08-14 by
 *    synthesizing the Athena stack with and without
 *    `-c enableSnowflakeConnector=false`, which produced 0 vs 9 Snowflake
 *    resources. CLI context does win. The remaining two reasons below are the
 *    real ones.)
 *  - `synth --all` also bundles Lambda/frontend source into the cloud assembly,
 *    so grepping the output dir matched application source (e.g. "semantic-rag"
 *    in a .jsx file), not CloudFormation resources.
 *  - It synthesized ~20 stacks 4x, which OOM'd the Node heap in CI.
 * In-process `Template.fromStack` on the single relevant stack avoids all three.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Template } from 'aws-cdk-lib/assertions';
import { DynamoDBStack } from '../../lib/stacks/backend/dynamodb-stack';
import { BedrockKnowledgeBaseStack } from '../../lib/stacks/backend/bedrock-kb-stack';
import { resolveSnowflakeConfig } from '../snowflake-config';
import { buildAthenaTestStack } from '../../__tests__/helpers/athena-test-stack';

const ENV = { account: '123456789012', region: 'us-east-1' };

/** Synthesize the DynamoDB stack with a given loadSyntheticData flag. */
function dynamoTemplate(loadSyntheticData: boolean): Template {
  const app = new cdk.App();
  const stack = new DynamoDBStack(app, `DynamoFlag${loadSyntheticData}`, {
    env: ENV,
    projectName: 'semantic-layer',
    loadSyntheticData,
  });
  return Template.fromStack(stack);
}

/** Synthesize the Bedrock KB stack with a given enableSemanticRag flag. */
function bedrockKbTemplate(enableSemanticRag: boolean): Template {
  const app = new cdk.App();
  // BedrockKnowledgeBaseStack needs an artifactsBucket; create one in a sibling
  // stack so the cross-stack reference resolves at synth time.
  const support = new cdk.Stack(app, `KbSupport${enableSemanticRag}`, { env: ENV });
  // Test-only fixture bucket (never deployed); BLOCK_ALL keeps the security
  // scanner happy and mirrors production bucket hygiene.
  const artifactsBucket = new s3.Bucket(support, 'ArtifactsBucket', {
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  });
  const stack = new BedrockKnowledgeBaseStack(app, `KbFlag${enableSemanticRag}`, {
    env: ENV,
    projectName: 'semantic-layer',
    artifactsBucket,
    enableSemanticRag,
  });
  return Template.fromStack(stack);
}

describe('enableSemanticRag flag', () => {
  test('=true provisions the semantic-rag Knowledge Base', () => {
    const tpl = bedrockKbTemplate(true);
    // Two KnowledgeBases: the always-on ontology-patterns KB + the semantic-rag KB.
    tpl.resourceCountIs('AWS::Bedrock::KnowledgeBase', 2);
  });

  test('=false omits the semantic-rag KB but keeps the ontology-patterns KB', () => {
    const tpl = bedrockKbTemplate(false);
    // Only the ontology-patterns KB remains; semantic-rag KB + data source dropped.
    tpl.resourceCountIs('AWS::Bedrock::KnowledgeBase', 1);
  });
});

describe('resolveSnowflakeConfig', () => {
  /**
   * Build a stub context getter over a plain object, mirroring the
   * `app.node.tryGetContext` contract (returns `undefined` for missing keys).
   */
  const ctx =
    (values: Record<string, unknown>) =>
    (key: string): unknown =>
      values[key];

  test('returns undefined when the flag is unset', () => {
    expect(resolveSnowflakeConfig(ctx({}))).toBeUndefined();
  });

  test('returns undefined when the flag is false', () => {
    expect(resolveSnowflakeConfig(ctx({ enableSnowflakeConnector: false }))).toBeUndefined();
    expect(resolveSnowflakeConfig(ctx({ enableSnowflakeConnector: 'false' }))).toBeUndefined();
  });

  test('returns config with generic defaults when flag on + account/warehouse supplied', () => {
    const cfg = resolveSnowflakeConfig(
      ctx({
        enableSnowflakeConnector: true,
        snowflakeAccount: 'acme-acct',
        snowflakeWarehouse: 'ACME_WH',
      })
    );
    expect(cfg).toEqual({
      account: 'acme-acct',
      warehouse: 'ACME_WH',
      databases: ['FHIR_DB'],
      role: 'ATHENA_CONNECTOR_ROLE',
      secretName: 'semantic-layer/snowflake/connector',
    });
  });

  test('honors the string form of the flag', () => {
    const cfg = resolveSnowflakeConfig(
      ctx({
        enableSnowflakeConnector: 'true',
        snowflakeAccount: 'acme-acct',
        snowflakeWarehouse: 'ACME_WH',
      })
    );
    expect(cfg?.account).toBe('acme-acct');
  });

  test('applies context overrides for database/role/secretName', () => {
    const cfg = resolveSnowflakeConfig(
      ctx({
        enableSnowflakeConnector: true,
        snowflakeAccount: 'acme-acct',
        snowflakeWarehouse: 'ACME_WH',
        snowflakeDatabase: 'CUSTOM_DB',
        snowflakeRole: 'CUSTOM_ROLE',
        snowflakeSecretName: 'custom/secret/name',
      })
    );
    expect(cfg).toEqual({
      account: 'acme-acct',
      warehouse: 'ACME_WH',
      databases: ['CUSTOM_DB'],
      role: 'CUSTOM_ROLE',
      secretName: 'custom/secret/name',
    });
  });

  test('snowflakeDatabases parses a comma-separated list, trimming and de-duplicating', () => {
    const cfg = resolveSnowflakeConfig(
      ctx({
        enableSnowflakeConnector: true,
        snowflakeAccount: 'acme-acct',
        snowflakeWarehouse: 'ACME_WH',
        // Trailing comma and stray spaces are what a hand-edited .env actually looks like.
        snowflakeDatabases: 'FHIR_DB, CLINICAL_CSV , FHIR_DB,',
      })
    );
    expect(cfg?.databases).toEqual(['FHIR_DB', 'CLINICAL_CSV']);
  });

  test('throws when both snowflakeDatabases and snowflakeDatabase are set', () => {
    // Silently preferring one would deploy a catalog set the deployer did not ask for.
    expect(() =>
      resolveSnowflakeConfig(
        ctx({
          enableSnowflakeConnector: true,
          snowflakeAccount: 'acme-acct',
          snowflakeWarehouse: 'ACME_WH',
          snowflakeDatabases: 'FHIR_DB',
          snowflakeDatabase: 'FHIR_DB',
        })
      )
    ).toThrow(/Both snowflakeDatabases and snowflakeDatabase are set/);
  });

  test('throws when snowflakeDatabases contains no usable names', () => {
    expect(() =>
      resolveSnowflakeConfig(
        ctx({
          enableSnowflakeConnector: true,
          snowflakeAccount: 'acme-acct',
          snowflakeWarehouse: 'ACME_WH',
          snowflakeDatabases: ' , ,',
        })
      )
    ).toThrow(/contains no database names/);
  });

  test('throws when flag on but account missing', () => {
    expect(() =>
      resolveSnowflakeConfig(ctx({ enableSnowflakeConnector: true, snowflakeWarehouse: 'ACME_WH' }))
    ).toThrow(/snowflakeAccount \/ snowflakeWarehouse/);
  });

  test('throws when flag on but warehouse missing', () => {
    expect(() =>
      resolveSnowflakeConfig(ctx({ enableSnowflakeConnector: true, snowflakeAccount: 'acme-acct' }))
    ).toThrow(/snowflakeAccount \/ snowflakeWarehouse/);
  });
});

describe('enableSnowflakeConnector wiring (app.ts → AthenaStack)', () => {
  /**
   * These tests prove the Task 4 wiring: `app.ts` computes `snowflakeConfig` via
   * `resolveSnowflakeConfig(...)` and threads it into `new AthenaStack(...)`.
   *
   * Why NOT synthesize the whole app here (import bin/app.ts or `cdk synth`)?
   *  - `app.ts` calls `app.synth()` at module top-level and gives no seam to inject
   *    `-c` context from jest — so we could not toggle the flag in-process.
   *  - Constructing the full app instantiates `BedrockKnowledgeBaseStack`, whose
   *    `s3deploy.BucketDeployment` stages `data/ontology-docs` at construct time.
   *    That directory is git-ignored (see `data/.gitignore`) and absent in fresh
   *    checkouts/CI, so any full-app synth throws `CannotFindAsset` — the exact
   *    pre-existing failure in the `enableSemanticRag` suite above. We deliberately
   *    do NOT couple this wiring test to that unrelated missing fixture.
   *
   * Instead we reproduce app.ts's data path exactly: call the SAME
   * `resolveSnowflakeConfig` app.ts calls, over the SAME context keys, and feed its
   * output into `AthenaStack`'s `snowflakeConfig` prop the SAME way app.ts does
   * (the harness passes `opts.snowflakeConfig` straight through to the prop). The
   * `snowflake_<database>` DataCatalog is the resource that only exists when that prop
   * is set, so its presence/absence proves the resolve→prop wiring end to end.
   */
  const ctx =
    (values: Record<string, unknown>) =>
    (key: string): unknown =>
      values[key];

  test('flag on (+account/warehouse) → snowflake_fhir_db DataCatalog appears', () => {
    // Mirror app.ts line 108: resolveSnowflakeConfig((k) => app.node.tryGetContext(k)).
    const snowflakeConfig = resolveSnowflakeConfig(
      ctx({
        enableSnowflakeConnector: true,
        snowflakeAccount: 'testacct',
        snowflakeWarehouse: 'testwh',
      })
    );
    // Sanity: Task 1 makes account/warehouse mandatory when the flag is on.
    expect(snowflakeConfig).toBeDefined();

    const { template } = buildAthenaTestStack({ snowflakeConfig });
    // Default database FHIR_DB → catalog snowflake_fhir_db.
    template.hasResourceProperties('AWS::Athena::DataCatalog', {
      Name: 'snowflake_fhir_db',
      Type: 'LAMBDA',
    });
  });

  test('flag absent → no Snowflake DataCatalog', () => {
    // NOT the deployed default any more — cdk.json ships the flag `true` as of
    // 2026-08-14. This covers the resolver's absent-context branch only, which is
    // what a checkout with no cdk.json context (or an explicit `-c …=false`) hits.
    const snowflakeConfig = resolveSnowflakeConfig(ctx({}));
    expect(snowflakeConfig).toBeUndefined();

    const { template } = buildAthenaTestStack({ snowflakeConfig });
    const catalogNames = Object.values(template.findResources('AWS::Athena::DataCatalog')).map(
      (c) => String(c.Properties?.Name ?? '')
    );
    expect(catalogNames.filter((n) => n.startsWith('snowflake'))).toHaveLength(0);
  });

  test('app.ts threads snowflakeConfig into the AthenaStack props', () => {
    // Source-level guard: the in-process tests above exercise resolveSnowflakeConfig
    // and the AthenaStack prop, but none would catch someone deleting the
    // `snowflakeConfig,` line from the `new AthenaStack(...)` call in app.ts. Assert
    // on the source text so that regression is caught cheaply.
    const src = fs.readFileSync(path.join(__dirname, '../app.ts'), 'utf8');
    const idx = src.indexOf('new AthenaStack(');
    expect(idx).toBeGreaterThan(-1);
    // Slice a fixed window from the AthenaStack instantiation so we only match
    // within its props object, not the whole file (the prop sits ~12 lines below).
    const props = src.slice(idx, idx + 800);
    expect(props).toMatch(/snowflakeConfig,/);
  });
});

describe('enableAcordSampleData / loadSyntheticData flag', () => {
  // Each DynamoDBDataLoader provisions a DataLoaderFunction Lambda; there are 12
  // datasets, so 12 loader Lambdas appear only when synthetic loading is enabled.
  const countLoaderFns = (tpl: Template): number =>
    Object.entries(tpl.toJSON().Resources as Record<string, { Type: string }>).filter(
      ([logicalId, r]) =>
        r.Type === 'AWS::Lambda::Function' && logicalId.includes('LoaderDataLoaderFunction')
    ).length;

  test('=true provisions a synthetic-data loader per dataset', () => {
    expect(countLoaderFns(dynamoTemplate(true))).toBe(12);
  });

  test('=false provisions no synthetic-data loaders', () => {
    expect(countLoaderFns(dynamoTemplate(false))).toBe(0);
  });
});

describe('enableHybrid flag defaults to on', () => {
  // The Hybrid card in the layer-type picker requires
  //   enableOntologyAgents && enableSemanticRag && enableHybrid
  // and REACT_APP_ENABLE_HYBRID is baked into the frontend bundle at BUILD time.
  // While enableHybrid defaulted to false, a normal `cdk deploy` produced a UI
  // offering only two of the three layer types, with nothing failing to signal it.
  // Nothing pinned the default, which is how that went unnoticed — hence these.
  const src = fs.readFileSync(path.join(__dirname, '../app.ts'), 'utf8');
  const cdkJson = JSON.parse(fs.readFileSync(path.join(__dirname, '../../cdk.json'), 'utf8')) as {
    context: Record<string, unknown>;
  };

  test('app.ts resolves an absent context value to true, not false', () => {
    // `=== true || === 'true'` is the opt-IN shape and would default the flag off.
    // `!== false && !== 'false'` is the opt-OUT shape required here.
    const decl = src.slice(src.indexOf("tryGetContext('enableHybrid')"));
    expect(decl.slice(0, 200)).toMatch(
      /const enableHybrid = _hybridCtx !== false && _hybridCtx !== 'false';/
    );
  });

  test('cdk.json declares it true, like the other two layer-type flags', () => {
    // Declared in both places so the default is visible where the sibling flags
    // are, rather than implicit in app.ts alone.
    expect(cdkJson.context.enableHybrid).toBe(true);
    expect(cdkJson.context.enableOntologyAgents).toBe(true);
    expect(cdkJson.context.enableSemanticRag).toBe(true);
  });

  test('the frontend stack receives the flag from app.ts', () => {
    // Without the prop the card is hidden no matter what the context says.
    const idx = src.indexOf('new FrontendStack(');
    expect(idx).toBeGreaterThan(-1);
    expect(src.slice(idx, idx + 2000)).toMatch(/^\s*enableHybrid,$/m);
  });
});

describe('enableSnowflakeConnector is enabled in checked-in config', () => {
  // WHY THIS IS PINNED (2026-08-14): the flag used to be `false` in cdk.json and had
  // to be re-supplied as `-c enableSnowflakeConnector=true` on EVERY deploy. Omitting
  // it does not merely skip the connector — it DESTROYS the deployed one (spill
  // bucket, secret, connector role, AWS::Serverless::Application,
  // AWS::Athena::DataCatalog) and strips the Snowflake grants out of every agent
  // role's policy. That destructive diff hides among routine asset-hash churn, so
  // nothing loudly signals the mistake. This test fails if the default flips back.
  const cdkJson = JSON.parse(fs.readFileSync(path.join(__dirname, '../../cdk.json'), 'utf8')) as {
    context: Record<string, unknown>;
  };

  test('cdk.json ships the connector enabled', () => {
    expect(cdkJson.context.enableSnowflakeConnector).toBe(true);
  });

  test('the cdk.json value alone is enough to resolve a config', () => {
    // resolveSnowflakeConfig accepts the JSON boolean form, not just the CLI string
    // 'true' — if it only accepted the string, the cdk.json default would be inert.
    const cfg = resolveSnowflakeConfig((key: string) =>
      key === 'enableSnowflakeConnector'
        ? cdkJson.context.enableSnowflakeConnector
        : { snowflakeAccount: 'acme-acct', snowflakeWarehouse: 'ACME_WH' }[key]
    );
    expect(cfg).toBeDefined();
    expect(cfg?.account).toBe('acme-acct');
  });

  test('a CLI --context value can still turn it off', () => {
    // The escape hatch documented in app.ts and .env.example. CLI context wins over
    // cdk.json (verified by synthesizing the Athena stack both ways: 0 vs 9
    // Snowflake resources), so this must resolve to undefined.
    expect(
      resolveSnowflakeConfig((key: string) =>
        key === 'enableSnowflakeConnector' ? 'false' : undefined
      )
    ).toBeUndefined();
  });
});
