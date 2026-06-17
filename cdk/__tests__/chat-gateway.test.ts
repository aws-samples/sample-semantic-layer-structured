import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { McpServerStack } from '../lib/stacks/backend/mcp-server-stack';

// The chat-gateway handler code is now packaged as a bundled Lambda asset (so a
// newer boto3 >=1.38 ships with it — the runtime's boto3 ~1.34 still marks
// create_gateway's protocolType REQUIRED, which fails param validation). The
// Python therefore lives on disk rather than inline in the template, so the
// logic assertions below read the source file directly.
const handlerSrc = fs.readFileSync(
  path.join(__dirname, '../lib/stacks/backend/chat-gateway-handler/index.py'),
  'utf8'
);

// Instantiate McpServerStack directly with fake props so the streaming
// chat-gateway block is active (both query runtime ARNs + Cognito info
// present). The ArmBuildConstruct only zips a local source dir at synth time
// (no docker, no network), so direct instantiation is hermetic.
//
// The chat gateway can NOT be modeled by CloudFormation's
// AWS::BedrockAgentCore::Gateway (ProtocolType is required + enum=['MCP'],
// but a runtime-target gateway must be non-MCP). It is therefore created via
// a Lambda-backed CloudFormation custom resource that calls the control-plane
// API. These tests assert that custom-resource shape — NOT a CfnGateway.
const app = new cdk.App();
const stack = new McpServerStack(app, 'ChatGatewayTestStack', {
  env: { account: '123456789012', region: 'us-east-1' },
  projectName: 'semantic-layer',
  queryRuntimeArn: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/ontology-query-xyz',
  metadataQueryRuntimeArn:
    'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/metadata-query-abc',
  suggestionsRuntimeArn: 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/suggestions-def',
  guardrailId: 'gr-123',
  guardrailVersion: '1',
  userPoolId: 'us-east-1_FakePool',
  userPoolClientId: 'fakeclientid123',
});
const template = Template.fromStack(stack);

describe('McpServerStack streaming chat gateway (Lambda custom resource)', () => {
  test('does NOT model the chat gateway as a second AWS::BedrockAgentCore::Gateway', () => {
    // Only the MCP gateway is a native CfnGateway (AWS_IAM, protocolType MCP).
    // The chat gateway lives entirely in the custom resource, so there must be
    // exactly one CfnGateway and none with CUSTOM_JWT.
    const gateways = template.findResources('AWS::BedrockAgentCore::Gateway');
    expect(Object.keys(gateways)).toHaveLength(1);
    for (const g of Object.values(gateways)) {
      expect((g as any).Properties.AuthorizerType).not.toBe('CUSTOM_JWT');
    }
  });

  test('creates a CloudFormation custom resource for the chat gateway', () => {
    // The CDK Provider framework renders the resource as an AWS::CloudFormation::
    // CustomResource (or Custom::*) backed by the provider service token.
    const customResources = template.findResources('AWS::CloudFormation::CustomResource');
    const ids = Object.keys(customResources);
    expect(ids.length).toBeGreaterThanOrEqual(1);

    // Exactly one of them carries the chat-gateway inputs as resource
    // properties (the custom resource backing the chat gateway).
    const chatCr = Object.values(customResources).find(
      (r: any) => r.Properties?.GatewayName === 'semantic-layer-agent-gateway'
    ) as any;
    expect(chatCr).toBeDefined();
    expect(chatCr.Properties.AllowedClientId).toBe('fakeclientid123');
    expect(chatCr.Properties.DiscoveryUrl).toMatch(
      /https:\/\/cognito-idp\.us-east-1\.amazonaws\.com\/us-east-1_FakePool\//
    );
    expect(chatCr.Properties.MetadataQueryRuntimeArn).toContain('metadata-query-abc');
    expect(chatCr.Properties.OntologyQueryRuntimeArn).toContain('ontology-query-xyz');
  });

  test('the two custom-resource Lambdas are bundled assets with the control-plane handlers', () => {
    // Both handlers are now packaged as bundled S3 assets (NOT fromInline),
    // each pointing at one of the two entry points in index.py. There must be
    // no inline (ZipFile) handler carrying the control-plane code anymore.
    const fns = template.findResources('AWS::Lambda::Function');
    const handlers = Object.values(fns)
      .map((f: any) => f.Properties?.Handler)
      .filter((h: any): h is string => typeof h === 'string');
    expect(handlers).toContain('index.on_event_handler');
    expect(handlers).toContain('index.is_complete_handler');

    // No Lambda should still carry the control-plane code inline.
    const inlineControlPlaneFns = Object.values(fns).filter((f: any) => {
      const code = f.Properties?.Code?.ZipFile;
      return typeof code === 'string' && code.includes('bedrock-agentcore-control');
    });
    expect(inlineControlPlaneFns).toHaveLength(0);
  });

  test('create_gateway is called WITHOUT a protocolType keyword argument', () => {
    // The bundled handler source must call create_gateway (non-MCP gateway) and
    // must NOT pass a protocolType= keyword — that regression was the deploy
    // failure (and only works because boto3>=1.38 makes protocolType optional).
    // The word may still appear in an explanatory comment, so match the kwarg
    // form only.
    expect(handlerSrc).toContain('create_gateway(');
    expect(handlerSrc).not.toMatch(/protocolType\s*=/);
  });

  test('create_gateway uses a UNIQUE name per attempt (avoids the async-delete ConflictException race)', () => {
    // FIX: delete_gateway is async (lingers in DELETING), so recreating with
    // the SAME name raised ConflictException. Each create_gateway call must now
    // use a unique name = base (from ResourceProperties GatewayName) + a short
    // uuid token, so it can never collide with a DELETING/FAILED same-base gw.
    expect(handlerSrc).toContain('import uuid');
    expect(handlerSrc).toMatch(/uuid\.uuid4\(\)\.hex\[:8\]/);
    // The create call must pass the computed unique name, NOT props['GatewayName']
    // directly. _unique_gateway_name builds it from the base name.
    expect(handlerSrc).toContain('def _unique_gateway_name(');
    expect(handlerSrc).toMatch(/name\s*=\s*gateway_name/);
    expect(handlerSrc).not.toMatch(/name\s*=\s*props\['GatewayName'\]/);
  });

  test('onEvent reaps FAILED orphan gateways of the same base name before creating', () => {
    // FIX (defensive hygiene): a gateway that went FAILED in a prior rolled-back
    // deploy lingers. onEvent best-effort list_gateways, matches name prefixes
    // against the base name, and deletes only FAILED ones — never READY ones.
    expect(handlerSrc).toContain('def _reap_failed_orphans(');
    expect(handlerSrc).toContain('list_gateways(');
    // Only FAILED gateways are reaped (READY left alone) and matched by base-name prefix.
    expect(handlerSrc).toMatch(/status\s*==\s*'FAILED'/);
    expect(handlerSrc).toContain('startswith(base_name)');
    // Reaping is invoked from the create path.
    expect(handlerSrc).toContain("_reap_failed_orphans(props['GatewayName'])");
  });

  test('the chat gateway custom-resource Lambdas can call ListGateways (orphan reaping)', () => {
    // _reap_failed_orphans calls list_gateways; without this grant it would
    // always AccessDeny + no-op, so the cleanup needs the permission to work.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['bedrock-agentcore:ListGateways']),
          }),
        ]),
      },
    });
  });

  test('the bundled handlers pin boto3>=1.38 so protocolType is optional', () => {
    const reqs = fs.readFileSync(
      path.join(__dirname, '../lib/stacks/backend/chat-gateway-handler/requirements.txt'),
      'utf8'
    );
    expect(reqs).toMatch(/boto3>=1\.38/);
  });

  test('target creation + readiness polling live in the isComplete handler, not onEvent', () => {
    // CRITICAL FIX: create_gateway_target raises ConflictException while the
    // gateway is still CREATING, so target creation must be gated on the
    // gateway reaching READY — which happens in the isComplete handler, NOT
    // eagerly in onEvent. The isComplete handler must create targets AND poll
    // their readiness via get_gateway_target; the onEvent handler (which calls
    // create_gateway) must NOT create targets.
    expect(handlerSrc).toContain('def is_complete_handler(');
    expect(handlerSrc).toContain('def on_event_handler(');
    expect(handlerSrc).toContain('create_gateway_target');
    expect(handlerSrc).toContain('get_gateway_target');

    // The onEvent handler must NOT call create_gateway_target. Slice the source
    // at the isComplete section boundary so the assertion only inspects the
    // onEvent-side code (TARGET_SPECS et al. live in the isComplete section).
    const isCompleteStart = handlerSrc.indexOf('TARGET_SPECS');
    expect(isCompleteStart).toBeGreaterThan(0);
    const onEventSection = handlerSrc.slice(0, isCompleteStart);
    expect(onEventSection).toContain('def _create_gateway(');
    expect(onEventSection).not.toMatch(/client\.create_gateway_target\(/);
  });

  test('the chat gateway custom-resource Lambdas can call CreateGatewayTarget + GetGatewayTarget', () => {
    // Target creation + readiness polling now run in isComplete, so the
    // control-plane policy must grant both the create and the get-target ops.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'bedrock-agentcore:CreateGatewayTarget',
              'bedrock-agentcore:GetGatewayTarget',
            ]),
          }),
        ]),
      },
    });
  });

  test('a Provider framework Lambda exists', () => {
    const fns = template.findResources('AWS::Lambda::Function');
    const frameworkFns = Object.values(fns).filter((f: any) =>
      f.Properties?.Handler?.startsWith?.('framework.onEvent')
    );
    expect(frameworkFns.length).toBeGreaterThanOrEqual(1);
  });

  test('the chat gateway custom-resource Lambdas can call CreateGateway + PassRole', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['bedrock-agentcore:CreateGateway']),
          }),
        ]),
      },
    });
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'iam:PassRole',
          }),
        ]),
      },
    });
  });

  test('chatGatewayRole can InvokeAgentRuntime on both runtimes', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'bedrock-agentcore:InvokeAgentRuntime',
            Resource: Match.arrayWith([
              'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/metadata-query-abc',
              'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/ontology-query-xyz',
            ]),
          }),
        ]),
      },
    });
  });

  test('ChatGatewayUrl output is sourced from the custom resource', () => {
    template.hasOutput('ChatGatewayUrl', {
      Description: Match.stringLikeRegexp('Streaming chat gateway endpoint'),
    });
  });
});
