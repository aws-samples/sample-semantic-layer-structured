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
 *  - cdk.json hardcodes both flags to `true` in its `context` block, and a CLI
 *    `--context` value does NOT override a key already present there — so every
 *    `synth` produced identical output and the flag could never be toggled.
 *  - `synth --all` also bundles Lambda/frontend source into the cloud assembly,
 *    so grepping the output dir matched application source (e.g. "semantic-rag"
 *    in a .jsx file), not CloudFormation resources.
 *  - It synthesized ~20 stacks 4x, which OOM'd the Node heap in CI.
 * In-process `Template.fromStack` on the single relevant stack avoids all three.
 */

import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Template } from 'aws-cdk-lib/assertions';
import { DynamoDBStack } from '../../lib/stacks/backend/dynamodb-stack';
import { BedrockKnowledgeBaseStack } from '../../lib/stacks/backend/bedrock-kb-stack';

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
