import * as cdk from 'aws-cdk-lib';
import { runtimeFingerprint } from '../lib/stacks/backend/runtime-fingerprint';

// A minimal stack instance is required as scope; resolver walks up to
// Stack.of(scope) but never inspects construct identity for plain string
// values, so a single shared stack is sufficient for these tests.
const stack = new cdk.Stack(new cdk.App(), 'TestStack');

const IMAGE_TAG = 'sha256abcdef';

const baseEnv: Record<string, string> = {
  AWS_REGION: 'us-east-1',
  PROJECT_NAME: 'semantic-layer',
  KNOWLEDGE_BASE_ID: 'kb-abc',
  AGENT_OBSERVABILITY_ENABLED: 'true',
  OTEL_PYTHON_DISTRO: 'aws_distro',
  OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
  OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
  OTEL_RESOURCE_ATTRIBUTES: 'service.name=svc.DEFAULT,aws.log.group.names=/aws/lg',
  OTEL_EXPORTER_OTLP_LOGS_HEADERS: 'x-aws-log-group=/aws/lg,x-aws-log-stream=runtime-logs',
  OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
  OTEL_TRACES_EXPORTER: 'otlp',
  OTEL_METRICS_EXPORTER: 'none',
  OTEL_LOGS_EXPORTER: 'otlp',
  OTEL_TRACES_SAMPLER: 'always_on',
  OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
};

describe('runtimeFingerprint', () => {
  test('stability — same env + same image tag yields the same digest', () => {
    expect(runtimeFingerprint(stack, baseEnv, IMAGE_TAG)).toBe(
      runtimeFingerprint(stack, { ...baseEnv }, IMAGE_TAG)
    );
  });

  test('key-order independence — reordering env keys does not change the digest', () => {
    const reordered = Object.fromEntries(Object.entries(baseEnv).reverse());
    expect(runtimeFingerprint(stack, reordered, IMAGE_TAG)).toBe(
      runtimeFingerprint(stack, baseEnv, IMAGE_TAG)
    );
  });

  test('OTEL sensitivity — flipping any OTEL_ value changes the digest', () => {
    const flipped = {
      ...baseEnv,
      OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_latest_experimental',
    };
    expect(runtimeFingerprint(stack, flipped, IMAGE_TAG)).not.toBe(
      runtimeFingerprint(stack, baseEnv, IMAGE_TAG)
    );
  });

  test('non-OTEL sensitivity — ANY env change flips the digest (the whole fix)', () => {
    // This is the inverse of the old OTEL-only behavior: a non-OTEL env change
    // still re-pushes the runtime env and strips the patch, so it MUST re-fire
    // the handler.
    const withExtra = {
      ...baseEnv,
      KNOWLEDGE_BASE_ID: 'kb-xyz',
    };
    expect(runtimeFingerprint(stack, withExtra, IMAGE_TAG)).not.toBe(
      runtimeFingerprint(stack, baseEnv, IMAGE_TAG)
    );
  });

  test('image-tag sensitivity — a code-only redeploy (tag change) flips the digest', () => {
    // The most common drift trigger: env is byte-identical, only the image
    // changes, but the runtime resource still updates and resets its env.
    expect(runtimeFingerprint(stack, baseEnv, 'sha256NEWTAG')).not.toBe(
      runtimeFingerprint(stack, baseEnv, IMAGE_TAG)
    );
  });

  test('token stability — unresolved Token in an env value is normalized to a stable Ref', () => {
    const tokenStack = new cdk.Stack(new cdk.App(), 'TokenStack');
    const lg = new cdk.aws_logs.LogGroup(tokenStack, 'LG');
    const envWithToken = {
      ...baseEnv,
      OTEL_RESOURCE_ATTRIBUTES: `service.name=svc.DEFAULT,aws.log.group.names=${lg.logGroupName}`,
    };
    // Two separate calls within the same synth must agree.
    const a = runtimeFingerprint(tokenStack, envWithToken, IMAGE_TAG);
    const b = runtimeFingerprint(tokenStack, { ...envWithToken }, IMAGE_TAG);
    expect(a).toBe(b);
    // And it must differ from the same baseEnv without the token.
    expect(a).not.toBe(runtimeFingerprint(tokenStack, baseEnv, IMAGE_TAG));
  });
});
