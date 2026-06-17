import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as neptune from 'aws-cdk-lib/aws-neptune';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as path from 'path';
import { ArmBuildConstruct } from '../../../common/constructs/arm-build-construct';

export interface AgentCoreNeptuneGatewayProps {
  projectName: string;
  neptuneCluster: neptune.CfnDBCluster;
  neptuneSecurityGroup: ec2.SecurityGroup;
  neptuneConnectionSecret: secretsmanager.Secret;
  vpc: ec2.Vpc;
}

/**
 * AgentCore Neptune Gateway Construct
 *
 * Creates AgentCore Gateway with Lambda target for Neptune SPARQL operations.
 * Uses IAM authentication (AWS_IAM authorizer) for secure access.
 *
 * Provides nine Neptune tools via Gateway:
 * - discover_named_graphs: List all named graphs
 * - get_ontology_from_neptune: Retrieve full ontology by ontology_id
 * - persist_to_neptune: Write RDF data to Neptune
 * - delete_graph: Drop all triples in a named graph by ontology_id
 * - execute_sparql_query: Execute generic SPARQL queries
 * - get_graph_summary: Get summary statistics (class/property/triple counts)
 * - get_graph_stats: Get class distribution statistics
 * - get_graph_classes: List all classes with labels and comments
 * - get_graph_properties: List all properties with labels and comments
 */
export class AgentCoreNeptuneGateway extends Construct {
  public readonly lambdaFunction: lambda.DockerImageFunction;
  public readonly gatewayRole: iam.Role;
  public readonly gateway: agentcore.CfnGateway;
  public readonly gatewayArn: string;
  public readonly gatewayUrl: string;
  public readonly gatewayId: string;

  constructor(scope: Construct, id: string, props: AgentCoreNeptuneGatewayProps) {
    super(scope, id);

    // Gateway IAM Role (will be used by AgentCore Gateway)
    this.gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: {
            'aws:SourceAccount': cdk.Stack.of(this).account,
          },
          ArnLike: {
            'aws:SourceArn': `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:*`,
          },
        },
      }),
      description: 'Execution role for Neptune Gateway',
    });

    // Security group for Lambda
    // Note: Neptune security group already allows access from VPC CIDR
    // No need to add specific ingress rule to avoid cyclic dependencies
    const lambdaSg = new ec2.SecurityGroup(this, 'LambdaSecurityGroup', {
      vpc: props.vpc,
      description: 'Security group for Neptune Tools Lambda',
      allowAllOutbound: true,
    });

    // IAM role for Lambda
    const lambdaRole = new iam.Role(this, 'LambdaExecutionRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for Neptune Tools Lambda',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole'),
      ],
    });

    // cdk-nag: AWSLambdaVPCAccessExecutionRole is the documented Lambda
    // pattern for ENI lifecycle management when running inside a VPC; no
    // narrower customer-managed equivalent without re-implementing the
    // exact resource-* grants AWS already maintains for this role.
    NagSuppressions.addResourceSuppressions(lambdaRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'AWSLambdaVPCAccessExecutionRole is the AWS-recommended managed policy for Lambda ENI lifecycle inside a VPC.',
        appliesTo: [
          'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole',
        ],
      },
    ]);

    // Grant Neptune access
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['neptune-db:*'],
        resources: ['*'], // Neptune doesn't support resource-level permissions
      })
    );

    // Grant Secrets Manager access
    props.neptuneConnectionSecret.grantRead(lambdaRole);

    // Build Lambda Docker image using dedicated CodeBuild project with native ARM64 compute
    // This avoids cross-compilation issues and ECR Public rate limits during local builds
    const lambdaBuild = new ArmBuildConstruct(this, 'NeptuneToolsArmBuild', {
      sourcePath: '../lambda/neptune-tools',
      region: cdk.Stack.of(this).region,
      namePrefix: `${props.projectName}-neptune-tools`,
      buildTimeoutMinutes: 10,
    });

    // Create Lambda function from pre-built ECR image
    // Use the hash-based imageTag so CloudFormation detects source changes and
    // automatically updates the Lambda — same pattern as lambda-rest-api/index.ts
    this.lambdaFunction = new lambda.DockerImageFunction(this, 'NeptuneToolsFunction', {
      functionName: `${props.projectName}-neptune-tools-v2`,
      code: lambda.DockerImageCode.fromEcr(lambdaBuild.repository, {
        tagOrDigest: lambdaBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      vpc: props.vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [lambdaSg],
      role: lambdaRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        NEPTUNE_SECRET_NAME: props.neptuneConnectionSecret.secretName,
        // AWS_REGION is automatically provided by Lambda runtime
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: 'Neptune SPARQL tools for AgentCore Gateway',
    });

    // Grant Lambda permission to pull from the build ECR repository
    lambdaBuild.repository.grantPull(lambdaRole);

    // Ensure Lambda is created after the build completes
    // This is critical: Lambda must wait for the custom resource to finish building the image
    this.lambdaFunction.node.addDependency(lambdaBuild.buildCompletion);

    // Grant Gateway role permission to invoke Lambda
    this.lambdaFunction.grantInvoke(this.gatewayRole);

    // Also add principal-based permission for Gateway service
    this.lambdaFunction.addPermission('AllowGatewayInvoke', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:gateway/*`,
    });

    // ============================================================
    // Ontop SPARQL->SQL translate Lambda (VKG Phase 5)
    // ============================================================
    // Translate-only: the agent passes the ontologyJson payload in, so this
    // Lambda needs NO VPC and NO Neptune/Athena IAM — it only reformulates
    // SPARQL into Athena SQL using the in-payload ontology mappings. It is a
    // separate Java/JVM Lambda; the gateway routes per-target by lambdaArn, so
    // two Lambdas behind one gateway is fine. PC=1 keeps the JVM warm.
    const ontopBuild = new ArmBuildConstruct(this, 'OntopTranslateArmBuild', {
      sourcePath: '../lambda/ontop-translate',
      region: cdk.Stack.of(this).region,
      namePrefix: `${props.projectName}-ontop-translate`,
      buildTimeoutMinutes: 20, // Maven shade of Ontop is slower than pip
    });

    const ontopRole = new iam.Role(this, 'OntopTranslateRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      description: 'Execution role for the Ontop SPARQL-to-SQL translate Lambda',
    });

    // cdk-nag: AWSLambdaBasicExecutionRole only grants CloudWatch Logs write
    // (the documented minimal Lambda execution role). This Lambda has no VPC
    // and no Neptune/Athena access, so no narrower customer-managed policy is
    // warranted.
    NagSuppressions.addResourceSuppressions(ontopRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'AWSLambdaBasicExecutionRole is the AWS-recommended minimal managed policy for Lambda CloudWatch Logs access.',
        appliesTo: [
          'Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
        ],
      },
    ]);

    ontopBuild.repository.grantPull(ontopRole);

    const ontopFn = new lambda.DockerImageFunction(this, 'OntopTranslateFunction', {
      functionName: `${props.projectName}-ontop-translate`,
      code: lambda.DockerImageCode.fromEcr(ontopBuild.repository, {
        tagOrDigest: ontopBuild.imageTag,
      }),
      architecture: lambda.Architecture.ARM_64,
      role: ontopRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 2048, // JVM + Ontop reformulation
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: 'Ontop translate-only SPARQL-to-Athena SQL (VKG Phase 5)',
    });

    // Ensure Lambda is created after the build completes
    ontopFn.node.addDependency(ontopBuild.buildCompletion);

    // PC=1 requires a Version/alias — keep the JVM warm to avoid cold starts.
    const ontopAlias = new lambda.Alias(this, 'OntopTranslateAlias', {
      aliasName: 'live',
      version: ontopFn.currentVersion,
      provisionedConcurrentExecutions: 1, // keep the JVM warm — no cold start
    });

    // Grant Gateway role permission to invoke the alias
    ontopAlias.grantInvoke(this.gatewayRole);

    // Also add principal-based permission for Gateway service
    ontopAlias.addPermission('AllowGatewayInvokeOntop', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:bedrock-agentcore:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:gateway/*`,
    });

    // ============================================================
    // AgentCore Gateway (AWS_IAM authentication)
    // ============================================================

    this.gateway = new agentcore.CfnGateway(this, 'NeptuneGateway', {
      name: `${props.projectName}-neptune-gateway`,
      description:
        'Neptune SPARQL tools: discover graphs, read/write ontologies, execute queries, graph statistics',
      roleArn: this.gatewayRole.roleArn,
      authorizerType: 'AWS_IAM',
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
    this.gatewayId = this.gateway.attrGatewayIdentifier;

    // ============================================================
    // Gateway Targets - Neptune Tools
    // ============================================================

    // Tool 1: discover_named_graphs
    new agentcore.CfnGatewayTarget(this, 'DiscoverGraphsTarget', {
      name: 'discover-named-graphs',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Discover all named graphs in Neptune',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'discover_named_graphs',
                  description: 'Discover all available named graphs in Neptune database',
                  inputSchema: {
                    type: 'object',
                    properties: {},
                    required: [],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 2: get_ontology_from_neptune
    new agentcore.CfnGatewayTarget(this, 'GetOntologyTarget', {
      name: 'get-ontology-from-neptune',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Read full ontology from Neptune by ontology_id',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'get_ontology_from_neptune',
                  description:
                    'Read full ontology (classes, properties, mappings, databases) from Neptune for a given ontology_id (UUID)',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description: 'Ontology identifier (UUID assigned at creation time)',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 3: persist_to_neptune
    new agentcore.CfnGatewayTarget(this, 'PersistToNeptuneTarget', {
      name: 'persist-to-neptune',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Persist RDF n-quad data to Neptune',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'persist_to_neptune',
                  description:
                    'Persist RDF n-quad data to Neptune database using SPARQL INSERT DATA',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      nquad_data: {
                        type: 'string',
                        description: 'The n-quad/RDF data to persist (format: <s> <p> <o> <g> .)',
                      },
                    },
                    required: ['nquad_data'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 4: delete_graph
    new agentcore.CfnGatewayTarget(this, 'DeleteGraphTarget', {
      name: 'delete-graph',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Drop all triples in a named ontology graph by ontology_id',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'delete_graph',
                  description:
                    'Delete (drop) all triples in the named graph for a given ontology_id. Use this to clean up or regenerate an ontology.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description:
                          'Ontology identifier (UUID) whose named graph should be dropped',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 5: execute_sparql_query
    new agentcore.CfnGatewayTarget(this, 'ExecuteSparqlTarget', {
      name: 'execute-sparql-query',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Execute generic SPARQL queries against Neptune',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'execute_sparql_query',
                  description:
                    'Execute a generic SPARQL query against Neptune (SELECT, CONSTRUCT, DESCRIBE, or UPDATE). ' +
                    'SELECT returns SPARQL-results JSON; CONSTRUCT/DESCRIBE return an RDF graph as Turtle ' +
                    '(in a {"turtle": ...} field); UPDATE returns a success status.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      sparql_query: {
                        type: 'string',
                        description: 'SPARQL query string to execute',
                      },
                      query_type: {
                        type: 'string',
                        description:
                          'Type of query: SELECT, CONSTRUCT, DESCRIBE, or UPDATE (default: SELECT)',
                      },
                    },
                    required: ['sparql_query'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 6: get_graph_summary
    new agentcore.CfnGatewayTarget(this, 'GetGraphSummaryTarget', {
      name: 'get-graph-summary',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Get summary statistics for an ontology graph',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'get_graph_summary',
                  description:
                    'Get summary statistics for an ontology graph (class count, property count, triple count)',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description: 'Ontology identifier',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 7: get_graph_stats
    new agentcore.CfnGatewayTarget(this, 'GetGraphStatsTarget', {
      name: 'get-graph-stats',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Get class distribution statistics for an ontology graph',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'get_graph_stats',
                  description:
                    'Get class distribution statistics for an ontology graph (top 20 classes by instance count)',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description: 'Ontology identifier',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 8: get_graph_classes
    new agentcore.CfnGatewayTarget(this, 'GetGraphClassesTarget', {
      name: 'get-graph-classes',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Get list of all classes in the ontology graph',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'get_graph_classes',
                  description:
                    'Get list of all classes in the ontology with URIs, labels, and comments',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description: 'Ontology identifier',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // Tool 9: get_graph_properties
    new agentcore.CfnGatewayTarget(this, 'GetGraphPropertiesTarget', {
      name: 'get-graph-properties',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Get list of all properties in the ontology graph',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: this.lambdaFunction.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'get_graph_properties',
                  description:
                    'Get list of all properties in the ontology with URIs, labels, and comments',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      ontology_id: {
                        type: 'string',
                        description: 'Ontology identifier',
                      },
                    },
                    required: ['ontology_id'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // VKG Phase 5 execution: translate a grounded SPARQL SELECT into Athena SQL
    // via the Ontop translate-only Lambda (separate Java Lambda, served by the
    // same gateway). lambdaArn points at the PC=1 alias so the gateway always
    // hits the warm JVM. The agent passes the ontologyJson it already fetched in
    // Phase 1, so this Lambda needs no Neptune/Athena access.
    new agentcore.CfnGatewayTarget(this, 'TranslateSparqlToSqlTarget', {
      name: 'translate-sparql-to-sql',
      gatewayIdentifier: this.gateway.attrGatewayIdentifier,
      description: 'Translate a grounded SPARQL query to Athena SQL via Ontop (VKG execution)',
      credentialProviderConfigurations: [
        {
          credentialProviderType: 'GATEWAY_IAM_ROLE',
        },
      ],
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: ontopAlias.functionArn,
            toolSchema: {
              inlinePayload: [
                {
                  name: 'translate_sparql_to_sql',
                  description:
                    'Translate a grounded SPARQL SELECT into Athena SQL using the ontology mappings. Returns {sql, database, catalog}.',
                  inputSchema: {
                    type: 'object',
                    properties: {
                      sparql: {
                        type: 'string',
                        description: 'The grounded SPARQL SELECT query to translate',
                      },
                      ontologyId: {
                        type: 'string',
                        description: 'Ontology identifier (used as the reformulator cache key)',
                      },
                      ontologyJson: {
                        type: 'object',
                        description:
                          'The get_ontology_from_neptune payload (classes/properties/mappings/databases)',
                      },
                    },
                    required: ['sparql', 'ontologyJson'],
                  },
                },
              ],
            },
          },
        },
      },
    });

    // ============================================================
    // Parameter Store - Gateway Configuration
    // ============================================================

    new ssm.StringParameter(this, 'GatewayUrlParameter', {
      parameterName: `/${props.projectName}/neptune-gateway/url`,
      stringValue: this.gatewayUrl,
      description: 'Neptune Gateway URL for agent access (IAM authenticated)',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'GatewayArnParameter', {
      parameterName: `/${props.projectName}/neptune-gateway/arn`,
      stringValue: this.gatewayArn,
      description: 'Neptune Gateway ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'GatewayIdParameter', {
      parameterName: `/${props.projectName}/neptune-gateway/id`,
      stringValue: this.gatewayId,
      description: 'Neptune Gateway Identifier',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Outputs
    // ============================================================

    new cdk.CfnOutput(this, 'GatewayArn', {
      value: this.gatewayArn,
      description: 'Neptune Gateway ARN',
      exportName: `${props.projectName}-neptune-gateway-arn`,
    });

    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: this.gatewayUrl,
      description: 'Neptune Gateway URL (requires IAM/SigV4 authentication)',
      exportName: `${props.projectName}-neptune-gateway-url`,
    });

    new cdk.CfnOutput(this, 'GatewayId', {
      value: this.gatewayId,
      description: 'Neptune Gateway Identifier',
      exportName: `${props.projectName}-neptune-gateway-id`,
    });

    new cdk.CfnOutput(this, 'GatewayStatus', {
      value: this.gateway.attrStatus,
      description: 'Gateway Status',
    });

    new cdk.CfnOutput(this, 'LambdaFunctionArn', {
      value: this.lambdaFunction.functionArn,
      description: 'Neptune Tools Lambda Function ARN',
      exportName: `${props.projectName}-neptune-tools-lambda-arn`,
    });

    new cdk.CfnOutput(this, 'GatewayRoleArn', {
      value: this.gatewayRole.roleArn,
      description: 'Gateway Execution Role ARN',
      exportName: `${props.projectName}-neptune-gateway-role-arn`,
    });

    new cdk.CfnOutput(this, 'UsageInstructions', {
      value: `
AgentCore Neptune Gateway deployed successfully!

Gateway URL: ${this.gatewayUrl}
Authentication: AWS_IAM (SigV4)

For agents to access Neptune tools:
1. Set environment variable:
   export NEPTUNE_GATEWAY_URL=${this.gatewayUrl}

2. Or read from Parameter Store:
   NEPTUNE_GATEWAY_URL=$(aws ssm get-parameter --name /${props.projectName}/neptune-gateway/url --query Parameter.Value --output text)

3. Agents use IAM roles for authentication (automatic via mcp-proxy-for-aws)

Available Tools (9):
- discover_named_graphs
- get_ontology_from_neptune
- persist_to_neptune
- delete_graph
- execute_sparql_query
- get_graph_summary
- get_graph_stats
- get_graph_classes
- get_graph_properties
      `.trim(),
      description: 'Usage instructions for Neptune Gateway',
    });
  }
}
