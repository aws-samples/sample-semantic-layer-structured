/**
 * The ruleminer stage is wired into the doc pipeline between link and index.
 *
 * Asserting on the synthesized state machine catches an ordering regression at
 * build time: a miner that runs before `link` has no `linkedClass` IRIs to bind
 * against, so it would silently emit rules whose conditions point at nothing —
 * and a miner that runs after `index` would never reach the Knowledge Base.
 * Neither failure raises an error anywhere; both just produce useless rules.
 *
 * DocPipelineStack synthesizes hermetically (plain Python `Code.fromAsset`, no
 * Docker) so this builds the real stack rather than asserting on source text the
 * way governed-rules-flag.test.ts has to.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { aws_dynamodb as dynamodb, aws_s3 as s3 } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DocPipelineStack } from '../lib/stacks/backend/doc-pipeline-stack';

/**
 * Synthesize DocPipelineStack against minimal real upstream constructs.
 *
 * @returns the synthesized template plus its raw resource map.
 */
function synth(): Template {
  const app = new cdk.App();
  const fixture = new cdk.Stack(app, 'Fixture', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  // BLOCK_ALL even though this bucket only ever exists inside a test app: the
  // repo's security scan reads source text, not deploy targets, and an
  // unqualified `new s3.Bucket` here blocks every `cdk synth`/`cdk deploy`.
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
  });
  return Template.fromStack(stack);
}

/**
 * Recover the stage order from the synthesized state machine definition.
 *
 * The DefinitionString is an `Fn::Join` of literal fragments interleaved with
 * ARN tokens; the state names live in the literal fragments, so dropping the
 * tokens and concatenating gives a searchable (if not JSON-parseable) body.
 *
 * @param template - the synthesized DocPipelineStack template.
 * @returns the pipeline stage names in the order they are declared.
 */
function stageOrder(template: Template): string[] {
  const machines = Object.values(template.findResources('AWS::StepFunctions::StateMachine'));
  expect(machines).toHaveLength(1);
  const definition = (
    machines[0] as { Properties: { DefinitionString: { 'Fn::Join': [string, unknown[]] } } }
  ).Properties.DefinitionString;
  const body = definition['Fn::Join'][1].filter((p): p is string => typeof p === 'string').join('');
  return [...body.matchAll(/"(Chunk|Ner|Embed|Link|RuleMiner|Index)":\{/g)].map((m) => m[1]);
}

describe('doc-pipeline ruleminer stage', () => {
  const template = synth();

  test('creates a RuleMinerFn Lambda on the shared pipeline runtime', () => {
    const fns = template.findResources('AWS::Lambda::Function');
    const logicalId = Object.keys(fns).find((k) => k.startsWith('RuleMinerFn'));
    expect(logicalId).toBeDefined();
    expect(fns[logicalId as string].Properties).toMatchObject({
      Runtime: 'python3.12',
      Handler: 'handler.handler',
    });
  });

  test('runs RuleMiner after Link and before Index', () => {
    expect(stageOrder(template)).toEqual(['Chunk', 'Ner', 'Embed', 'Link', 'RuleMiner', 'Index']);
  });

  test('the state machine may invoke the miner', () => {
    // The definition referencing the function ARN is not enough — without an
    // explicit grantInvoke the execution fails at runtime with AccessDenied,
    // which surfaces to the admin as a stuck document, not as a permission
    // error.
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const grantsMiner = policies.some((p) => {
      const doc = JSON.stringify((p as { Properties: unknown }).Properties);
      return doc.includes('lambda:InvokeFunction') && doc.includes('RuleMinerFn');
    });
    expect(grantsMiner).toBe(true);
  });

  test('grants the miner bedrock:InvokeModel on the model its handler calls', () => {
    // Agreement test. The NER stage already drifted this way: its grant names
    // claude-3-5-sonnet while its handler defaults to claude-sonnet-5. A grant
    // that names the wrong model produces an AccessDeniedException per chunk,
    // which the fail-soft handler swallows into `ruleMinerError` — every
    // document then mines zero rules with no alarm anywhere.
    const handlerSrc = fs.readFileSync(
      path.join(__dirname, '../../lambda/doc-pipeline/ruleminer/handler.py'),
      'utf8'
    );
    const match = handlerSrc.match(/RULE_MINER_MODEL_ID', '([^']+)'/);
    expect(match).not.toBeNull();
    // The grant is written against the bare foundation-model id, so strip the
    // cross-region inference-profile prefix the handler calls through.
    const modelId = (match as RegExpMatchArray)[1].replace(/^global\./, '');

    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const grants = policies.filter((p) => {
      const doc = JSON.stringify((p as { Properties: unknown }).Properties);
      return doc.includes('bedrock:InvokeModel') && doc.includes(modelId);
    });
    expect(grants.length).toBeGreaterThan(0);
    // Both the profile and the underlying foundation model must be permitted,
    // or a `global.`-prefixed invoke is denied at the profile.
    const doc = JSON.stringify((grants[0] as { Properties: unknown }).Properties);
    expect(doc).toContain(`foundation-model/${modelId}`);
    expect(doc).toContain(`inference-profile/global.${modelId}`);
  });
});
