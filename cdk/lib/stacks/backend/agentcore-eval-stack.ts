import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { NagSuppressions } from 'cdk-nag';
import { execSync } from 'child_process';
import * as crypto from 'crypto';
import * as path from 'path';
import * as fs from 'fs';
import { AgentCoreStack } from './agentcore-stack';

// ── Custom LLM-as-Judge evaluator definitions (query agents) ──────────────────
// These evaluators are ground-truth-FREE (they reference only {context} and
// {available_tools}), so they run on live (online) traffic.
// CreateEvaluator validates that the CALLER (the EvalConfigHandler Lambda role) can invoke
// the judge model, so that caller role is granted bedrock:InvokeModel below; the
// EvalExecutionRole grant is separate and covers invocation at evaluation time.
//
// SOURCE OF TRUTH — NO HAND-COPIED PROMPTS. The prompt text is authored ONCE in
// `agents/shared/eval_judges.py` (the same module the notebooks import for their
// on-demand batch judges) and exported to `agents/shared/online_judge_prompts.json`
// by `agents/shared/online_judge_prompts_export.py` (run
// `python -m agents.shared.online_judge_prompts_export`). This stack loads that
// JSON at synth time, so the deployed online judges are byte-identical to the
// canonical Python definitions and the two copies CANNOT drift (the prior bug: the
// TS file carried trimmed inline copies that diverged from eval_judges.py — e.g.
// the online SqlGrounded lacked the degraded-run pass branch). A parity test
// (`tests/unit/test_online_judge_prompts.py::test_checked_in_json_is_fresh`)
// re-runs the exporter and fails if the checked-in JSON is stale.
//
// ARCHITECTURE NOTE (why these prompts do NOT name retrieve_kb_context /
// disambiguate_query_terms tool calls): the deployed query agents do NOT run a
// ReAct tool loop. Each entrypoint runs a deterministic Tier 2 Strands GRAPH (phase
// functions phase1..phase5 — KB/ontology retrieval → disambiguation → slice builder →
// SQL/SPARQL generate+validate → grounding gate + bounded execution). The
// retrieval/disambiguation/slice steps are plain function calls, NOT model tool calls,
// so they never appear as tool spans in {context}. For the RAG (metadata_query) agent
// the ONLY real Strands tool span is `execute_sql_query`; the VKG (ontology_query)
// agent translates SPARQL→SQL via Ontop and runs it on Athena directly (no tool span),
// so its grounding judge reads the executed SQL from the Phase 5 output in {context}.
// Sonnet 5 — matches the canonical batch judges in agents/shared/eval_judges.py
// (JUDGE_MODEL_ID there), so the online and on-demand judges score on the same
// model. This constant is folded into each evaluator's CONTENT HASH (see
// createEvaluator), so bumping it changes every evaluator NAME and CFN performs a
// clean REPLACEMENT: create the new-named evaluator, re-point the referencing
// config to its id, then delete the old (now-dereferenced, unlocked) evaluator
// last. That avoids BOTH failure modes seen on 2026-07-01: the same-name
// ConflictException (name not freed within the old handler's 127s retry) AND the
// "Cannot delete a locked evaluator" ValidationException (an evaluator cannot be
// deleted while an active config still references it — which stranded the deploy
// AND its rollback, taking 4 of 5 online configs offline). Configs keep fixed
// names + an async isComplete waiter for their own name-release race; see the
// TWO-STRATEGIES note in agentcore-eval-handler/index.py.
const JUDGE_MODEL_ID = 'global.anthropic.claude-sonnet-5';

// Load the canonical reference-free online judge prompts exported from
// agents/shared/eval_judges.py. Keyed { rag: {GoalSuccess, SqlGrounded,
// ToolCallOrdering}, vkg: {...} }. Loaded at synth time so a prompt edit in the
// Python source (after re-running the exporter) flows straight into the deploy.
interface OnlineJudgePrompts {
  rag: { GoalSuccess: string; SqlGrounded: string; ToolCallOrdering: string };
  vkg: { GoalSuccess: string; SqlGrounded: string; ToolCallOrdering: string };
}
const ONLINE_JUDGE_PROMPTS: OnlineJudgePrompts = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, '..', '..', '..', '..', 'agents', 'shared', 'online_judge_prompts.json'),
    'utf-8'
  )
);

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
  /** Project name with hyphens → underscores; the prefix of every config/evaluator name. */
  private readonly safeName: string;

  constructor(scope: Construct, id: string, props: AgentCoreEvalStackProps) {
    super(scope, id, props);

    const defaultRate = props.samplingRate ?? 100;
    const rates = props.samplingRates ?? {};
    const pName = props.projectName;
    // Config names must match [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens allowed
    const safeName = pName.replace(/-/g, '_');
    this.safeName = safeName;

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
    //
    // The handler is an ASYNC custom resource: on_event issues the delete and
    // returns immediately; is_complete is re-invoked by the provider's Step
    // Functions waiter until the recreate lands (see EvalConfigProvider below and
    // the ASYNC note in agentcore-eval-handler/index.py). Both framework hooks are
    // served by ONE code asset via two lambda.Function entrypoints that SHARE the
    // execution role — the is_complete function performs the create (including the
    // aws/spans index-policy validation the service runs with the caller's creds),
    // so it needs every grant the on_event function has. Attaching all policies to
    // one shared role keeps them identical and keeps the IAM test's assertion
    // (InvokeModel grant on an 'EvalConfigHandler' role) valid.
    const handlerDir = path.join(__dirname, 'agentcore-eval-handler');
    const bundledHandlerCode = lambda.Code.fromAsset(handlerDir, {
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
    });

    // Shared execution role for both handler entrypoints (see note above).
    const evalHandlerRole = new iam.Role(this, 'EvalConfigHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      description:
        'Shared execution role for the AgentCore eval-config on_event + is_complete handlers',
    });

    const evalConfigHandler = new lambda.Function(this, 'EvalConfigHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.on_event',
      code: bundledHandlerCode,
      role: evalHandlerRole,
      timeout: cdk.Duration.minutes(5),
      description:
        'on_event handler for AgentCore online evaluation configs (issues deletes, defers creates)',
    });

    // is_complete is polled by the provider's waiter state machine. It performs the
    // actual create_* call and returns IsComplete=False (retry) while the name is
    // still held by an in-flight delete. Same code asset + shared role as on_event.
    const evalConfigCompleteHandler = new lambda.Function(this, 'EvalConfigCompleteHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.is_complete',
      code: bundledHandlerCode,
      role: evalHandlerRole,
      timeout: cdk.Duration.minutes(5),
      description:
        'is_complete handler for AgentCore online evaluation configs (polls until the recreate lands)',
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

    // Async provider: on_event issues the delete, then the framework polls
    // is_complete every queryInterval (creating the resource, retrying while the
    // name is still held by the in-flight delete) up to totalTimeout. 30 min is
    // well beyond any observed AgentCore name-release delay, so a model/prompt
    // change that forces a delete→recreate no longer strands the resource.
    const evalConfigProvider = new cr.Provider(this, 'EvalConfigProvider', {
      onEventHandler: evalConfigHandler,
      isCompleteHandler: evalConfigCompleteHandler,
      queryInterval: cdk.Duration.seconds(10),
      totalTimeout: cdk.Duration.minutes(30),
    });

    this.evalConfigServiceToken = evalConfigProvider.serviceToken;

    // The isCompleteHandler makes cr.Provider synthesize a waiter Step Functions
    // state machine (absent when there is no isCompleteHandler). It is framework-
    // managed — its logging and tracing cannot be configured from here — so the
    // two SF nags it raises are suppressed on the provider subtree. This mirrors
    // the SgEniDrainerProvider suppressions in agentcore-stack.ts.
    NagSuppressions.addResourceSuppressions(
      evalConfigProvider,
      [
        {
          id: 'AwsSolutions-SF1',
          reason:
            'Waiter state machine is generated by the cr.Provider framework; its CloudWatch logging level cannot be configured from this stack.',
        },
        {
          id: 'AwsSolutions-SF2',
          reason:
            'Waiter state machine is generated by the cr.Provider framework; X-Ray tracing cannot be enabled from this stack.',
        },
        {
          id: 'AwsSolutions-IAM4',
          reason: 'cr.Provider framework Lambda uses AWSLambdaBasicExecutionRole for CW Logs.',
        },
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'cr.Provider framework Lambdas need lambda:InvokeFunction on their inner handlers and states:* on the waiter state machine.',
        },
        {
          id: 'AwsSolutions-L1',
          reason: 'Provider framework runtime is managed by CDK; cannot be overridden here.',
        },
      ],
      true
    );

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
      // The metadata query agent (RAG family) gets TWO reference-free custom
      // LLM-as-Judge evaluators, created/owned by the same handler so we have
      // stable IDs to attach to the config. This mirrors the SESSION judge set the
      // notebooks score with (notebooks/2_metadata_query_agent_ondemand_groundtruth_eval.ipynb),
      // minus the reference-based FinalAnswerFaithfulness which cannot run ONLINE:
      //   - GoalSuccess: reference-free replacement for Builtin.GoalSuccessRate,
      //     which is un-editable and mis-grades this deterministic-graph agent
      //     (it reads an intermediate intent-classification JSON span as the
      //     assistant's turn). The custom judge knows the "Final-answer record"
      //     marker and ignores intermediate graph phases. Its reference-free form
      //     also checks answer-faithfulness (figures drawn from tool results, not
      //     invented), covering what FinalAnswerFaithfulness does with ground truth.
      //   - SqlGrounded: every table/column/join in the executed SQL must appear
      //     in the retrieved schema slice / KB context (carries the degraded-run
      //     pass branch — no execute_sql_query call = grounding upheld).
      // NOT deployed online:
      //   - FinalAnswerFaithfulness — reference-BASED (reads {assertions}, the
      //     expected answer); AgentCore rejects reference placeholders on a
      //     live-traffic online config, so it is on-demand/batch-only (notebooks).
      //   - ToolCallOrdering — removed upstream (no diagnostic signal on a
      //     deterministic-graph agent; see notebook 5). Dropped here to match.
      // Discriminators are kept short (≤20 chars) so `{safeName}_{disc}_{hash8}`
      // fits the 48-char evaluator-name limit; createEvaluator asserts this.
      const goalSuccessId = this.createEvaluator(
        'RagGoalSuccessEvaluator',
        'rag_goal_success',
        'SESSION',
        'Agent reached a responsive, grounded user-facing answer (or apt clarification).',
        ONLINE_JUDGE_PROMPTS.rag.GoalSuccess
      );
      const sqlGroundedId = this.createEvaluator(
        'SqlGroundedEvaluator',
        'rag_sql_grounded',
        'SESSION',
        'SQL references only tables/columns present in the retrieved schema slice / KB context.',
        ONLINE_JUDGE_PROMPTS.rag.SqlGrounded
      );

      this.createEvalConfig(
        'MetadataQueryOnlineEval',
        `${safeName}_metadata_query_eval`,
        runtimeLogGroup(props.agentCoreStack.metadataQueryRuntimeArn),
        `${safeName}_metadata_query.DEFAULT`,
        rates.metadataQuery ?? defaultRate,
        evalExecutionRole.roleArn,
        [goalSuccessId, sqlGroundedId],
        // Custom GoalSuccess replaces Builtin.GoalSuccessRate on this config; keep
        // Builtin.Correctness (per-trace answer-faithfulness, still meaningful).
        ['Builtin.Correctness']
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
      // The ontology query agent (VKG family) gets the SAME two reference-free
      // judges as the RAG family, with the VKG-specific prompts: the VKG agent
      // translates SPARQL→SQL via Ontop and runs it on Athena directly (no
      // execute_sql_query tool span), so its SqlGrounded judge reads the executed
      // SQL from the Phase 5 output in {context} and reasons about the ontology
      // slice. Custom GoalSuccess replaces Builtin.GoalSuccessRate here too.
      // FinalAnswerFaithfulness (reference-based) and ToolCallOrdering (removed
      // upstream) are NOT deployed online — see the metadata_query block above and
      // notebooks/5_ontology_queryagent_ac_eval.ipynb.
      const vkgGoalSuccessId = this.createEvaluator(
        'VkgGoalSuccessEvaluator',
        'vkg_goal_success',
        'SESSION',
        'VKG agent reached a responsive, grounded user-facing answer (or apt clarification).',
        ONLINE_JUDGE_PROMPTS.vkg.GoalSuccess
      );
      const vkgSqlGroundedId = this.createEvaluator(
        'VkgSqlGroundedEvaluator',
        'vkg_sql_grounded',
        'SESSION',
        'Executed SQL maps only to classes/properties present in the retrieved ontology slice.',
        ONLINE_JUDGE_PROMPTS.vkg.SqlGrounded
      );

      this.createEvalConfig(
        'OntologyQueryOnlineEval',
        `${safeName}_ontology_query_eval`,
        runtimeLogGroup(props.agentCoreStack.queryRuntimeArn),
        `${safeName}_ontology_query.DEFAULT`,
        rates.ontologyQuery ?? defaultRate,
        evalExecutionRole.roleArn,
        [vkgGoalSuccessId, vkgSqlGroundedId],
        ['Builtin.Correctness']
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
    extraEvaluatorIds: string[] = [],
    builtinEvaluatorIds: string[] = []
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
        // time. The handler appends these custom IDs to the built-in evaluator set (see
        // BuiltinEvaluatorIds below).
        // NOTE: Builtin.Correctness is per-trace, so it needs an answer span on every sampled
        // invocation. The metadata/ontology query agents guarantee this via
        // shared/answer_span.emit_answer_span (clarification turns make no model call and would
        // otherwise have no answer span). No CDK change is required for that — it's agent-side.
        ExtraEvaluatorIds: extraEvaluatorIds,
        // Per-config OVERRIDE of the handler's default built-in set (_BUILTIN_EVALUATORS =
        // GoalSuccessRate + Correctness). When non-empty it REPLACES the default. The two
        // query-agent configs pass ['Builtin.Correctness'] because their custom GoalSuccess
        // judge replaces the un-editable Builtin.GoalSuccessRate (which mis-grades the
        // deterministic-graph agents). An empty list means "use the handler default", so the
        // three non-query configs (metadata, ontology, query_suggestions) keep both built-ins.
        BuiltinEvaluatorIds: builtinEvaluatorIds,
        // The intended evaluator SET partly lives outside these CFN properties (the handler's
        // _BUILTIN_EVALUATORS default), so a handler-only change does NOT by itself re-trigger
        // this CustomResource. Bump this version whenever the intended evaluator SET changes so
        // CFN re-runs Update → delete+recreate the config with the current set.
        //   Query-agent configs (metadata_query, ontology_query): Builtin.Correctness + custom
        //     GoalSuccess + SqlGrounded (3; GoalSuccessRate replaced).
        //   Other configs (metadata, ontology, query_suggestions): GoalSuccessRate + Correctness.
        // 2026-06-24 bump: query-agent online judges synced to agents/shared/eval_judges.py via
        // the generated online_judge_prompts.json — adds the reference-free custom GoalSuccess
        // (replacing Builtin.GoalSuccessRate), restores the degraded-run pass branch to
        // SqlGrounded, and gives ontology_query its own VKG custom judges (was builtins-only).
        // 2026-07-01 bump: JUDGE_MODEL_ID → Sonnet 5 + MaxTokens 4096 (evaluators now use
        // content-hashed names → CFN replacement, no lock), AND drop ToolCallOrdering to match
        // the notebook SESSION set (GoalSuccess + SqlGrounded; FinalAnswerFaithfulness is
        // reference-based → on-demand only). Re-asserts all five configs with the current set.
        EvaluatorSetVersion: '2026-07-01-sonnet5-2judge',
      },
    });
  }

  /**
   * Create a custom binary LLM-as-Judge evaluator via the eval-config handler
   * (Kind: 'evaluator') and return its service-assigned evaluatorId as a token.
   *
   * CONTENT-HASHED NAME (evaluator-lock workaround): the evaluator name is
   * `{safeName}_{disc}_{hash}` where `hash` is an 8-hex digest over the mutable
   * content (judge model + maxTokens + instructions). A content change therefore
   * changes the NAME, so CFN performs a REPLACEMENT rather than an in-place
   * delete→recreate: it creates the new-named evaluator, the referencing config
   * re-points to the new id, and only THEN does CFN delete the old evaluator —
   * which is unlocked by then. This is what avoids the "Cannot delete a locked
   * evaluator" ValidationException that took the online configs offline on
   * 2026-07-01 (an evaluator cannot be deleted while a live config references it).
   *
   * @param id - CDK construct id.
   * @param disc - short discriminator (e.g. 'sql_grounded'); kept short so the
   *   final `{safeName}_{disc}_{hash}` fits the 48-char evaluator-name limit.
   * @param level - 'SESSION' | 'TRACE' | 'TOOL_CALL'.
   * @param description - human-readable description (NOT part of the hash — it is
   *   cosmetic and changing it should not force a replacement).
   * @param instructions - judge prompt. For an ONLINE-eval evaluator the prompt must use only
   *   reference-FREE placeholders ({context}, {available_tools}); {expected_response},
   *   {assertions}, and {actual_tool_trajectory} are reference inputs and make the evaluator
   *   on-demand-only. The two judges below are deliberately reference-free so they run online.
   * @returns the evaluatorId attribute token, for inclusion in an online-eval config.
   */
  private createEvaluator(
    id: string,
    disc: string,
    level: string,
    description: string,
    instructions: string
  ): string {
    // 4096 (raised from 1024): JUDGE_MODEL_ID is Sonnet 5, whose adaptive thinking
    // shares the output-token budget — 1024 can truncate the verdict before the
    // numeric score is emitted. Matches maxTokens in the canonical batch judges
    // (agents/shared/eval_judges.py).
    const maxTokens = 4096;

    // Hash the MUTABLE content only (model, maxTokens, instructions). Any change
    // here yields a new name → CFN replacement (create-new → config re-points →
    // delete-old-once-unlocked). `disc`/level/description are identity, not content.
    const contentHash = crypto
      .createHash('sha256')
      .update(`${JUDGE_MODEL_ID} ${maxTokens} ${instructions}`)
      .digest('hex')
      .slice(0, 8);
    const evaluatorName = `${this.safeName}_${disc}_${contentHash}`;
    // Evaluator names must match [a-zA-Z][a-zA-Z0-9_]{0,47} (48 chars max). Fail at
    // synth if a longer deploy suffix pushes a name over the limit rather than
    // letting CreateEvaluator reject it mid-deploy.
    if (evaluatorName.length > 48) {
      throw new Error(
        `Evaluator name '${evaluatorName}' is ${evaluatorName.length} chars (>48). ` +
          `Shorten the discriminator '${disc}' or the project/suffix.`
      );
    }

    const resource = new cdk.CustomResource(this, id, {
      serviceToken: this.evalConfigServiceToken,
      properties: {
        Kind: 'evaluator',
        EvaluatorName: evaluatorName,
        Level: level,
        Description: description,
        Instructions: instructions,
        JudgeModelId: JUDGE_MODEL_ID,
        MaxTokens: maxTokens,
      },
    });
    return resource.getAttString('EvaluatorId');
  }
}
