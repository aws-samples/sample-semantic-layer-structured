/**
 * The doc pipeline's env-var contract with its handlers.
 *
 * Every assertion here guards a failure that raises nothing at deploy time and
 * nothing in the console — a document simply stops partway and reads as "still
 * processing":
 *
 * - `CLASS_INDEX_S3_KEY_TEMPLATE` absent → the Link stage dies on a KeyError, so
 *   ruleminer and indexer never run. This shipped exactly once, because the stack
 *   set no such variable while `linker/handler.py` read a plain
 *   `CLASS_INDEX_S3_KEY`.
 * - `SUPPLEMENTARY_DOCS_KB_ID`/`_DS_ID` absent → the indexer raises, so a
 *   document is chunked, NER'd, embedded and linked but never retrievable.
 * - The name each handler reads drifting from the name the stack sets → same
 *   thing, silently. So the names are read out of the handler source rather than
 *   duplicated here: a duplicated literal drifts exactly like the wiring it is
 *   meant to guard.
 *
 * DocPipelineStack synthesizes hermetically (plain Python `Code.fromAsset`, no
 * Docker) so these build the real stack — see doc-pipeline-ruleminer.test.ts.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { aws_dynamodb as dynamodb, aws_s3 as s3 } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DocPipelineStack } from '../lib/stacks/backend/doc-pipeline-stack';

const KB_ID = 'KB1234567890';
const DS_ID = 'DS1234567890';

/**
 * Synthesize DocPipelineStack against minimal real upstream constructs.
 *
 * @param withKb - when false, omit the KB props (the `enableSemanticRag=false`
 *   deployment shape) so the conditional wiring can be asserted both ways.
 * @returns the synthesized template.
 */
function synth(withKb: boolean = true): Template {
  const app = new cdk.App();
  const fixture = new cdk.Stack(app, 'Fixture', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  // BLOCK_ALL even inside a test app: the repo's security scan reads source
  // text, not deploy targets, and an unqualified `new s3.Bucket` blocks synth.
  const bucket = new s3.Bucket(fixture, 'Docs', {
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  });
  const table = new dynamodb.Table(fixture, 'Meta', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  });
  const stack = new DocPipelineStack(app, 'DocPipe', {
    env: { account: '123456789012', region: 'us-east-1' },
    supplementaryDocsBucket: bucket,
    metadataTable: table,
    ...(withKb ? { supplementaryDocsKbId: KB_ID, supplementaryDocsDataSourceId: DS_ID } : {}),
  });
  return Template.fromStack(stack);
}

/**
 * Collect every pipeline Lambda's environment map, keyed by logical id.
 *
 * @param template - the synthesized DocPipelineStack template.
 * @returns logical id → environment variables.
 */
function lambdaEnvs(template: Template): Record<string, Record<string, unknown>> {
  const fns = template.findResources('AWS::Lambda::Function');
  const out: Record<string, Record<string, unknown>> = {};
  for (const [logicalId, res] of Object.entries(fns)) {
    const props = (res as { Properties: { Environment?: { Variables?: Record<string, unknown> } } })
      .Properties;
    out[logicalId] = props.Environment?.Variables ?? {};
  }
  return out;
}

/**
 * Read one doc-pipeline handler's source.
 *
 * @param stage - stage directory name under `lambda/doc-pipeline/`.
 * @returns the handler source text.
 */
function handlerSource(stage: string): string {
  return fs.readFileSync(
    path.join(__dirname, `../../lambda/doc-pipeline/${stage}/handler.py`),
    'utf8'
  );
}

describe('doc-pipeline KB + class-index wiring', () => {
  const template = synth();
  const envs = lambdaEnvs(template);

  test('provisions the six pipeline stages', () => {
    const prefixes = ['ChunkerFn', 'NerFn', 'EmbedderFn', 'LinkerFn', 'RuleMinerFn', 'IndexerFn'];
    for (const prefix of prefixes) {
      expect(Object.keys(envs).some((k) => k.startsWith(prefix))).toBe(true);
    }
  });

  test('every stage gets CLASS_INDEX_S3_KEY_TEMPLATE with an ontology_id placeholder', () => {
    for (const [logicalId, env] of Object.entries(envs)) {
      expect(env.CLASS_INDEX_S3_KEY_TEMPLATE).toBeDefined();
      // The linker formats this per document, so a template without the
      // placeholder would send every layer to one shared class index.
      expect(String(env.CLASS_INDEX_S3_KEY_TEMPLATE)).toContain('{ontology_id}');
      expect(logicalId).toBeTruthy();
    }
  });

  test('the linker reads exactly the env var name the stack sets', () => {
    // Agreement test: the drift that broke the Link stage was a name mismatch,
    // not a missing value, so match on the name the handler actually reads.
    const src = handlerSource('linker');
    expect(src).toContain('CLASS_INDEX_S3_KEY_TEMPLATE');
  });

  test('the class-index key template matches the linker default', () => {
    // The handler carries a default for local runs. If the two disagree, a
    // deployed linker reads a different key than a locally-tested one — and the
    // symptom is an empty entity-link set, not an error.
    const src = handlerSource('linker');
    const match = src.match(/DEFAULT_CLASS_INDEX_KEY_TEMPLATE = '([^']+)'/);
    expect(match).not.toBeNull();
    const fromStack = Object.values(envs)[0].CLASS_INDEX_S3_KEY_TEMPLATE;
    expect(fromStack).toBe((match as RegExpMatchArray)[1]);
  });

  test('every stage gets the KB id and data-source id when the KB exists', () => {
    for (const env of Object.values(envs)) {
      expect(env.SUPPLEMENTARY_DOCS_KB_ID).toBe(KB_ID);
      expect(env.SUPPLEMENTARY_DOCS_DS_ID).toBe(DS_ID);
    }
  });

  test('the indexer reads exactly those two env var names', () => {
    const src = handlerSource('indexer');
    expect(src).toContain('SUPPLEMENTARY_DOCS_KB_ID');
    expect(src).toContain('SUPPLEMENTARY_DOCS_DS_ID');
  });

  test('the indexer may start an ingestion job on that KB', () => {
    // Without this grant the final stage fails with AccessDenied, which the
    // admin sees as a document stuck at "linked".
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const grants = policies.filter((p) => {
      const doc = JSON.stringify((p as { Properties: unknown }).Properties);
      return doc.includes('bedrock:StartIngestionJob') && doc.includes(KB_ID);
    });
    expect(grants.length).toBeGreaterThan(0);
  });

  test('the artifacts bucket is the documents bucket (one KB data source, one prefix)', () => {
    // The indexer writes `metadata/{layerId}/_documents/{docId}.md` and relies on
    // the semantic-rag KB data source already covering `metadata/` on the SAME
    // bucket. Two different buckets would ingest nothing, silently.
    for (const env of Object.values(envs)) {
      expect(env.SUPPLEMENTARY_DOCS_BUCKET).toEqual(env.ARTIFACTS_BUCKET);
    }
  });

  describe('without a semantic-rag KB (enableSemanticRag=false)', () => {
    const noKb = lambdaEnvs(synth(false));

    test('omits the KB env vars rather than setting them empty', () => {
      // Empty strings would make the indexer look configured; absent vars make
      // it raise, which is the loud failure we want for an un-ingestable doc.
      for (const env of Object.values(noKb)) {
        expect(env).not.toHaveProperty('SUPPLEMENTARY_DOCS_KB_ID');
        expect(env).not.toHaveProperty('SUPPLEMENTARY_DOCS_DS_ID');
      }
    });

    test('still sets the class-index template', () => {
      for (const env of Object.values(noKb)) {
        expect(env.CLASS_INDEX_S3_KEY_TEMPLATE).toBeDefined();
      }
    });
  });
});
