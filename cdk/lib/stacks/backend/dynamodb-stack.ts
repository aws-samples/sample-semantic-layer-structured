import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { DynamoDBDataLoader } from './dynamodb/data-loader-construct';
import * as path from 'path';

export interface DynamoDBStackProps extends cdk.StackProps {
  projectName: string;
  loadSyntheticData?: boolean; // Whether to load synthetic data on deployment
}

/**
 * DynamoDB Stack
 * Creates separate DynamoDB tables for each dataset in complete_synthetic_data
 * Based on the synthetic data structure from SYNTHETIC_DATA_README.md
 */
export class DynamoDBStack extends cdk.Stack {
  // Individual tables for each dataset
  public readonly adminCodesTable: dynamodb.Table;
  public readonly coverageProductsTable: dynamodb.Table;
  public readonly coveragesTable: dynamodb.Table;
  public readonly financialActivitiesTable: dynamodb.Table;
  public readonly financialStatementsTable: dynamodb.Table;
  public readonly holdingsTable: dynamodb.Table;
  public readonly investProductsTable: dynamodb.Table;
  public readonly partiesTable: dynamodb.Table;
  public readonly policyProductsTable: dynamodb.Table;
  public readonly relationsTable: dynamodb.Table;
  public readonly ridersTable: dynamodb.Table;
  public readonly typeCodesTable: dynamodb.Table;

  // Metadata table for semantic ontology tracking
  public readonly metadataTable: dynamodb.Table;
  public readonly metricsTable: dynamodb.Table;

  // Chat sessions table for AG-UI multi-turn chat (TTL=24h)
  public readonly chatSessionsTable: dynamodb.Table;

  // Per-turn user feedback (👍/👎 + comment), keyed by ontology so the
  // admin tab can list/delete per ontology.
  public readonly feedbackTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DynamoDBStackProps) {
    super(scope, id, props);

    // Helper function to create a standard table configuration
    // All tables use pk/sk composite key pattern for single-table design
    const createTable = (logicalId: string, tableSuffix: string) => {
      const table = new dynamodb.Table(this, logicalId, {
        tableName: `${props.projectName}-${tableSuffix}`,
        partitionKey: {
          name: 'pk',
          type: dynamodb.AttributeType.STRING,
        },
        sortKey: {
          name: 'sk',
          type: dynamodb.AttributeType.STRING,
        },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        // DynamoDB-owned encryption (DEFAULT). AWS_MANAGED (aws/dynamodb) is explicitly
        // unsupported for Zero-ETL integrations — its key policy cannot be modified to
        // grant the Glue service principal kms:Decrypt.
        encryption: dynamodb.TableEncryption.DEFAULT,
        pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      });

      // Resource-based policy required for Glue Zero-ETL integrations.
      // Allows the Glue service to DescribeTable, ExportTableToPointInTime,
      // and DescribeExport on behalf of any integration in this account.
      (table.node.defaultChild as dynamodb.CfnTable).resourcePolicy = {
        policyDocument: {
          Version: '2012-10-17',
          Statement: [
            {
              Sid: 'AllowGlueZeroETL',
              Effect: 'Allow',
              Principal: { Service: 'glue.amazonaws.com' },
              Resource: '*',
              Action: [
                'dynamodb:ExportTableToPointInTime',
                'dynamodb:DescribeTable',
                'dynamodb:DescribeExport',
              ],
              Condition: {
                StringEquals: { 'aws:SourceAccount': cdk.Aws.ACCOUNT_ID },
                ArnLike: {
                  'aws:SourceArn': `arn:aws:glue:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:integration:*`,
                },
              },
            },
          ],
        },
      };

      return table;
    };

    // Create separate table for each dataset
    // All tables use pk/sk composite keys matching the data schema
    this.adminCodesTable = createTable('AdminCodesTable', 'admin-codes');
    this.coverageProductsTable = createTable('CoverageProductsTable', 'coverage-products');
    this.coveragesTable = createTable('CoveragesTable', 'coverages');
    this.financialActivitiesTable = createTable('FinancialActivitiesTable', 'financial-activities');
    this.financialStatementsTable = createTable('FinancialStatementsTable', 'financial-statements');
    this.holdingsTable = createTable('HoldingsTable', 'holdings');
    this.investProductsTable = createTable('InvestProductsTable', 'invest-products');
    this.partiesTable = createTable('PartiesTable', 'parties');
    this.policyProductsTable = createTable('PolicyProductsTable', 'policy-products');
    this.relationsTable = createTable('RelationsTable', 'relations');
    this.ridersTable = createTable('RidersTable', 'riders');
    this.typeCodesTable = createTable('TypeCodesTable', 'type-codes');

    // Metadata table for semantic ontology tracking
    // NOTE: If updating an existing table that has a 'version' sort key, you must either:
    // 1. Keep using version='v1' in all operations (current approach in ontology_service.py)
    // 2. Or destroy and recreate the table to remove the sort key
    // CloudFormation cannot modify the schema of existing tables (add/remove keys)
    this.metadataTable = new dynamodb.Table(this, 'SemanticMetadata', {
      tableName: `${props.projectName}-metadata`,
      partitionKey: {
        name: 'id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'version',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Chat sessions for the AG-UI multi-turn chat (item #1).
    // TTL=24h is enforced by the `ttl` attribute. Items hold the full transcript so a
    // browser refresh can restore mid-conversation.
    this.chatSessionsTable = new dynamodb.Table(this, 'ChatSessionsTable', {
      tableName: `${props.projectName}-chat-sessions`,
      partitionKey: {
        name: 'sessionId',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: false },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: 'ttl',
    });

    // Governed-metrics table (Tier 1 progressive disclosure).
    // pk=NS#<namespace>, sk=METRIC#<id> so all metrics in a namespace fetch
    // in one Query. The lifecycle GSI lets the admin UI list metrics by
    // status (draft/published/archived) without scanning.
    this.metricsTable = new dynamodb.Table(this, 'MetricsTable', {
      tableName: `${props.projectName}-metrics`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });
    this.metricsTable.addGlobalSecondaryIndex({
      indexName: 'lifecycle-index',
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'lifecycle', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Per-user chat session list (chat-first redesign 2026-05-24).
    // Sidebar query: "give me this user's most-recent sessions across all
    // ontologies." KEYS_ONLY keeps GSI write costs minimal — the list endpoint
    // hydrates titles/ontology metadata via a follow-up BatchGetItem.
    this.chatSessionsTable.addGlobalSecondaryIndex({
      indexName: 'userId-updatedAt-index',
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'updatedAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });

    // Lessons-learned is no longer DDB-backed. Long-term records live in
    // Bedrock AgentCore Memory (managed by ``agentcore-memory-stack.ts``);
    // there is no ``lessons`` table in this stack.

    // Per-turn user feedback: 👍/👎 + comment for one assistant turn. The
    // partition key is the ontology so the admin tab can scope listing
    // without a scan, and the sort key is ``createdAt#feedbackId`` so we
    // get newest-first ordering with stable ids when timestamps tie.
    // Comments are PII-redacted by Bedrock Guardrails before write — see
    // services/feedback_service.py.
    this.feedbackTable = new dynamodb.Table(this, 'FeedbackTable', {
      tableName: `${props.projectName}-feedback`,
      partitionKey: {
        name: 'ontologyId',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'sk',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Create IAM role for Athena DynamoDB connector
    const athenaConnectorRole = new iam.Role(this, 'AthenaDynamoDBConnectorRole', {
      assumedBy: new iam.ServicePrincipal('athena.amazonaws.com'),
      description: 'Role for Athena to query DynamoDB tables',
    });

    // Grant Athena read access to all tables
    const allTables = [
      this.adminCodesTable,
      this.coverageProductsTable,
      this.coveragesTable,
      this.financialActivitiesTable,
      this.financialStatementsTable,
      this.holdingsTable,
      this.investProductsTable,
      this.partiesTable,
      this.policyProductsTable,
      this.relationsTable,
      this.ridersTable,
      this.typeCodesTable,
      this.metadataTable,
    ];

    allTables.forEach((table) => table.grantReadData(athenaConnectorRole));

    // Outputs for all tables
    const outputTables = [
      { name: 'AdminCodes', table: this.adminCodesTable },
      { name: 'CoverageProducts', table: this.coverageProductsTable },
      { name: 'Coverages', table: this.coveragesTable },
      { name: 'FinancialActivities', table: this.financialActivitiesTable },
      { name: 'FinancialStatements', table: this.financialStatementsTable },
      { name: 'Holdings', table: this.holdingsTable },
      { name: 'InvestProducts', table: this.investProductsTable },
      { name: 'Parties', table: this.partiesTable },
      { name: 'PolicyProducts', table: this.policyProductsTable },
      { name: 'Relations', table: this.relationsTable },
      { name: 'Riders', table: this.ridersTable },
      { name: 'TypeCodes', table: this.typeCodesTable },
      { name: 'Metadata', table: this.metadataTable },
      { name: 'ChatSessions', table: this.chatSessionsTable },
    ];

    outputTables.forEach(({ name, table }) => {
      new cdk.CfnOutput(this, `${name}TableName`, {
        value: table.tableName,
        description: `${name} table name`,
        exportName: `${props.projectName}-${name.toLowerCase()}-table-name`,
      });

      new cdk.CfnOutput(this, `${name}TableArn`, {
        value: table.tableArn,
        description: `${name} table ARN`,
        exportName: `${props.projectName}-${name.toLowerCase()}-table-arn`,
      });
    });

    new cdk.CfnOutput(this, 'AthenaDynamoDBConnectorRoleArn', {
      value: athenaConnectorRole.roleArn,
      description: 'Athena DynamoDB connector role ARN',
      exportName: `${props.projectName}-athena-dynamodb-role-arn`,
    });

    // Load synthetic data if requested - each dataset into its own table
    if (props.loadSyntheticData) {
      const dataPath = path.join(__dirname, '../../../../data/complete_synthetic_data');

      // Map each dataset file to its corresponding table
      const datasetMappings = [
        { table: this.typeCodesTable, filename: 'type_codes.json', displayName: 'Type Codes' },
        { table: this.adminCodesTable, filename: 'admin_codes.json', displayName: 'Admin Codes' },
        {
          table: this.policyProductsTable,
          filename: 'policy_products.json',
          displayName: 'Policy Products',
        },
        {
          table: this.coverageProductsTable,
          filename: 'coverage_products.json',
          displayName: 'Coverage Products',
        },
        {
          table: this.investProductsTable,
          filename: 'invest_products.json',
          displayName: 'Investment Products',
        },
        { table: this.partiesTable, filename: 'parties.json', displayName: 'Parties' },
        { table: this.coveragesTable, filename: 'coverages.json', displayName: 'Coverages' },
        { table: this.holdingsTable, filename: 'holdings.json', displayName: 'Holdings' },
        {
          table: this.financialActivitiesTable,
          filename: 'financial_activities.json',
          displayName: 'Financial Activities',
        },
        {
          table: this.financialStatementsTable,
          filename: 'financial_statements.json',
          displayName: 'Financial Statements',
        },
        { table: this.ridersTable, filename: 'riders.json', displayName: 'Riders' },
        { table: this.relationsTable, filename: 'relations.json', displayName: 'Relations' },
      ];

      // Create a data loader for each dataset/table pair
      datasetMappings.forEach(({ table, filename, displayName }) => {
        new DynamoDBDataLoader(this, `${displayName.replace(/\s+/g, '')}Loader`, {
          table: table,
          dataPath: dataPath,
          dataFiles: [{ filename, displayName }],
          autoLoad: true,
        });
      });

      cdk.Annotations.of(this).addInfo(
        'Synthetic data will be loaded automatically during deployment into separate tables. ' +
          'This may take 10-15 minutes for ~11MB of data across 12 tables.'
      );
    } else {
      cdk.Annotations.of(this).addWarning(
        'Synthetic data loading is disabled. ' +
          'Run scripts/load_to_dynamodb.py manually to load data, or ' +
          `enable by setting loadSyntheticData: true in the stack props.`
      );
    }
  }
}
