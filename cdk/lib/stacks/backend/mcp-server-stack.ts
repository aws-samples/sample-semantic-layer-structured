import {
  Stack,
  StackProps,
  Duration,
  CfnOutput,
  CustomResource,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_logs as logs,
  aws_ssm as ssm,
  aws_bedrockagentcore as agentcore,
} from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { execSync } from 'child_process';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { ArmBuildConstruct } from '../../common/constructs/arm-build-construct';
import { MCP_INVOKE_SCOPE, mcpGatewayUrlSsmParam } from '../../common/auth-constants';

/**
 * MCP server (item #6) — exposes the three query tools via an AgentCore
 * Gateway. The Gateway is the canonical AWS-managed MCP transport: it
 * speaks MCP to external clients (Claude Desktop, Cursor, …) and
 * translates each ``tools/call`` into a Lambda invocation against the
 * mcp-tools Lambda in this stack.
 *
 * This replaces the prior "standalone Python MCP server in a Lambda
 * Function URL" scaffolding which had no Lambda Runtime client and no
 * MCP transport — it would not have started.
 *
 * Three tools, each with an inline tool schema matching the design doc:
 *   * OntologyQuery     — VKG path
 *   * MetadataQuery     — Semantic-RAG path
 *   * QuerySuggestions  — synchronous suggestions agent
 *
 * Authentication is AWS_IAM at the Gateway. Clients SigV4-sign their MCP
 * requests; the Gateway validates and forwards. Outbound auth from the
 * Gateway → Lambda is the Gateway service role (GATEWAY_IAM_ROLE).
 */
export interface McpServerStackProps extends StackProps {
  readonly projectName: string;
  /** AgentCore Runtime ARNs the MCP tools shim into. */
  readonly queryRuntimeArn?: string;
  readonly metadataQueryRuntimeArn?: string;
  readonly suggestionsRuntimeArn?: string;
  /** Bedrock Guardrails (preserved across MCP calls per design). */
  readonly guardrailId?: string;
  readonly guardrailVersion?: string;
  /**
   * Cognito User Pool info for the streaming chat gateway's CUSTOM_JWT
   * authorizer. When both are present (alongside the query runtime ARNs) a
   * second AgentCore Gateway is created with AgentCore Runtime targets so the
   * browser can stream chat SSE directly through the gateway.
   */
  readonly userPoolId?: string;
  readonly userPoolClientId?: string;
  /**
   * Public PKCE 3LO client id (Claude Code / VSCode MCP login). The MCP query
   * gateway's CUSTOM_JWT authorizer validates inbound tokens against this client
   * + the `semantic-layer-mcp/invoke` scope. Required for the CUSTOM_JWT
   * authorizer; when absent the gateway falls back to AWS_IAM (SigV4).
   */
  readonly mcpClientId?: string;
  /** Confidential M2M client id (client_credentials) the mcp-tools Lambda uses to
   *  mint a Bearer token for invoking the JWT-inbound runtimes. */
  readonly m2mClientId?: string;
  /** Secrets Manager secret holding the M2M client secret. */
  readonly m2mClientSecret?: secretsmanager.ISecret;
  /** Cognito OAuth token endpoint (`${hostedUiDomain}/oauth2/token`). */
  readonly oauthTokenEndpoint?: string;
  /**
   * Ontology metadata DynamoDB table. The ListOntologies tool scans it to
   * enumerate published semantic layers (id + name + VKG/SemanticRAG mode) so a
   * caller can discover ontologies before choosing a query tool. Read-only.
   */
  readonly metadataTable?: dynamodb.ITable;
}

export class McpServerStack extends Stack {
  public readonly toolsLambda: lambda.IFunction;
  public readonly gateway: agentcore.CfnGateway;
  public readonly gatewayUrl: string;
  public readonly gatewayArn: string;
  /**
   * Streaming chat gateway (CUSTOM_JWT inbound, AgentCore Runtime targets
   * outbound). Only set when the query runtimes + Cognito info are supplied.
   */
  public readonly chatGatewayUrl?: string;
  public readonly chatGatewayArn?: string;

  constructor(scope: Construct, id: string, props: McpServerStackProps) {
    super(scope, id, props);

    // ---- Lambda role ---------------------------------------------------
    const lambdaRole = new iam.Role(this, 'McpToolsLambdaRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for the MCP tools Lambda',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // The mcp-tools Lambda invokes the JWT-inbound runtimes over HTTPS with an
    // M2M OAuth Bearer token, so it does not need
    // bedrock-agentcore:InvokeAgentRuntime — only read access to the M2M client
    // secret to mint that token. Grant least-privilege Secrets Manager read.
    if (props.m2mClientSecret) {
      props.m2mClientSecret.grantRead(lambdaRole);
    }

    // bedrock:ApplyGuardrail scoped to the deployed guardrail.
    // NOTE: props.guardrailId is the guardrail's CloudFormation Ref, which for
    // AWS::Bedrock::Guardrail resolves to the FULL ARN
    // (arn:aws:bedrock:…:guardrail/<id>), NOT a bare id. Interpolating it into
    // `…:guardrail/${guardrailId}` produced a broken double-ARN
    // (…:guardrail/arn:aws:bedrock:…:guardrail/<id>) that matched nothing, so
    // ApplyGuardrail was silently AccessDenied and the tool guardrails fail-open.
    // Use the ARN directly, plus its versioned child (some ApplyGuardrail calls
    // are authorized against guardrail/<id>:<version>).
    if (props.guardrailId) {
      lambdaRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:ApplyGuardrail'],
          resources: [props.guardrailId, `${props.guardrailId}:*`],
        })
      );
    }

    // ---- Lambda image build via CodeBuild (matches existing pattern) ----
    const lambdaBuild = new ArmBuildConstruct(this, 'McpToolsArmBuild', {
      sourcePath: '../lambda/mcp-tools',
      region: this.region,
      namePrefix: `${props.projectName}-mcp-tools`,
      buildTimeoutMinutes: 10,
    });

    this.toolsLambda = new lambda.DockerImageFunction(this, 'McpToolsFunction', {
      functionName: `${props.projectName}-mcp-tools`,
      code: lambda.DockerImageCode.fromEcr(lambdaBuild.repository, {
        tagOrDigest: lambdaBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      role: lambdaRole,
      // The query tools now read the runtime's FULL chat SSE stream (chat-shaped
      // payload so Monitoring captures MCP traffic). That HTTPS read is bounded by
      // MCP_MAX_WAIT_SECONDS (120); the Lambda timeout must exceed it so the Lambda
      // doesn't kill the request before the read's own timeout can surface an error.
      timeout: Duration.seconds(150),
      memorySize: 1024,
      environment: {
        QUERY_RUNTIME_ARN: props.queryRuntimeArn ?? '',
        METADATA_QUERY_RUNTIME_ARN: props.metadataQueryRuntimeArn ?? '',
        SUGGESTIONS_RUNTIME_ARN: props.suggestionsRuntimeArn ?? '',
        // Full-stream read budget for the chat-SSE query tools (see index.py).
        MCP_MAX_WAIT_SECONDS: '120',
        GUARDRAIL_IDENTIFIER: props.guardrailId ?? '',
        GUARDRAIL_VERSION: props.guardrailVersion ?? '',
        // OAuth (M2M client_credentials) for invoking JWT-inbound runtimes.
        OAUTH_TOKEN_ENDPOINT: props.oauthTokenEndpoint ?? '',
        OAUTH_SCOPE: MCP_INVOKE_SCOPE,
        M2M_CLIENT_ID: props.m2mClientId ?? '',
        M2M_CLIENT_SECRET_ARN: props.m2mClientSecret?.secretArn ?? '',
        // ListOntologies tool scans this table to enumerate published ontologies.
        ONTOLOGY_METADATA_TABLE: props.metadataTable?.tableName ?? '',
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: 'MCP query tools invoked by AgentCore Gateway',
    });
    lambdaBuild.repository.grantPull(lambdaRole);
    this.toolsLambda.node.addDependency(lambdaBuild.buildCompletion);

    // Read-only grant for the ListOntologies tool (scan/get on the metadata table).
    if (props.metadataTable) {
      props.metadataTable.grantReadData(lambdaRole);
    }

    // ---- Gateway service role -----------------------------------------
    const gatewayRole = new iam.Role(this, 'McpGatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`,
          },
        },
      }),
      description: 'Service role assumed by the MCP Gateway',
    });
    this.toolsLambda.grantInvoke(gatewayRole);

    // Principal-based permission so the Gateway service can invoke the Lambda
    // even before the role-based grant propagates.
    this.toolsLambda.addPermission('AllowMcpGatewayInvoke', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`,
    });

    // ---- Gateway -------------------------------------------------------
    // CUSTOM_JWT inbound (was AWS_IAM) when the PKCE client is wired: Claude Code
    // / VSCode / Cursor speak MCP OAuth (bearer token) via the mcp-proxy, not
    // SigV4. The gateway validates the Cognito access token against the PKCE
    // client + invoke scope. Outbound to the mcp-tools Lambda stays
    // GATEWAY_IAM_ROLE (unchanged). Falls back to AWS_IAM (SigV4) when no
    // PKCE client/pool is supplied. See
    // docs/plans/2026-06-02-agentcore-jwt-oauth-unification-design.md.
    const mcpJwtEnabled = Boolean(props.userPoolId && props.mcpClientId);
    const mcpDiscoveryUrl = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPoolId}/.well-known/openid-configuration`;
    this.gateway = new agentcore.CfnGateway(this, 'McpGateway', {
      name: `${props.projectName}-mcp-gateway`,
      description: 'MCP server: ListOntologies, OntologyQuery, MetadataQuery, QuerySuggestions',
      roleArn: gatewayRole.roleArn,
      // nosemgrep: generic-api-key — authorizer config / description string, not a secret
      authorizerType: mcpJwtEnabled ? 'CUSTOM_JWT' : 'AWS_IAM',
      // nosemgrep: generic-api-key — authorizer config / description string, not a secret
      authorizerConfiguration: mcpJwtEnabled
        ? {
            customJwtAuthorizer: {
              discoveryUrl: mcpDiscoveryUrl,
              allowedClients: [props.mcpClientId!], // nosemgrep: generic-api-key — Cognito OAuth2 client ID (public), not a secret
              allowedScopes: [MCP_INVOKE_SCOPE],
            },
          }
        : undefined,
      protocolType: 'MCP',
      exceptionLevel: 'DEBUG',
      protocolConfiguration: {
        mcp: {
          supportedVersions: ['2025-03-26', '2025-06-18'],
        },
      },
      tags: {
        Application: props.projectName,
        ManagedBy: 'CDK',
      },
    });
    this.gatewayArn = this.gateway.attrGatewayArn;
    this.gatewayUrl = this.gateway.attrGatewayUrl;

    // Publish the gateway URL to SSM so the mcp-proxy stack reads it without a
    // cross-stack ref/cycle (the proxy resolves it at runtime via ssm:GetParameter).
    new ssm.StringParameter(this, 'McpGatewayUrlParam', {
      parameterName: mcpGatewayUrlSsmParam(props.projectName),
      stringValue: this.gatewayUrl,
    });

    // ---- Tool targets — one per design-doc tool ------------------------
    // The tool descriptions encode the SEMANTIC-LAYER CALLER CHAIN. AgentCore
    // Gateway has no "skill/plugin" primitive (and its MCP "prompts" primitive is
    // only served by MCP-server targets, not these Lambda targets — and is
    // user-, not model-, controlled), so the recommended call order lives in the
    // model-controlled tool descriptions: (1) ListOntologies to discover the
    // available ontologies, (2) read each one's `mode` to choose the query tool,
    // (3) call OntologyQuery (VKG) or MetadataQuery (SemanticRAG) with that id.
    // See docs/plans/2026-06-04-todo-list-ontologies-and-skill.md.
    new agentcore.CfnGatewayTarget(this, 'ListOntologiesTarget', {
      name: 'mcp-tools-list-ontologies',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description:
        'ListOntologies — discover all published semantic layers (ontologies) and their query mode. Call this FIRST.',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.toolsLambda.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'ListOntologies',
                  description:
                    'STEP 1 of the semantic-layer caller chain. Lists all published ontologies ' +
                    '(semantic layers) with their id, name, build status, and query `mode`. ' +
                    'Use the returned `mode` to pick the next tool: mode="VKG" → call OntologyQuery; ' +
                    'mode="SemanticRAG" → call MetadataQuery, passing the chosen `id` as ontologyId. ' +
                    'Optionally filter by build status (e.g. "completed").',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      status: {
                        type: 'string',
                        description:
                          'Optional build-status filter (e.g. "completed"). Omit to list all ontologies.',
                      },
                    },
                    required: [],
                  },
                },
              ],
            },
          },
        },
      },
    });

    new agentcore.CfnGatewayTarget(this, 'OntologyQueryTarget', {
      name: 'mcp-tools-ontology-query',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description:
        'OntologyQuery — natural language question against the VKG path of a published semantic layer.',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.toolsLambda.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'OntologyQuery',
                  description:
                    'STEP 2 (VKG). Run a natural language question against the VKG (ontology) path ' +
                    'of a published semantic layer whose mode is "VKG". Get the ontologyId from ' +
                    'ListOntologies first.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontologyId: { type: 'string' },
                      question: { type: 'string' },
                      rowLimit: {
                        type: 'integer',
                        description: 'Default 10, max 100',
                      },
                    },
                    required: ['ontologyId', 'question'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    new agentcore.CfnGatewayTarget(this, 'MetadataQueryTarget', {
      name: 'mcp-tools-metadata-query',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'MetadataQuery — natural language question against the Semantic RAG path.',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.toolsLambda.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'MetadataQuery',
                  description:
                    'STEP 2 (SemanticRAG). Run a natural language question against the Semantic RAG ' +
                    'path of a published semantic layer whose mode is "SemanticRAG". Uses retrieved ' +
                    'metadata to generate SQL. Get the ontologyId from ListOntologies first.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontologyId: { type: 'string' },
                      question: { type: 'string' },
                      rowLimit: {
                        type: 'integer',
                        description: 'Default 10, max 100',
                      },
                    },
                    required: ['ontologyId', 'question'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    new agentcore.CfnGatewayTarget(this, 'QuerySuggestionsTarget', {
      name: 'mcp-tools-query-suggestions',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'QuerySuggestions — generate 5–8 contextually relevant suggested questions.',
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.toolsLambda.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'QuerySuggestions',
                  description:
                    'Generate 5–8 contextually relevant suggested questions for a published semantic layer.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontologyId: { type: 'string' },
                    },
                    required: ['ontologyId'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    new CfnOutput(this, 'McpGatewayUrl', {
      value: this.gatewayUrl,
      description:
        'AgentCore Gateway MCP endpoint — clients SigV4-sign requests with bedrock-agentcore service.',
    });
    new CfnOutput(this, 'McpGatewayArn', {
      value: this.gatewayArn,
      description: 'AgentCore Gateway ARN',
    });

    // ====================================================================
    // Streaming chat gateway (Task 6+7)
    // --------------------------------------------------------------------
    // A SECOND AgentCore Gateway in this same stack. It exists so the
    // browser can stream chat SSE *directly* through a JWT-authenticated
    // gateway to the AgentCore Runtimes, bypassing the buffered MCP-tools
    // Lambda.
    //
    // Why this is a Lambda-backed CloudFormation custom resource (NOT a
    // CfnGateway):
    //
    //   An AgentCore *Runtime target* CANNOT attach to an MCP-protocol
    //   gateway, so this gateway must be created WITHOUT a protocolType.
    //   But CloudFormation's AWS::BedrockAgentCore::Gateway REQUIRES
    //   ProtocolType (enum=['MCP'] only) — there is no way to model a
    //   non-MCP runtime-target gateway in CloudFormation. The control-plane
    //   API (bedrock-agentcore-control) DOES support it: protocolType is
    //   optional there and runtime targets are first-class. We therefore
    //   manage this gateway via the control-plane API through a Lambda
    //   custom resource, keeping it in IaC.
    //
    // The AgentCore Runtime target type is AWS public preview.
    //
    // Guarded so the block no-ops when the query runtimes or Cognito info
    // aren't available (mirrors how this stack treats runtime ARNs as
    // optional elsewhere).
    if (
      props.metadataQueryRuntimeArn &&
      props.queryRuntimeArn &&
      props.userPoolId &&
      props.userPoolClientId
    ) {
      // Separate service role (not the MCP gateway's) to avoid coupling. The
      // control-plane API passes/assumes this role on the gateway, and the
      // gateway uses it to invoke the runtimes on the caller's behalf.
      const chatGatewayRole = new iam.Role(this, 'ChatGatewayRole', {
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
          conditions: {
            StringEquals: { 'aws:SourceAccount': this.account },
            ArnLike: {
              'aws:SourceArn': `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`,
            },
          },
        }),
        description: 'Service role assumed by the streaming chat Gateway',
      });
      // The gateway invokes the runtimes on the caller's behalf. Scope to
      // both runtime ARNs plus their qualified children (${arn}/*).
      chatGatewayRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock-agentcore:InvokeAgentRuntime'],
          resources: [
            props.metadataQueryRuntimeArn,
            `${props.metadataQueryRuntimeArn}/*`,
            props.queryRuntimeArn,
            `${props.queryRuntimeArn}/*`,
          ],
        })
      );

      // Cognito OIDC discovery document — the gateway validates inbound JWTs
      // issued by this user pool and restricts to the app client.
      const discoveryUrl = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPoolId}/.well-known/openid-configuration`;

      // ---- onEvent Lambda: drives the control-plane API ----------------
      // Create  : create_gateway ONLY (NO protocolType, NO targets). The
      //           gateway returns status=CREATING and create_gateway_target
      //           raises ConflictException (409) until it is READY — so target
      //           creation is deferred to the isComplete poller (below), which
      //           gates it on gateway READY. onEvent just returns the gatewayId
      //           (PhysicalResourceId + Data) for the poller to drive.
      // Update  : delete the old gateway+targets (from the prior
      //           PhysicalResourceId) then create a fresh gateway. This is the
      //           SIMPLEST CORRECT approach: the control-plane API has no
      //           single "update everything" call that covers authorizer +
      //           targets, gateways are cheap, and returning a NEW
      //           PhysicalResourceId would make CFN issue a Delete on the old
      //           id afterwards — risking a double-delete race. Deleting the
      //           old resource in-place during Update and keeping the same
      //           logical id avoids orphaning and avoids that race. The
      //           isComplete poller then drives the new gateway + targets to
      //           READY just like a Create.
      // Delete  : best-effort delete of targets then gateway; ResourceNotFound
      //           is swallowed so a partially-created stack can always be
      //           torn down.
      // The handler code is packaged as a bundled asset (NOT fromInline) so a
      // newer boto3 (>=1.38.0, pinned in chat-gateway-handler/requirements.txt)
      // is shipped with it. The Lambda runtime's bundled boto3 (~1.34) still
      // marks protocolType as REQUIRED on create_gateway, which fails param
      // validation; bundling a newer boto3 makes protocolType optional (as the
      // control-plane API expects) so the non-MCP create_gateway call passes.
      // Bundling mirrors the CloudResourceIdHandler idiom in agentcore-stack.ts:
      // a local.tryBundle fast path (uses local pip, no docker) with a docker
      // image command fallback.
      const chatGatewayHandlerDir = path.join(__dirname, 'chat-gateway-handler');
      const chatGatewayBundling = {
        image: lambda.Runtime.PYTHON_3_12.bundlingImage,
        local: {
          tryBundle(outputDir: string): boolean {
            try {
              execSync(
                // nosemgrep: detect-child-process — CDK synth-time execSync on framework outputDir + static repo paths
                `pip install --quiet --target "${outputDir}" -r "${path.join(chatGatewayHandlerDir, 'requirements.txt')}"`,
                { stdio: 'pipe' }
              );
              for (const f of fs.readdirSync(chatGatewayHandlerDir)) {
                if (f.endsWith('.py')) {
                  fs.copyFileSync(path.join(chatGatewayHandlerDir, f), path.join(outputDir, f)); // nosemgrep: path-join-resolve-traversal,detect-non-literal-fs-filename — CDK synth-time paths, not user input
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
      };

      const onEventFn = new lambda.Function(this, 'ChatGatewayOnEvent', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.on_event_handler',
        timeout: Duration.minutes(5),
        code: lambda.Code.fromAsset(chatGatewayHandlerDir, { bundling: chatGatewayBundling }),
        logRetention: logs.RetentionDays.ONE_WEEK,
        description: 'Chat gateway custom resource — onEvent (control-plane create/update/delete)',
      });

      // ---- isComplete Lambda: drives the full readiness sequence -------
      // gateway READY → idempotently create the 2 runtime targets →
      // poll each target to READY. Reads runtime ARNs from ResourceProperties
      // (the provider passes them to isComplete too).
      const isCompleteFn = new lambda.Function(this, 'ChatGatewayIsComplete', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.is_complete_handler',
        // A bit more headroom than the previous 1 min: this handler now makes
        // several control-plane calls per poll (get_gateway, list/create/get
        // targets).
        timeout: Duration.minutes(2),
        // Same bundled asset as onEvent (boto3>=1.38) so the control-plane
        // target calls run on a current SDK.
        code: lambda.Code.fromAsset(chatGatewayHandlerDir, { bundling: chatGatewayBundling }),
        logRetention: logs.RetentionDays.ONE_WEEK,
        description: 'Chat gateway custom resource — isComplete (poll get_gateway until READY)',
      });

      // ---- IAM: control-plane gateway ops + PassRole on the gw role -----
      // Resources '*' for now (preview API; gateway/target ids are unknown at
      // synth time). PassRole is required because the control-plane API
      // assumes/passes chatGatewayRole onto the gateway.
      const gatewayApiStatement = new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateGateway',
          'bedrock-agentcore:CreateGatewayTarget',
          'bedrock-agentcore:DeleteGateway',
          'bedrock-agentcore:DeleteGatewayTarget',
          'bedrock-agentcore:GetGateway',
          'bedrock-agentcore:GetGatewayTarget',
          // ListGateways lets onEvent reap FAILED orphan gateways (from prior
          // rolled-back deploys) sharing the base name before it creates a new
          // one. Best-effort in the handler, but without this it would always
          // AccessDeny + no-op, so grant it to make the cleanup functional.
          'bedrock-agentcore:ListGateways',
          'bedrock-agentcore:ListGatewayTargets',
          'bedrock-agentcore:UpdateGateway',
        ],
        resources: ['*'],
      });
      const passRoleStatement = new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [chatGatewayRole.roleArn],
      });
      // Creating a gateway makes AgentCore provision a workload identity as a
      // dependency, and that call runs under the CALLER's identity (this Lambda
      // role) — not the gateway role. Without CreateWorkloadIdentity the gateway
      // transitions to FAILED with "Failed to create gateway dependencies ...
      // not authorized to perform: bedrock-agentcore:CreateWorkloadIdentity".
      // Scoped to the account's default workload-identity directory (mirrors the
      // agent-runtime roles' workload-identity-directory/default/* grants).
      //
      // DeleteWorkloadIdentity is REQUIRED on the delete path: deleting a gateway
      // tears down its dependency workload identity under the CALLER's identity
      // (this Lambda role). Without it, delete_gateway flips the gateway to FAILED
      // with "Failed to delete gateway ... not authorized to perform:
      // bedrock-agentcore:DeleteWorkloadIdentity" — which is exactly what stranded
      // the chat gateway in FAILED during the JWT-passthrough migration.
      const workloadIdentityStatement = new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateWorkloadIdentity',
          'bedrock-agentcore:GetWorkloadIdentity',
          'bedrock-agentcore:DeleteWorkloadIdentity',
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/*`,
        ],
      });
      onEventFn.addToRolePolicy(gatewayApiStatement);
      onEventFn.addToRolePolicy(passRoleStatement);
      onEventFn.addToRolePolicy(workloadIdentityStatement);
      isCompleteFn.addToRolePolicy(gatewayApiStatement);
      isCompleteFn.addToRolePolicy(passRoleStatement);
      isCompleteFn.addToRolePolicy(workloadIdentityStatement);

      // ---- Provider + custom resource (async polling) ------------------
      const chatGatewayProvider = new cr.Provider(this, 'ChatGatewayProvider', {
        onEventHandler: onEventFn,
        isCompleteHandler: isCompleteFn,
        queryInterval: Duration.seconds(15),
        // 30 min: the isComplete poller now drives the FULL readiness sequence
        // (gateway READY → 2 targets created → both targets READY), so it needs
        // the same headroom as arm-build-construct's 30-min poll loop.
        totalTimeout: Duration.minutes(30),
        logRetention: logs.RetentionDays.ONE_WEEK,
      });

      NagSuppressions.addResourceSuppressions(
        chatGatewayProvider,
        [
          {
            id: 'AwsSolutions-SF1',
            reason: 'Step Function auto-generated by CDK Provider framework',
          },
          {
            id: 'AwsSolutions-SF2',
            reason: 'Step Function auto-generated by CDK Provider framework',
          },
          {
            id: 'AwsSolutions-L1',
            reason: 'Provider framework Lambda runtime is managed by CDK.',
          },
        ],
        true
      );

      const chatGatewayResource = new CustomResource(this, 'ChatGatewayResource', {
        serviceToken: chatGatewayProvider.serviceToken,
        properties: {
          GatewayName: `${props.projectName}-agent-gateway`,
          RoleArn: chatGatewayRole.roleArn,
          DiscoveryUrl: discoveryUrl,
          AllowedClientId: props.userPoolClientId,
          MetadataQueryRuntimeArn: props.metadataQueryRuntimeArn,
          OntologyQueryRuntimeArn: props.queryRuntimeArn,
          // Bumping this changes the custom-resource properties, forcing CFN to
          // re-run the provider (Update) so _ensure_targets migrates existing
          // targets to the new outbound credential mode. The handler reads
          // TARGET_CREDENTIAL_TYPE internally; this property exists to trigger
          // the re-run when that mode changes (GATEWAY_IAM_ROLE → JWT_PASSTHROUGH).
          TargetCredentialType: 'JWT_PASSTHROUGH',
          // Bump to force a custom-resource re-run when the handler logic changes.
          // v2: _ensure_targets now reads credential type via get_gateway_target
          // (list_gateway_targets returns it empty → infinite delete+recreate churn
          // → CFN timeout). See chat-gateway-handler/index.py.
          HandlerVersion: 'v2-get-target-credtype',
        },
      });
      // Ensure the role (and its PassRole grant) exist before the resource
      // tries to create the gateway with it.
      chatGatewayResource.node.addDependency(chatGatewayRole);

      this.chatGatewayUrl = chatGatewayResource.getAttString('GatewayUrl');
      this.chatGatewayArn = chatGatewayResource.getAttString('GatewayArn');

      new CfnOutput(this, 'ChatGatewayUrl', {
        value: chatGatewayResource.getAttString('GatewayUrl'),
        description: 'Streaming chat gateway endpoint — browser sends Cognito JWT-authed SSE.',
      });
      new CfnOutput(this, 'ChatGatewayId', {
        value: chatGatewayResource.getAttString('GatewayId'),
        description: 'Streaming chat gateway identifier',
      });
    }

    // ---- cdk-nag suppressions ------------------------------------------
    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'AWS-managed AWSLambdaBasicExecutionRole / VPCAccessExecutionRole / CodeBuild policies match the project-wide pattern for Lambda + ARM build pipelines.',
      },
      {
        id: 'AwsSolutions-IAM5',
        reason:
          'Gateway sourceArn allow-list uses arn:aws:bedrock-agentcore:<region>:<acct>:gateway/* — Gateway IDs are unknown at synth time. Lambda invocation is scoped via gateway service-role trust + grantInvoke.',
      },
      {
        id: 'AwsSolutions-L1',
        reason:
          'Lambda runtime is pinned via the docker image (public.ecr.aws/lambda/python:3.12-arm64).',
      },
    ]);
  }
}
