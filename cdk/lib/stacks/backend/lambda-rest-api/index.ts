import {
  StackProps,
  Duration,
  CfnOutput,
  RemovalPolicy,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_logs as logs,
  aws_apigatewayv2 as apigw,
  aws_s3 as s3,
  aws_dynamodb as dynamodb,
  aws_lakeformation as lakeformation,
  aws_wafv2 as wafv2,
} from 'aws-cdk-lib';
import { HttpJwtAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { NagSuppressions } from 'cdk-nag';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { CommonStack } from '../../../common/constructs/stack';
import { ArmBuildConstruct } from '../../../common/constructs/arm-build-construct';
import { MCP_INVOKE_SCOPE } from '../../../common/auth-constants';
import * as path from 'path';
import * as fs from 'fs';

export interface LambdaRestApiStackProps extends StackProps {
  region: string;
  stage: string;
  projectId: string;
  userPool: cognito.IUserPool;
  userPoolClient: cognito.IUserPoolClient;
  identityPoolId: string;
  // S3 buckets for data access
  artifactsBucket: s3.IBucket;
  // CloudFront domain is now stored in Secrets Manager and read by Lambda at runtime
  cloudFrontHeaderSecret?: string; // Optional: Custom header secret for CloudFront verification
  // Ontology metadata table (required for semantic layer operations)
  ontologyMetadataTable?: dynamodb.ITable;
  /** Chat sessions table for AG-UI multi-turn chat (item #1). Optional for
   *  backwards-compat with environments that haven't redeployed the dynamodb stack. */
  chatSessionsTable?: dynamodb.ITable;
  /** Governed-metrics table (Tier 1 progressive disclosure). The REST API
   *  authors metric definitions; the agent runtime reads them at query
   *  time. Optional for backwards-compat. */
  metricsTable?: dynamodb.ITable;
  /** Per-turn user feedback (👍/👎 + comment) for the admin "Feedback" tab.
   *  When present the REST API persists feedback writes here (PII-redacted
   *  via Bedrock Guardrails) instead of into AgentCore Memory. */
  feedbackTable?: dynamodb.ITable;
  /** AgentCore Memory id for lessons-learned (item #2). The REST API uses
   *  this to list / delete long-term records via the bedrock-agentcore data
   *  plane. Optional — when missing the lessons endpoints return ``[]``. */
  lessonsMemoryId?: string;
  /** OBO identity passthrough toggle (item #4). When false, the identity
   *  service short-circuits and existing service-identity behaviour is kept. */
  enableOboPassthrough?: boolean;
  // AgentCore Runtime ARNs
  ontologyRuntimeArn?: string; // Ontology generation agent
  queryRuntimeArn?: string; // Semantic query agent
  metadataRuntimeArn?: string; // Metadata generation agent
  metadataQueryRuntimeArn?: string; // Metadata query agent
  suggestionsRuntimeArn?: string; // Query suggestions agent
  // OAuth (M2M client_credentials) for invoking JWT-inbound runtimes over HTTPS.
  // Replaces the prior SigV4 invoke_agent_runtime path.
  m2mClientId?: string;
  m2mClientSecret?: secretsmanager.ISecret;
  oauthTokenEndpoint?: string;
  // AgentCore Gateway configuration
  neptuneGatewayUrl?: string; // Neptune Gateway URL (omit when ontology agents disabled)
  neptuneGatewayArn?: string; // Neptune Gateway ARN for IAM permissions (omit when ontology agents disabled)
  // Data Lake configuration for Lake Formation permissions
  glueNamespaceDynamoDB: string; // Glue DynamoDB database name (e.g., 'semantic_layer_dynamodb')
  // Lake Formation admin chain — must carry forward all prior LF admins when superseding
  // AgentCoreLFAdminSettings (CfnDataLakeSettings is a singleton per account/region)
  lfGrantSingletonRoleArn: string; // AwsCustomResource singleton role from DataLakeStack
  /** Human/SSO role ARNs that must retain LF admin status across CDK redeploys.
   *  Carried forward into LambdaApiLFAdminSettings (last-writer-wins). */
  additionalLakeFormationAdmins?: string[];
  // Bedrock Guardrails
  guardrailId?: string;
  guardrailVersion?: string;
  /** Explicit origins to allow for CORS preflight. Include CloudFront domain and localhost for dev. */
  allowedOrigins: string[];
  /** ARN of the regional WAF WebACL to associate with the API Gateway stage */
  regionalWebAclArn: string;
  /**
   * When false, FastAPI does not mount the /metadata sub-app. Defaults to false.
   */
  enableSemanticRag?: boolean;
  /** Doc-pipeline state machine ARN — when present, DocumentService starts
   *  one execution per upload. Optional so deployments without the
   *  doc-pipeline stack still get the upload+status path. */
  docPipelineStateMachineArn?: string;
}

export class LambdaRestApiStack extends CommonStack {
  public readonly restApiFunction: lambda.DockerImageFunction;
  public readonly httpApi: apigw.HttpApi;
  public readonly apiGatewayWebAclArn: string;
  public readonly cloudFrontHeaderSecret: string;

  constructor(scope: Construct, id: string, props: LambdaRestApiStackProps) {
    super(scope, id, props);

    // Use props passed from parent stack
    const projectName = props.projectId;
    const environment = props.stage;

    // 1. Create IAM role for Lambda execution
    const lambdaRole = new iam.Role(this, 'LambdaExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      inlinePolicies: {
        LambdaPolicy: new iam.PolicyDocument({
          statements: [
            // S3 access for artifacts
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
              resources: [props.artifactsBucket.bucketArn, `${props.artifactsBucket.bucketArn}/*`],
            }),

            // Secrets Manager access for CloudFront domain (for CORS configuration)
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:secret:/semantic-layer/cloudfront-domain*`,
              ],
            }),

            // Bedrock Guardrails — apply guardrail to user inputs and agent outputs
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['bedrock:ApplyGuardrail'],
              resources: [`arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`],
            }),

            // Titan v2 embeddings — required when authoring/publishing a metric;
            // the REST handler embeds the metric label/synonyms before writing
            // into the OpenSearch metrics index. No grant → 403 on every publish.
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['bedrock:InvokeModel'],
              resources: [
                `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-embed-text-v2:0`,
              ],
            }),

            // AgentCore Runtime access for ontology generation and query agents.
            // Skip the statement entirely if no runtime ARNs are provided — IAM rejects
            // identity-based policies whose Resource list is empty.
            ...(() => {
              const runtimeResources = [
                ...(props.ontologyRuntimeArn
                  ? [props.ontologyRuntimeArn, `${props.ontologyRuntimeArn}/*`]
                  : []),
                ...(props.queryRuntimeArn
                  ? [props.queryRuntimeArn, `${props.queryRuntimeArn}/*`]
                  : []),
                ...(props.metadataRuntimeArn
                  ? [props.metadataRuntimeArn, `${props.metadataRuntimeArn}/*`]
                  : []),
                ...(props.metadataQueryRuntimeArn
                  ? [props.metadataQueryRuntimeArn, `${props.metadataQueryRuntimeArn}/*`]
                  : []),
                ...(props.suggestionsRuntimeArn
                  ? [props.suggestionsRuntimeArn, `${props.suggestionsRuntimeArn}/*`]
                  : []),
              ];
              return runtimeResources.length > 0
                ? [
                    new iam.PolicyStatement({
                      effect: iam.Effect.ALLOW,
                      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
                      resources: runtimeResources,
                    }),
                  ]
                : [];
            })(),

            // AgentCore Gateway access for Neptune graph operations
            ...(props.neptuneGatewayArn
              ? [
                  new iam.PolicyStatement({
                    effect: iam.Effect.ALLOW,
                    actions: ['bedrock-agentcore:InvokeGateway'],
                    resources: [props.neptuneGatewayArn, `${props.neptuneGatewayArn}/*`],
                  }),
                ]
              : []),

            // DynamoDB access for ontology metadata
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: [
                'dynamodb:PutItem',
                'dynamodb:GetItem',
                'dynamodb:UpdateItem',
                'dynamodb:DeleteItem',
                'dynamodb:Query',
                'dynamodb:Scan',
              ],
              resources: [
                props.ontologyMetadataTable?.tableArn ||
                  `arn:aws:dynamodb:${this.region}:${this.account}:table/semantic-layer-metadata`,
              ],
            }),

            // CloudWatch metrics for chat (item #1) — namespace
            // SemanticLayer/Chat scoped via condition key.
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['cloudwatch:PutMetricData'],
              resources: ['*'],
              conditions: {
                StringEquals: {
                  'cloudwatch:namespace': 'SemanticLayer/Chat',
                },
              },
            }),

            // AWS Glue access for data catalog operations (including federated catalogs)
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: [
                'glue:GetCatalog',
                'glue:GetCatalogs',
                'glue:GetDatabase',
                'glue:GetDatabases',
                'glue:GetTable',
                'glue:GetTables',
                'glue:GetPartition',
                'glue:GetPartitions',
                'glue:BatchGetPartition',
              ],
              resources: [
                `arn:aws:glue:${this.region}:${this.account}:catalog`,
                `arn:aws:glue:${this.region}:${this.account}:catalog/*`,
                `arn:aws:glue:${this.region}:${this.account}:database/*`,
                `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
              ],
            }),

            // Lake Formation credential vending for federated catalogs (S3 Tables via s3tablescatalog).
            // When GetDatabases/GetTables is called on a federated catalog, Glue asks Lake Formation
            // to vend temporary credentials to the calling principal — this requires GetDataAccess.
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['lakeformation:GetDataAccess'],
              resources: ['*'],
            }),

            // S3 Tables: list table buckets and tables via the dedicated s3tables API.
            // Glue's GetTables cannot enumerate S3 Tables — the s3tables client is required.
            // Iceberg REST endpoint action mapping (per AWS docs):
            //   loadTable  (GET .../tables/{table}) → s3tables:GetTableMetadataLocation + s3tables:GetTableData
            // GetTableData is required for _enrich_with_iceberg() to call catalog.load_table()
            // via the S3 Tables Iceberg REST endpoint (SigV4-signed). Without it, the REST
            // catalog returns IrcForbiddenResponse and Iceberg enrichment silently returns None.
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: [
                's3tables:ListTableBuckets',
                's3tables:ListTables',
                's3tables:GetTable',
                's3tables:GetTableMetadataLocation',
                's3tables:GetTableData', // required for loadTable REST operation (Iceberg enrichment)
              ],
              resources: [`arn:aws:s3tables:${this.region}:${this.account}:bucket/*`],
            }),

            // Self-invocation: Lambda invokes itself asynchronously to process long-running
            // AgentCore queries (avoids the 30-second API Gateway HTTP integration timeout).
            // ARN is constructed directly (not via grantInvoke) to avoid a circular CFN dependency:
            //   LambdaExecutionRoleDefaultPolicy → RestApiFunction → LambdaExecutionRole → LambdaExecutionRoleDefaultPolicy
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['lambda:InvokeFunction'],
              resources: [
                `arn:aws:lambda:${this.region}:${this.account}:function:${this.stackName}-rest-api`,
              ],
            }),
          ],
        }),
      },
    });

    // Grant Lake Formation permissions to Lambda REST API role — dynamodb database
    new lakeformation.CfnPermissions(this, 'LambdaLFDynamoDBDatabasePermissions', {
      dataLakePrincipal: {
        dataLakePrincipalIdentifier: lambdaRole.roleArn,
      },
      resource: {
        databaseResource: {
          name: props.glueNamespaceDynamoDB,
        },
      },
      permissions: ['DESCRIBE'],
    });

    new lakeformation.CfnPermissions(this, 'LambdaLFDynamoDBTablePermissions', {
      dataLakePrincipal: {
        dataLakePrincipalIdentifier: lambdaRole.roleArn,
      },
      resource: {
        tableResource: {
          databaseName: props.glueNamespaceDynamoDB,
          tableWildcard: {},
        },
      },
      permissions: ['SELECT', 'DESCRIBE'],
    });

    // S3 Tables access is controlled via IAM permissions and LF admin status below.
    // Lake Formation CfnPermissions do NOT work with S3 Tables namespaces as they are not
    // traditional Glue databases. Attempting to grant LF permissions on S3 Tables namespaces
    // results in "Database not found" errors.

    // Lake Formation: register Lambda REST API role as LF admin for s3tablescatalog access.
    //
    // The Lambda lists all Glue databases and tables (via GlueService) including
    // those served through the s3tablescatalog federated catalog (S3 Tables / Iceberg).
    // lakeformation:GrantPermissions on federated sub-catalog paths fails via
    // AwsCustomResource; the LF admin pattern is the only working workaround.
    //
    // This supersedes AgentCoreLFAdminSettings — ALL prior admins must be carried forward.
    new lakeformation.CfnDataLakeSettings(this, 'LambdaApiLFAdminSettings', {
      admins: [
        // CDK bootstrap roles (must be preserved)
        {
          dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}`,
        },
        {
          dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}`,
        },
        // AwsCustomResource singleton role (DataLakeStack)
        { dataLakePrincipalIdentifier: props.lfGrantSingletonRoleArn },
        // Lambda REST API role — lists all Glue databases/tables including s3tablescatalog
        { dataLakePrincipalIdentifier: lambdaRole.roleArn },
        // Human/SSO admin roles forwarded from app.ts
        ...(props.additionalLakeFormationAdmins ?? []).map((arn) => ({
          dataLakePrincipalIdentifier: arn,
        })),
      ],
    });

    // F2 fix (progressive disclosure): mirror agents/shared/ into the
    // REST API source tree at synth time so the metric router can import
    // from agents.shared. The build construct only zips lambda/rest-api,
    // so without this copy the Lambda would crash on import. Idempotent —
    // re-syncs every synth, ignores files that haven't changed.
    const sharedSrc = path.resolve(__dirname, '../../../../../agents/shared');
    const sharedDst = path.resolve(__dirname, '../../../../../lambda/rest-api/agents/shared');
    if (fs.existsSync(sharedSrc)) {
      fs.mkdirSync(sharedDst, { recursive: true });
      fs.cpSync(sharedSrc, sharedDst, { recursive: true });
    }

    // 2. Build Lambda Docker image using dedicated CodeBuild project with native ARM64 compute
    // This avoids cross-compilation issues and ECR Public rate limits during local builds
    const lambdaBuild = new ArmBuildConstruct(this, 'LambdaArmBuild', {
      sourcePath: '../lambda/rest-api',
      region: props.region,
      namePrefix: `${projectName}-lambda-${environment}`,
      buildTimeoutMinutes: 20,
    });

    // 3. Create Lambda function from pre-built ECR image
    // Use hash-based tag to ensure Lambda updates when code changes
    // The imageTag is calculated from source code hash, ensuring updates are detected
    this.restApiFunction = new lambda.DockerImageFunction(this, 'RestApiFunction', {
      functionName: `${this.stackName}-rest-api`,
      code: lambda.DockerImageCode.fromEcr(lambdaBuild.repository, {
        tagOrDigest: lambdaBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: Duration.seconds(900), // 15 minutes for long-running operations
      role: lambdaRole,
      environment: {
        ARTIFACTS_BUCKET: props.artifactsBucket.bucketName,
        // Cognito configuration for JWT validation and Identity Pool credentials
        COGNITO_USER_POOL_ID: props.userPool.userPoolId,
        COGNITO_APP_CLIENT_ID: props.userPoolClient.userPoolClientId,
        COGNITO_IDENTITY_POOL_ID: props.identityPoolId,
        // CORS configuration - CloudFront domain read from Secrets Manager
        CLOUDFRONT_DOMAIN_SECRET_NAME: '/semantic-layer/cloudfront-domain',
        // DynamoDB configuration for ontology metadata
        ONTOLOGY_METADATA_TABLE:
          props.ontologyMetadataTable?.tableName || 'semantic-layer-metadata',
        // Chat sessions table for AG-UI multi-turn chat (empty string when feature
        // hasn't been wired in older deployments)
        CHAT_SESSIONS_TABLE: props.chatSessionsTable?.tableName ?? '',
        // Per-turn user feedback table (item #2). Empty string when not wired
        // — the feedback endpoint short-circuits with 503 in that case.
        FEEDBACK_TABLE: props.feedbackTable?.tableName ?? '',
        // Lessons-learned long-term memory id (item #2). The REST API
        // consumes this via ``AgentCoreMemoryService`` for list/delete only;
        // writes happen turn-by-turn inside the agent runtime hook.
        LESSONS_MEMORY_ID: props.lessonsMemoryId ?? '',
        // OBO identity passthrough (item #4) — Lambda's IdentityService
        // checks this flag and short-circuits when off.
        ENABLE_OBO_PASSTHROUGH: String(props.enableOboPassthrough === true),
        // AgentCore Runtime ARNs
        ONTOLOGY_RUNTIME_ARN: props.ontologyRuntimeArn || '',
        QUERY_RUNTIME_ARN: props.queryRuntimeArn || '',
        METADATA_RUNTIME_ARN: props.metadataRuntimeArn || '',
        METADATA_QUERY_RUNTIME_ARN: props.metadataQueryRuntimeArn || '',
        SUGGESTIONS_RUNTIME_ARN: props.suggestionsRuntimeArn || '',
        // OAuth (M2M client_credentials) for invoking JWT-inbound runtimes.
        OAUTH_TOKEN_ENDPOINT: props.oauthTokenEndpoint || '',
        OAUTH_SCOPE: MCP_INVOKE_SCOPE,
        M2M_CLIENT_ID: props.m2mClientId || '',
        M2M_CLIENT_SECRET_ARN: props.m2mClientSecret?.secretArn || '',
        // Neptune Gateway configuration (IAM authenticated)
        NEPTUNE_GATEWAY_URL: props.neptuneGatewayUrl ?? '',
        // Bedrock Guardrails
        GUARDRAIL_IDENTIFIER: props.guardrailId ?? '',
        GUARDRAIL_VERSION: props.guardrailVersion ?? '',
        // Capability flag — FastAPI mounts /metadata sub-app only when true
        ENABLE_SEMANTIC_RAG: String(props.enableSemanticRag === true),
        // Doc-pipeline state machine ARN. DocumentService.upload_document
        // calls states:StartExecution against this ARN after persisting
        // the doc + status row. Empty string short-circuits.
        DOC_PIPELINE_STATE_MACHINE_ARN: props.docPipelineStateMachineArn ?? '',
        // Governed-metrics (Tier 1 progressive disclosure). The metrics router
        // mounts when METRICS_TABLE is non-empty; the embedding for each metric
        // is persisted on the DDB row at publish time and rehydrated into a
        // per-namespace in-memory KNN index by the agent runtime on first
        // lookup, so no external KNN endpoint is required.
        METRICS_TABLE: props.metricsTable?.tableName ?? '',
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // NOTE on chat SSE transport:
    //
    // We considered fronting /query/chat with a Lambda Function URL using
    // InvokeMode=RESPONSE_STREAM to bypass API Gateway's response buffering.
    // That broke at the auth layer: the frontend authenticates with Cognito
    // bearer tokens (Authorization: Bearer <jwt>), but a Function URL with
    // AWS_IAM auth requires SigV4-signed requests — the bearer header is
    // ignored and every request 403s. The alternative (NONE auth) would
    // expose an unauthenticated SSE endpoint.
    //
    // Resolution: keep chat SSE on the API Gateway HTTP API path. With
    // sse-starlette the events arrive in chunks; if buffering becomes a
    // measurable UX issue, the options are Cognito-Identity-Pool SigV4 signing on
    // the frontend or fronting the chat endpoint with CloudFront + Lambda@Edge.

    // Grant Lambda permission to pull from the build ECR repository
    lambdaBuild.repository.grantPull(lambdaRole);

    // Grant the REST Lambda read on the M2M client secret so it can mint the
    // OAuth Bearer token used to invoke the JWT-inbound runtimes.
    if (props.m2mClientSecret) {
      props.m2mClientSecret.grantRead(lambdaRole);
    }

    // Grant Lambda permissions to DynamoDB table
    if (props.ontologyMetadataTable) {
      props.ontologyMetadataTable.grantReadWriteData(lambdaRole);
    }

    // Grant Lambda read/write on the chat sessions table (when wired)
    if (props.chatSessionsTable) {
      props.chatSessionsTable.grantReadWriteData(lambdaRole);
    }
    // Grant Lambda read/write on the governed-metrics table (Tier 1 authoring)
    if (props.metricsTable) {
      props.metricsTable.grantReadWriteData(lambdaRole);
    }
    // Per-turn user feedback table — list/insert/delete from the admin tab
    // and the chat feedback endpoint.
    if (props.feedbackTable) {
      props.feedbackTable.grantReadWriteData(lambdaRole);
    }
    // Bedrock Guardrails: feedback comments + agent turns are PII-redacted
    // through ApplyGuardrail before persistence. Scoped to the configured
    // guardrail when present; falls back to '*' so environments without a
    // guardrail provisioned still pass through (the service checks `enabled`
    // and short-circuits).
    if (props.guardrailId) {
      lambdaRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:ApplyGuardrail'],
          resources: ['*'],
        })
      );
    }
    // Lessons-learned: read/delete on the AgentCore Memory data plane.
    // The memory id itself is stored as plain text (not an ARN), so we
    // grant the API at the account-level memory-record actions.
    //
    // GetMemoryRecord + ListEvents + DeleteEvent back the durable delete:
    // deleting a long-term record alone lets the SEMANTIC strategy re-extract
    // the same lesson from the retained conversation events, so delete_record
    // resolves the record's namespace (GetMemoryRecord) and also deletes the
    // session's source events (ListEvents + DeleteEvent). See
    // services/agentcore_memory_service.delete_record.
    if (props.lessonsMemoryId) {
      lambdaRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:ListMemoryRecords',
            'bedrock-agentcore:GetMemoryRecord',
            'bedrock-agentcore:DeleteMemoryRecord',
            'bedrock-agentcore:ListEvents',
            'bedrock-agentcore:DeleteEvent',
          ],
          resources: ['*'],
        })
      );
    }

    // Grant Lambda permission to start doc-pipeline executions, scoped to
    // the specific state machine ARN. Wired through cdk/bin/app.ts.
    if (props.docPipelineStateMachineArn) {
      lambdaRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['states:StartExecution'],
          resources: [props.docPipelineStateMachineArn],
        })
      );
    }

    // Ensure Lambda is created after the build completes
    // This is critical: Lambda must wait for the custom resource to finish building the image
    this.restApiFunction.node.addDependency(lambdaBuild.buildCompletion);

    // 4. Create CloudWatch Log Group for API Gateway access logs
    const apiLogGroup = new logs.LogGroup(this, 'ApiGatewayAccessLogs', {
      logGroupName: `/aws/apigateway/${this.stackName}-rest-api`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // 5. Create HTTP API Gateway
    this.httpApi = new apigw.HttpApi(this, 'RestHttpApi', {
      apiName: `${this.stackName}-rest-api`,
      description: 'REST API for Interview Assistant (serverless)',
      corsPreflight: {
        // Explicitly enumerate allowed origins — never use wildcard in production
        allowOrigins: props.allowedOrigins,
        allowMethods: [
          apigw.CorsHttpMethod.GET,
          apigw.CorsHttpMethod.POST,
          apigw.CorsHttpMethod.PUT,
          apigw.CorsHttpMethod.DELETE,
          apigw.CorsHttpMethod.OPTIONS,
        ],
        // Enumerate required headers explicitly — do not use wildcard
        allowHeaders: [
          'Authorization',
          'Content-Type',
          'X-Amz-Date',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
        allowCredentials: true,
      },
    });

    // Configure access logging and throttling on the default stage
    const defaultStage = this.httpApi.defaultStage?.node.defaultChild as apigw.CfnStage;
    if (defaultStage) {
      // Stage-level throttle — secondary defense behind WAF rate-based rules
      defaultStage.defaultRouteSettings = {
        throttlingRateLimit: 1000, // sustained requests per second
        throttlingBurstLimit: 2000, // token bucket burst capacity
      };
      defaultStage.accessLogSettings = {
        destinationArn: apiLogGroup.logGroupArn,
        format: JSON.stringify({
          requestId: '$context.requestId',
          ip: '$context.identity.sourceIp',
          caller: '$context.identity.caller',
          user: '$context.identity.user',
          requestTime: '$context.requestTime',
          httpMethod: '$context.httpMethod',
          resourcePath: '$context.resourcePath',
          status: '$context.status',
          protocol: '$context.protocol',
          responseLength: '$context.responseLength',
          errorMessage: '$context.error.message',
          errorType: '$context.error.messageString',
          integrationErrorMessage: '$context.integrationErrorMessage',
        }),
      };
    }

    // Generate or use provided CloudFront header secret for origin verification
    this.cloudFrontHeaderSecret =
      props.cloudFrontHeaderSecret || `cf-secret-${this.account}-${Date.now()}`;

    // NOTE: WAFv2 AssociateWebACL does not support HTTP API v2 $default stages.
    // WAF protection is provided at the CloudFront layer via the cloudFrontHeaderSecret.
    this.apiGatewayWebAclArn = props.regionalWebAclArn;

    // 6. Create JWT Authorizer
    const authorizer = new HttpJwtAuthorizer(
      'CognitoAuthorizer',
      `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}`,
      {
        jwtAudience: [props.userPoolClient.userPoolClientId],
      }
    );

    // 7. Create Lambda integration
    const lambdaIntegration = new HttpLambdaIntegration('LambdaIntegration', this.restApiFunction);

    // 8. Add routes
    // IMPORTANT: Add OPTIONS routes WITHOUT authorizer first (for CORS preflight)
    // Then add authenticated routes for other methods.
    //
    // The frontend calls the execute-api URL directly via REACT_APP_API_URL
    // (see frontend/src/services/api.js). There is no /api/* prefix routing.
    //
    // Single catch-all route. FastAPI's main.py mounts every sub-app
    // (/ontology, /datasource, /query, /neptune, /metadata, /lessons,
    // /documents, /status), and the Lambda is the sole integration target,
    // so a single `/{proxy+}` matches all of them. This shape uses only 2
    // AWS::Lambda::Permission statements (OPTIONS + ANY) regardless of how
    // many sub-apps mount — essential because each per-route permission counts
    // against Lambda's 20KB resource-policy limit (~43 statements is enough to
    // exceed it).
    const directRoutes: string[] = ['/{proxy+}'];

    directRoutes.forEach((path) => {
      // OPTIONS without auth (CORS preflight).
      this.httpApi.addRoutes({
        path,
        methods: [apigw.HttpMethod.OPTIONS],
        integration: lambdaIntegration,
      });

      // ANY (= GET/POST/PUT/DELETE/PATCH/HEAD) with auth. Using ANY instead
      // of an explicit per-method list collapses 5 AWS::Lambda::Permission
      // entries into 1 per route, which is required to keep Lambda's 20KB
      // resource-policy budget under cap with this many sub-apps.
      this.httpApi.addRoutes({
        path,
        methods: [apigw.HttpMethod.ANY],
        integration: lambdaIntegration,
        authorizer,
      });
    });

    // Health check (no auth)
    const healthRoute = this.httpApi.addRoutes({
      path: '/health',
      methods: [apigw.HttpMethod.GET],
      integration: lambdaIntegration,
    });

    // Suppress auth warning for health check endpoint
    NagSuppressions.addResourceSuppressions(
      healthRoute,
      [
        {
          id: 'AwsSolutions-APIG4',
          reason:
            'Health check endpoint intentionally has no authorization for monitoring purposes',
        },
      ],
      true
    );

    // Outputs
    new CfnOutput(this, 'RestApiUrl', {
      value: this.httpApi.apiEndpoint,
      description: 'REST API Gateway endpoint URL',
    });

    new CfnOutput(this, 'RestApiFunctionName', {
      value: this.restApiFunction.functionName,
      description: 'Lambda function name',
    });

    new CfnOutput(this, 'RestApiFunctionArn', {
      value: this.restApiFunction.functionArn,
      description: 'Lambda function ARN',
    });

    new CfnOutput(this, 'ApiGatewayLogGroup', {
      value: apiLogGroup.logGroupName,
      description: 'API Gateway access logs CloudWatch Log Group',
    });

    // Note: API Gateway regional WAF output removed - HTTP APIs don't support WAF association
    // CloudFront WAF provides protection (configured in storage stack)

    // NOTE: cloudFrontHeaderSecret value is intentionally NOT output to CFN outputs —
    // outputting a plaintext secret value in CFN is a security vulnerability.
    // Retrieve the value from the CDK app's cross-stack reference or store in Secrets Manager.

    // Nag Suppressions - Access logging is now enabled
    // NagSuppressions removed as AwsSolutions-APIG1 is now satisfied

    // Suppress APIG4 (authorization) for OPTIONS routes: CORS preflight requests are by design public
    // and do not require authorization. The actual POST/GET/etc methods have JWT authorization via Cognito.
    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-APIG4',
        reason:
          'OPTIONS routes are CORS preflight requests that must be publicly accessible; actual data routes (POST/GET/etc) enforce JWT authorization via Cognito User Pool',
      },
    ]);
  }
}
