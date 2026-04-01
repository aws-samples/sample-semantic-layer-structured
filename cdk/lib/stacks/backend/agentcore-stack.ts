import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import { Construct } from 'constructs';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import { ArmBuildConstruct } from '../../common/constructs/arm-build-construct';
import { AgentCoreNeptuneGateway } from './agentcore/neptune-gateway-construct';
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
  public readonly metadataAgentRole: iam.Role;
  public readonly metadataQueryAgentRole: iam.Role;
  public readonly agentRepository: ecr.Repository;
  public readonly ontologyRuntime?: agentcore.Runtime;
  public readonly ontologyRuntimeArn?: string;
  public readonly queryRuntime?: agentcore.Runtime; // Ontology Query Agent
  public readonly queryRuntimeArn?: string; // Ontology Query Agent
  public readonly metadataRuntime: agentcore.Runtime;
  public readonly metadataRuntimeArn: string;
  public readonly metadataQueryRuntime: agentcore.Runtime;
  public readonly metadataQueryRuntimeArn: string;
  public readonly suggestionsAgentRole: iam.Role;
  public readonly suggestionsRuntime: agentcore.Runtime;
  public readonly suggestionsRuntimeArn: string;
  public readonly neptuneGateway?: AgentCoreNeptuneGateway;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    // Feature flag: skip Neptune gateway and ontology/query agents when false.
    // bedrockKbStack is always present — both KBs are needed by metadata/query/suggestions agents.
    const ontologyEnabled = !!props.neptuneStack;

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
            'glue:GetDatabase',
            'glue:GetDatabases',
            'glue:GetTable',
            'glue:GetTables',
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
            's3tables:GetTableMetadata',
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

      this.ontologyAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: [
            // Foundation models with region (2 colons)
            `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:${this.region}::foundation-model/anthropic.*`,
            // Foundation models without region (3 colons) - used by global models
            `arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:::foundation-model/anthropic.*`,
            // Inference profiles for global models
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.*`,
          ],
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

      // Grant DynamoDB query permissions (insuranceTable removed from DynamoDBStack)
      props.dynamodbStack.metadataTable.grantReadData(this.queryAgentRole);

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
            's3tables:GetTableMetadata',
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

      // Grant Bedrock model invocation for foundation models and inference profiles
      this.queryAgentRole.addToPolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: [
            // Foundation models with region (2 colons)
            `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-sonnet-4-6*`,
            // Foundation models without region (3 colons) - used by global models
            `arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6*`,
            `arn:aws:bedrock:::foundation-model/anthropic.*`,
            // Inference profiles for global models
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-4-6-*`,
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-sonnet-4-6*`,
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.*`,
          ],
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
    this.metadataAgentRole = new iam.Role(this, 'MetadataAgentRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Role for metadata generation agent (Glue catalog enrichment + KB ingestion)',
    });

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

    // Bedrock model invocation
    this.metadataAgentRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-opus-4-6-*`,
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.*`,
          `arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-6-*`,
          `arn:aws:bedrock:::foundation-model/anthropic.*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.claude-opus-4-6-*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.*`,
        ],
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
          's3tables:GetTableMetadata',
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

    // DynamoDB Scan permission for DynamoDB-backed Glue tables.
    // When Athena cannot query a DynamoDB-sourced table (URISyntaxException on ARN-based
    // StorageDescriptor.Location), the metadata agent falls back to a direct DynamoDB Scan
    // to retrieve sample rows for description generation.
    this.metadataAgentRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Scan', 'dynamodb:DescribeTable'],
        resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${props.projectName}-*`],
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

    // ============================================================
    // IAM role for Metadata Query Agent (Bedrock KB + Athena)
    // ============================================================
    this.metadataQueryAgentRole = new iam.Role(this, 'MetadataQueryAgentRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Role for metadata query agent (Bedrock KB + Athena)',
    });

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

    // Bedrock model invocation (Sonnet 4.6 + Opus 4.6)
    this.metadataQueryAgentRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.*`,
          `arn:aws:bedrock:::foundation-model/anthropic.*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.anthropic.*`,
        ],
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
          's3tables:GetTableMetadata',
          's3tables:GetNamespace',
          's3tables:ListNamespaces',
          's3tables:ListTables',
          's3tables:GetTableData',
        ],
        resources: [props.dataLakeStack.tableBucketArn, `${props.dataLakeStack.tableBucketArn}/*`],
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

    // Grant DynamoDB read access — metadata_query agent calls metadata_table.query() at every invocation
    props.dynamodbStack.metadataTable.grantReadData(this.metadataQueryAgentRole);

    // ============================================================
    // IAM role for Query Suggestions Agent (KB retrieval only)
    // ============================================================
    this.suggestionsAgentRole = new iam.Role(this, 'QuerySuggestionsAgentRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Role for query suggestions agent - KB retrieval only, no Athena',
    });

    // DynamoDB read access
    props.dynamodbStack.metadataTable.grantReadData(this.suggestionsAgentRole);

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

    // Bedrock model invocation
    this.suggestionsAgentRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.*`,
          `arn:aws:bedrock:::foundation-model/anthropic.*`,
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/global.*`,
        ],
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
    const metadataLogGroup = new logs.LogGroup(this, 'MetadataAgentLogGroup', {
      logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_metadata`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    new logs.CfnLogStream(this, 'MetadataAgentRuntimeLogsStream', {
      logGroupName: metadataLogGroup.logGroupName,
      logStreamName: 'runtime-logs',
    });
    const metadataQueryLogGroup = new logs.LogGroup(this, 'MetadataQueryAgentLogGroup', {
      logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_metadata_query`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    new logs.CfnLogStream(this, 'MetadataQueryAgentRuntimeLogsStream', {
      logGroupName: metadataQueryLogGroup.logGroupName,
      logStreamName: 'runtime-logs',
    });
    const suggestionsLogGroup = new logs.LogGroup(this, 'QuerySuggestionsAgentLogGroup', {
      logGroupName: `/aws/bedrock-agentcore/runtimes/${props.projectName.replace(/-/g, '_')}_query_suggestions`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    new logs.CfnLogStream(this, 'QuerySuggestionsAgentRuntimeLogsStream', {
      logGroupName: suggestionsLogGroup.logGroupName,
      logStreamName: 'runtime-logs',
    });

    // Grant all agent roles permissions to pull from ECR
    if (ontologyEnabled) {
      this.agentRepository.grantPull(this.ontologyAgentRole!);
      this.agentRepository.grantPull(this.queryAgentRole!);
    }
    this.agentRepository.grantPull(this.metadataAgentRole);
    this.agentRepository.grantPull(this.metadataQueryAgentRole);
    this.agentRepository.grantPull(this.suggestionsAgentRole);

    // Build ARM64 Docker image for Metadata Agent
    const metadataBuild = new ArmBuildConstruct(this, 'MetadataAgentBuild', {
      sourcePath: '../agents',
      region: this.region,
      namePrefix: `${props.projectName}-metadata`,
      buildTimeoutMinutes: 20,
      dockerfileName: 'Dockerfile.metadata',
    });

    // Build ARM64 Docker image for Metadata Query Agent
    const metadataQueryBuild = new ArmBuildConstruct(this, 'MetadataQueryAgentBuild', {
      sourcePath: '../agents',
      region: this.region,
      namePrefix: `${props.projectName}-metadata-query`,
      buildTimeoutMinutes: 20,
      dockerfileName: 'Dockerfile.metadataquery',
    });

    // Build ARM64 Docker image for Query Suggestions Agent
    const suggestionsBuild = new ArmBuildConstruct(this, 'QuerySuggestionsAgentBuild', {
      sourcePath: '../agents',
      region: this.region,
      namePrefix: `${props.projectName}-query-suggestions`,
      buildTimeoutMinutes: 20,
      dockerfileName: 'Dockerfile.querysuggestions',
    });

    const metadataArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
      metadataBuild.repository,
      metadataBuild.imageTag
    );

    const metadataQueryArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
      metadataQueryBuild.repository,
      metadataQueryBuild.imageTag
    );

    const suggestionsArtifact = agentcore.AgentRuntimeArtifact.fromEcrRepository(
      suggestionsBuild.repository,
      suggestionsBuild.imageTag
    );

    if (ontologyEnabled) {
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
      this.ontologyRuntime = new agentcore.Runtime(this, 'OntologyGenerationRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_ontology`,
        agentRuntimeArtifact: ontologyArtifact,
        executionRole: this.ontologyAgentRole!,
        description: 'AgentCore Runtime for Ontology Generation Agent',
        authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingIAM(),
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: {
          AWS_REGION: this.region,
          KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
          ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
          NEPTUNE_LOAD_ROLE: props.neptuneStack!.loadRole.roleArn,
          PROJECT_NAME: props.projectName,
          NEPTUNE_GATEWAY_URL: this.neptuneGateway.gatewayUrl,
          ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
          ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
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
        },
      });

      // Deploy Ontology Query Agent to AgentCore Runtime
      this.queryRuntime = new agentcore.Runtime(this, 'OntologyQueryRuntime', {
        runtimeName: `${props.projectName.replace(/-/g, '_')}_ontology_query`,
        agentRuntimeArtifact: queryArtifact,
        executionRole: this.queryAgentRole!,
        description: 'AgentCore Runtime for Ontology Query Agent',
        authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingIAM(),
        networkConfiguration: agentcoreNetworkConfig,
        lifecycleConfiguration: {
          idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
          maxLifetime: cdk.Duration.hours(8),
        },
        environmentVariables: {
          AWS_REGION: this.region,
          ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
          PROJECT_NAME: props.projectName,
          ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
          NEPTUNE_GATEWAY_URL: this.neptuneGateway.gatewayUrl,
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
        },
      });

      // Grant ontology/query roles permission to pull from their build ECR repositories
      ontologyBuild.repository.grantPull(this.ontologyAgentRole!);
      queryBuild.repository.grantPull(this.queryAgentRole!);

      // Ensure runtimes are created after builds complete
      this.ontologyRuntime.node.addDependency(ontologyBuild.buildCompletion);
      this.queryRuntime.node.addDependency(queryBuild.buildCompletion);

      this.ontologyRuntimeArn = this.ontologyRuntime.agentRuntimeArn;
      this.queryRuntimeArn = this.queryRuntime.agentRuntimeArn;
    } // end if (ontologyEnabled) — builds, gateway, ontology/query runtimes

    // Deploy Metadata Agent to AgentCore Runtime
    this.metadataRuntime = new agentcore.Runtime(this, 'MetadataRuntime', {
      runtimeName: `${props.projectName.replace(/-/g, '_')}_metadata`,
      agentRuntimeArtifact: metadataArtifact,
      executionRole: this.metadataAgentRole,
      description: 'AgentCore Runtime for Metadata Generation Agent (Glue Catalog + KB enrichment)',
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingIAM(),
      networkConfiguration: agentcoreNetworkConfig,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
        maxLifetime: cdk.Duration.hours(8),
      },
      environmentVariables: {
        AWS_REGION: this.region,
        KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
        // Semantic RAG KB — metadata_agent writes docs to S3 then triggers ingestion
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId,
        SEMANTIC_RAG_DATA_SOURCE_ID: props.bedrockKbStack.semanticRagDataSourceId,
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
          `aws.log.group.names=${metadataLogGroup.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${metadataLogGroup.logGroupName}`,
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
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_latest_experimental,gen_ai_tool_definitions',
      },
    });

    // Deploy Metadata Query Agent to AgentCore Runtime
    this.metadataQueryRuntime = new agentcore.Runtime(this, 'MetadataQueryRuntime', {
      runtimeName: `${props.projectName.replace(/-/g, '_')}_metadata_query`,
      agentRuntimeArtifact: metadataQueryArtifact,
      executionRole: this.metadataQueryAgentRole,
      description: 'AgentCore Runtime for Metadata Query Agent (Bedrock KB + Athena)',
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingIAM(),
      networkConfiguration: agentcoreNetworkConfig,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
        maxLifetime: cdk.Duration.hours(8),
      },
      environmentVariables: {
        AWS_REGION: this.region,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId,
        ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        PROJECT_NAME: props.projectName,
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_metadata_query.DEFAULT`,
          `aws.log.group.names=${metadataQueryLogGroup.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${metadataQueryLogGroup.logGroupName}`,
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
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_latest_experimental,gen_ai_tool_definitions',
      },
    });

    // Deploy Query Suggestions Agent to AgentCore Runtime
    this.suggestionsRuntime = new agentcore.Runtime(this, 'QuerySuggestionsRuntime', {
      runtimeName: `${props.projectName.replace(/-/g, '_')}_query_suggestions`,
      agentRuntimeArtifact: suggestionsArtifact,
      executionRole: this.suggestionsAgentRole,
      description:
        'AgentCore Runtime for Query Suggestions Agent — generates contextual questions from KB',
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingIAM(),
      networkConfiguration: agentcoreNetworkConfig,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
        maxLifetime: cdk.Duration.hours(8),
      },
      environmentVariables: {
        AWS_REGION: this.region,
        ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId,
        PROJECT_NAME: props.projectName,
        // OpenTelemetry — routes traces/logs to CloudWatch GenAI Observability
        AGENT_OBSERVABILITY_ENABLED: 'true',
        OTEL_PYTHON_DISTRO: 'aws_distro',
        OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
        OTEL_RESOURCE_ATTRIBUTES: [
          `service.name=${props.projectName.replace(/-/g, '_')}_query_suggestions.DEFAULT`,
          `aws.log.group.names=${suggestionsLogGroup.logGroupName}`,
        ].join(','),
        OTEL_EXPORTER_OTLP_LOGS_HEADERS: [
          `x-aws-log-group=${suggestionsLogGroup.logGroupName}`,
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
        OTEL_SEMCONV_STABILITY_OPT_IN: 'gen_ai_latest_experimental,gen_ai_tool_definitions',
      },
    });

    // Grant metadata/suggestions roles permission to pull from build ECR repositories
    metadataBuild.repository.grantPull(this.metadataAgentRole);
    metadataQueryBuild.repository.grantPull(this.metadataQueryAgentRole);
    suggestionsBuild.repository.grantPull(this.suggestionsAgentRole);

    // Ensure runtimes are created after the builds complete
    this.metadataRuntime.node.addDependency(metadataBuild.buildCompletion);
    this.metadataQueryRuntime.node.addDependency(metadataQueryBuild.buildCompletion);
    this.suggestionsRuntime.node.addDependency(suggestionsBuild.buildCompletion);

    // Store ARNs
    this.metadataRuntimeArn = this.metadataRuntime.agentRuntimeArn;
    this.metadataQueryRuntimeArn = this.metadataQueryRuntime.agentRuntimeArn;
    this.suggestionsRuntimeArn = this.suggestionsRuntime.agentRuntimeArn;

    // ── Native log delivery (APPLICATION_LOGS + USAGE_LOGS) ─────────────────
    // Configures AgentCore Runtime → CloudWatch Logs delivery so that:
    //   1. The AgentCore console shows "Log delivery: N"
    //   2. APPLICATION_LOGS session data flows to the log group used by online eval
    //   3. USAGE_LOGS token/CPU/memory metrics populate the GenAI Observability
    //      "Resource consumption" dashboard section
    const pn = props.projectName.replace(/-/g, '_');

    const runtimeDeliveries: { id: string; runtimeArn: string; runtimeName: string }[] = [
      { id: 'Metadata', runtimeArn: this.metadataRuntimeArn, runtimeName: `${pn}_metadata` },
      {
        id: 'MetadataQuery',
        runtimeArn: this.metadataQueryRuntimeArn,
        runtimeName: `${pn}_metadata_query`,
      },
      {
        id: 'QuerySuggestions',
        runtimeArn: this.suggestionsRuntimeArn,
        runtimeName: `${pn}_query_suggestions`,
      },
    ];
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
                  `pip install --quiet --target "${outputDir}" -r "${path.join(cloudResourceIdHandlerDir, 'requirements.txt')}"`,
                  { stdio: 'pipe' }
                );
                for (const f of fs.readdirSync(cloudResourceIdHandlerDir)) {
                  if (f.endsWith('.py')) {
                    fs.copyFileSync(
                      path.join(cloudResourceIdHandlerDir, f),
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
    const passRoleArns: string[] = [
      this.metadataAgentRole.roleArn,
      this.metadataQueryAgentRole.roleArn,
      this.suggestionsAgentRole.roleArn,
    ];
    if (this.ontologyAgentRole) passRoleArns.push(this.ontologyAgentRole.roleArn);
    if (this.queryAgentRole) passRoleArns.push(this.queryAgentRole.roleArn);
    cloudResourceIdHandler.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: passRoleArns,
      })
    );

    const cloudResourceIdProvider = new cr.Provider(this, 'CloudResourceIdProvider', {
      onEventHandler: cloudResourceIdHandler,
    });

    // OtelEnvFingerprint — any string that changes whenever runtime env vars are updated by CDK.
    // Changing this property forces CFN to re-invoke the CloudResourceIdHandler Lambda,
    // which re-injects cloud.resource_id after a CDK-initiated runtime env var reset.
    // ⚠️ IMPORTANT: bump this value whenever you change OTEL_* env vars in this stack.
    const otelEnvFingerprint = 'v4-stable-semconv';

    const cloudResourceIdRuntimes: { id: string; runtimeArn: string }[] = [
      { id: 'Metadata', runtimeArn: this.metadataRuntimeArn },
      { id: 'MetadataQuery', runtimeArn: this.metadataQueryRuntimeArn },
      { id: 'QuerySuggestions', runtimeArn: this.suggestionsRuntimeArn },
    ];
    if (ontologyEnabled && this.ontologyRuntimeArn) {
      cloudResourceIdRuntimes.push({ id: 'Ontology', runtimeArn: this.ontologyRuntimeArn });
    }
    if (ontologyEnabled && this.queryRuntimeArn) {
      cloudResourceIdRuntimes.push({ id: 'OntologyQuery', runtimeArn: this.queryRuntimeArn });
    }
    for (const { id, runtimeArn } of cloudResourceIdRuntimes) {
      new cdk.CustomResource(this, `${id}CloudResourceId`, {
        serviceToken: cloudResourceIdProvider.serviceToken,
        properties: {
          AgentRuntimeArn: runtimeArn,
          Region: this.region,
          OtelEnvFingerprint: otelEnvFingerprint,
        },
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

    new cdk.CfnOutput(this, 'MetadataAgentRoleArn', {
      value: this.metadataAgentRole.roleArn,
      description: 'IAM role for metadata agent',
      exportName: `${props.projectName}-metadata-agent-role`,
    });

    new cdk.CfnOutput(this, 'MetadataQueryAgentRoleArn', {
      value: this.metadataQueryAgentRole.roleArn,
      description: 'IAM role for metadata query agent',
      exportName: `${props.projectName}-metadata-query-agent-role`,
    });

    new cdk.CfnOutput(this, 'MetadataRuntimeArn', {
      value: this.metadataRuntimeArn,
      description: 'AgentCore Runtime ARN for Metadata Agent',
      exportName: `${props.projectName}-metadata-runtime-arn`,
    });

    new cdk.CfnOutput(this, 'MetadataQueryRuntimeArn', {
      value: this.metadataQueryRuntimeArn,
      description: 'AgentCore Runtime ARN for Metadata Query Agent',
      exportName: `${props.projectName}-metadata-query-runtime-arn`,
    });

    new cdk.CfnOutput(this, 'MetadataRuntimeEndpoint', {
      value: cdk.Fn.join('', [
        'https://bedrock-agentcore.',
        this.region,
        '.amazonaws.com/runtimes/',
        this.metadataRuntimeArn,
      ]),
      description: 'AgentCore Runtime endpoint for Metadata Agent',
    });

    new cdk.CfnOutput(this, 'MetadataQueryRuntimeEndpoint', {
      value: cdk.Fn.join('', [
        'https://bedrock-agentcore.',
        this.region,
        '.amazonaws.com/runtimes/',
        this.metadataQueryRuntimeArn,
      ]),
      description: 'AgentCore Runtime endpoint for Metadata Query Agent',
    });

    new cdk.CfnOutput(this, 'MetadataAgentLogGroupName', {
      value: metadataLogGroup.logGroupName,
      description: 'CloudWatch log group for Metadata Agent OTEL telemetry',
    });
    new cdk.CfnOutput(this, 'MetadataQueryAgentLogGroupName', {
      value: metadataQueryLogGroup.logGroupName,
      description: 'CloudWatch log group for Metadata Query Agent OTEL telemetry',
    });
    new cdk.CfnOutput(this, 'QuerySuggestionsAgentLogGroupName', {
      value: suggestionsLogGroup.logGroupName,
      description: 'CloudWatch log group for Query Suggestions Agent OTEL telemetry',
    });

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
        METADATA_AGENT: {
          AWS_REGION: this.region,
          KNOWLEDGE_BASE_ID: props.bedrockKbStack.ontologyPatternsKbId,
          SEMANTIC_RAG_KB_ID: props.bedrockKbStack.semanticRagKbId,
          SEMANTIC_RAG_DATA_SOURCE_ID: props.bedrockKbStack.semanticRagDataSourceId,
          ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
          ARTIFACTS_BUCKET: props.dataLakeStack.artifactsBucket.bucketName,
          ONTOLOGY_METADATA_TABLE: props.dynamodbStack.metadataTable.tableName,
        },
        METADATA_QUERY_AGENT: {
          AWS_REGION: this.region,
          BEDROCK_KB_ID: props.bedrockKbStack.semanticRagKbId,
          ATHENA_WORKGROUP: props.athenaStack.workgroup.name,
        },
      }),
      description: 'Environment configuration for agents (Neptune access via Gateway)',
    });

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
