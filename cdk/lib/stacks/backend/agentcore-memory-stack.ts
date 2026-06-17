import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

export interface AgentCoreMemoryStackProps extends cdk.StackProps {
  projectName: string;
  /** Short-term raw-event retention in days. Long-term semantic-strategy
   *  records are not bound by this — they live until the admin deletes them
   *  through the lessons API. Default: 90. */
  eventExpiryDays?: number;
}

/**
 * Manages the single Bedrock AgentCore Memory resource that backs the
 * lessons-learned feature (item #2).
 *
 * The control-plane API isn't covered by L1/L2 CDK constructs, so we use a
 * custom resource that calls ``CreateMemory`` with one ``SemanticStrategy``
 * (namespace ``/lessons/{actorId}/{sessionId}/`` — callers encode the
 * actor as ``<semanticLayerId>/<semanticLayerVersion>/<userId>`` so the
 * resolved namespace is
 * ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/``,
 * scoping lessons per layer, per layer-version, per user, per session). The
 * strategy template is fixed; only the application-side actor encoding carries
 * the extra segments, so changing it needs no memory-resource replacement.
 * Both query-agent runtimes write to this memory via the guarded
 * ``persist_turn_pair`` path (PII-redacted by Bedrock Guardrails); the Lambda
 * REST API reads/deletes for the admin UI.
 */
export class AgentCoreMemoryStack extends cdk.Stack {
  public readonly memoryId: string;

  constructor(scope: Construct, id: string, props: AgentCoreMemoryStackProps) {
    super(scope, id, props);

    const expiryDays = props.eventExpiryDays ?? 90;
    // Memory names must be alphanumeric/underscore; strip hyphens. The
    // ``_v2`` suffix forces resource replacement when the strategy template
    // itself changes — strategy templates are fixed at create time. The 5-part
    // namespace (layer/version/user/session) is encoded entirely in the
    // application-side actorId, so it needs NO bump; the template is unchanged.
    const memoryName = `${props.projectName.replace(/-/g, '_')}_lessons_v2`;

    // Custom-resource handler — bundled with bedrock-agentcore-starter-toolkit
    // so it can call create_memory / delete_memory.
    const handlerDir = path.join(__dirname, 'agentcore-memory-handler');
    const handler = new lambda.Function(this, 'MemoryHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.on_event',
      code: lambda.Code.fromAsset(handlerDir, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(
                  // nosemgrep: detect-child-process — CDK synth-time execSync on framework outputDir + static repo paths
                  `pip install --quiet --target "${outputDir}" -r "${path.join(handlerDir, 'requirements.txt')}"`,
                  { stdio: 'pipe' }
                );
                for (const f of fs.readdirSync(handlerDir)) {
                  if (f.endsWith('.py')) {
                    fs.copyFileSync(path.join(handlerDir, f), path.join(outputDir, f)); // nosemgrep: detect-non-literal-fs-filename,path-join-resolve-traversal — CDK synth-time paths
                  }
                }
                return true;
              } catch {
                return false;
              }
            },
          },
          command: [
            'bash',
            '-c',
            'pip install --quiet --target /asset-output -r requirements.txt && cp *.py /asset-output/',
          ],
        },
      }),
      timeout: cdk.Duration.minutes(10),
      description: 'Custom resource handler for AgentCore Memory (lessons-learned)',
    });

    handler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateMemory',
          'bedrock-agentcore:GetMemory',
          'bedrock-agentcore:ListMemories',
          'bedrock-agentcore:DeleteMemory',
          'bedrock-agentcore:UpdateMemory',
        ],
        resources: ['*'],
      })
    );

    const provider = new cr.Provider(this, 'MemoryProvider', {
      onEventHandler: handler,
    });

    const resource = new cdk.CustomResource(this, 'LessonsMemory', {
      serviceToken: provider.serviceToken,
      properties: {
        MemoryName: memoryName,
        EventExpiryDays: expiryDays,
      },
    });

    this.memoryId = resource.getAttString('MemoryId');

    new cdk.CfnOutput(this, 'LessonsMemoryId', {
      value: this.memoryId,
      description: 'AgentCore Memory id for lessons-learned',
      exportName: `${props.projectName}-lessons-memory-id`,
    });
  }
}
