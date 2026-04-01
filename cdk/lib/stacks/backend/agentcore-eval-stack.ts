import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import { AgentCoreStack } from './agentcore-stack';

export interface AgentCoreEvalStackProps extends cdk.StackProps {
  projectName: string;
  agentCoreStack: AgentCoreStack;
  /** Global default sampling rate (0–100). Default: 100 */
  samplingRate?: number;
  /** Per-runtime overrides — takes precedence over samplingRate */
  samplingRates?: {
    ontology?: number;
    ontologyQuery?: number;
    metadata?: number;
    metadataQuery?: number;
    querySuggestions?: number;
  };
}

export class AgentCoreEvalStack extends cdk.Stack {
  private readonly evalConfigServiceToken: string;

  constructor(scope: Construct, id: string, props: AgentCoreEvalStackProps) {
    super(scope, id, props);

    const defaultRate = props.samplingRate ?? 100;
    const rates = props.samplingRates ?? {};
    const pName = props.projectName;
    // Config names must match [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens allowed
    const safeName = pName.replace(/-/g, '_');

    // Shared IAM execution role for all eval configs
    const evalExecutionRole = new iam.Role(this, 'EvalExecutionRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore online evaluation configs',
    });

    // CWL list/describe actions (e.g. DescribeLogGroups) require Resource: "*"
    // The eval API validates permissions via IAM simulation against *, so narrow
    // resource ARNs cause the ValidationException even when patterns would match.
    // This role is only assumable by bedrock-agentcore.amazonaws.com, so * is safe.
    evalExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
        'logs:DescribeLogGroups',
        'logs:DescribeLogStreams',
        'logs:GetLogEvents',
        'logs:FilterLogEvents',
        'logs:StartQuery',
        'logs:StopQuery',
        'logs:GetQueryResults',
        'logs:DescribeIndexPolicies',
        'logs:PutIndexPolicy',
      ],
      resources: ['*'],
    }));

    evalExecutionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:${this.region}::foundation-model/anthropic.*`,
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.*`,
      ],
    }));

    // Python Lambda custom resource handler — uses bundled boto3 for bedrock-agentcore-control
    // (Lambda Python 3.12 runtime ships an older boto3 that lacks create_online_evaluation_config;
    //  bundling installs a newer version alongside the handler code.)
    const handlerDir = path.join(__dirname, 'agentcore-eval-handler');
    const evalConfigHandler = new lambda.Function(this, 'EvalConfigHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.on_event',
      code: lambda.Code.fromAsset(handlerDir, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          // Try local bundling first (fast, no Docker required)
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(
                  `pip install --quiet --target "${outputDir}" -r "${path.join(handlerDir, 'requirements.txt')}"`,
                  { stdio: 'pipe' },
                );
                // Copy handler source files
                for (const f of fs.readdirSync(handlerDir)) {
                  if (f.endsWith('.py')) {
                    fs.copyFileSync(path.join(handlerDir, f), path.join(outputDir, f)); // nosemgrep: detect-non-literal-fs-filename,path-join-resolve-traversal — CDK synth-time paths, not user input
                  }
                }
                return true;
              } catch {
                return false;
              }
            },
          },
          // Fallback: Docker bundling
          command: [
            'bash', '-c',
            'pip install --quiet --target /asset-output -r requirements.txt && cp *.py /asset-output/',
          ],
        },
      }),
      timeout: cdk.Duration.minutes(5),
      description: 'Custom resource handler for AgentCore online evaluation configs',
    });

    evalConfigHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock-agentcore:CreateOnlineEvaluationConfig',
        'bedrock-agentcore:DeleteOnlineEvaluationConfig',
        'bedrock-agentcore:ListOnlineEvaluationConfigs',
      ],
      resources: ['*'],
    }));

    // The bedrock-agentcore service uses the Lambda caller's credentials to validate and configure
    // the aws/spans log group index (for OTEL spans). These permissions are needed on the Lambda's role.
    evalConfigHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:DescribeIndexPolicies', 'logs:PutIndexPolicy'],
      resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:aws/spans:*`],
    }));

    // PassRole so Lambda can hand evalExecutionRole to the evaluation service
    evalConfigHandler.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iam:PassRole'],
      resources: [evalExecutionRole.roleArn],
    }));

    const evalConfigProvider = new cr.Provider(this, 'EvalConfigProvider', {
      onEventHandler: evalConfigHandler,
    });

    this.evalConfigServiceToken = evalConfigProvider.serviceToken;

    // OTLP logs land in {runtimeId}-DEFAULT (redirected by CloudResourceIdHandler).
    // The SDK's query_runtime_logs_by_traces hardcodes that pattern:
    //   f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"
    // Derive runtimeId from ARN at deploy time: arn:.../agent-runtime/<runtimeId>
    const runtimeLogGroup = (runtimeArn: string): string =>
      cdk.Fn.join('', [
        '/aws/bedrock-agentcore/runtimes/',
        cdk.Fn.select(1, cdk.Fn.split('/', runtimeArn)),
        '-DEFAULT',
      ]);

    // Always-present runtimes
    this.createEvalConfig('MetadataOnlineEval', `${safeName}_metadata_eval`,
      runtimeLogGroup(props.agentCoreStack.metadataRuntimeArn), `${safeName}_metadata.DEFAULT`,
      rates.metadata ?? defaultRate, evalExecutionRole.roleArn);

    this.createEvalConfig('MetadataQueryOnlineEval', `${safeName}_metadata_query_eval`,
      runtimeLogGroup(props.agentCoreStack.metadataQueryRuntimeArn), `${safeName}_metadata_query.DEFAULT`,
      rates.metadataQuery ?? defaultRate, evalExecutionRole.roleArn);

    this.createEvalConfig('QuerySuggestionsOnlineEval', `${safeName}_query_suggestions_eval`,
      runtimeLogGroup(props.agentCoreStack.suggestionsRuntimeArn), `${safeName}_query_suggestions.DEFAULT`,
      rates.querySuggestions ?? defaultRate, evalExecutionRole.roleArn);

    // Ontology runtimes — only when ontologyEnabled
    if (props.agentCoreStack.ontologyRuntimeArn) {
      this.createEvalConfig('OntologyOnlineEval', `${safeName}_ontology_eval`,
        runtimeLogGroup(props.agentCoreStack.ontologyRuntimeArn), `${safeName}_ontology.DEFAULT`,
        rates.ontology ?? defaultRate, evalExecutionRole.roleArn);
    }

    if (props.agentCoreStack.queryRuntimeArn) {
      this.createEvalConfig('OntologyQueryOnlineEval', `${safeName}_ontology_query_eval`,
        runtimeLogGroup(props.agentCoreStack.queryRuntimeArn), `${safeName}_ontology_query.DEFAULT`,
        rates.ontologyQuery ?? defaultRate, evalExecutionRole.roleArn);
    }

    // Outputs
    new cdk.CfnOutput(this, 'MetadataEvalConfigName', {
      value: `${safeName}_metadata_eval`,
      description: 'Online eval config name for Metadata Agent',
      exportName: `${pName}-metadata-eval-config`,
    });
    new cdk.CfnOutput(this, 'MetadataQueryEvalConfigName', {
      value: `${safeName}_metadata_query_eval`,
      description: 'Online eval config name for Metadata Query Agent',
      exportName: `${pName}-metadata-query-eval-config`,
    });
    new cdk.CfnOutput(this, 'QuerySuggestionsEvalConfigName', {
      value: `${safeName}_query_suggestions_eval`,
      description: 'Online eval config name for Query Suggestions Agent',
      exportName: `${pName}-query-suggestions-eval-config`,
    });
    new cdk.CfnOutput(this, 'EvalExecutionRoleArn', {
      value: evalExecutionRole.roleArn,
      description: 'IAM role ARN used by all online eval configs',
      exportName: `${pName}-eval-execution-role`,
    });
    if (props.agentCoreStack.ontologyRuntimeArn) {
      new cdk.CfnOutput(this, 'OntologyEvalConfigName', {
        value: `${safeName}_ontology_eval`,
        description: 'Online eval config name for Ontology Agent',
        exportName: `${pName}-ontology-eval-config`,
      });
    }
    if (props.agentCoreStack.queryRuntimeArn) {
      new cdk.CfnOutput(this, 'OntologyQueryEvalConfigName', {
        value: `${safeName}_ontology_query_eval`,
        description: 'Online eval config name for Ontology Query Agent',
        exportName: `${pName}-ontology-query-eval-config`,
      });
    }
  }

  private createEvalConfig(
    id: string,
    configName: string,
    logGroupName: string,
    serviceName: string,
    samplingRate: number,
    executionRoleArn: string,
  ): void {
    new cdk.CustomResource(this, id, {
      serviceToken: this.evalConfigServiceToken,
      properties: {
        ConfigName: configName,
        Description: `Online evaluation for ${serviceName}`,
        LogGroupName: logGroupName,
        ServiceName: serviceName,
        SamplingRate: samplingRate,
        ExecutionRoleArn: executionRoleArn,
      },
    });
  }
}
