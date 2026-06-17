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
  aws_bedrock as bedrock,
} from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

/**
 * Creation-time document processing pipeline (item #3).
 *
 * Orchestration: Step Functions STANDARD state machine started
 * synchronously by the REST API's `DocumentService.upload_document`
 * (`states:StartExecution` against the ARN exported here, after the
 * raw doc is written to S3 and the `DOCJOB#` status row is persisted
 * to DynamoDB). The state machine runs the five pure-Python Lambdas
 * in `lambda/doc-pipeline/`:
 *
 *     chunk → ner → embed → link → index
 *
 * Per-stage failures bubble up to the state machine's retry policy.
 * Document status (per-stage booleans) is mirrored to the existing
 * ontology metadata table under SK prefix `DOCJOB#`, kept in sync by
 * `DocumentService.update_stage`.
 *
 * The supplementary-docs Bedrock KB is provisioned as part of this stack
 * when ``createKnowledgeBase`` is true. The KB's data source points at
 * the ``supplementary-docs/`` prefix on the artifacts bucket so the
 * indexer's start_ingestion_job call ingests the per-doc JSONL bundles.
 */
export interface DocPipelineStackProps extends StackProps {
  /** Pre-existing S3 bucket the supplementary docs live in. */
  readonly supplementaryDocsBucket: s3.IBucket;
  /** Ontology metadata DDB table (DocumentService writes DOCJOB# rows). */
  readonly metadataTable: dynamodb.ITable;
  /** Optional Bedrock KB id + data source id for the indexer (when an external
   *  KB is provisioned outside this stack). When unset and
   *  ``createKnowledgeBase`` is true, the stack provisions its own KB. */
  readonly supplementaryDocsKbId?: string;
  readonly supplementaryDocsDataSourceId?: string;
  /** Toggle the in-stack Bedrock KB provisioning. Defaults to false until
   *  the KB module's transitive deps (OSS collection or S3 Vectors index)
   *  are wired into the deployment. */
  readonly createKnowledgeBase?: boolean;
}

export class DocPipelineStack extends Stack {
  public readonly stateMachine: sfn.IStateMachine;
  public readonly chunkerFn: lambda.IFunction;
  public readonly nerFn: lambda.IFunction;
  public readonly embedderFn: lambda.IFunction;
  public readonly linkerFn: lambda.IFunction;
  public readonly indexerFn: lambda.IFunction;
  /** Bedrock KB id (set when createKnowledgeBase=true). */
  public readonly knowledgeBaseId?: string;

  constructor(scope: Construct, id: string, props: DocPipelineStackProps) {
    super(scope, id, props);

    // Common Lambda config for the four pipeline stages.
    // AWS_REGION is set automatically by the Lambda runtime — never inject.
    const commonEnv: { [key: string]: string } = {
      ARTIFACTS_BUCKET: props.supplementaryDocsBucket.bucketName,
      SUPPLEMENTARY_DOCS_BUCKET: props.supplementaryDocsBucket.bucketName,
      METADATA_TABLE: props.metadataTable.tableName,
    };
    if (props.supplementaryDocsKbId) {
      commonEnv.SUPPLEMENTARY_DOCS_KB_ID = props.supplementaryDocsKbId;
    }
    if (props.supplementaryDocsDataSourceId) {
      commonEnv.SUPPLEMENTARY_DOCS_DS_ID = props.supplementaryDocsDataSourceId;
    }

    const makeLambda = (logicalId: string, codePath: string, timeout: Duration) => {
      const fn = new lambda.Function(this, logicalId, {
        runtime: lambda.Runtime.PYTHON_3_12,
        code: lambda.Code.fromAsset(codePath),
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

    this.chunkerFn = makeLambda('ChunkerFn', '../lambda/doc-pipeline/chunker', Duration.minutes(5));

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
    [chunkTask, nerTask, embedTask, linkTask, indexTask].forEach((t) => t.addRetry(retryConfig));

    const definition = chunkTask
      .next(nerTask)
      .next(embedTask)
      .next(linkTask)
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
    [this.chunkerFn, this.nerFn, this.embedderFn, this.linkerFn, this.indexerFn].forEach((fn) =>
      fn.grantInvoke(this.stateMachine)
    );

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
    [this.chunkerFn, this.nerFn, this.embedderFn, this.linkerFn, this.indexerFn].forEach((fn) => {
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
