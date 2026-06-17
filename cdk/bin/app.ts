#!/usr/bin/env node
/**
 * Semantic Layer CDK Application
 *
 * Deploys complete infrastructure for insurance semantic layer including:
 * - Cognito authentication and authorization
 * - DynamoDB for operational data
 * - S3 data lake for historical data
 * - AWS Glue for metadata catalog
 * - Amazon Neptune for knowledge graph
 * - Amazon Bedrock Knowledge Base for ontology patterns
 * - Amazon Athena for federated queries
 * - Lambda REST API for semantic layer operations
 * - Bedrock AgentCore Runtime with Strands agents
 * - React frontend with CloudFront
 */

import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { NetworkingStack } from '../lib/stacks/backend/networking-stack';
import { DynamoDBStack } from '../lib/stacks/backend/dynamodb-stack';
import { DataLakeStack } from '../lib/stacks/backend/data-lake-stack';
import { DocPipelineStack } from '../lib/stacks/backend/doc-pipeline-stack';
import { McpServerStack } from '../lib/stacks/backend/mcp-server-stack';
import { McpProxyStack } from '../lib/stacks/backend/mcp-proxy-stack';
import { MCP_PKCE_CALLBACK_URLS } from '../lib/common/auth-constants';
import { DynamoDBStreamProcessorStack } from '../lib/stacks/backend/dynamodb-stream-processor-stack';
import { GlueCatalogStack } from '../lib/stacks/backend/glue-catalog-stack';
import { NeptuneStack } from '../lib/stacks/backend/neptune-stack';
import { BedrockKnowledgeBaseStack } from '../lib/stacks/backend/bedrock-kb-stack';
import { AthenaStack } from '../lib/stacks/backend/athena-stack';
import { AgentCoreStack } from '../lib/stacks/backend/agentcore-stack';
import { AgentCoreEvalStack } from '../lib/stacks/backend/agentcore-eval-stack';
import { AgentCoreMemoryStack } from '../lib/stacks/backend/agentcore-memory-stack';
import { FrontendStack } from '../lib/stacks/frontend';
import { CloudFrontStorageStack } from '../lib/stacks/frontend/cloudfront-storage';
import { AuthStack } from '../lib/stacks/backend/auth';
import { LambdaRestApiStack } from '../lib/stacks/backend/lambda-rest-api';
import { GuardrailsStack } from '../lib/stacks/backend/guardrails';
import { ZeroEtlStack } from '../lib/stacks/backend/zeroetl';
import { NormalizedViewsStack } from '../lib/stacks/backend/normalized-views-stack';

const app = new cdk.App();

// Environment configuration
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
};

const projectName = 'semantic-layer';

// Optional deployment suffix — allows multiple independent deployments in the same account/region.
// Usage: cdk deploy --context suffix=v2
// Stack names become: semantic-layer-v2-networking, semantic-layer-v2-dynamodb, etc.
// Resource names inside stacks are also suffixed so there are no name conflicts.
// When omitted the behaviour is identical to the previous default (no suffix).
const suffix = (app.node.tryGetContext('suffix') as string | undefined) ?? 'dev';
const deployName = `${projectName}-${suffix}`;

// Feature flag: set to false to skip Neptune, ontology KB, Neptune Gateway, and VKG agents
// Note: --context values are always strings, so we check for both boolean false and string 'false'
const _ontologyCtx = app.node.tryGetContext('enableOntologyAgents');
const enableOntologyAgents = _ontologyCtx !== false && _ontologyCtx !== 'false';

// CDC pipeline mode flags — mutually exclusive replication strategies.
//   enableRealtimeReplication (default true)  → DynamoDB Streams + Lambda PyIceberg CDC
//   enableBatchReplication    (default false) → Glue Zero-ETL integrations to S3 Tables
// Pass via CDK context to override defaults:
//   cdk deploy --context enableRealtimeReplication=false --context enableBatchReplication=true
const _realtimeCtx = app.node.tryGetContext('enableRealtimeReplication');
const enableRealtimeReplication = _realtimeCtx !== false && _realtimeCtx !== 'false';

const _batchCtx = app.node.tryGetContext('enableBatchReplication');
const enableBatchReplication = _batchCtx === true || _batchCtx === 'true';

// Optional capability flags (read from cdk.json or `--context` overrides):
//   enableSemanticRag        (default true)  → semantic-rag Bedrock KB +
//                                              metadata/metadata-query/query-suggestions runtimes
//   enableAcordSampleData    (default false) → synthetic-data loader for the 12 insurance tables
const _semanticRagCtx = app.node.tryGetContext('enableSemanticRag');
const enableSemanticRag = _semanticRagCtx !== false && _semanticRagCtx !== 'false';

const _acordCtx = app.node.tryGetContext('enableAcordSampleData');
const enableAcordSampleData = _acordCtx === true || _acordCtx === 'true';

// NOTE: the AG-UI multi-turn streaming chat is always on — it is the only
// natural-language query UI (the legacy single-input page + /query/chat proxy
// were removed). There is no enableChatUi flag; the chat gateway + chat-sessions
// table are always deployed.

// OBO identity passthrough (item #4) — defaults to false (Phase 0/1 of
// rollout). Set `-c enableOboPassthrough=true` after LF grants are
// migrated to per-group basis.
const _oboCtx = app.node.tryGetContext('enableOboPassthrough');
const enableOboPassthrough = _oboCtx === true || _oboCtx === 'true';

// 1. Networking Stack - VPC for Neptune and private resources
const networkingStack = new NetworkingStack(app, `${deployName}-networking`, {
  env,
  description: 'VPC and networking infrastructure for semantic layer',
  projectName: deployName,
});

// 2. DynamoDB Stack - Operational data storage
const dynamodbStack = new DynamoDBStack(app, `${deployName}-dynamodb`, {
  env,
  description: 'DynamoDB tables for insurance operational data',
  projectName: deployName,
  loadSyntheticData: enableAcordSampleData, // gated by --context enableAcordSampleData=true
});

// 3. Glue Catalog Stack - Schema discovery and Iceberg database
const glueCatalogStack = new GlueCatalogStack(app, `${deployName}-glue-catalog`, {
  env,
  description: 'AWS Glue Data Catalog for DynamoDB schema discovery and S3 Tables (Iceberg)',
  projectName: deployName,
  dynamodbStack,
  autoStartCrawlers: true, // Auto-start DynamoDB crawler on deployment
});

// 4. Data Lake Stack - S3 Tables (Iceberg) for real-time analytics
const dataLakeStack = new DataLakeStack(app, `${deployName}-data-lake`, {
  env,
  description: 'S3 Tables data lake for real-time analytics with DynamoDB CDC',
  projectName: deployName,
  glueCatalogStack,
  enableRealtimeReplication,
});

// 5. CDC Pipeline — choose between real-time (Streams) or batch (Zero-ETL) replication.
//    Default: real-time. Override via CDK context flags (see feature flags above).
const streamProcessorStack = enableRealtimeReplication
  ? new DynamoDBStreamProcessorStack(app, `${deployName}-stream-processor`, {
      env,
      description: 'DynamoDB Streams to S3 Tables CDC pipeline',
      projectName: deployName,
      dynamodbStack,
      dataLakeStack,
    })
  : undefined;

const zeroEtlStack = enableBatchReplication
  ? new ZeroEtlStack(app, `${deployName}-zeroetl`, {
      env,
      description: 'Glue Zero-ETL integrations from DynamoDB tables to S3 Tables',
      projectName: deployName,
      dynamodbStack,
      tableBucketArn: dataLakeStack.tableBucketArn,
      tableBucketName: dataLakeStack.tableBucketName,
      tableBucketNamespace: dataLakeStack.namespace,
    })
  : undefined;

// 6. Normalized Views Stack — only when enableBatchReplication=true (cdk.json)
// Creates 40 Glue materialized views in 'normalized' S3 Tables namespace
const normalizedViewsStack =
  enableBatchReplication && zeroEtlStack
    ? new NormalizedViewsStack(app, `${deployName}-normalized-views`, {
        env,
        description: 'Glue materialized views normalizing Zero-ETL S3 Tables into entity tables',
        projectName: deployName,
        dataLakeStack,
        zeroEtlStack,
        refreshIntervalHours: 6,
      })
    : undefined;

// 7. Neptune Stack - Knowledge graph for semantic ontology (conditional)
const neptuneStack = enableOntologyAgents
  ? new NeptuneStack(app, `${deployName}-neptune`, {
      env,
      description: 'Amazon Neptune RDF graph database for ontology',
      projectName: deployName,
      vpc: networkingStack.vpc,
    })
  : undefined;

// 8. Bedrock Knowledge Base Stack - always deployed.
// Both KBs (ontology patterns + semantic RAG) are required by the metadata, metadata-query,
// and query-suggestions agents regardless of the ontology flag.  The ontology generation
// agent also reads from the ontology-patterns KB, but that code path is gated by ontologyEnabled
// inside AgentCoreStack rather than here.
const bedrockKbStack = new BedrockKnowledgeBaseStack(app, `${deployName}-bedrock-kb`, {
  env,
  description: 'Amazon Bedrock Knowledge Base for ontology patterns and semantic RAG',
  projectName: deployName,
  artifactsBucket: dataLakeStack.artifactsBucket,
  autoStartIngestion: true,
  enableSemanticRag,
});

// Human/SSO roles that must retain LF admin status across all CDK redeploys.
// Passed to every stack that owns a CfnDataLakeSettings so none are silently dropped.
// Add any IAM role ARN that needs to browse s3tablescatalog in the Glue/Athena console.
const additionalLakeFormationAdmins = [
  `arn:aws:iam::${process.env.CDK_DEFAULT_ACCOUNT}:role/Admin`,
];

// 9. Athena Stack - Federated query engine
const athenaStack = new AthenaStack(app, `${deployName}-athena`, {
  env,
  description: 'Amazon Athena federated query infrastructure',
  projectName: deployName,
  dataLakeStack,
  glueCatalogStack,
  dynamodbStack,
  vpc: networkingStack.vpc,
  additionalLakeFormationAdmins,
});

// 10b. AgentCore Memory Stack — single Memory resource with a SemanticStrategy
// for lessons-learned (item #2). Created before AgentCoreStack so the memory
// id can be threaded into every runtime's environment.
const agentcoreMemoryStack = new AgentCoreMemoryStack(app, `${deployName}-agentcore-memory`, {
  env,
  description: 'Bedrock AgentCore Memory resource for lessons-learned',
  projectName: deployName,
  eventExpiryDays: 90,
});

// 10c. Guardrails Stack — Bedrock Guardrails for AI safety. Created before
// AgentCoreStack so the guardrail id/version can be threaded into every
// runtime: the lessons-learned memory hook PII-redacts every turn through
// ``ApplyGuardrail`` before persisting.
const guardrailsStack = new GuardrailsStack(app, `${deployName}-guardrails`, {
  env,
  description: 'Bedrock Guardrails for semantic layer AI protection',
});

// 11. AgentCore Stack - Strands agents on Bedrock AgentCore Runtime
// CloudFront + Auth are created BEFORE agentcore so the query runtimes can
// configure a Cognito JWT inbound authorizer (chat-gateway JWT_PASSTHROUGH
// forwards the browser's validated access token to the runtime; the runtime
// re-validates it against the same user-pool client and decodes the `sub` for
// chat-session ownership). CloudFront depends only on dataLake.loggingBucket
// and Auth only on CloudFront URLs, so this reorder introduces no cycle.
const cloudfrontStorageStack = new CloudFrontStorageStack(app, `${deployName}-cloudfront-storage`, {
  env,
  description: 'CloudFront distribution and S3 storage for frontend',
  projectName: deployName,
  loggingBucket: dataLakeStack.loggingBucket,
});

const authStack = new AuthStack(app, `${deployName}-auth`, {
  env,
  description: 'Cognito authentication and authorization for semantic layer',
  urls: cloudfrontStorageStack.urls,
  // Per-group LF grants (item #4 OBO migration). Glue databases here mirror
  // the deterministic names produced by glue-catalog-stack so the auth
  // stack doesn't need a direct dep on it.
  lfGrantDatabases: [
    { name: `${deployName}_dynamodb`.replace(/-/g, '_') },
    { name: `${deployName}_iceberg`.replace(/-/g, '_') },
  ],
});

const agentcoreStack = new AgentCoreStack(app, `${deployName}-agentcore`, {
  env,
  description: 'Bedrock AgentCore Runtime with Strands agents',
  projectName: deployName,
  vpc: networkingStack.vpc,
  neptuneStack, // undefined when flag off
  bedrockKbStack, // always present
  glueCatalogStack,
  dynamodbStack,
  athenaStack,
  dataLakeStack,
  additionalLakeFormationAdmins,
  normalizedViewsEnabled: enableBatchReplication,
  enableRealtimeReplication,
  enableSemanticRag,
  // Cognito user pool + client for the query runtimes' JWT inbound authorizer
  // (chat-gateway JWT_PASSTHROUGH → runtime re-validates → decodes sub).
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
  // M2M client so all runtimes accept the backend client_credentials token
  // (mcp-tools Lambda + REST generation jobs), replacing their SigV4 invocation.
  m2mClient: authStack.m2mClient,
  // Lessons-learned: every runtime writes turns into this memory via the
  // Strands hook (PII-redacted by Bedrock Guardrails).
  lessonsMemoryId: agentcoreMemoryStack.memoryId,
  guardrailId: guardrailsStack.guardrailId,
  guardrailVersion: guardrailsStack.guardrailVersion,
});
agentcoreStack.addDependency(agentcoreMemoryStack);
agentcoreStack.addDependency(guardrailsStack);
agentcoreStack.addDependency(authStack);

// 12. AgentCore Eval Stack - Online evaluation configs for all 5 runtimes
const agentcoreEvalStack = new AgentCoreEvalStack(app, `${deployName}-agentcore-eval`, {
  env,
  description: 'AgentCore Evaluations online eval configs for all 5 runtimes',
  projectName: deployName,
  agentCoreStack: agentcoreStack,
  samplingRate: 100,
});
agentcoreEvalStack.addDependency(agentcoreStack);

// 13. CloudFront Storage Stack - Create CloudFront distribution and S3 bucket FIRST
// This allows the CloudFront URL to be used in Cognito callback URLs
// (CloudFrontStorageStack + AuthStack are created earlier — moved above
// agentcore so the query runtimes can use a Cognito JWT inbound authorizer for
// chat JWT_PASSTHROUGH. CloudFront depends only on dataLake.loggingBucket.)

// 15b. Doc-pipeline stack (item #3) — Step Functions + five pipeline Lambdas.
// Provisioned BEFORE the lambda-rest-api stack because the API Lambda needs
// the state-machine ARN at synth time to start executions on document upload.
const docPipelineStack = new DocPipelineStack(app, `${deployName}-doc-pipeline`, {
  env,
  description: 'Step Functions doc pipeline (chunk → ner → embed → link → index)',
  supplementaryDocsBucket: dataLakeStack.artifactsBucket,
  metadataTable: dynamodbStack.metadataTable,
});
docPipelineStack.addDependency(dataLakeStack);
docPipelineStack.addDependency(dynamodbStack);

// 16. Lambda REST API Stack - Serverless API for semantic layer operations
// Lambda handles REST API endpoints and delegates to AgentCore Runtime for data operations
const lambdaApiStack = new LambdaRestApiStack(app, `${deployName}-lambda-api`, {
  env,
  region: env.region!,
  stage: 'dev',
  projectId: deployName,
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
  identityPoolId: authStack.identityPool.identityPoolId,
  // S3 buckets
  artifactsBucket: dataLakeStack.artifactsBucket,
  // Ontology metadata table
  ontologyMetadataTable: dynamodbStack.metadataTable,
  // Chat sessions table for AG-UI multi-turn chat (item #1 — see
  // docs/plans/2026-05-16-frontend-chat-ag-ui-design.md)
  chatSessionsTable: dynamodbStack.chatSessionsTable,
  // Governed-metrics table (Tier 1 progressive disclosure)
  metricsTable: dynamodbStack.metricsTable,
  // Per-turn user feedback (👍/👎) table — surfaced as the admin
  // "Feedback" tab; comments are guardrail-redacted before write.
  feedbackTable: dynamodbStack.feedbackTable,
  // Lessons-learned long-term memory id (item #2). Lambda reads/deletes
  // records via the bedrock-agentcore data plane.
  lessonsMemoryId: agentcoreMemoryStack.memoryId,
  // OBO identity passthrough flag (item #4)
  enableOboPassthrough,
  // AgentCore Runtime ARNs
  ontologyRuntimeArn: agentcoreStack.ontologyRuntimeArn, // undefined when flag off
  queryRuntimeArn: agentcoreStack.queryRuntimeArn, // undefined when flag off
  metadataRuntimeArn: agentcoreStack.metadataRuntimeArn,
  metadataQueryRuntimeArn: agentcoreStack.metadataQueryRuntimeArn,
  suggestionsRuntimeArn: agentcoreStack.suggestionsRuntimeArn,
  // M2M OAuth so the REST API invokes JWT-inbound runtimes via Bearer token.
  m2mClientId: authStack.m2mClient.userPoolClientId,
  m2mClientSecret: authStack.m2mClientSecret,
  oauthTokenEndpoint: `${authStack.mcpHostedUiDomainUrl}/oauth2/token`,
  // AgentCore Gateway configuration for Neptune access (IAM authenticated)
  neptuneGatewayUrl: agentcoreStack.neptuneGateway?.gatewayUrl,
  neptuneGatewayArn: agentcoreStack.neptuneGateway?.gatewayArn,
  // Data Lake configuration for Lake Formation permissions
  glueNamespaceDynamoDB: glueCatalogStack.dynamodbDatabase.ref,
  // Lake Formation admin chain — carry forward all prior LF admins so this stack's
  // CfnDataLakeSettings (last writer wins) preserves existing grants while adding the Lambda role
  lfGrantSingletonRoleArn: dataLakeStack.lfGrantSingletonRoleArn,
  additionalLakeFormationAdmins,
  // Bedrock Guardrails
  guardrailId: guardrailsStack.guardrailId,
  guardrailVersion: guardrailsStack.guardrailVersion,
  // Explicit CORS allowed origins — CloudFront domain + localhost for dev
  allowedOrigins: [
    `https://${cloudfrontStorageStack.distribution.distributionDomainName}`,
    'http://localhost:3000',
    'http://localhost:5173',
  ],
  // Regional WAF WebACL for API Gateway protection
  regionalWebAclArn: authStack.regionalWebAclArn,
  // Capability flag — gates /metadata FastAPI sub-app
  enableSemanticRag,
  // Doc-pipeline state machine ARN — Lambda starts executions on upload (item #3).
  docPipelineStateMachineArn: docPipelineStack.stateMachine.stateMachineArn,
});
lambdaApiStack.addDependency(docPipelineStack);
lambdaApiStack.addDependency(agentcoreMemoryStack);

// MCP server stack (item #6) — exposes the three query tools through an
// AgentCore Gateway with a Lambda target. Replaces the prior standalone
// MCP-in-a-Function-URL approach which had no Lambda Runtime client.
// Created BEFORE the frontend stack so the frontend build can read the chat
// gateway URL (REACT_APP_CHAT_GATEWAY_URL).
const mcpServerStack = new McpServerStack(app, `${deployName}-mcp-server`, {
  env,
  description: 'MCP server via AgentCore Gateway + Lambda target',
  projectName: deployName,
  queryRuntimeArn: agentcoreStack.queryRuntimeArn,
  metadataQueryRuntimeArn: agentcoreStack.metadataQueryRuntimeArn,
  suggestionsRuntimeArn: agentcoreStack.suggestionsRuntimeArn,
  guardrailId: guardrailsStack.guardrailId,
  guardrailVersion: guardrailsStack.guardrailVersion,
  userPoolId: authStack.userPool.userPoolId,
  userPoolClientId: authStack.userPoolClient.userPoolClientId,
  // PKCE 3LO client → flips the MCP query gateway to CUSTOM_JWT (Claude Code OAuth).
  mcpClientId: authStack.mcpClient.userPoolClientId,
  // M2M OAuth so the mcp-tools Lambda invokes JWT-inbound runtimes via Bearer token.
  m2mClientId: authStack.m2mClient.userPoolClientId,
  m2mClientSecret: authStack.m2mClientSecret,
  oauthTokenEndpoint: `${authStack.mcpHostedUiDomainUrl}/oauth2/token`,
  // ListOntologies tool scans this table to enumerate published ontologies.
  metadataTable: dynamodbStack.metadataTable,
});
mcpServerStack.addDependency(agentcoreStack);
mcpServerStack.addDependency(dynamodbStack);
mcpServerStack.addDependency(guardrailsStack);
mcpServerStack.addDependency(authStack);

// 16b. MCP OAuth proxy — lets Claude Code / VSCode / Cursor reach the MCP query
// gateway via browser OAuth (no SigV4). Reads the gateway URL from SSM (written
// by mcp-server) so there is no cross-stack cycle; appends its /callback URL to
// the PKCE client.
const mcpProxyStack = new McpProxyStack(app, `${deployName}-mcp-proxy`, {
  env,
  description: 'OAuth proxy enabling MCP OAuth (Claude Code/VSCode) to the MCP gateway',
  projectName: deployName,
  cognitoDomainUrl: authStack.mcpHostedUiDomainUrl,
  mcpClientId: authStack.mcpClient.userPoolClientId,
  mcpClient: authStack.mcpClient,
  userPool: authStack.userPool,
  existingCallbackUrls: MCP_PKCE_CALLBACK_URLS,
});
mcpProxyStack.addDependency(mcpServerStack); // needs the SSM gateway-url param written
mcpProxyStack.addDependency(authStack);

// 17. Frontend Stack - React app build and deployment
// Uses existing CloudFront distribution and S3 bucket from CloudFrontStorageStack
const frontendStack = new FrontendStack(app, `${deployName}-frontend`, {
  env,
  description: 'Frontend React application build and deployment',
  apiUrl: lambdaApiStack.httpApi.apiEndpoint,
  userPoolId: authStack.userPool.userPoolId,
  userPoolClientId: authStack.userPoolClient.userPoolClientId,
  userPoolDomain: authStack.userPoolDomain?.domainName,
  projectName: deployName,
  apiGatewayEndpoint: lambdaApiStack.httpApi.apiEndpoint,
  cloudFrontHeaderSecret: lambdaApiStack.cloudFrontHeaderSecret,
  distribution: cloudfrontStorageStack.distribution,
  websiteBucket: cloudfrontStorageStack.websiteBucket,
  enableOntologyAgents,
  enableSemanticRag,
  chatGatewayUrl: mcpServerStack.chatGatewayUrl,
  // MCP endpoints surfaced on the Settings page (proxy = what Claude Code adds;
  // gateway = the AgentCore Gateway behind it).
  mcpProxyUrl: mcpProxyStack.httpApi.apiEndpoint,
  mcpGatewayUrl: mcpServerStack.gatewayUrl,
});
frontendStack.addDependency(mcpProxyStack);

// Stack dependencies
if (streamProcessorStack) {
  streamProcessorStack.addDependency(dynamodbStack);
  streamProcessorStack.addDependency(dataLakeStack);
}
if (zeroEtlStack) {
  zeroEtlStack.addDependency(dynamodbStack);
  zeroEtlStack.addDependency(dataLakeStack);
}
if (normalizedViewsStack) {
  normalizedViewsStack.addDependency(zeroEtlStack!);
  normalizedViewsStack.addDependency(dataLakeStack);
}
glueCatalogStack.addDependency(dynamodbStack);
// Note: GlueCatalogStack creates the Iceberg database first
// dataLakeStack depends on it (uses the database name)
dataLakeStack.addDependency(glueCatalogStack);
if (neptuneStack) {
  neptuneStack.addDependency(networkingStack);
  agentcoreStack.addDependency(neptuneStack);
}
bedrockKbStack.addDependency(dataLakeStack);
agentcoreStack.addDependency(bedrockKbStack);
athenaStack.addDependency(dataLakeStack);
athenaStack.addDependency(glueCatalogStack);
athenaStack.addDependency(dynamodbStack);
athenaStack.addDependency(networkingStack);
agentcoreStack.addDependency(glueCatalogStack);
agentcoreStack.addDependency(athenaStack);
cloudfrontStorageStack.addDependency(dataLakeStack); // Needs logging bucket
authStack.addDependency(cloudfrontStorageStack); // Needs CloudFront URL for callback URLs
lambdaApiStack.addDependency(authStack); // Needs Cognito configuration
lambdaApiStack.addDependency(dataLakeStack); // Needs artifacts bucket
lambdaApiStack.addDependency(dynamodbStack); // Needs ontology metadata table
lambdaApiStack.addDependency(agentcoreStack); // Needs AgentCore Runtime ARNs
lambdaApiStack.addDependency(guardrailsStack); // Needs guardrail ID and version
frontendStack.addDependency(cloudfrontStorageStack); // Uses existing distribution and bucket
frontendStack.addDependency(authStack);
frontendStack.addDependency(lambdaApiStack);
frontendStack.addDependency(mcpServerStack); // Reads chat gateway URL for build env
// CloudFront integration is part of FrontendStack

// Global tags
cdk.Tags.of(app).add('Project', deployName);
cdk.Tags.of(app).add('Environment', 'development');
cdk.Tags.of(app).add('ManagedBy', 'CDK');

// Apply cdk-nag security checks to all stacks
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

// Suppress known false positives and CDK-managed construct violations
// These suppressions are intentional architectural decisions documented per rule
const suppressions = [
  {
    id: 'AwsSolutions-L1',
    reason:
      'System-generated Lambda functions (custom resource handlers, log retention, CodeBuild custom resource handlers) are created by CDK constructs and use Node.js 14.x as designed by the framework. These are framework internals, not application code.',
  },
  {
    id: 'AwsSolutions-IAM4',
    reason:
      'AWSLambdaBasicExecutionRole is required minimum for Lambda logging to CloudWatch Logs. This is the standard AWS managed policy for Lambda execution and cannot be replaced with a more restrictive custom policy without breaking core functionality.',
    appliesTo: [
      'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
    ],
  },
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'Wildcard permissions in CodeBuild and Lambda custom resource roles are generated by CDK constructs (e.g., S3 asset bucket access, log group patterns). These are CDK-managed and follow least privilege for their respective scopes.',
  },
];

// Apply suppressions to all stacks
[
  networkingStack,
  dynamodbStack,
  glueCatalogStack,
  dataLakeStack,
  ...(streamProcessorStack ? [streamProcessorStack] : []),
  ...(zeroEtlStack ? [zeroEtlStack] : []),
  ...(normalizedViewsStack ? [normalizedViewsStack] : []),
  ...(neptuneStack ? [neptuneStack] : []),
  bedrockKbStack,
  athenaStack,
  agentcoreStack,
  agentcoreEvalStack,
  agentcoreMemoryStack,
  cloudfrontStorageStack,
  authStack,
  guardrailsStack,
  lambdaApiStack,
  frontendStack,
  docPipelineStack,
  mcpServerStack,
].forEach((stack) => {
  if (stack) {
    NagSuppressions.addStackSuppressions(stack, suppressions);
  }
});

app.synth();
