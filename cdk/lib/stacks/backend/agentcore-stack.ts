import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import { ArmBuildConstruct } from '../../common/constructs/arm-build-construct';
import { AgentCoreNeptuneGateway } from './agentcore/neptune-gateway-construct';
import { runtimeFingerprint } from './runtime-fingerprint';
import { NeptuneStack } from './neptune-stack';
import { BedrockKnowledgeBaseStack } from './bedrock-kb-stack';
import { GlueCatalogStack } from './glue-catalog-stack';
import { DynamoDBStack } from './dynamodb-stack';
import { AthenaStack } from './athena-stack';
import { DataLakeStack } from './data-lake-stack';

export interface AgentCoreStackProps extends cdk.StackProps {
  projectName: string;
  vpc: ec2.Vpc;
  neptuneStack?: NeptuneStack;
  bedrockKbStack: BedrockKnowledgeBaseStack;
  glueCatalogStack: GlueCatalogStack;
  dynamodbStack: DynamoDBStack;
  athenaStack: AthenaStack;
  dataLakeStack: DataLakeStack;
  /** Human/SSO role ARNs that must retain LF admin status across CDK redeploys.
   *  Carried forward into AgentCoreLFAdminSettings (last-writer-wins). */
  additionalLakeFormationAdmins?: string[];
  /** When true (enableBatchReplication=true), grant SELECT on the 'normalized' S3 Tables
   *  namespace to all query agent roles so Athena SQL against normalized.* tables succeeds. */
  normalizedViewsEnabled?: boolean;
  /** When true (enableRealtimeReplication=true), create LF grants for the
   *  semantic_layer_iceberg namespace.  When false (Zero-ETL / batch mode),
   *  that namespace is never created so these grants would fail. Default: true. */
  enableRealtimeReplication?: boolean;
  /**
   * When false, the metadata, metadata-query, and query-suggestions runtimes
   * (and their IAM roles) are not provisioned.
   * @default false
   */
  enableSemanticRag?: boolean;
  /** AgentCore Memory id for lessons-learned (item #2). Threaded into every
   *  runtime as ``LESSONS_MEMORY_ID``; the Strands ``LessonsMemoryHooks``
   *  provider PII-redacts and writes turns into this memory. Optional —
   *  when missing the hook short-circuits and no lessons are persisted. */
  lessonsMemoryId?: string;
  /** Bedrock Guardrail id, version. Threaded into every runtime so the
   *  PII-redaction hook can call ``ApplyGuardrail`` before any memory write. */
  guardrailId?: string;
  guardrailVersion?: string;
  /** Cognito user pool + app client for the chat query runtimes' JWT inbound
   *  authorizer. The chat gateway (CUSTOM_JWT) validates the browser's access
   *  token, then forwards it to the runtime target via JWT_PASSTHROUGH; the
   *  runtime re-validates against this same pool/client and decodes the `sub`
   *  for chat-session ownership. Optional — when absent the runtimes fall back
   *  to IAM inbound auth (chat history then persists under 'anonymous'). */
  userPool?: cognito.IUserPool;
  userPoolClient?: cognito.IUserPoolClient;
  /** Confidential M2M client (client_credentials). Backend callers (mcp-tools
   *  Lambda, REST generation jobs) invoke runtimes with this client's token, so
   *  every runtime's JWT authorizer must accept it. The chat-query runtimes
   *  accept BOTH this and `userPoolClient` (user token via gateway passthrough);
   *  the generation/suggestions runtimes accept only this (no user context). */
  m2mClient?: cognito.IUserPoolClient;
}

/**
 * Read `agents/<name>/models.json` and translate the declared foundation
 * model ids into Bedrock IAM resource ARNs (regional + regionless +
 * inference-profile shapes covering both literal anthropic.* ids and
 * cross-region inference profiles like `global.anthropic.*`).
 *
 * This is the single source of truth that closes the gap that caused the
 * `MetadataQueryAgentRole` AccessDenied on `amazon.titan-embed-text-v2:0` —
 * the agent's `models.json` declares what it invokes, and CDK derives the
 * grant from that file rather than from a hand-maintained list.
 */
function bedrockInvokeModelResources(agentName: string, region: string, account: string): string[] {
  const manifestPath = path.resolve(
    // nosemgrep: path-join-resolve-traversal,detect-non-literal-fs-filename — agentName is from this.agentNames (controlled internal list), CDK synth-time only
    __dirname,
    '..',
    '..',
    '..',
    '..',
    'agents',
    // nosemgrep: path-join-resolve-traversal — static repo path constants, build-time, not request input
    agentName,
    'models.json'
  );
  // nosemgrep: detect-non-literal-fs-filename — CDK build dir / static repo path, not user input
  if (!fs.existsSync(manifestPath)) {
    throw new Error(
      `Missing model manifest at ${manifestPath}. ` +
        `Add it (see agents/metadata_query_agent/models.json for the schema) — ` +
        `its absence means CDK has no way to derive bedrock:InvokeModel grants.`
    );
  }
  // nosemgrep: detect-non-literal-fs-filename — CDK build dir / static repo path, not user input
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8')) as {
    // nosemgrep: detect-non-literal-fs-filename — manifestPath resolved from controlled agentName list above
    foundation_models?: string[];
  };
  const models = manifest.foundation_models ?? [];
  if (models.length === 0) {
    throw new Error(
      `agents/${agentName}/models.json declares no foundation_models. ` +
        `If this agent does not call bedrock:InvokeModel, do not call ` +
        `bedrockInvokeModelResources() for it.`
    );
  }

  const resources: string[] = [];
  for (const id of models) {
    if (
      id.startsWith('global.') ||
      id.startsWith('us.') ||
      id.startsWith('eu.') ||
      id.startsWith('apac.')
    ) {
      // Cross-region inference profile id like `global.anthropic.claude-sonnet-5`.
      // Both the inference-profile ARN and the underlying foundation-model ARN
      // need to be allowed — Bedrock validates the caller against the profile
      // ARN, but the routed model ARN is also evaluated.
      const bareId = id.replace(/^(?:global|us|eu|apac)\./, '');
      resources.push(
        `arn:aws:bedrock:${region}:${account}:inference-profile/${id}`,
        `arn:aws:bedrock:${region}::foundation-model/${bareId}`,
        `arn:aws:bedrock:::foundation-model/${bareId}`
      );
    } else {
      // Direct foundation-model id like `amazon.titan-embed-text-v2:0`
      // or `anthropic.claude-3-5-haiku-20241022-v1:0`.
      resources.push(
        `arn:aws:bedrock:${region}::foundation-model/${id}`,
        `arn:aws:bedrock:::foundation-model/${id}`
      );
    }
  }
  // Dedupe while preserving order.
  return Array.from(new Set(resources));
}

/**
 * AgentCore Stack
 * Deploys Strands agents to Bedrock AgentCore Runtime
 *
 * Provides:
 * - Four AgentCore Runtimes (Ontology Generation, Ontology Query, Metadata, Metadata Query)
 * - IAM roles for each agent
 * - ECR repositories for agent container images
 * - CloudWatch log group
 */
export class AgentCoreStack extends cdk.Stack {
  public readonly ontologyAgentRole?: iam.Role;
  public readonly queryAgentRole?: iam.Role; // Ontology Query Agent
  public readonly metadataAgentRole?: iam.Role;
  public readonly metadataQueryAgentRole?: iam.Role;
  public readonly agentRepository: ecr.Repository;
  public readonly ontologyRuntime?: agentcore.Runtime;
  public readonly ontologyRuntimeArn?: string;
  public readonly queryRuntime?: agentcore.Runtime; // Ontology Query Agent
  public readonly queryRuntimeArn?: string; // Ontology Query Agent
  public readonly metadataRuntime?: agentcore.Runtime;
  public readonly metadataRuntimeArn?: string;
  public readonly metadataQueryRuntime?: agentcore.Runtime;
  public readonly metadataQueryRuntimeArn?: string;
  public readonly suggestionsAgentRole?: iam.Role;
  public readonly suggestionsRuntime?: agentcore.Runtime;
  public readonly suggestionsRuntimeArn?: string;
  public readonly neptuneGateway?: AgentCoreNeptuneGateway;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    // Feature flag: skip Neptune gateway and ontology/query agents when false.
    // bedrockKbStack is always present — both KBs are needed by metadata/query/suggestions agents.
    const ontologyEnabled = !!props.neptuneStack;
    // Feature flag: skip metadata, metadata-query, and query-suggestions runtimes when false.
    // When disabled, props.bedrockKbStack.semanticRagKbId / semanticRagDataSourceId may be undefined.
    const semanticRagEnabled = props.enableSemanticRag === true;

    // Two-step migration helper for replacing the SemanticRAG KB data source
    // (e.g. a chunking-strategy change, which is immutable on CfnDataSource).
    // CloudFormation refuses to replace a cross-stack export while it is in use,
    // so step 1 deploys agentcore with `-c releaseRagDsExport=true` to drop the
    // reference (releasing the export); step 2 deploys bedrock-kb (replaces the
    // source) + agentcore without the flag (re-points to the new id). The id is
    // only used by the metadata agent to trigger KB ingestion, so a transient
    // placeholder is harmless. Resolve it once here.
    const releaseRagDsExport =
      this.node.tryGetContext('releaseRagDsExport') === true ||
      this.node.tryGetContext('releaseRagDsExport') === 'true';
    const ragDataSourceId = releaseRagDsExport
      ? 'PENDING_REINGEST'
      : props.bedrockKbStack.semanticRagDataSourceId;

    // Lessons-learned env vars (item #2). Threaded into every runtime so the
    // Strands ``LessonsMemoryHooks`` can PII-redact + persist turns into a
    // single shared AgentCore Memory resource. Empty strings short-circuit
    // the hook (no memory writes) for environments without the memory stack.
    const lessonsEnv: Record<string, string> = {
      LESSONS_MEMORY_ID: props.lessonsMemoryId ?? '',
      GUARDRAIL_IDENTIFIER: props.guardrailId ?? '',
      GUARDRAIL_VERSION: props.guardrailVersion ?? '',
    };

    // Helper: grant the lessons-learned permissions to any agent role we
    // create below. The agents call bedrock-runtime:ApplyGuardrail (via the
    // shim) and bedrock-agentcore:CreateEvent (via MemorySession.add_turns) to
    // WRITE turns, plus RetrieveMemoryRecords to READ long-term lessons back
    // during Phase-2 disambiguation (memory-backed term resolution). Resource:
    // '*' is required because the APIs validate via simulation against ``*``
    // and the role is only assumable by bedrock-agentcore.
    const grantLessonsPerms = (role: iam.Role | undefined): void => {
      if (!role) return;
      role.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:ApplyGuardrail'],
          resources: ['*'],
        })
      );
      role.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            // Write side — persist turns / mapping lessons.
            'bedrock-agentcore:CreateEvent',
            'bedrock-agentcore:ListEvents',
            'bedrock-agentcore:GetMemoryRecord',
            // Read side — recall a user's prior-session lessons to bias
            // disambiguation (search_long_term_memories -> RetrieveMemoryRecords).
            'bedrock-agentcore:RetrieveMemoryRecords',
          ],
          resources: ['*'],
        })
      );
    };

    // Shared catalog ID for S3 Tables Iceberg federated catalog — used by all LF grants
    const icebergCatalogId = `${this.account}:s3tablescatalog/${props.dataLakeStack.tableBucketName}`;
    // Only create LF grants for semantic_layer_iceberg when that namespace actually exists
    const icebergEnabled = props.enableRealtimeReplication !== false;

    // ECR repository for agent container images
    this.agentRepository = new ecr.Repository(this, 'AgentRepository', {
      repositoryName: `${props.projectName}-agents`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      lifecycleRules: [
        {
          maxImageCount: 10,
          description: 'Keep last 10 images',
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
    });

    // Shared security group for all AgentCore Runtime containers.
    // All outbound traffic travels through VPC Interface endpoints — no inbound rules needed
    // because AgentCore invocations enter via the service control-plane, not a direct port.
    const agentcoreSecurityGroup = new ec2.SecurityGroup(this, 'AgentCoreRuntimeSecurityGroup', {
      vpc: props.vpc,
      description: 'Security group for all AgentCore Runtime containers',
      allowAllOutbound: true,
    });

    // ── ENI drainer custom resource ─────────────────────────────────
    // AgentCore Runtimes attach AWS-managed ENIs (``ela-attach-*``) to the
    // shared SG. When the stack is deleted, CFN issues DeleteRuntime and
    // gets DELETE_COMPLETE back from the Bedrock control plane, but the
    // service takes 30-90s *after* that to actually detach those ENIs.
    // CFN does not wait — it proceeds to delete the SG, which then fails
    // with ``DependencyViolation: has a dependent object``.
    //
    // The drainer is a Lambda-backed custom resource whose Delete handler
    // polls DescribeNetworkInterfaces filtered on this SG and waits for
    // the count to reach zero (deleting any 'available' ENIs along the
    // way). We wire ``agentcoreSecurityGroup`` to depend on the drainer
    // so on stack delete CFN sequences:  runtimes → drainer → SG.
    const sgEniDrainerFunction = new lambda.Function(this, 'SgEniDrainerFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'agentcore', 'sg-eni-drainer-handler')),
      timeout: cdk.Duration.minutes(6),
      description: 'Drains AgentCore Runtime ENIs from the shared SG on stack delete',
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    sgEniDrainerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ec2:DescribeNetworkInterfaces',
          'ec2:DeleteNetworkInterface',
          'ec2:DetachNetworkInterface',
        ],
        // EC2 describe/delete/detach NetworkInterface APIs do not support
        // resource-level permissions — they require '*'. The handler scopes
        // its operations at runtime by filtering on the SG ID.
        resources: ['*'],
      })
    );

    const sgEniDrainerProvider = new cr.Provider(this, 'SgEniDrainerProvider', {
      onEventHandler: sgEniDrainerFunction,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    const sgEniDrainerCustomResource = new cdk.CustomResource(this, 'SgEniDrainerCustomResource', {
      serviceToken: sgEniDrainerProvider.serviceToken,
      resourceType: 'Custom::AgentCoreSgEniDrainer',
      properties: {
        SecurityGroupId: agentcoreSecurityGroup.securityGroupId,
        TimeoutSeconds: '300',
        PollIntervalSeconds: '10',
      },
    });

    // Note: we deliberately do NOT add ``SG.dependsOn(CR)`` — that would cycle,
    // because ``CR`` already depends on ``SG`` via its ``SecurityGroupId``
    // property reference. Instead, each AgentCore Runtime is wired below to
    // depend on the drainer CR. This produces the correct sequencing:
    //   create: SG → CR → Runtime
    //   delete: Runtime → CR → SG
    // On delete, runtimes are torn down first (which causes the AgentCore
    // service to begin detaching the ``ela-attach-*`` ENIs), the drainer's
    // Delete handler then polls until those ENIs are gone, and only then
    // does CFN delete the SG (which is now a leaf with no attachments).

    NagSuppressions.addResourceSuppressions(
      sgEniDrainerFunction,
      [
        {
          id: 'AwsSolutions-IAM4',
          reason: 'AWSLambdaBasicExecutionRole is the standard managed policy for Lambda CW Logs.',
        },
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'EC2 DescribeNetworkInterfaces/DeleteNetworkInterface/DetachNetworkInterface do not support resource-level permissions; the handler scopes by SG-id filter at runtime.',
        },
      ],
      true
    );
    NagSuppressions.addResourceSuppressions(
      sgEniDrainerProvider,
      [
        {
          id: 'AwsSolutions-IAM4',
          reason: 'cr.Provider framework Lambda uses AWSLambdaBasicExecutionRole for CW Logs.',
        },
        {
          id: 'AwsSolutions-IAM5',
          reason: 'cr.Provider framework Lambda needs lambda:InvokeFunction on its inner handler.',
        },
        {
          id: 'AwsSolutions-L1',
          reason: 'Provider framework runtime is managed by CDK; cannot be overridden here.',
        },
      ],
      true
    );

    // Shared VPC network configuration — places every Runtime container in the private
    // (PRIVATE_WITH_EGRESS) subnets so it reaches VPC endpoints without a public IP.
    // NAT gateway provides fallback egress for any endpoint not covered by PrivateLink.
    const agentcoreNetworkConfig = agentcore.RuntimeNetworkConfiguration.usingVpc(this, {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [agentcoreSecurityGroup],
    });

    if (ontologyEnabled) {
      // IAM role for Ontology Generation Agent
      this.ontologyAgentRole = new iam.Role(this, 'OntologyGenerationAgentRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: 'Role for ontology generation agent',
      });
      grantLessonsPerms(this.ontologyAgentRole);

      // Grant permissions for ontology generation
      // Grant Glue permissions — standard Data Catalog + S3 Tables federated catalog.
      // S3 Tables (CatalogId='s3tablescatalog/<bucket>') resolves to path-based ARNs:
      //   arn:aws:glue:<region>:<account>:catalog/s3tablescatalog/<bucket>
      //   arn:aws:glue:<region>:<account>:table/s3tablescatalog/<bucket>/<ns>/<table>
      // The standard arn:...:catalog ARN does NOT cover these sub-paths.
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            // glue:GetCatalog + glue:GetPartitions are required for Athena to
            // RESOLVE the s3tablescatalog/<bucket> federated catalog — without
            // GetCatalog, queries against S3 Tables fail with CATALOG_NOT_FOUND.
            // The build-time enum-shape probe (select_distinct_values) runs
            // SELECT DISTINCT against these tables, so the ontology agent needs
            // the same federated-catalog resolution the query agent already has.
            'glue:GetCatalog',
            'glue:GetDatabase',
            'glue:GetDatabases',
            'glue:GetTable',
            'glue:GetTables',
            'glue:GetPartitions',
            'glue:UpdateTable',
          ],
          resources: [
            // Standard Glue Data Catalog
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
            // S3 Tables federated catalog (path-based ARNs).
            // glue:GetTable on CatalogId='s3tablescatalog/<bucket>' evaluates
            // against 'catalog/s3tablescatalog' (without wildcard suffix).
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
            `arn:aws:glue:${this.region}:${this.account}:database/s3tablescatalog/*/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/s3tablescatalog/*/*/*`,
          ],
        })
      );

      // Grant S3 Tables permissions — read for schema discovery + write for Iceberg metadata updates.
      // IAM actions map to Iceberg REST operations per AWS docs:
      //   loadTable  (GET .../tables/{table}) → s3tables:GetTableMetadataLocation + s3tables:GetTableData
      //   updateTable (POST .../tables/{table}) → s3tables:UpdateTableMetadataLocation + s3tables:PutTableData + s3tables:GetTableData
      //   getConfig  (GET /v1/config)          → s3tables:GetTableBucket
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3tables:GetTable',
            's3tables:GetTableMetadataLocation',
            's3tables:GetTableData', // required for loadTable REST operation
            's3tables:GetTableBucket',
            's3tables:GetNamespace',
            's3tables:ListTables',
            's3tables:ListNamespaces',
            's3tables:UpdateTableMetadataLocation',
            's3tables:PutTableData', // required for updateTable REST operation
          ],
          resources: [`arn:aws:s3tables:${this.region}:${this.account}:bucket/*`],
        })
      );

      // DynamoDB Scan permission for DynamoDB-backed Glue tables.
      // When Athena cannot query a DynamoDB-sourced table (URISyntaxException on ARN-based
      // StorageDescriptor.Location), the ontology agent falls back to a direct DynamoDB Scan
      // to retrieve sample rows for FK pattern analysis.
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:Scan', 'dynamodb:DescribeTable'],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.projectName}-*`,
          ],
        })
      );

      // Grant Athena permissions so sample_table_data can query S3 Tables / federated catalogs
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:StartQueryExecution',
            'athena:GetQueryExecution',
            'athena:GetQueryResults',
            'athena:StopQueryExecution',
          ],
          resources: [
            `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.athenaStack.workgroup.name}`,
          ],
        })
      );

      // Grant S3 permissions for Athena results (ontology agent)
      props.dataLakeStack.athenaResultsBucket.grantReadWrite(this.ontologyAgentRole);

      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.bedrockKbStack.ontologyPatternsKbId}`,
          ],
        })
      );

      // Bedrock model invocation — derived from agents/ontology_agent/models.json
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: bedrockInvokeModelResources('ontology_agent', this.region, this.account),
        })
      );

      // Grant ECR permissions for AgentCore to pull container images
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: ['*'],
        })
      );

      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        })
      );

      // Grant CloudWatch Logs permissions
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
          ],
        })
      );

      // Grant AgentCore Memory access
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
          ],
        })
      );

      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lakeformation:GetDataAccess'],
          resources: ['*'],
        })
      );

      // EC2 VPC permissions — required for AgentCore to attach container ENIs to VPC subnets
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:AssignPrivateIpAddresses',
            'ec2:UnassignPrivateIpAddresses',
          ],
          resources: ['*'],
        })
      );

      // Grant S3 permissions for ontology storage
      props.dataLakeStack.artifactsBucket.grantReadWrite(this.ontologyAgentRole);

      // Grant DynamoDB permissions for ontology metadata table
      props.dynamodbStack.metadataTable.grantReadWriteData(this.ontologyAgentRole);

      // EventBridge — emit ontology.published so the topic-router rebuild
      // Lambda re-indexes the per-namespace KNN store. Without this Tier 2
      // Phase 1 always returns no candidates and queries silently degrade.
      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['events:PutEvents'],
          resources: [`arn:aws:events:${this.region}:${this.account}:event-bus/default`],
        })
      );

      // Grant Lake Formation permissions to Ontology Agent role — dynamodb database
      new lakeformation.CfnPermissions(this, 'OntologyLFDynamoDBDatabasePermissions', {
        dataLakePrincipal: {
          dataLakePrincipalIdentifier: this.ontologyAgentRole.roleArn,
        },
        resource: {
          databaseResource: {
            name: props.glueCatalogStack.dynamodbDatabase.ref,
          },
        },
        permissions: ['DESCRIBE', 'ALTER'],
      });

      new lakeformation.CfnPermissions(this, 'OntologyLFDynamoDBTablePermissions', {
        dataLakePrincipal: {
          dataLakePrincipalIdentifier: this.ontologyAgentRole.roleArn,
        },
        resource: {
          tableResource: {
            databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
      });

      // Grant Lake Formation permissions to Ontology Agent role — S3 Tables Iceberg database
      if (icebergEnabled) {
        new lakeformation.CfnPermissions(this, 'OntologyLFIcebergDatabasePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.ontologyAgentRole.roleArn },
          resource: {
            databaseResource: {
              catalogId: icebergCatalogId,
              name: 'semantic_layer_iceberg',
            },
          },
          permissions: ['DESCRIBE', 'ALTER'],
        });

        new lakeformation.CfnPermissions(this, 'OntologyLFIcebergTablePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.ontologyAgentRole!.roleArn },
          resource: {
            tableResource: {
              catalogId: icebergCatalogId,
              databaseName: 'semantic_layer_iceberg',
              tableWildcard: {},
            },
          },
          permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
        });
      }
    } // end if (ontologyEnabled) — ontology agent role

    if (ontologyEnabled) {
      // IAM role for Semantic Query Agent
      this.queryAgentRole = new iam.Role(this, 'SemanticQueryAgentRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: 'Role for semantic query agent',
      });
      grantLessonsPerms(this.queryAgentRole);

      // Grant DynamoDB query permissions (insuranceTable removed from DynamoDBStack)
      props.dynamodbStack.metadataTable.grantReadData(this.queryAgentRole);
      // Read prior turns AND write user/assistant turns — chat persistence now
      // runs inside the runtime (see docs/plans/2026-05-30-gateway-runtime-target-streaming-chat.md;
      // agents/shared/chat_sessions.py). Read still used for prior-results lazy fetch.
      props.dynamodbStack.chatSessionsTable.grantReadWriteData(this.queryAgentRole);

      // Tier 1 governed-metric lookup reads from the metrics table (read-only —
      // authoring is exclusively through the REST API).
      props.dynamodbStack.metricsTable.grantReadData(this.queryAgentRole);

      // Tier 1 metric lookup uses an in-memory KNN index hydrated from DDB
      // on cold start — no OpenSearch dependency.

      // Grant Athena query permissions
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:StartQueryExecution',
            'athena:GetQueryExecution',
            'athena:GetQueryResults',
            'athena:StopQueryExecution',
          ],
          resources: [
            `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.athenaStack.workgroup.name}`,
          ],
        })
      );

      // Athena data-catalog metadata access — REQUIRED to query any non-default
      // catalog (e.g. a federated `dynamodb_catalog`). Athena calls GetDataCatalog
      // to resolve the catalog before running SQL; without it the query fails with
      // "not authorized to perform: athena:GetDataCatalog". Mirrors the RAG
      // metadataQueryAgentRole grant below so both query agents can reach the
      // raw-DynamoDB layer's federated catalog.
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:GetDataCatalog',
            'athena:GetDatabase',
            'athena:GetTableMetadata',
            'athena:ListDataCatalogs',
            'athena:ListDatabases',
            'athena:ListTableMetadata',
          ],
          resources: [`arn:aws:athena:${this.region}:${this.account}:datacatalog/*`],
        })
      );

      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'glue:GetCatalog', // needed to resolve s3tablescatalog/<bucket> federated catalog
            'glue:GetDatabase',
            'glue:GetDatabases',
            'glue:GetTable',
            'glue:GetTables',
            'glue:GetPartitions',
          ],
          resources: [
            // Default AwsDataCatalog
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/*`,
            // Federated S3 Tables catalog (s3tablescatalog/<bucket>)
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
            `arn:aws:glue:${this.region}:${this.account}:database/s3tablescatalog/*/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/s3tablescatalog/*/*/*`,
          ],
        })
      );

      // Grant S3 permissions for Athena results
      props.dataLakeStack.athenaResultsBucket.grantReadWrite(this.queryAgentRole);

      // Grant S3 Tables permissions for analytics queries
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3tables:GetTable',
            's3tables:GetTableMetadataLocation',
            's3tables:GetNamespace',
            's3tables:ListNamespaces',
            's3tables:ListTables',
            's3tables:GetTableData',
          ],
          resources: [
            props.dataLakeStack.tableBucketArn,
            `${props.dataLakeStack.tableBucketArn}/*`,
          ],
        })
      );

      // Bedrock model invocation — derived from agents/ontology_query_agent/models.json
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: bedrockInvokeModelResources('ontology_query_agent', this.region, this.account),
        })
      );

      // Grant ECR permissions for AgentCore to pull container images
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: ['*'],
        })
      );

      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        })
      );

      // Grant CloudWatch Logs permissions
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
          ],
        })
      );

      // Grant AgentCore Memory access
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
          ],
        })
      );

      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lakeformation:GetDataAccess'],
          resources: ['*'],
        })
      );

      // EC2 VPC permissions — required for AgentCore to attach container ENIs to VPC subnets
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:AssignPrivateIpAddresses',
            'ec2:UnassignPrivateIpAddresses',
          ],
          resources: ['*'],
        })
      );

      // Grant SSM Parameter Store permissions to read Athena bucket
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:GetParameter', 'ssm:GetParameters'],
          resources: [
            `arn:aws:ssm:${this.region}:${this.account}:parameter/${props.projectName}/athena/query-results-bucket`,
          ],
        })
      );

      // Athena routes dynamodb_catalog queries through the DDB connector Lambda;
      // the agent role must be able to invoke it. The connector spills oversize
      // result fragments to S3 with KMS encryption, and the caller (agent role)
      // reads them back — so it also needs S3 + KMS Decrypt on the spill bucket.
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lambda:InvokeFunction'],
          resources: [
            `arn:aws:lambda:${this.region}:${this.account}:function:${props.projectName}-ddb-connector`,
          ],
        })
      );
      props.athenaStack.spillBucket.grantReadWrite(this.queryAgentRole);
      props.athenaStack.spillEncryptionKey.grantEncryptDecrypt(this.queryAgentRole);

      // Grant Lake Formation permissions to Query Agent role — dynamodb database
      new lakeformation.CfnPermissions(this, 'QueryLFDynamoDBDatabasePermissions', {
        dataLakePrincipal: {
          dataLakePrincipalIdentifier: this.queryAgentRole.roleArn,
        },
        resource: {
          databaseResource: {
            name: props.glueCatalogStack.dynamodbDatabase.ref,
          },
        },
        permissions: ['DESCRIBE'],
      });

      new lakeformation.CfnPermissions(this, 'QueryLFDynamoDBTablePermissions', {
        dataLakePrincipal: {
          dataLakePrincipalIdentifier: this.queryAgentRole.roleArn,
        },
        resource: {
          tableResource: {
            databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT', 'DESCRIBE'],
      });

      // Grant Lake Formation permissions to Query Agent role — S3 Tables Iceberg database
      // LF admin status alone is insufficient for federated catalogs; explicit grants are required.
      if (icebergEnabled) {
        new lakeformation.CfnPermissions(this, 'QueryLFIcebergDatabasePermissions', {
          dataLakePrincipal: {
            dataLakePrincipalIdentifier: this.queryAgentRole.roleArn,
          },
          resource: {
            databaseResource: {
              catalogId: icebergCatalogId,
              name: 'semantic_layer_iceberg',
            },
          },
          permissions: ['DESCRIBE'],
        });

        new lakeformation.CfnPermissions(this, 'QueryLFIcebergTablePermissions', {
          dataLakePrincipal: {
            dataLakePrincipalIdentifier: this.queryAgentRole.roleArn,
          },
          resource: {
            tableResource: {
              catalogId: icebergCatalogId,
              databaseName: 'semantic_layer_iceberg',
              tableWildcard: {},
            },
          },
          permissions: ['SELECT', 'DESCRIBE'],
        });
      }
    } // end if (ontologyEnabled) — query agent role

    // ============================================================
    // IAM role for Metadata Agent (Virtual Knowledge Graph Query)
    // ============================================================
    if (semanticRagEnabled) {
      this.metadataAgentRole = new iam.Role(this, 'MetadataAgentRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: 'Role for metadata generation agent (Glue catalog enrichment + KB ingestion)',
      });
      grantLessonsPerms(this.metadataAgentRole);

      // Bedrock KB ingestion — metadata_agent writes docs to S3 then triggers re-ingestion
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock:StartIngestionJob',
            'bedrock:GetIngestionJob',
            'bedrock:ListIngestionJobs',
          ],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.bedrockKbStack.semanticRagKbId}`,
          ],
        })
      );
      // Bedrock KB retrieval — retrieve_ontology_patterns reads from ontology patterns KB
      // to enrich table/column descriptions with domain context and relationship patterns
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.bedrockKbStack.ontologyPatternsKbId}`,
          ],
        })
      );

      // Bedrock model invocation — derived from agents/metadata_agent/models.json
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: bedrockInvokeModelResources('metadata_agent', this.region, this.account),
        })
      );

      // Athena query execution
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:StartQueryExecution',
            'athena:GetQueryExecution',
            'athena:GetQueryResults',
            'athena:StopQueryExecution',
          ],
          resources: [
            `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.athenaStack.workgroup.name}`,
          ],
        })
      );

      // Glue catalog access (read + write descriptions) — standard catalog + S3 Tables federated catalog.
      // S3 Tables (CatalogId='s3tablescatalog/<bucket>') resolves to path-based ARNs:
      //   arn:aws:glue:<region>:<account>:catalog/s3tablescatalog/<bucket>
      //   arn:aws:glue:<region>:<account>:table/s3tablescatalog/<bucket>/<ns>/<table>
      // The standard arn:...:catalog ARN does NOT cover these sub-paths.
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'glue:GetDatabase',
            'glue:GetDatabases',
            'glue:GetTable',
            'glue:GetTables',
            'glue:GetPartitions',
            'glue:GetCatalog',
            'glue:UpdateDatabase',
            'glue:UpdateTable',
          ],
          resources: [
            // Standard Glue Data Catalog
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
            // S3 Tables federated catalog (path-based ARNs)
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
            `arn:aws:glue:${this.region}:${this.account}:database/s3tablescatalog/*/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/s3tablescatalog/*/*/*`,
          ],
        })
      );

      // S3 Tables API permissions — mirrors OntologyGenerationAgentRole exactly.
      // Iceberg REST endpoint action mapping (per AWS docs):
      //   loadTable  (GET .../tables/{table}) → s3tables:GetTableMetadataLocation + s3tables:GetTableData
      //   updateTable (POST .../tables/{table}) → s3tables:UpdateTableMetadataLocation + s3tables:PutTableData + s3tables:GetTableData
      //   getConfig  (GET /v1/config)          → s3tables:GetTableBucket
      // GetTableData is required for any catalog.load_table() call via the REST endpoint.
      // PutTableData is required for any catalog.update_table() / schema-update call.
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3tables:GetTable',
            's3tables:GetTableMetadataLocation',
            's3tables:GetTableData', // required for loadTable REST operation
            's3tables:GetTableBucket',
            's3tables:GetNamespace',
            's3tables:ListTables',
            's3tables:ListNamespaces',
            's3tables:UpdateTableMetadataLocation',
            's3tables:PutTableData', // required for updateTable REST operation
          ],
          resources: [`arn:aws:s3tables:${this.region}:${this.account}:bucket/*`],
        })
      );

      // S3 permissions for Athena results
      props.dataLakeStack.athenaResultsBucket.grantReadWrite(this.metadataAgentRole);

      // S3 permissions for metadata documents (Bedrock KB source)
      props.dataLakeStack.artifactsBucket.grantReadWrite(this.metadataAgentRole);

      // DynamoDB permissions for job progress tracking
      props.dynamodbStack.metadataTable.grantReadWriteData(this.metadataAgentRole);

      // EventBridge — emit evaluation.requested on the default bus once a metadata
      // build reaches 'completed' (agents/shared/eval_trigger.emit_evaluation_requested,
      // called from agents/metadata_agent/main.py). Without this the emit is silently
      // swallowed (best-effort, never raises) and the post-build evaluation never fires.
      // Mirrors the ontology agent role's grant.
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['events:PutEvents'],
          resources: [`arn:aws:events:${this.region}:${this.account}:event-bus/default`],
        })
      );

      // DynamoDB Scan permission for DynamoDB-backed Glue tables.
      // When Athena cannot query a DynamoDB-sourced table (URISyntaxException on ARN-based
      // StorageDescriptor.Location), the metadata agent falls back to a direct DynamoDB Scan
      // to retrieve sample rows for description generation.
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['dynamodb:Scan', 'dynamodb:DescribeTable'],
          resources: [
            `arn:aws:dynamodb:${this.region}:${this.account}:table/${props.projectName}-*`,
          ],
        })
      );

      // SSM Parameter Store
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:GetParameter', 'ssm:GetParameters'],
          resources: [
            `arn:aws:ssm:${this.region}:${this.account}:parameter/${props.projectName}/athena/query-results-bucket`,
          ],
        })
      );

      // ECR permissions
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: ['*'],
        })
      );

      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        })
      );

      // CloudWatch Logs
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
          ],
        })
      );

      // AgentCore workload token
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
          ],
        })
      );

      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lakeformation:GetDataAccess'],
          resources: ['*'],
        })
      );

      // EC2 VPC permissions — required for AgentCore to attach container ENIs to VPC subnets
      this.metadataAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:AssignPrivateIpAddresses',
            'ec2:UnassignPrivateIpAddresses',
          ],
          resources: ['*'],
        })
      );

      // Lake Formation permissions for Metadata Agent — dynamodb database
      new lakeformation.CfnPermissions(this, 'MetadataLFDynamoDBDatabasePermissions', {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
        resource: { databaseResource: { name: props.glueCatalogStack.dynamodbDatabase.ref } },
        permissions: ['DESCRIBE', 'ALTER'],
      });

      new lakeformation.CfnPermissions(this, 'MetadataLFDynamoDBTablePermissions', {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
        resource: {
          tableResource: {
            databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
      });

      // Grant Lake Formation permissions to Metadata Agent role — S3 Tables Iceberg database
      if (icebergEnabled) {
        new lakeformation.CfnPermissions(this, 'MetadataLFIcebergDatabasePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
          resource: {
            databaseResource: {
              catalogId: icebergCatalogId,
              name: 'semantic_layer_iceberg',
            },
          },
          permissions: ['DESCRIBE', 'ALTER'],
        });

        new lakeformation.CfnPermissions(this, 'MetadataLFIcebergTablePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
          resource: {
            tableResource: {
              catalogId: icebergCatalogId,
              databaseName: 'semantic_layer_iceberg',
              tableWildcard: {},
            },
          },
          permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
        });
      }

      // normalized namespace grants for metadataAgentRole — the enrichment agent
      // samples data via Athena for all selected tables, including normalized.* MVs.
      // Without SELECT here, sample_table_data Athena queries fail with
      // AccessDeniedException (LF hides tables as TABLE_NOT_FOUND).
      if (props.normalizedViewsEnabled) {
        const tableBucketArnMeta = `arn:aws:s3tables:${this.region}:${this.account}:bucket/${props.dataLakeStack.tableBucketName}`;
        const ensureNormalizedNsMeta = new cr.AwsCustomResource(
          this,
          'EnsureNormalizedNamespaceMeta',
          {
            onCreate: {
              service: 'S3Tables',
              action: 'createNamespace',
              parameters: { tableBucketARN: tableBucketArnMeta, namespace: ['normalized'] },
              physicalResourceId: cr.PhysicalResourceId.of('agentcore-normalized-namespace-meta'),
              ignoreErrorCodesMatching: 'ConflictException',
            },
            policy: cr.AwsCustomResourcePolicy.fromStatements([
              new iam.PolicyStatement({
                actions: ['s3tables:CreateNamespace', 's3tables:GetNamespace'],
                resources: [tableBucketArnMeta],
              }),
            ]),
          }
        );

        const lfMetaNormalizedDb = new lakeformation.CfnPermissions(
          this,
          'MetadataLFNormalizedDbPermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
            resource: {
              databaseResource: { catalogId: icebergCatalogId, name: 'normalized' },
            },
            permissions: ['DESCRIBE', 'ALTER'],
          }
        );
        lfMetaNormalizedDb.node.addDependency(ensureNormalizedNsMeta);

        const lfMetaNormalizedTables = new lakeformation.CfnPermissions(
          this,
          'MetadataLFNormalizedTablePermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataAgentRole.roleArn },
            resource: {
              tableResource: {
                catalogId: icebergCatalogId,
                databaseName: 'normalized',
                tableWildcard: {},
              },
            },
            permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
          }
        );
        lfMetaNormalizedTables.node.addDependency(ensureNormalizedNsMeta);
      }
    } // end if (semanticRagEnabled) — metadata agent role

    // ============================================================
    // IAM role for Metadata Query Agent (Bedrock KB + Athena)
    // ============================================================
    if (semanticRagEnabled) {
      this.metadataQueryAgentRole = new iam.Role(this, 'MetadataQueryAgentRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: 'Role for metadata query agent (Bedrock KB + Athena)',
      });
      grantLessonsPerms(this.metadataQueryAgentRole);

      // Tier 1 — read governed metrics from DDB; the in-memory KNN index is
      // hydrated from DDB on cold start, so no OpenSearch grant is needed.
      props.dynamodbStack.metricsTable.grantReadData(this.metadataQueryAgentRole);

      // Bedrock KB retrieve — reads from Semantic RAG KB for query enrichment
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:Retrieve'],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.bedrockKbStack.semanticRagKbId}`,
          ],
        })
      );

      // Bedrock model invocation — derived from agents/metadata_query_agent/models.json
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: bedrockInvokeModelResources('metadata_query_agent', this.region, this.account),
        })
      );

      // Athena query execution
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:StartQueryExecution',
            'athena:GetQueryExecution',
            'athena:GetQueryResults',
            'athena:StopQueryExecution',
          ],
          resources: [
            `arn:aws:athena:${this.region}:${this.account}:workgroup/${props.athenaStack.workgroup.name}`,
          ],
        })
      );

      // Athena data-catalog metadata access — REQUIRED to query any non-default
      // catalog (e.g. the raw-DynamoDB layer's federated `dynamodb_catalog`).
      // Athena calls GetDataCatalog to resolve the catalog before running SQL;
      // without it a query against dynamodb_catalog fails with
      //   "not authorized to perform: athena:GetDataCatalog ... on dynamodb_catalog".
      // Scoped to all data catalogs in this account/region (datacatalog ARNs, not
      // the workgroup ARN above), mirroring the Athena execution role.
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'athena:GetDataCatalog',
            'athena:GetDatabase',
            'athena:GetTableMetadata',
            'athena:ListDataCatalogs',
            'athena:ListDatabases',
            'athena:ListTableMetadata',
          ],
          resources: [`arn:aws:athena:${this.region}:${this.account}:datacatalog/*`],
        })
      );

      // Athena routes dynamodb_catalog queries through the DDB connector Lambda;
      // the agent role must be able to invoke it. The connector spills oversize
      // result fragments to S3 with KMS encryption, and the caller (agent role)
      // reads them back — so it also needs S3 + KMS Decrypt on the spill bucket.
      // Mirrors the VKG queryAgentRole grant so the RAG query agent can query the
      // raw-DynamoDB layer's federated catalog.
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lambda:InvokeFunction'],
          resources: [
            `arn:aws:lambda:${this.region}:${this.account}:function:${props.projectName}-ddb-connector`,
          ],
        })
      );
      props.athenaStack.spillBucket.grantReadWrite(this.metadataQueryAgentRole);
      props.athenaStack.spillEncryptionKey.grantEncryptDecrypt(this.metadataQueryAgentRole);

      // Glue catalog access — matches queryAgentRole, including S3 Tables federated catalog ARNs
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'glue:GetCatalog',
            'glue:GetDatabase',
            'glue:GetDatabases',
            'glue:GetTable',
            'glue:GetTables',
            'glue:GetPartitions',
          ],
          resources: [
            // Default AwsDataCatalog
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/*`,
            // Federated S3 Tables catalog (s3tablescatalog/<bucket>)
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
            `arn:aws:glue:${this.region}:${this.account}:database/s3tablescatalog/*/*`,
            `arn:aws:glue:${this.region}:${this.account}:table/s3tablescatalog/*/*/*`,
          ],
        })
      );

      // S3 Tables permissions for analytics queries — matches queryAgentRole
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3tables:GetTable',
            's3tables:GetNamespace',
            's3tables:ListNamespaces',
            's3tables:ListTables',
            's3tables:GetTableData',
          ],
          resources: [
            props.dataLakeStack.tableBucketArn,
            `${props.dataLakeStack.tableBucketArn}/*`,
          ],
        })
      );

      // S3 permissions for Athena results
      props.dataLakeStack.athenaResultsBucket.grantReadWrite(this.metadataQueryAgentRole);

      // SSM Parameter Store
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ssm:GetParameter', 'ssm:GetParameters'],
          resources: [
            `arn:aws:ssm:${this.region}:${this.account}:parameter/${props.projectName}/athena/query-results-bucket`,
          ],
        })
      );

      // ECR permissions
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: ['*'],
        })
      );

      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        })
      );

      // CloudWatch Logs
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
          ],
        })
      );

      // AgentCore workload token
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
          ],
        })
      );

      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['lakeformation:GetDataAccess'],
          resources: ['*'],
        })
      );

      // EC2 VPC permissions — required for AgentCore to attach container ENIs to VPC subnets
      this.metadataQueryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:AssignPrivateIpAddresses',
            'ec2:UnassignPrivateIpAddresses',
          ],
          resources: ['*'],
        })
      );

      // Lake Formation permissions for Metadata Query Agent — dynamodb database
      new lakeformation.CfnPermissions(this, 'MetadataQueryLFDynamoDBDatabasePermissions', {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
        resource: { databaseResource: { name: props.glueCatalogStack.dynamodbDatabase.ref } },
        permissions: ['DESCRIBE'],
      });

      new lakeformation.CfnPermissions(this, 'MetadataQueryLFDynamoDBTablePermissions', {
        dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
        resource: {
          tableResource: {
            databaseName: props.glueCatalogStack.dynamodbDatabase.ref,
            tableWildcard: {},
          },
        },
        permissions: ['SELECT', 'DESCRIBE'],
      });

      // Grant Lake Formation permissions to Metadata Query Agent role — S3 Tables Iceberg database
      if (icebergEnabled) {
        new lakeformation.CfnPermissions(this, 'MetadataQueryLFIcebergDatabasePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
          resource: {
            databaseResource: {
              catalogId: icebergCatalogId,
              name: 'semantic_layer_iceberg',
            },
          },
          permissions: ['DESCRIBE'],
        });

        new lakeformation.CfnPermissions(this, 'MetadataQueryLFIcebergTablePermissions', {
          dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
          resource: {
            tableResource: {
              catalogId: icebergCatalogId,
              databaseName: 'semantic_layer_iceberg',
              tableWildcard: {},
            },
          },
          permissions: ['SELECT', 'DESCRIBE'],
        });
      }
    } // end if (semanticRagEnabled) — metadata query agent role + iceberg LF

    // ── normalized namespace grants (enableBatchReplication=true only) ──────────
    // When Zero-ETL + NormalizedViewsStack are deployed, agent roles need LF SELECT
    // on the 'normalized' S3 Tables namespace for Athena SQL queries to succeed.
    // LF hides tables without SELECT as TABLE_OR_VIEW_NOT_FOUND, not ACCESS_DENIED.
    if (props.normalizedViewsEnabled) {
      const tableBucketArn = `arn:aws:s3tables:${this.region}:${this.account}:bucket/${props.dataLakeStack.tableBucketName}`;

      // Pre-create 'normalized' namespace — idempotent with NormalizedViewsStack's own CR.
      // LF cannot grant on a non-existent database, so we ensure it exists first.
      const ensureNormalizedNs = new cr.AwsCustomResource(this, 'EnsureNormalizedNamespace', {
        onCreate: {
          service: 'S3Tables',
          action: 'createNamespace',
          parameters: {
            tableBucketARN: tableBucketArn,
            namespace: ['normalized'],
          },
          physicalResourceId: cr.PhysicalResourceId.of('agentcore-normalized-namespace'),
          ignoreErrorCodesMatching: 'ConflictException',
        },
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['s3tables:CreateNamespace', 's3tables:GetNamespace'],
            resources: [tableBucketArn],
          }),
        ]),
      });

      // metadataQueryAgentRole only exists when semantic-rag is enabled
      if (semanticRagEnabled && this.metadataQueryAgentRole) {
        const lfNormalizedDb = new lakeformation.CfnPermissions(
          this,
          'MetadataQueryLFNormalizedDbPermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
            resource: {
              databaseResource: {
                catalogId: icebergCatalogId,
                name: 'normalized',
              },
            },
            permissions: ['DESCRIBE'],
          }
        );
        lfNormalizedDb.node.addDependency(ensureNormalizedNs);

        const lfNormalizedTables = new lakeformation.CfnPermissions(
          this,
          'MetadataQueryLFNormalizedTablePermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.metadataQueryAgentRole.roleArn },
            resource: {
              tableResource: {
                catalogId: icebergCatalogId,
                databaseName: 'normalized',
                tableWildcard: {},
              },
            },
            permissions: ['SELECT', 'DESCRIBE'],
          }
        );
        lfNormalizedTables.node.addDependency(ensureNormalizedNs);
      }

      // queryAgentRole only exists when ontology agents are enabled
      if (ontologyEnabled && this.queryAgentRole) {
        const lfNormalizedDbQuery = new lakeformation.CfnPermissions(
          this,
          'QueryLFNormalizedDbPermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.queryAgentRole.roleArn },
            resource: {
              databaseResource: {
                catalogId: icebergCatalogId,
                name: 'normalized',
              },
            },
            permissions: ['DESCRIBE'],
          }
        );
        lfNormalizedDbQuery.node.addDependency(ensureNormalizedNs);

        const lfNormalizedTablesQuery = new lakeformation.CfnPermissions(
          this,
          'QueryLFNormalizedTablePermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.queryAgentRole.roleArn },
            resource: {
              tableResource: {
                catalogId: icebergCatalogId,
                databaseName: 'normalized',
                tableWildcard: {},
              },
            },
            permissions: ['SELECT', 'DESCRIBE'],
          }
        );
        lfNormalizedTablesQuery.node.addDependency(ensureNormalizedNs);
      }

      // ontologyAgentRole only exists when ontology agents are enabled
      if (ontologyEnabled && this.ontologyAgentRole) {
        const lfNormalizedDbOntology = new lakeformation.CfnPermissions(
          this,
          'OntologyLFNormalizedDbPermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.ontologyAgentRole.roleArn },
            resource: {
              databaseResource: {
                catalogId: icebergCatalogId,
                name: 'normalized',
              },
            },
            permissions: ['DESCRIBE', 'ALTER'],
          }
        );
        lfNormalizedDbOntology.node.addDependency(ensureNormalizedNs);

        const lfNormalizedTablesOntology = new lakeformation.CfnPermissions(
          this,
          'OntologyLFNormalizedTablePermissions',
          {
            dataLakePrincipal: { dataLakePrincipalIdentifier: this.ontologyAgentRole.roleArn },
            resource: {
              tableResource: {
                catalogId: icebergCatalogId,
                databaseName: 'normalized',
                tableWildcard: {},
              },
            },
            permissions: ['SELECT', 'DESCRIBE', 'ALTER'],
          }
        );
        lfNormalizedTablesOntology.node.addDependency(ensureNormalizedNs);
      }
    }
    // ── end normalized namespace grants ──────────────────────────────────────────

    if (semanticRagEnabled) {
      // Grant DynamoDB read access — metadata_query agent calls metadata_table.query() at every invocation
      props.dynamodbStack.metadataTable.grantReadData(this.metadataQueryAgentRole!);
      // Read prior turns AND write user/assistant turns — chat persistence now
      // runs inside the runtime (see docs/plans/2026-05-30-gateway-runtime-target-streaming-chat.md).
      props.dynamodbStack.chatSessionsTable.grantReadWriteData(this.metadataQueryAgentRole!);

      // ============================================================
      // IAM role for Query Suggestions Agent (KB retrieval only)
      // ============================================================
      this.suggestionsAgentRole = new iam.Role(this, 'QuerySuggestionsAgentRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: 'Role for query suggestions agent - KB retrieval only, no Athena',
      });
      grantLessonsPerms(this.suggestionsAgentRole);

      // DynamoDB read access
      props.dynamodbStack.metadataTable.grantReadData(this.suggestionsAgentRole);
      // Governed-metric catalog read — advisory answers enumerate a layer's
      // PUBLISHED metrics (definitions only; never compiled/executed).
      props.dynamodbStack.metricsTable.grantReadData(this.suggestionsAgentRole);

      // Bedrock KB retrieve
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:Retrieve'],
          resources: [
            `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/${props.bedrockKbStack.semanticRagKbId}`,
          ],
        })
      );

      // Bedrock model invocation — derived from agents/query_suggestions_agent/models.json
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: bedrockInvokeModelResources(
            'query_suggestions_agent',
            this.region,
            this.account
          ),
        })
      );

      // ECR permissions
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchCheckLayerAvailability',
          ],
          resources: ['*'],
        })
      );

      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ecr:GetAuthorizationToken'],
          resources: ['*'],
        })
      );

      // CloudWatch Logs
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
            `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
          ],
        })
      );

      // AgentCore workload token
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock-agentcore:GetWorkloadAccessToken',
            'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          ],
          resources: [
            `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
          ],
        })
      );

      // EC2 VPC permissions — required for AgentCore to attach container ENIs to VPC subnets
      this.suggestionsAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:AssignPrivateIpAddresses',
            'ec2:UnassignPrivateIpAddresses',
          ],
          resources: ['*'],
        })
      );
    } // end if (semanticRagEnabled) — metadata-query DDB read + suggestions agent role

    // ============================================================
    // Lake Formation: preserve infrastructure admins
    //
    // This CfnDataLakeSettings supersedes AthenaStack's
    // AthenaDataLakeAdminSettings (last writer wins per account/region).
    // ALL prior admins must be carried forward here.
    //
    // Agent roles are NOT listed as LF admins — they receive narrow
    // CfnPermissions grants above (DESCRIBE on databases, SELECT +
    // DESCRIBE on tables).  For the S3 Tables federated catalog
    // (semantic_layer_iceberg), the queryAgentRole is granted via
    // QueryLFIcebergDatabasePermissions / QueryLFIcebergTablePermissions.
    // ============================================================
    new lakeformation.CfnDataLakeSettings(this, 'AgentCoreLFAdminSettings', {
      admins: [
        // CDK bootstrap roles (must be preserved)
        {
          dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}`,
        },
        {
          dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}`,
        },
        // AwsCustomResource singleton role (established by DataLakeStack's S3TablesLFRegistration)
        { dataLakePrincipalIdentifier: props.dataLakeStack.lfGrantSingletonRoleArn },
        // Human/SSO admin roles forwarded from app.ts
        ...(props.additionalLakeFormationAdmins ?? []).map((arn) => ({
          dataLakePrincipalIdentifier: arn,
        })),
      ],
    });

    // CloudWatch Logs — one static log group per AgentCore runtime.
    // AgentCore also auto-creates per-deployment groups (<name>-<randomId>-DEFAULT) that
    // capture container stdout/stderr. These static groups are the OTEL telemetry destination
    // (traces, spans) via the x-aws-log-group header. Names must match runtime names exactly
    // (underscores, no random suffix) so the pre-created group is found by the OTEL exporter.

    // Backend (M2M-only) JWT inbound authorizer, shared by the generation +
    // suggestions runtimes. These runtimes are invoked server-to-server (REST
    // generation jobs, MCP suggestions) with NO end-user context, so they accept
    // only the confidential M2M client's client_credentials token. A runtime is
    // either IAM or JWT — never both — so going all-JWT means even these backend
    // runtimes drop SigV4 in favor of the M2M token. IAM fallback when no pool.
    const backendRuntimeAuthorizer =
      props.userPool && props.m2mClient
        ? agentcore.RuntimeAuthorizerConfiguration.usingCognito(props.userPool, [props.m2mClient])
        : agentcore.RuntimeAuthorizerConfiguration.usingIAM();

    // Chat-query runtimes (Ontology/MetadataQuery) accept BOTH the SPA user
    // client (browser token forwarded by the chat gateway via JWT_PASSTHROUGH)
    // AND the M2M client (mcp-tools Lambda / REST invoking the query tools
    // server-to-server). A runtime is either IAM or JWT — never both — and these
    // are all-JWT, so even the backend callers present a JWT (the M2M token).
    // Hoisted here so both query-runtime blocks reuse it.
    const chatRuntimeClients = [props.userPoolClient, props.m2mClient].filter(
      (c): c is cognito.IUserPoolClient => Boolean(c)
    );

    // Authorizer inputs for the consolidated CloudResourceId custom resource. CFN's
    // AWS::BedrockAgentCore::Runtime drops AuthorizerConfiguration on create, so the
    // CustomJWT authorizer is (re-)applied post-create via the control-plane
    // update-agent-runtime API. That re-apply is folded into the SAME CloudResourceId
    // CR that owns env (see the loop below + design doc) so a single full-replace
    // re-sends env AND authorizer every deploy — neither can be wiped. Only enabled
    // when BOTH the user pool and the M2M client exist — otherwise the runtime keeps
    // its IAM inbound-auth fallback and no authorizer is sent.
    const authorizerEnabled = Boolean(props.userPool && props.m2mClient);
    const authorizerDiscoveryUrl = props.userPool
      ? `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}/.well-known/openid-configuration`
      : undefined;
    // String client ids per the authorizer matrix. filter(Boolean) guards
    // against an undefined userPoolClient ever reaching allowedClients.
    const chatAuthorizerClientIds = chatRuntimeClients.map((c) => c.userPoolClientId);
    const backendAuthorizerClientIds = props.m2mClient ? [props.m2mClient.userPoolClientId] : [];

    // Declared at outer scope so the second semanticRagEnabled block (after the
    // ontologyEnabled block) can still reach them when constructing runtimes.
    let metadataLogGroup: logs.LogGroup | undefined;
    let metadataQueryLogGroup: logs.LogGroup | undefined;
    let suggestionsLogGroup: logs.LogGroup | undefined;
    let metadataArtifact: agentcore.AgentRuntimeArtifact | undefined;
    let metadataQueryArtifact: agentcore.AgentRuntimeArtifact | undefined;
    let suggestionsArtifact: agentcore.AgentRuntimeArtifact | undefined;
    let metadataBuild: ArmBuildConstruct | undefined;
    let metadataQueryBuild: ArmBuildConstruct | undefined;
    let suggestionsBuild: ArmBuildConstruct | undefined;
    // Outer-scope handles to each runtime's env block + container image tag so
    // the RuntimeFingerprint custom-resource loop below can digest them (the
    // ontology builds are block-scoped consts, so we hoist just their tags).
    let ontologyImageTag: string | undefined;
    let ontologyQueryImageTag: string | undefined;
    let ontologyRuntimeEnv: Record<string, string> | undefined;
    let ontologyQueryRuntimeEnv: Record<string, string> | undefined;
    let metadataRuntimeEnv: Record<string, string> | undefined;
    let metadataQueryRuntimeEnv: Record<string, string> | undefined;
    let querySuggestionsRuntimeEnv: Record<string, string> | undefined;

    if (semanticRagEnabled) {
      metadataLogGroup = new logs.LogGroup(this, 'MetadataAgentLogGroup', {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_metadata`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      new logs.CfnLogStream(this, 'MetadataAgentRuntimeLogsStream', {
        logGroupName: metadataLogGroup.logGroupName,
        logStreamName: 'runtime-logs',
      });
      metadataQueryLogGroup = new logs.LogGroup(this, 'MetadataQueryAgentLogGroup', {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_metadata_query`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      new logs.CfnLogStream(this, 'MetadataQueryAgentRuntimeLogsStream', {
        logGroupName: metadataQueryLogGroup.logGroupName,
        logStreamName: 'runtime-logs',
      });
      suggestionsLogGroup = new logs.LogGroup(this, 'QuerySuggestionsAgentLogGroup', {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_query_suggestions`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      new logs.CfnLogStream(this, 'QuerySuggestionsAgentRuntimeLogsStream', {
        logGroupName: suggestionsLogGroup.logGroupName,
        logStreamName: 'runtime-logs',
      });

      // Grant metadata-family agent roles permission to pull from shared ECR
      this.agentRepository.grantPull(this.metadataAgentRole!);
      this.agentRepository.grantPull(this.metadataQueryAgentRole!);
      this.agentRepository.grantPull(this.suggestionsAgentRole!);

      // Build ARM64 Docker image for Metadata Agent
      metadataBuild = new ArmBuildConstruct(this, 'MetadataAgentBuild', {
        sourcePath: '../agents',
        region: this.region,
        namePrefix: `${props.projectName}-metadata`,
        buildTimeoutMinutes: 20,
        dockerfileName: 'Dockerfile.metadata',
      });

      // Build ARM64 Docker image for Metadata Query Agent
      metadataQueryBuild = new ArmBuildConstruct(this, 'MetadataQueryAgentBuild', {
        sourcePath: '../agents',
        region: this.region,
        namePrefix: `${props.projectName}-metadata-query`,
        buildTimeoutMinutes: 20,
        dockerfileName: 'Dockerfile.metadataquery',
      });

      // Build ARM64 Docker image for Query Suggestions Agent
      suggestionsBuild = new ArmBuildConstruct(this, 'QuerySuggestionsAgentBuild', {
        sourcePath: '../agents',
        region: this.region,
        namePrefix: `${props.projectName}-query-suggestions`,
        buildTimeoutMinutes: 20,
        dockerfileName: 'Dockerfile.querysuggestions',
      });

      metadataArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
        metadataBuild.repository,
        metadataBuild.imageTag
      );

      metadataQueryArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
        metadataQueryBuild.repository,
        metadataQueryBuild.imageTag
      );

      suggestionsArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
        suggestionsBuild.repository,
        suggestionsBuild.imageTag
      );
    } // end if (semanticRagEnabled) — log groups, ECR grants, builds, artifacts

    if (ontologyEnabled) {
      // Grant ontology + query agent roles permission to pull from shared ECR
      this.agentRepository.grantPull(this.ontologyAgentRole!);
      this.agentRepository.grantPull(this.queryAgentRole!);

      // Build ARM64 Docker images for Ontology and Query agents
      const ontologyBuild = new ArmBuildConstruct(this, 'OntologyAgentBuild', {
        sourcePath: '../agents',
        region: this.region,
        namePrefix: `${props.projectName}-ontology`,
        buildTimeoutMinutes: 20,
        dockerfileName: 'Dockerfile.ontology',
      });

      const queryBuild = new ArmBuildConstruct(this, 'OntologyQueryAgentBuild', {
        sourcePath: '../agents',
        region: this.region,
        namePrefix: `${props.projectName}-ontology-query`,
        buildTimeoutMinutes: 20,
        dockerfileName: 'Dockerfile.ontologyquery',
      });

      // Hoist the image tags so the RuntimeFingerprint loop can fold them in
      // (a code-only redeploy changes only the tag, but still resets runtime env).
      ontologyImageTag = ontologyBuild.imageTag;
      ontologyQueryImageTag = queryBuild.imageTag;

      const ontologyArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
        ontologyBuild.repository,
        ontologyBuild.imageTag
      );

      const queryArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
        queryBuild.repository,
        queryBuild.imageTag
      );

      // ============================================================
      // Neptune Gateway - Create BEFORE Runtimes
      // ============================================================

      this.neptuneGateway = new AgentCoreNeptuneGateway(this, 'NeptuneGateway', {
        projectName: props.projectName,
        neptuneCluster: props.neptuneStack!.cluster,
        neptuneSecurityGroup: props.neptuneStack!.securityGroup,
        neptuneConnectionSecret: props.neptuneStack!.connectionSecret,
        vpc: props.vpc,
      });

      // Grant agent roles permission to invoke Neptune Gateway (IAM/SigV4)
      this.ontologyAgentRole!.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock-agentcore:InvokeGateway'],
          resources: [this.neptuneGateway.gatewayArn],
        })
      );

      this.queryAgentRole!.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock-agentcore:InvokeGateway'],
          resources: [this.neptuneGateway.gatewayArn],
        })
      );

      // Per-runtime OTEL log groups for ontology agents (only created when ontologyEnabled)
      const ontologyLogGroup = new logs.LogGroup(this, 'OntologyAgentLogGroup', {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_ontology`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      new logs.CfnLogStream(this, 'OntologyAgentRuntimeLogsStream', {
        logGroupName: ontologyLogGroup.logGroupName,
        logStreamName: 'runtime-logs',
      });
      const queryLogGroup = new logs.LogGroup(this, 'OntologyQueryAgentLogGroup', {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_ontology_query`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      new logs.CfnLogStream(this, 'OntologyQueryAgentRuntimeLogsStream', {
        logGroupName: queryLogGroup.logGroupName,
        logStreamName: 'runtime-logs',
      });

      // Deploy Ontology Generation Agent to AgentCore Runtime
      ontologyRuntimeEnv = {
        ...lessonsEnv,
        AWS_REGION: this.region,
        KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
        ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
        NEPTUNE_LOAD_ROLE: props.neptuneStack!.loadRole.roleArn,
        PROJECT_NAME: props.projectName,
        NEPTUNE_GATEWAY_URL: this.neptuneGateway.gatewayUrl,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        // Tier 1 governed-metric lookup hydrates an in-memory KNN index
        // from this DDB table on cold start — no OpenSearch dependency.
        METRICS_TABLE: props.dynamodbStack.metricsTable.tableName,
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_ontology.DEFAULT`,
          `aws.log.group.names=${ontologyLogGroup.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${ontologyLogGroup.logGroupName}`,
          'x-aws-log-stream=runtime-logs',
          'x-aws-metric-namespace=bedrock-agentcore',
        ].join(','),
        OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_TRACES_EXPORTER: 'otlp',
        OTEL_METRICS_EXPORTER: 'none',
        OTEL_LOGS_EXPORTER: 'otlp',
        // Force sampling on — BedrockAgentCore propagates sampled=false by default,
        // which causes the ParentBased sampler to drop all child spans. always_on
        // overrides this so Strands-level spans (tool calls, LLM turns) are exported.
        OTEL_TRACES_SAMPLER: 'always_on',
        // Suppress low-level AWS SDK auto-instrumentation — we only want Strands spans.
        OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
        // Enable latest GenAI semantic conventions in Strands spans — adds provider name,
        // token usage, and tool definitions as span attributes for richer observability.
        // gen_ai_latest_experimental emits messages with parts[] arrays instead of
        // simple content strings. The AgentCore evaluation service expects the stable
        // OTEL format (content as string) — experimental format causes null scores.
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
        // Cap span *attributes* at 4 KB (AgentCore aws/spans recommendation) while
        // leaving span event attributes uncapped. OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT
        // would truncate both attributes AND event attributes (the Python SDK applies
        // the global limit to add_event() too — there is no separate event limit).
        // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT only affects span attributes,
        // preserving full tool-output content in span events for the evaluator.
        OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4096',
      };
      this.ontologyRuntime = new agentcore.Runtime(this, 'OntologyGenerationRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_ontology`,
        agentRuntimeArtifact: ontologyArtifact,
        executionRole: this.ontologyAgentRole!,
        description: 'AgentCore Runtime for Ontology Generation Agent',
        authorizerConfiguration: backendRuntimeAuthorizer,
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: ontologyRuntimeEnv,
      });

      // The CUSTOM_JWT authorizer is applied (and re-applied on every deploy) by
      // the consolidated CloudResourceId custom resource below — see that block
      // and docs/plans/2026-06-04-runtime-authorizer-wipe-design.md.

      // Deploy Ontology Query Agent to AgentCore Runtime
      ontologyQueryRuntimeEnv = {
        ...lessonsEnv,
        AWS_REGION: this.region,
        ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        PROJECT_NAME: props.projectName,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        // Tier 1 governed-metric lookup hydrates an in-memory KNN index
        // from this DDB table on cold start — no OpenSearch dependency.
        METRICS_TABLE: props.dynamodbStack.metricsTable.tableName,
        // Read by agents/shared/prior_results.py to lazily fetch full SQL
        // rows from a prior assistant turn when the user follow-up needs
        // them — keeps history token cost low.
        CHAT_SESSIONS_TABLE: props.dynamodbStack.chatSessionsTable.tableName,
        NEPTUNE_GATEWAY_URL: this.neptuneGateway.gatewayUrl,
        // Drive AG-UI tool_call_* and message_chunk events from the live
        // Strands callback handler instead of synthesising them after the
        // agent finishes. Without this the chat UI sees a 30-60s silent
        // wait followed by a flood of events at the end.
        ENABLE_LIVE_STREAMING: 'true',
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_ontology_query.DEFAULT`,
          `aws.log.group.names=${queryLogGroup.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${queryLogGroup.logGroupName}`,
          'x-aws-log-stream=runtime-logs',
          'x-aws-metric-namespace=bedrock-agentcore',
        ].join(','),
        OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_TRACES_EXPORTER: 'otlp',
        OTEL_METRICS_EXPORTER: 'none',
        OTEL_LOGS_EXPORTER: 'otlp',
        // Force sampling on — BedrockAgentCore propagates sampled=false by default,
        // which causes the ParentBased sampler to drop all child spans. always_on
        // overrides this so Strands-level spans (tool calls, LLM turns) are exported.
        OTEL_TRACES_SAMPLER: 'always_on',
        // Suppress low-level AWS SDK auto-instrumentation — we only want Strands spans.
        OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
        // Enable latest GenAI semantic conventions in Strands spans — adds provider name,
        // token usage, and tool definitions as span attributes for richer observability.
        // gen_ai_latest_experimental emits messages with parts[] arrays instead of
        // simple content strings. The AgentCore evaluation service expects the stable
        // OTEL format (content as string) — experimental format causes null scores.
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
        // Cap span *attributes* at 4 KB (AgentCore aws/spans recommendation) while
        // leaving span event attributes uncapped. OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT
        // would truncate both attributes AND event attributes (the Python SDK applies
        // the global limit to add_event() too — there is no separate event limit).
        // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT only affects span attributes,
        // preserving full tool-output content in span events for the evaluator.
        OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4096',
      };
      // Chat query runtimes use a Cognito JWT inbound authorizer (when the
      // user pool is wired) so the chat gateway's JWT_PASSTHROUGH-forwarded
      // access token is re-validated at the runtime and its `sub` is readable
      // for chat-session ownership. Falls back to IAM when the pool is absent.
      // chatRuntimeClients (SPA user client + M2M client) is hoisted to outer
      // scope so the metadata-query block can reuse it. See its definition near
      // backendRuntimeAuthorizer.
      const chatRuntimeAuthorizer =
        props.userPool && chatRuntimeClients.length > 0
          ? agentcore.RuntimeAuthorizerConfiguration.usingCognito(
              props.userPool,
              chatRuntimeClients
            )
          : agentcore.RuntimeAuthorizerConfiguration.usingIAM();
      // Allow the validated Authorization header through to the agent code so
      // _user_id_from_context can decode the Cognito `sub`.
      const chatRuntimeHeaders =
        props.userPool && chatRuntimeClients.length > 0
          ? { allowlistedHeaders: ['Authorization'] }
          : undefined;

      this.queryRuntime = new agentcore.Runtime(this, 'OntologyQueryRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_ontology_query`,
        agentRuntimeArtifact: queryArtifact,
        executionRole: this.queryAgentRole!,
        description: 'AgentCore Runtime for Ontology Query Agent',
        authorizerConfiguration: chatRuntimeAuthorizer,
        requestHeaderConfiguration: chatRuntimeHeaders,
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: ontologyQueryRuntimeEnv,
      });

      // The chat-query CUSTOM_JWT authorizer (SPA user token + M2M) and the
      // Authorization header allowlist are applied/re-applied by the consolidated
      // CloudResourceId custom resource below (see the design doc).

      // Grant ontology/query roles permission to pull from their build ECR repositories
      ontologyBuild.repository.grantPull(this.ontologyAgentRole!);
      queryBuild.repository.grantPull(this.queryAgentRole!);

      // Ensure runtimes are created after builds complete
      this.ontologyRuntime.node.addDependency(ontologyBuild.buildCompletion);
      this.queryRuntime.node.addDependency(queryBuild.buildCompletion);

      // Runtimes depend on the SG ENI drainer CR so on delete CFN tears the
      // runtimes down first; the drainer then polls until ENIs detach before
      // the SG is deleted. Avoids the SG ⇄ CR cycle from a direct SG dep.
      this.ontologyRuntime.node.addDependency(sgEniDrainerCustomResource);
      this.queryRuntime.node.addDependency(sgEniDrainerCustomResource);

      this.ontologyRuntimeArn = this.ontologyRuntime.agentRuntimeArn;
      this.queryRuntimeArn = this.queryRuntime.agentRuntimeArn;
    } // end if (ontologyEnabled) — builds, gateway, ontology/query runtimes

    if (semanticRagEnabled) {
      // Deploy Metadata Agent to AgentCore Runtime
      metadataRuntimeEnv = {
        ...lessonsEnv,
        AWS_REGION: this.region,
        KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
        // Semantic RAG KB — metadata_agent writes docs to S3 then triggers ingestion
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId!,
        SEMANTIC_RAG_DATA_SOURCE_ID: ragDataSourceId!,
        ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        PROJECT_NAME: props.projectName,
        ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_metadata.DEFAULT`,
          `aws.log.group.names=${metadataLogGroup!.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${metadataLogGroup!.logGroupName}`,
          'x-aws-log-stream=runtime-logs',
          'x-aws-metric-namespace=bedrock-agentcore',
        ].join(','),
        OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_TRACES_EXPORTER: 'otlp',
        OTEL_METRICS_EXPORTER: 'none',
        OTEL_LOGS_EXPORTER: 'otlp',
        // Force sampling on — BedrockAgentCore propagates sampled=false by default,
        // which causes the ParentBased sampler to drop all child spans. always_on
        // overrides this so Strands-level spans (tool calls, LLM turns) are exported.
        OTEL_TRACES_SAMPLER: 'always_on',
        // Suppress low-level AWS SDK auto-instrumentation — we only want Strands spans.
        OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
        // Enable latest GenAI semantic conventions in Strands spans — adds provider name,
        // token usage, and tool definitions as span attributes for richer observability.
        // gen_ai_latest_experimental emits messages with parts[] arrays instead of
        // simple content strings. The AgentCore evaluation service expects the stable
        // OTEL format (content as string) — experimental format causes null scores.
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
        // Cap span *attributes* at 4 KB (AgentCore aws/spans recommendation) while
        // leaving span event attributes uncapped. OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT
        // would truncate both attributes AND event attributes (the Python SDK applies
        // the global limit to add_event() too — there is no separate event limit).
        // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT only affects span attributes,
        // preserving full tool-output content in span events for the evaluator.
        OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4096',
      };
      this.metadataRuntime = new agentcore.Runtime(this, 'MetadataRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_metadata`,
        agentRuntimeArtifact: metadataArtifact!,
        executionRole: this.metadataAgentRole!,
        description:
          'AgentCore Runtime for Metadata Generation Agent (Glue Catalog + KB enrichment)',
        authorizerConfiguration: backendRuntimeAuthorizer,
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: metadataRuntimeEnv,
      });

      // The CUSTOM_JWT authorizer is applied/re-applied by the consolidated
      // CloudResourceId custom resource below (see the design doc).

      // Deploy Metadata Query Agent to AgentCore Runtime
      metadataQueryRuntimeEnv = {
        ...lessonsEnv,
        AWS_REGION: this.region,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        // Read by agents/shared/prior_results.py — see ontology runtime env.
        CHAT_SESSIONS_TABLE: props.dynamodbStack.chatSessionsTable.tableName,
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId!,
        // Phase-1 RAG reranking — over-fetch then rerank the candidate table docs
        // down to top_k so query-relevant tables survive instead of raw vector
        // order (see retrieve_kb_context_structured). Cohere Rerank 3.5 is the only
        // reranker available in us-east-1. The KB service role holds bedrock:Rerank
        // (bedrock-kb-stack.ts). Set to '' to disable reranking.
        RERANK_MODEL_ID: 'cohere.rerank-v3-5:0',
        ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        PROJECT_NAME: props.projectName,
        // Tier 1 governed-metric lookup hydrates an in-memory KNN index
        // from this DDB table on cold start — no OpenSearch dependency.
        METRICS_TABLE: props.dynamodbStack.metricsTable.tableName,
        // Live AG-UI streaming — see comment on ontologyQueryRuntimeEnv.
        ENABLE_LIVE_STREAMING: 'true',
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_metadata_query.DEFAULT`,
          `aws.log.group.names=${metadataQueryLogGroup!.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${metadataQueryLogGroup!.logGroupName}`,
          'x-aws-log-stream=runtime-logs',
          'x-aws-metric-namespace=bedrock-agentcore',
        ].join(','),
        OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_TRACES_EXPORTER: 'otlp',
        OTEL_METRICS_EXPORTER: 'none',
        OTEL_LOGS_EXPORTER: 'otlp',
        // Force sampling on — BedrockAgentCore propagates sampled=false by default,
        // which causes the ParentBased sampler to drop all child spans. always_on
        // overrides this so Strands-level spans (tool calls, LLM turns) are exported.
        OTEL_TRACES_SAMPLER: 'always_on',
        // Suppress low-level AWS SDK auto-instrumentation — we only want Strands spans.
        OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
        // Enable latest GenAI semantic conventions in Strands spans — adds provider name,
        // token usage, and tool definitions as span attributes for richer observability.
        // gen_ai_latest_experimental emits messages with parts[] arrays instead of
        // simple content strings. The AgentCore evaluation service expects the stable
        // OTEL format (content as string) — experimental format causes null scores.
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
        // Cap span *attributes* at 4 KB (AgentCore aws/spans recommendation) while
        // leaving span event attributes uncapped. OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT
        // would truncate both attributes AND event attributes (the Python SDK applies
        // the global limit to add_event() too — there is no separate event limit).
        // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT only affects span attributes,
        // preserving full tool-output content in span events for the evaluator.
        OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4096',
      };
      // Same Cognito JWT inbound authorizer + Authorization passthrough as the
      // ontology-query runtime (SPA user token via gateway passthrough + M2M for
      // backend callers; see chatRuntimeClients above); IAM fallback when no pool.
      const mqChatAuthorizer =
        props.userPool && chatRuntimeClients.length > 0
          ? agentcore.RuntimeAuthorizerConfiguration.usingCognito(
              props.userPool,
              chatRuntimeClients
            )
          : agentcore.RuntimeAuthorizerConfiguration.usingIAM();
      const mqChatHeaders =
        props.userPool && chatRuntimeClients.length > 0
          ? { allowlistedHeaders: ['Authorization'] }
          : undefined;

      this.metadataQueryRuntime = new agentcore.Runtime(this, 'MetadataQueryRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_metadata_query`,
        agentRuntimeArtifact: metadataQueryArtifact!,
        executionRole: this.metadataQueryAgentRole!,
        description: 'AgentCore Runtime for Metadata Query Agent (Bedrock KB + Athena)',
        authorizerConfiguration: mqChatAuthorizer,
        requestHeaderConfiguration: mqChatHeaders,
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: metadataQueryRuntimeEnv,
      });

      // The chat-query CUSTOM_JWT authorizer (SPA user token + M2M) and the
      // Authorization header allowlist are applied/re-applied by the consolidated
      // CloudResourceId custom resource below (see the design doc).

      // Deploy Query Suggestions Agent to AgentCore Runtime
      querySuggestionsRuntimeEnv = {
        ...lessonsEnv,
        AWS_REGION: this.region,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        // Governed-metric catalog — advisory mode enumerates a layer's metrics.
        METRICS_TABLE: props.dynamodbStack.metricsTable.tableName,
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId!,
        PROJECT_NAME: props.projectName,
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_query_suggestions.DEFAULT`,
          `aws.log.group.names=${suggestionsLogGroup!.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${suggestionsLogGroup!.logGroupName}`,
          'x-aws-log-stream=runtime-logs',
          'x-aws-metric-namespace=bedrock-agentcore',
        ].join(','),
        OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
        OTEL_TRACES_EXPORTER: 'otlp',
        OTEL_METRICS_EXPORTER: 'none',
        OTEL_LOGS_EXPORTER: 'otlp',
        // Force sampling on — BedrockAgentCore propagates sampled=false by default,
        // which causes the ParentBased sampler to drop all child spans. always_on
        // overrides this so Strands-level spans (tool calls, LLM turns) are exported.
        OTEL_TRACES_SAMPLER: 'always_on',
        // Suppress low-level AWS SDK auto-instrumentation — we only want Strands spans.
        OTEL_PYTHON_DISABLED_INSTRUMENTATIONS: 'botocore,requests,urllib3',
        // Enable latest GenAI semantic conventions in Strands spans — adds provider name,
        // token usage, and tool definitions as span attributes for richer observability.
        // gen_ai_latest_experimental emits messages with parts[] arrays instead of
        // simple content strings. The AgentCore evaluation service expects the stable
        // OTEL format (content as string) — experimental format causes null scores.
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_tool_definitions',
        // Cap span *attributes* at 4 KB (AgentCore aws/spans recommendation) while
        // leaving span event attributes uncapped. OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT
        // would truncate both attributes AND event attributes (the Python SDK applies
        // the global limit to add_event() too — there is no separate event limit).
        // OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT only affects span attributes,
        // preserving full tool-output content in span events for the evaluator.
        OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT: '4096',
      };
      this.suggestionsRuntime = new agentcore.Runtime(this, 'QuerySuggestionsRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_query_suggestions`,
        agentRuntimeArtifact: suggestionsArtifact!,
        executionRole: this.suggestionsAgentRole!,
        description:
          'AgentCore Runtime for Query Suggestions Agent — generates contextual questions from KB',
        authorizerConfiguration: backendRuntimeAuthorizer,
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: querySuggestionsRuntimeEnv,
      });

      // The CUSTOM_JWT authorizer is applied/re-applied by the consolidated
      // CloudResourceId custom resource below (see the design doc).

      // Grant metadata/suggestions roles permission to pull from build ECR repositories
      metadataBuild!.repository.grantPull(this.metadataAgentRole!);
      metadataQueryBuild!.repository.grantPull(this.metadataQueryAgentRole!);
      suggestionsBuild!.repository.grantPull(this.suggestionsAgentRole!);

      // Ensure runtimes are created after the builds complete
      this.metadataRuntime.node.addDependency(metadataBuild!.buildCompletion);
      this.metadataQueryRuntime.node.addDependency(metadataQueryBuild!.buildCompletion);
      this.suggestionsRuntime.node.addDependency(suggestionsBuild!.buildCompletion);

      // Runtimes depend on the SG ENI drainer CR — see comment at the
      // ontology/query block above for why this avoids the SG ⇄ CR cycle.
      this.metadataRuntime.node.addDependency(sgEniDrainerCustomResource);
      this.metadataQueryRuntime.node.addDependency(sgEniDrainerCustomResource);
      this.suggestionsRuntime.node.addDependency(sgEniDrainerCustomResource);

      // Store ARNs
      this.metadataRuntimeArn = this.metadataRuntime.agentRuntimeArn;
      this.metadataQueryRuntimeArn = this.metadataQueryRuntime.agentRuntimeArn;
      this.suggestionsRuntimeArn = this.suggestionsRuntime.agentRuntimeArn;
    } // end if (semanticRagEnabled) — metadata/metadata-query/suggestions runtimes

    // ── Native log delivery (APPLICATION_LOGS + USAGE_LOGS) ─────────────────
    // Configures AgentCore Runtime → CloudWatch Logs delivery so that:
    //   1. The AgentCore console shows "Log delivery: N"
    //   2. APPLICATION_LOGS session data flows to the log group used by online eval
    //   3. USAGE_LOGS token/CPU/memory metrics populate the GenAI Observability
    //      "Resource consumption" dashboard section
    const pn = props.projectName.replace(/-/g, '_');

    const runtimeDeliveries: { id: string; runtimeArn: string; runtimeName: string }[] = [];
    if (semanticRagEnabled) {
      runtimeDeliveries.push(
        { id: 'Metadata', runtimeArn: this.metadataRuntimeArn!, runtimeName: `${pn}_metadata` },
        {
          id: 'MetadataQuery',
          runtimeArn: this.metadataQueryRuntimeArn!,
          runtimeName: `${pn}_metadata_query`,
        },
        {
          id: 'QuerySuggestions',
          runtimeArn: this.suggestionsRuntimeArn!,
          runtimeName: `${pn}_query_suggestions`,
        }
      );
    }
    if (ontologyEnabled && this.ontologyRuntimeArn) {
      runtimeDeliveries.push({
        id: 'Ontology',
        runtimeArn: this.ontologyRuntimeArn,
        runtimeName: `${pn}_ontology`,
      });
    }
    if (ontologyEnabled && this.queryRuntimeArn) {
      runtimeDeliveries.push({
        id: 'OntologyQuery',
        runtimeArn: this.queryRuntimeArn,
        runtimeName: `${pn}_ontology_query`,
      });
    }

    for (const { id, runtimeArn, runtimeName } of runtimeDeliveries) {
      const baseName = runtimeName.replace(/_/g, '-');

      // USAGE_LOGS → separate log group for token/CPU/memory metrics
      // (populates "Resource consumption" section in GenAI Observability dashboard)
      const usageLogGroup = new logs.LogGroup(this, `${id}UsageLogGroup`, {
        logGroupName: `/aws/bedrock-agentcore/runtimes/${runtimeName}-usage`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });
      usageLogGroup.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: `AllowUsageDelivery${id}`,
          principals: [new iam.ServicePrincipal('delivery.logs.amazonaws.com')],
          actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [`${usageLogGroup.logGroupArn}:log-stream:*`],
          conditions: {
            StringEquals: { 'aws:SourceAccount': this.account },
            ArnLike: { 'aws:SourceArn': `arn:aws:logs:${this.region}:${this.account}:*` },
          },
        })
      );

      const usageLogsSrc = new logs.CfnDeliverySource(this, `${id}UsageLogsSrc`, {
        name: `${baseName}-usage-src`,
        resourceArn: runtimeArn,
        logType: 'USAGE_LOGS',
      });
      const usageLogsDest = new logs.CfnDeliveryDestination(this, `${id}UsageLogsDest`, {
        name: `${baseName}-usage-dest`,
        destinationResourceArn: usageLogGroup.logGroupArn,
      });
      new logs.CfnDelivery(this, `${id}UsageLogsDelivery`, {
        deliverySourceName: usageLogsSrc.ref,
        deliveryDestinationArn: usageLogsDest.attrArn,
      });
    }

    // ── cloud.resource_id post-creation injection ─────────────────────────────
    // The runtime ARN can't self-reference at CloudFormation create time
    // (circular dependency), so we inject cloud.resource_id into
    // OTEL_RESOURCE_ATTRIBUTES after creation via a Lambda custom resource.
    // Required by bedrock_agentcore_starter_toolkit on-demand eval span query:
    //   `parse resource.attributes.cloud.resource_id "runtime/*/" as parsedAgentId`
    const cloudResourceIdHandlerDir = path.join(__dirname, 'agentcore-cloud-resource-id-handler');
    const cloudResourceIdHandler = new lambda.Function(this, 'CloudResourceIdHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.on_event',
      code: lambda.Code.fromAsset(cloudResourceIdHandlerDir, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                execSync(
                  // nosemgrep: detect-child-process — CDK synth-time execSync on framework outputDir + static repo paths
                  `pip install --quiet --target "${outputDir}" -r "${path.join(cloudResourceIdHandlerDir, 'requirements.txt')}"`,
                  { stdio: 'pipe' }
                );
                for (const f of fs.readdirSync(cloudResourceIdHandlerDir)) {
                  if (f.endsWith('.py')) {
                    fs.copyFileSync(
                      path.join(cloudResourceIdHandlerDir, f),
                      // nosemgrep: detect-non-literal-fs-filename,path-join-resolve-traversal — CDK build dir / static repo path, not user input; static repo path constants, build-time, not request input
                      path.join(outputDir, f)
                    ); // nosemgrep: path-join-resolve-traversal,detect-non-literal-fs-filename — CDK synth-time paths, not user input
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
      timeout: cdk.Duration.minutes(5),
      description:
        'Injects cloud.resource_id into AgentCore Runtime OTEL_RESOURCE_ATTRIBUTES post-creation',
    });

    cloudResourceIdHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock-agentcore:GetAgentRuntime', 'bedrock-agentcore:UpdateAgentRuntime'],
        resources: ['*'],
      })
    );

    // Needed to create the 'runtime-logs' stream in the {runtimeId}-DEFAULT log group
    cloudResourceIdHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['logs:CreateLogStream'],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*:*`,
        ],
      })
    );

    // update_agent_runtime passes the execution role ARN back — Lambda needs PassRole
    const passRoleArns: string[] = [];
    if (this.metadataAgentRole) passRoleArns.push(this.metadataAgentRole.roleArn);
    if (this.metadataQueryAgentRole) passRoleArns.push(this.metadataQueryAgentRole.roleArn);
    if (this.suggestionsAgentRole) passRoleArns.push(this.suggestionsAgentRole.roleArn);
    if (this.ontologyAgentRole) passRoleArns.push(this.ontologyAgentRole.roleArn);
    if (this.queryAgentRole) passRoleArns.push(this.queryAgentRole.roleArn);
    if (passRoleArns.length > 0) {
      cloudResourceIdHandler.addToRolePolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['iam:PassRole'],
          resources: passRoleArns,
        })
      );
    }

    const cloudResourceIdProvider = new cr.Provider(this, 'CloudResourceIdProvider', {
      onEventHandler: cloudResourceIdHandler,
    });

    // RuntimeFingerprint — synth-time SHA-256 digest of each runtime's FULL env
    // block + container image tag. The CloudResourceIdHandler Lambda re-patches
    // the runtime's env post-create (cloud.resource_id etc., needed by AgentCore
    // Evaluations) and only re-fires when CFN sees a property change on its
    // CustomResource. CFN re-pushes a runtime's BASE env — wiping that patch —
    // whenever the runtime resource updates, which happens on (a) any env change
    // AND (b) a code-only redeploy that changes just the image tag. The previous
    // OTEL-keys-only digest missed both non-OTEL env churn and the image-tag
    // case, so routine deploys silently stripped the patch and broke evals.
    // Folding env + imageTag in makes the patch re-apply on exactly the deploys
    // that reset it, and stay skipped otherwise. See otel-fingerprint.ts.
    const envBlocksById: Record<string, Record<string, string>> = {};
    const imageTagById: Record<string, string> = {};
    // Authorizer inputs per runtime. The CloudResourceId handler is now the SINGLE
    // authoritative full-replace caller (it owns env AND the JWT authorizer), so it
    // must be told each runtime's intended authorizer or a container-only redeploy
    // would re-push the runtime without it and wipe it. allowedClients is [] (and
    // allowlistedHeaders undefined) when the pool is absent → handler omits the
    // authorizer and the runtime keeps its IAM inbound-auth fallback.
    type CloudResourceIdRuntime = {
      id: string;
      runtimeArn: string;
      allowedClients: string[];
      allowlistedHeaders?: string[];
    };
    const cloudResourceIdRuntimes: CloudResourceIdRuntime[] = [];
    if (semanticRagEnabled) {
      envBlocksById['Metadata'] = metadataRuntimeEnv!;
      envBlocksById['MetadataQuery'] = metadataQueryRuntimeEnv!;
      envBlocksById['QuerySuggestions'] = querySuggestionsRuntimeEnv!;
      imageTagById['Metadata'] = metadataBuild!.imageTag;
      imageTagById['MetadataQuery'] = metadataQueryBuild!.imageTag;
      imageTagById['QuerySuggestions'] = suggestionsBuild!.imageTag;
      cloudResourceIdRuntimes.push(
        // Metadata + QuerySuggestions: backend (M2M-only) runtimes, no header allowlist.
        {
          id: 'Metadata',
          runtimeArn: this.metadataRuntimeArn!,
          allowedClients: backendAuthorizerClientIds,
        },
        // MetadataQuery: chat-query runtime — SPA user token + M2M, Authorization passthrough.
        {
          id: 'MetadataQuery',
          runtimeArn: this.metadataQueryRuntimeArn!,
          allowedClients: chatAuthorizerClientIds,
          allowlistedHeaders: authorizerEnabled ? ['Authorization'] : undefined,
        },
        {
          id: 'QuerySuggestions',
          runtimeArn: this.suggestionsRuntimeArn!,
          allowedClients: backendAuthorizerClientIds,
        }
      );
    }
    if (ontologyEnabled && this.ontologyRuntimeArn) {
      envBlocksById['Ontology'] = ontologyRuntimeEnv!;
      imageTagById['Ontology'] = ontologyImageTag!;
      // Ontology generation: backend (M2M-only) runtime, no header allowlist.
      cloudResourceIdRuntimes.push({
        id: 'Ontology',
        runtimeArn: this.ontologyRuntimeArn,
        allowedClients: backendAuthorizerClientIds,
      });
    }
    if (ontologyEnabled && this.queryRuntimeArn) {
      envBlocksById['OntologyQuery'] = ontologyQueryRuntimeEnv!;
      imageTagById['OntologyQuery'] = ontologyQueryImageTag!;
      // OntologyQuery: chat-query runtime — SPA user token + M2M, Authorization passthrough.
      cloudResourceIdRuntimes.push({
        id: 'OntologyQuery',
        runtimeArn: this.queryRuntimeArn,
        allowedClients: chatAuthorizerClientIds,
        allowlistedHeaders: authorizerEnabled ? ['Authorization'] : undefined,
      });
    }
    for (const { id, runtimeArn, allowedClients, allowlistedHeaders } of cloudResourceIdRuntimes) {
      // Only attach a CUSTOM_JWT authorizer when the pool/clients exist; otherwise
      // pass empty clients so the handler omits the authorizer (IAM fallback).
      const authzEnabled = authorizerEnabled && allowedClients.length > 0;
      const crProps: Record<string, unknown> = {
        AgentRuntimeArn: runtimeArn,
        Region: this.region,
        // AUTHORITATIVE ENV: pass the full synthesized base env block so the
        // handler RESTORES it on every deploy (source of truth = CDK), layering
        // the cloud.resource_id / log-group OTEL patches on top. This self-heals
        // any out-of-band update_agent_runtime that wiped env (full-replace API:
        // omitting environmentVariables nulls all vars → agent hits hardcoded
        // defaults → wrong table → AccessDenied). See the handler docstring.
        BaseEnvironmentVariables: envBlocksById[id],
      };
      // AUTHORITATIVE AUTHORIZER: pass discovery URL + allowed clients so the
      // SAME handler re-applies the CUSTOM_JWT authorizer on every full-replace.
      // This resource re-fires on an image-tag change, so folding the authorizer
      // in here means a container-only redeploy re-sends everything in one
      // full-replace and can't leave the authorizer unset. See the design doc.
      if (authzEnabled) {
        crProps.DiscoveryUrl = authorizerDiscoveryUrl!;
        crProps.AllowedClients = allowedClients;
        if (allowlistedHeaders) {
          crProps.AllowlistedHeaders = allowlistedHeaders;
        }
      }
      // RuntimeFingerprint folds env + imageTag + the authorizer inputs: any change
      // that should re-push runtime state (env edit, code redeploy, or an
      // allowedClients/headers change) flips the digest and re-fires the handler.
      crProps.RuntimeFingerprint = runtimeFingerprint(this, envBlocksById[id], imageTagById[id], {
        allowedClients: authzEnabled ? [...allowedClients].sort() : [],
        allowlistedHeaders:
          authzEnabled && allowlistedHeaders ? [...allowlistedHeaders].sort() : [],
      });
      new cdk.CustomResource(this, `${id}CloudResourceId`, {
        serviceToken: cloudResourceIdProvider.serviceToken,
        properties: crProps,
      });
    }

    // Outputs
    new cdk.CfnOutput(this, 'AgentRepositoryUri', {
      value: this.agentRepository.repositoryUri,
      description: 'ECR repository URI for agent images',
      exportName: `${props.projectName}-agent-repo`,
    });

    if (ontologyEnabled) {
      new cdk.CfnOutput(this, 'OntologyAgentRoleArn', {
        value: this.ontologyAgentRole!.roleArn,
        description: 'IAM role for ontology generation agent',
        exportName: `${props.projectName}-ontology-agent-role`,
      });

      new cdk.CfnOutput(this, 'QueryAgentRoleArn', {
        value: this.queryAgentRole!.roleArn,
        description: 'IAM role for ontology query agent',
        exportName: `${props.projectName}-query-agent-role`,
      });

      new cdk.CfnOutput(this, 'OntologyRuntimeArn', {
        value: this.ontologyRuntimeArn!,
        description: 'AgentCore Runtime ARN for Ontology Agent',
        exportName: `${props.projectName}-ontology-runtime-arn`,
      });

      new cdk.CfnOutput(this, 'QueryRuntimeArn', {
        value: this.queryRuntimeArn!,
        description: 'AgentCore Runtime ARN for Ontology Query Agent',
        exportName: `${props.projectName}-query-runtime-arn`,
      });

      new cdk.CfnOutput(this, 'OntologyRuntimeEndpoint', {
        value: cdk.Fn.join('', [
          'https://bedrock-agentcore.',
          this.region,
          '.amazonaws.com/runtimes/',
          this.ontologyRuntimeArn!,
        ]),
        description: 'AgentCore Runtime endpoint for Ontology Agent',
      });

      new cdk.CfnOutput(this, 'QueryRuntimeEndpoint', {
        value: cdk.Fn.join('', [
          'https://bedrock-agentcore.',
          this.region,
          '.amazonaws.com/runtimes/',
          this.queryRuntimeArn!,
        ]),
        description: 'AgentCore Runtime endpoint for Ontology Query Agent',
      });
    }

    if (semanticRagEnabled) {
      new cdk.CfnOutput(this, 'MetadataAgentRoleArn', {
        value: this.metadataAgentRole!.roleArn,
        description: 'IAM role for metadata agent',
        exportName: `${props.projectName}-metadata-agent-role`,
      });

      new cdk.CfnOutput(this, 'MetadataQueryAgentRoleArn', {
        value: this.metadataQueryAgentRole!.roleArn,
        description: 'IAM role for metadata query agent',
        exportName: `${props.projectName}-metadata-query-agent-role`,
      });

      new cdk.CfnOutput(this, 'MetadataRuntimeArn', {
        value: this.metadataRuntimeArn!,
        description: 'AgentCore Runtime ARN for Metadata Agent',
        exportName: `${props.projectName}-metadata-runtime-arn`,
      });

      new cdk.CfnOutput(this, 'MetadataQueryRuntimeArn', {
        value: this.metadataQueryRuntimeArn!,
        description: 'AgentCore Runtime ARN for Metadata Query Agent',
        exportName: `${props.projectName}-metadata-query-runtime-arn`,
      });

      new cdk.CfnOutput(this, 'MetadataRuntimeEndpoint', {
        value: cdk.Fn.join('', [
          'https://bedrock-agentcore.',
          this.region,
          '.amazonaws.com/runtimes/',
          this.metadataRuntimeArn!,
        ]),
        description: 'AgentCore Runtime endpoint for Metadata Agent',
      });

      new cdk.CfnOutput(this, 'MetadataQueryRuntimeEndpoint', {
        value: cdk.Fn.join('', [
          'https://bedrock-agentcore.',
          this.region,
          '.amazonaws.com/runtimes/',
          this.metadataQueryRuntimeArn!,
        ]),
        description: 'AgentCore Runtime endpoint for Metadata Query Agent',
      });

      new cdk.CfnOutput(this, 'MetadataAgentLogGroupName', {
        value: metadataLogGroup!.logGroupName,
        description: 'CloudWatch log group for Metadata Agent OTEL telemetry',
      });
      new cdk.CfnOutput(this, 'MetadataQueryAgentLogGroupName', {
        value: metadataQueryLogGroup!.logGroupName,
        description: 'CloudWatch log group for Metadata Query Agent OTEL telemetry',
      });
      new cdk.CfnOutput(this, 'QuerySuggestionsAgentLogGroupName', {
        value: suggestionsLogGroup!.logGroupName,
        description: 'CloudWatch log group for Query Suggestions Agent OTEL telemetry',
      });
    }

    // Environment configuration for agent deployment
    new cdk.CfnOutput(this, 'AgentEnvironmentConfig', {
      value: JSON.stringify({
        ...(ontologyEnabled
          ? {
              ONTOLOGY_AGENT: {
                AWS_REGION: this.region,
                KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
                ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
                NEPTUNE_LOAD_ROLE: props.neptuneStack!.loadRole.roleArn,
                NEPTUNE_GATEWAY_URL: this.neptuneGateway!.gatewayUrl,
                ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
                ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
              },
              QUERY_AGENT: {
                AWS_REGION: this.region,
                BEDROCK_KB_ID: props.bedrockKbStack.semanticRagKbId,
                ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
                NEPTUNE_GATEWAY_URL: this.neptuneGateway!.gatewayUrl,
              },
            }
          : {}),
        ...(semanticRagEnabled
          ? {
              METADATA_AGENT: {
                AWS_REGION: this.region,
                KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
                SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId,
                SEMANTIC_RAG_DATA_SOURCE_ID: ragDataSourceId,
                ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
                ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
                ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
              },
              METADATA_QUERY_AGENT: {
                AWS_REGION: this.region,
                BEDROCK_KB_ID: props.bedrockKbStack.semanticRagKbId,
                ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
              },
            }
          : {}),
      }),
      description: 'Environment configuration for agents (Neptune access via Gateway)',
    });

    // Topic-router KNN index is now hydrated lazily inside the agent runtime
    // on first Tier 2 lookup per namespace (see
    // agents/shared/knn_hydration.hydrate_topic_router_namespace). The
    // EventBridge-driven rebuild Lambda has been removed along with the
    // OpenSearch Serverless collection it wrote into.

    // LakeFormation CfnPermissions fail to delete when the underlying Glue/DynamoDB resources
    // are removed first (e.g. during a full cdk destroy). Since the permissions are virtual and
    // become a no-op once the underlying resources are gone, retaining them is safe.
    this.node.findAll().forEach((child) => {
      if (child instanceof lakeformation.CfnPermissions) {
        child.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
      }
    });
  }
}
