import {
  Stack,
  StackProps,
  Duration,
  RemovalPolicy,
  CfnOutput,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_s3 as s3,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as tasks,
  aws_dynamodb as dynamodb,
  aws_logs as logs,
} from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Creation-time document processing pipeline (item #3).
 *
 * Orchestration: Step Functions STANDARD state machine started
 * synchronously by the REST API's `DocumentService.upload_document`
 * (`states:StartExecution` against the ARN exported here, after the
 * raw doc is written to S3 and the `DOCJOB#` status row is persisted
 * to DynamoDB). The state machine runs the six pure-Python Lambdas
 * in `lambda/doc-pipeline/`:
 *
 *     chunk → ner → embed → link → ruleminer → index
 *
 * `ruleminer` sits after `link` because it binds rule roles to the ontology
 * IRIs the linker resolves, and before `index` so KB ingestion stays last. It
 * runs unconditionally: making the pipeline shape depend on
 * `enableGovernedRules` would mean two state-machine definitions and a redeploy
 * to toggle, and its output is inert until an admin imports it. The flag gates
 * the /rules API and the admin UI, which is where the governance surface is.
 *
 * Per-stage failures bubble up to the state machine's retry policy.
 * Document status (per-stage booleans) is mirrored to the existing
 * ontology metadata table under SK prefix `DOCJOB#`, kept in sync by
 * `DocumentService.update_stage`.
 *
 * This stack provisions NO Knowledge Base of its own. The indexer writes each
 * document to ``metadata/{layerId}/_documents/{docId}.md`` on the artifacts
 * bucket and ingests it into the **existing semantic-rag KB**, whose data source
 * already covers the ``metadata/`` prefix on that same bucket. Document chunks
 * are separated from table chunks by a ``doc_type`` sidecar attribute rather than
 * by a separate KB — see ``lambda/doc-pipeline/indexer/handler.py``.
 */
export interface DocPipelineStackProps extends StackProps {
  /** Pre-existing S3 bucket the supplementary docs live in. Must be the same
   *  bucket the semantic-rag KB data source reads (the artifacts bucket). */
  readonly supplementaryDocsBucket: s3.IBucket;
  /** Ontology metadata DDB table (DocumentService writes DOCJOB# rows). */
  readonly metadataTable: dynamodb.ITable;
  /** Bedrock KB id + data source id the indexer ingests documents into — the
   *  semantic-rag KB. Optional only because `enableSemanticRag=false` leaves the
   *  KB unprovisioned; in that case the indexer raises rather than silently
   *  skipping ingestion, because a document that is never ingested is a document
   *  that silently cannot be retrieved. */
  readonly supplementaryDocsKbId?: string;
  readonly supplementaryDocsDataSourceId?: string;
}

export class DocPipelineStack extends Stack {
  public readonly stateMachine: sfn.IStateMachine;
  public readonly chunkerFn: lambda.IFunction;
  public readonly nerFn: lambda.IFunction;
  public readonly embedderFn: lambda.IFunction;
  public readonly linkerFn: lambda.IFunction;
  public readonly ruleMinerFn: lambda.IFunction;
  public readonly indexerFn: lambda.IFunction;

  constructor(scope: Construct, id: string, props: DocPipelineStackProps) {
    super(scope, id, props);

    // Common Lambda config for the four pipeline stages.
    // AWS_REGION is set automatically by the Lambda runtime — never inject.
    const commonEnv: { [key: string]: string } = {
      ARTIFACTS_BUCKET: props.supplementaryDocsBucket.bucketName,
      SUPPLEMENTARY_DOCS_BUCKET: props.supplementaryDocsBucket.bucketName,
      METADATA_TABLE: props.metadataTable.tableName,
      // Per-layer class index the linker resolves from the event's ontologyId.
      // This was previously absent while `linker/handler.py` read a plain
      // `CLASS_INDEX_S3_KEY`, so every Link state died with a KeyError and the
      // ruleminer and indexer never ran. Asserted in
      // __tests__/doc-pipeline-kb-wiring.test.ts so it cannot go missing again.
      CLASS_INDEX_S3_KEY_TEMPLATE: 'ontologies/{ontology_id}/class-index.json',
    };
    if (props.supplementaryDocsKbId) {
      commonEnv.SUPPLEMENTARY_DOCS_KB_ID = props.supplementaryDocsKbId;
    }
    if (props.supplementaryDocsDataSourceId) {
      commonEnv.SUPPLEMENTARY_DOCS_DS_ID = props.supplementaryDocsDataSourceId;
    }

    /**
     * Build a pip-installed code asset for a stage whose requirements.txt lists
     * dependencies beyond the Lambda runtime's built-in boto3.
     *
     * A plain `Code.fromAsset(dir)` ships the directory verbatim and NEVER
     * installs requirements.txt — which is how the chunker shipped without a PDF
     * parser while its docstring claimed to have one. Stages that need real
     * dependencies must go through here.
     *
     * Local pip first (fast, no Docker), falling back to the bundling image;
     * this mirrors the benchmark-eval asset in agentcore-stack.ts. Local pip is
     * only safe because these requirements are pure-Python (`py3-none-any`)
     * wheels — an architecture-specific wheel installed on an x86_64 workstation
     * would not run on these ARM64 functions.
     */
    const bundledCode = (codePath: string): lambda.Code => {
      // codePath is cwd-relative (`../lambda/...` from the `cdk/` directory),
      // matching what the unbundled `Code.fromAsset(codePath)` branch resolves.
      const assetDir = path.resolve(codePath);
      const requirements = path.join(assetDir, 'requirements.txt');
      return lambda.Code.fromAsset(assetDir, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(
                  // nosemgrep: detect-child-process — CDK synth-time execSync on framework outputDir + static repo paths
                  `pip install --quiet --target "${outputDir}" -r "${requirements}"`,
                  { stdio: 'pipe' }
                );
                for (const f of fs.readdirSync(assetDir)) {
                  if (f.endsWith('.py')) {
                    fs.copyFileSync(
                      path.join(assetDir, f),
                      // nosemgrep: detect-non-literal-fs-filename,path-join-resolve-traversal — CDK synth-time paths, not user input
                      path.join(outputDir, f)
                    );
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
      });
    };

    const makeLambda = (
      logicalId: string,
      codePath: string,
      timeout: Duration,
      opts: { bundleDependencies?: boolean } = {}
    ) => {
      const fn = new lambda.Function(this, logicalId, {
        runtime: lambda.Runtime.PYTHON_3_12,
        code: opts.bundleDependencies ? bundledCode(codePath) : lambda.Code.fromAsset(codePath),
        handler: 'handler.handler',
        timeout,
        memorySize: 1024,
        architecture: lambda.Architecture.ARM_64,
        environment: commonEnv,
      });
      props.supplementaryDocsBucket.grantReadWrite(fn);
      props.metadataTable.grantReadWriteData(fn);
      return fn;
    };

    // The chunker is the one stage with a real third-party dependency (pypdf for
    // PDF text extraction), so its asset must be pip-bundled.
    this.chunkerFn = makeLambda(
      'ChunkerFn',
      '../lambda/doc-pipeline/chunker',
      Duration.minutes(5),
      {
        bundleDependencies: true,
      }
    );

    // NER Lambda — uses Anthropic Claude 3.5 Sonnet. Bedrock invoke permissions
    // are scoped to that foundation model; per-chunk failures degrade
    // gracefully (entities=[] + nerError) so downstream stages can continue.
    this.nerFn = makeLambda('NerFn', '../lambda/doc-pipeline/ner', Duration.minutes(15));
    this.nerFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-5-sonnet-20240620-v1:0`,
        ],
      })
    );

    // Embedder needs Bedrock invoke permissions for Titan v2.
    this.embedderFn = makeLambda(
      'EmbedderFn',
      '../lambda/doc-pipeline/embedder',
      Duration.minutes(10)
    );
    this.embedderFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
        ],
      })
    );

    this.linkerFn = makeLambda('LinkerFn', '../lambda/doc-pipeline/linker', Duration.minutes(5));

    // Rule-mining Lambda — extracts conditional business rules (conditions,
    // thresholds, exceptions) from each chunk. Runs AFTER the linker because it
    // binds rule roles to the `linkedClass` IRIs the linker resolves, and BEFORE
    // the indexer so KB ingestion stays last. Mined rules are written to S3 as
    // candidates; an admin imports them as DRAFTs via the REST API, so this
    // Lambda needs no Neptune access. 15 minutes matches the NER stage — one
    // Bedrock call per chunk is the same cost shape.
    this.ruleMinerFn = makeLambda(
      'RuleMinerFn',
      '../lambda/doc-pipeline/ruleminer',
      Duration.minutes(15)
    );
    this.ruleMinerFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        // The handler defaults to global.anthropic.claude-sonnet-5, which
        // resolves through a cross-region inference profile — both the profile
        // and the underlying foundation model must be permitted, or the invoke
        // is denied at the profile. Keep this in step with the handler's
        // RULE_MINER_MODEL_ID default: a mismatch denies every chunk, and the
        // fail-soft handler swallows that into `ruleMinerError` (see
        // __tests__/doc-pipeline-ruleminer.test.ts, which asserts the pair).
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-sonnet-5*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-sonnet-5*`,
        ],
      })
    );

    this.indexerFn = makeLambda('IndexerFn', '../lambda/doc-pipeline/indexer', Duration.minutes(5));
    if (props.supplementaryDocsKbId) {
      this.indexerFn.addToRolePolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:StartIngestionJob'],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.supplementaryDocsKbId}`,
          ],
        })
      );
    }

    // ---- Step Functions definition --------------------------------------

    const chunkTask = new tasks.LambdaInvoke(this, 'Chunk', {
      lambdaFunction: this.chunkerFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    const nerTask = new tasks.LambdaInvoke(this, 'Ner', {
      lambdaFunction: this.nerFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    const embedTask = new tasks.LambdaInvoke(this, 'Embed', {
      lambdaFunction: this.embedderFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    const linkTask = new tasks.LambdaInvoke(this, 'Link', {
      lambdaFunction: this.linkerFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    const ruleMinerTask = new tasks.LambdaInvoke(this, 'RuleMiner', {
      lambdaFunction: this.ruleMinerFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });
    const indexTask = new tasks.LambdaInvoke(this, 'Index', {
      lambdaFunction: this.indexerFn,
      outputPath: '$.Payload',
      retryOnServiceExceptions: true,
    });

    // Each task gets a backoff retry: failures (Bedrock throttle, KB
    // ingestion races) recover on retry. Permanent errors (bad input)
    // surface via the state machine's `Errors` for the document status row.
    const retryConfig = {
      errors: ['States.ALL'],
      interval: Duration.seconds(5),
      maxAttempts: 3,
      backoffRate: 2.0,
    };
    [chunkTask, nerTask, embedTask, linkTask, ruleMinerTask, indexTask].forEach((t) =>
      t.addRetry(retryConfig)
    );

    const definition = chunkTask
      .next(nerTask)
      .next(embedTask)
      .next(linkTask)
      .next(ruleMinerTask)
      .next(indexTask)
      .next(new sfn.Succeed(this, 'PipelineComplete'));

    const stateMachineLogGroup = new logs.LogGroup(this, 'DocPipelineLogs', {
      logGroupName: `/aws/states/${id}-doc-pipeline`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.stateMachine = new sfn.StateMachine(this, 'DocPipelineSm', {
      stateMachineName: `${id}-doc-pipeline`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      stateMachineType: sfn.StateMachineType.STANDARD,
      timeout: Duration.minutes(30),
      tracingEnabled: true,
      logs: {
        destination: stateMachineLogGroup,
        level: sfn.LogLevel.ALL,
        includeExecutionData: true,
      },
    });

    // Step Functions invokes each Lambda — express the grants once.
    [
      this.chunkerFn,
      this.nerFn,
      this.embedderFn,
      this.linkerFn,
      this.ruleMinerFn,
      this.indexerFn,
    ].forEach((fn) => fn.grantInvoke(this.stateMachine));

    new CfnOutput(this, 'DocPipelineStateMachineArn', {
      value: this.stateMachine.stateMachineArn,
      exportName: `${id}-doc-pipeline-sm-arn`,
    });

    // cdk-nag suppressions: Step Functions auto-generates lambda:Invoke
    // grants with the standard `:*` suffix on the function ARN. This is
    // the canonical AWS-recommended pattern; we accept it.
    NagSuppressions.addResourceSuppressions(
      this.stateMachine,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'Step Functions auto-generates lambda:Invoke grants with `:*` ARN suffixes for the four pipeline Lambdas; the wildcard is scoped to the Lambda version namespace.',
        },
      ],
      true
    );

    // Each pipeline Lambda's basic execution role uses the AWS-managed
    // policy AWSLambdaBasicExecutionRole — accepted standard.
    [
      this.chunkerFn,
      this.nerFn,
      this.embedderFn,
      this.linkerFn,
      this.ruleMinerFn,
      this.indexerFn,
    ].forEach((fn) => {
      NagSuppressions.addResourceSuppressions(
        fn,
        [
          {
            id: 'AwsSolutions-IAM4',
            reason: 'AWS-managed AWSLambdaBasicExecutionRole is the standard for CloudWatch Logs.',
          },
          {
            id: 'AwsSolutions-IAM5',
            reason:
              'Pipeline Lambda has read/write across the supplementary-docs S3 prefix per ontology; bucket policy limits scope.',
          },
          {
            id: 'AwsSolutions-L1',
            reason:
              'Python 3.12 is the latest stable runtime targeted by the project (matches existing agent and Lambda functions).',
          },
        ],
        true
      );
    });
  }
}
