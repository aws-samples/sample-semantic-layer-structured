import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import { AgentCoreStack } from './agentcore-stack';

// ── Custom LLM-as-Judge evaluator definitions (metadata query agent) ──────────
// These two evaluators are ground-truth-free, so they run on live (online) traffic.
// CreateEvaluator validates that the CALLER (the EvalConfigHandler Lambda role) can invoke
// the judge model, so that caller role is granted bedrock:InvokeModel below; the
// EvalExecutionRole grant is separate and covers invocation at evaluation time.
// The same prompt text lives in notebooks 2 & 3 for ad-hoc use; the deployed pipeline owns
// these copies so it no longer depends on a notebook being run.
//
// ARCHITECTURE NOTE (why these prompts do NOT name retrieve_kb_context /
// disambiguate_query_terms tool calls): the deployed metadata query agent does NOT run a
// ReAct tool loop. Its entrypoint runs a deterministic Tier 2 Strands GRAPH (phase functions
// phase1..phase5 — KB topic router → disambiguation → slice builder → SQL generate+validate →
// grounding gate + bounded execution). The retrieval/disambiguation/slice steps are plain
// function calls, NOT model tool calls, so they never appear as tool spans in {context}. The
// ONLY real Strands tool span at runtime is `execute_sql_query` (the Phase 5 bounded execution
// agent's single tool). What DOES land in {context} is: the retrieved schema the SQL was built
// from (Phase 1 KB chunks + the Phase 3/4 slice embedded in the SQL-generator prompt) and the
// `execute_sql_query` call carrying the executed SQL. Both judges are therefore framed around
// "the retrieved schema context" (slice / KB chunks) rather than a specific retrieval tool.
const JUDGE_MODEL_ID = 'global.anthropic.claude-sonnet-4-6';

// SESSION: every table/column/join in the executed SQL must appear in the retrieved schema
// context. {context} carries the full session, including the Phase 1 KB chunks / Phase 3 slice
// (the schema the SQL generator was given) and the execute_sql_query tool CALL arguments (the
// SQL). Grounding is judged from those directly — no retrieval tool call is expected.
const SQL_GROUNDED_INSTRUCTIONS = [
  'You are a strict binary grounding evaluator for a text-to-SQL data agent.',
  '',
  'Session context (full conversation, including phase outputs, tool calls and tool results):',
  '{context}',
  '',
  'Available tools: {available_tools}',
  '',
  // NOTE: do NOT add {actual_tool_trajectory} here — AgentCore classifies it as a
  // ground-truth placeholder, which makes the evaluator online-eval-incompatible.
  // The retrieved schema (slice / KB chunks) and the execute_sql_query arguments are
  // already in {context}; grounding is judged from those directly, no trajectory needed.
  'This agent runs a deterministic resolution graph: it retrieves Knowledge Base schema for ' +
    'the question, assembles a SCHEMA SLICE (the allowed tables/columns/joins), generates SQL ' +
    'against that slice, then executes it with the `execute_sql_query` tool. In the context, ' +
    'locate:',
  '  (a) the RETRIEVED SCHEMA CONTEXT — the KB chunks / schema slice describing the allowed ' +
    'tables, columns, and joins (the ONLY schema the agent may use); and',
  '  (b) the ARGUMENTS of the `execute_sql_query` tool — the SQL the agent actually ran.',
  '',
  'Score 1 (pass) iff EVERY table, column, and join referenced in the executed SQL appears in ' +
    'the retrieved schema context (case-insensitive; tolerate aliases, quoted vs unquoted ' +
    'identifiers, and SQL builtin functions such as COUNT/SUM/DATE_TRUNC — those are not ' +
    'schema). Score 0 (fail) if the SQL references any table or column that is absent from the ' +
    'retrieved schema context (hallucinated schema), or if no retrieved schema context is ' +
    'present at all (grounding cannot be verified). Briefly name the first offending identifier ' +
    'when you fail it.',
].join('\n');

// SESSION: did the agent retrieve/assemble its schema context BEFORE executing SQL? In the
// deterministic graph the only model tool span is `execute_sql_query`; the schema-retrieval and
// disambiguation steps are graph phases (function calls), so this judge checks the invariant
// that still matters — schema-before-execution — rather than a multi-tool call order.
//
// ONLINE-EVAL CONSTRAINT: only `{context}` and `{available_tools}` are valid SESSION-level
// placeholders for ONLINE evaluation. `{actual_tool_trajectory}` is classified by AgentCore
// as a GROUND-TRUTH placeholder (it pairs with `{expected_tool_trajectory}` /
// `evaluationReferenceInputs`), so any evaluator referencing it is rejected from online
// configs ("require reference inputs"). We therefore derive ordering from `{context}` itself
// (which contains the phase outputs + the execute_sql_query call in chronological order) — no
// reference data, fully online-compatible.
const TOOL_ORDERING_INSTRUCTIONS = [
  'You are a strict binary evaluator checking whether a text-to-SQL agent grounded its SQL in ' +
    'retrieved schema BEFORE executing it.',
  '',
  'This agent runs a deterministic resolution graph, not a free-form tool loop. The prescribed ' +
    'flow for a NEW question is, in this order:',
  '  1. retrieve Knowledge Base schema for the question (graph phase — appears as KB chunks ' +
    'in the context, not a model tool call);',
  '  2. assemble + disambiguate a schema SLICE of the allowed tables/columns (graph phase);',
  '  3. generate SQL against that slice, then call the `execute_sql_query` tool to run it.',
  '',
  'Available tools: {available_tools}',
  'Session context (contains the phase outputs and the execute_sql_query call in chronological ' +
    'order): {context}',
  '',
  'From the session context, determine whether the retrieved schema context (KB chunks / schema ' +
    'slice) and the SQL generated from it appear BEFORE the `execute_sql_query` call — i.e. the ' +
    'agent did not execute SQL before any schema was retrieved/assembled. Ignore tool RESULTS ' +
    'and any other tools; judge only this ordering invariant.',
  '',
  'Score 1 (pass) iff a retrieved schema context (KB chunks / slice) is present and precedes ' +
    'the first `execute_sql_query` call (a follow-up that reuses already-in-scope schema and ' +
    'skips fresh retrieval is acceptable). Score 0 (fail) if `execute_sql_query` is invoked with ' +
    'no retrieved schema context anywhere before it, or if SQL is executed against schema that ' +
    'was never retrieved/assembled. Briefly explain the offending ordering when you fail it.',
].join('\n');

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
    evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
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
      })
    );

    // EvalExecutionRole invokes the judge at evaluation time. Region is unpinned on the
    // foundation-model ARN because the judge is a `global.` cross-region inference profile,
    // which a region-pinned ARN would not satisfy.
    evalExecutionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/global.anthropic.*`,
        ],
      })
    );

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
                  // nosemgrep: detect-child-process — CDK synth-time execSync on framework outputDir + static repo paths
                  `pip install --quiet --target "${outputDir}" -r "${path.join(handlerDir, 'requirements.txt')}"`,
                  { stdio: 'pipe' }
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
            'bash',
            '-c',
            'pip install --quiet --target /asset-output -r requirements.txt && cp *.py /asset-output/',
          ],
        },
      }),
      timeout: cdk.Duration.minutes(5),
      description: 'Custom resource handler for AgentCore online evaluation configs',
    });

    evalConfigHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateOnlineEvaluationConfig',
          'bedrock-agentcore:DeleteOnlineEvaluationConfig',
          'bedrock-agentcore:ListOnlineEvaluationConfigs',
          // Custom LLM-as-Judge evaluators (SqlGrounded, ToolCallOrdering) are created
          // and managed by this same handler so the online config can reference their IDs.
          'bedrock-agentcore:CreateEvaluator',
          'bedrock-agentcore:DeleteEvaluator',
          'bedrock-agentcore:ListEvaluators',
          'bedrock-agentcore:GetEvaluator',
        ],
        resources: ['*'],
      })
    );

    // The bedrock-agentcore service uses the Lambda caller's credentials to validate and configure
    // the aws/spans log group index (for OTEL spans). These permissions are needed on the Lambda's role.
    evalConfigHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['logs:DescribeIndexPolicies', 'logs:PutIndexPolicy'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:aws/spans:*`],
      })
    );

    // PassRole so Lambda can hand evalExecutionRole to the evaluation service
    evalConfigHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [evalExecutionRole.roleArn],
      })
    );

    // CreateEvaluator validates that the CALLER (this Lambda role) can invoke the judge
    // model — without this grant the API rejects the call with a ValidationException.
    // Region is unpinned and the global inference-profile ARN is included because the judge
    // (JUDGE_MODEL_ID) is a `global.` cross-region inference profile.
    evalConfigHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [
          `arn:aws:bedrock:*::foundation-model/anthropic.*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/global.anthropic.*`,
        ],
      })
    );

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

    // SemanticRAG runtimes — provisioned only when enableSemanticRag=true.
    // Eval configs are skipped when the runtime doesn't exist; the type system
    // requires these guards because the ARN fields are optional on AgentCoreStack.
    if (props.agentCoreStack.metadataRuntimeArn) {
      this.createEvalConfig(
        'MetadataOnlineEval',
        `${safeName}_metadata_eval`,
        runtimeLogGroup(props.agentCoreStack.metadataRuntimeArn),
        `${safeName}_metadata.DEFAULT`,
        rates.metadata ?? defaultRate,
        evalExecutionRole.roleArn
      );
    }

    if (props.agentCoreStack.metadataQueryRuntimeArn) {
      // The metadata query agent gets two extra custom LLM-as-Judge evaluators. They are
      // created/owned by the same handler so we have stable IDs to attach to the config.
      const sqlGroundedId = this.createEvaluator(
        'SqlGroundedEvaluator',
        `${safeName}_sql_grounded`,
        'SESSION',
        'SQL references only tables/columns present in the retrieved schema slice / KB context.',
        SQL_GROUNDED_INSTRUCTIONS
      );
      const toolOrderingId = this.createEvaluator(
        'ToolCallOrderingEvaluator',
        `${safeName}_tool_call_ordering`,
        'SESSION',
        'Schema is retrieved/assembled before execute_sql_query runs (graph ordering invariant).',
        TOOL_ORDERING_INSTRUCTIONS
      );

      this.createEvalConfig(
        'MetadataQueryOnlineEval',
        `${safeName}_metadata_query_eval`,
        runtimeLogGroup(props.agentCoreStack.metadataQueryRuntimeArn),
        `${safeName}_metadata_query.DEFAULT`,
        rates.metadataQuery ?? defaultRate,
        evalExecutionRole.roleArn,
        [sqlGroundedId, toolOrderingId]
      );
    }

    if (props.agentCoreStack.suggestionsRuntimeArn) {
      this.createEvalConfig(
        'QuerySuggestionsOnlineEval',
        `${safeName}_query_suggestions_eval`,
        runtimeLogGroup(props.agentCoreStack.suggestionsRuntimeArn),
        `${safeName}_query_suggestions.DEFAULT`,
        rates.querySuggestions ?? defaultRate,
        evalExecutionRole.roleArn
      );
    }

    // Ontology runtimes — only when ontologyEnabled
    if (props.agentCoreStack.ontologyRuntimeArn) {
      this.createEvalConfig(
        'OntologyOnlineEval',
        `${safeName}_ontology_eval`,
        runtimeLogGroup(props.agentCoreStack.ontologyRuntimeArn),
        `${safeName}_ontology.DEFAULT`,
        rates.ontology ?? defaultRate,
        evalExecutionRole.roleArn
      );
    }

    if (props.agentCoreStack.queryRuntimeArn) {
      this.createEvalConfig(
        'OntologyQueryOnlineEval',
        `${safeName}_ontology_query_eval`,
        runtimeLogGroup(props.agentCoreStack.queryRuntimeArn),
        `${safeName}_ontology_query.DEFAULT`,
        rates.ontologyQuery ?? defaultRate,
        evalExecutionRole.roleArn
      );
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
    extraEvaluatorIds: string[] = []
  ): void {
    new cdk.CustomResource(this, id, {
      serviceToken: this.evalConfigServiceToken,
      properties: {
        Kind: 'config',
        ConfigName: configName,
        Description: `Online evaluation for ${serviceName}`,
        LogGroupName: logGroupName,
        ServiceName: serviceName,
        SamplingRate: samplingRate,
        ExecutionRoleArn: executionRoleArn,
        // Passed as a CFN list-of-strings; the embedded evaluatorId tokens resolve at deploy
        // time. The handler appends these custom IDs to the built-in evaluators
        // (GoalSuccessRate + Correctness; the ToolSelection/ToolParameter ReAct-trajectory
        // built-ins were removed 2026-06-06 — see _BUILTIN_EVALUATORS in the handler).
        // NOTE: Builtin.Correctness is per-trace, so it needs an answer span on every sampled
        // invocation. The metadata/ontology query agents guarantee this via
        // shared/answer_span.emit_answer_span (clarification turns make no model call and would
        // otherwise have no answer span). No CDK change is required for that — it's agent-side.
        ExtraEvaluatorIds: extraEvaluatorIds,
        // The built-in evaluator set lives in the handler's _BUILTIN_EVALUATORS, NOT in these
        // CFN properties — so a handler-only change (e.g. the 2026-06-06 removal of the
        // ToolSelection/ToolParameter ReAct-trajectory built-ins) does NOT by itself
        // re-trigger this CustomResource, leaving the live config stale (it kept scoring live
        // traffic with the removed-but-still-attached built-ins). Bump this version whenever
        // the intended evaluator SET changes so CFN re-runs Update → delete+recreate the
        // config with the current set. Current set: GoalSuccessRate + Correctness + the custom
        // SqlGrounded/ToolCallOrdering judges (4 total; the 2 ReAct built-ins are gone).
        // 2026-06-11 bump: re-runs every config CR through the FIXED handler Update path
        // (retry-through-ConflictException) to restore the 5 configs net-deleted by the prior
        // Update race — the old path swallowed the name-still-reserved conflict into a
        // stale-id lookup and never actually recreated the configs.
        EvaluatorSetVersion: '2026-06-11-conflict-retry-fix',
      },
    });
  }

  /**
   * Create a custom binary LLM-as-Judge evaluator via the eval-config handler
   * (Kind: 'evaluator') and return its service-assigned evaluatorId as a token.
   *
   * @param id - CDK construct id.
   * @param evaluatorName - deterministic name ([a-zA-Z][a-zA-Z0-9_]{0,47}); reused on redeploy.
   * @param level - 'SESSION' | 'TRACE' | 'TOOL_CALL'.
   * @param description - human-readable description.
   * @param instructions - judge prompt. For an ONLINE-eval evaluator the prompt must use only
   *   reference-FREE placeholders ({context}, {available_tools}); {expected_response},
   *   {assertions}, and {actual_tool_trajectory} are reference inputs and make the evaluator
   *   on-demand-only. The two judges below are deliberately reference-free so they run online.
   * @returns the evaluatorId attribute token, for inclusion in an online-eval config.
   */
  private createEvaluator(
    id: string,
    evaluatorName: string,
    level: string,
    description: string,
    instructions: string
  ): string {
    const resource = new cdk.CustomResource(this, id, {
      serviceToken: this.evalConfigServiceToken,
      properties: {
        Kind: 'evaluator',
        EvaluatorName: evaluatorName,
        Level: level,
        Description: description,
        Instructions: instructions,
        JudgeModelId: JUDGE_MODEL_ID,
        MaxTokens: 1024,
      },
    });
    return resource.getAttString('EvaluatorId');
  }
}
