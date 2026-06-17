import * as cdk from 'aws-cdk-lib';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lakeformation from 'aws-cdk-lib/aws-lakeformation';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { DynamoDBStack } from './dynamodb-stack';
import { GlueCrawlerTrigger } from './glue-catalog/crawler-trigger-construct';

export interface GlueCatalogStackProps extends cdk.StackProps {
  projectName: string;
  dynamodbStack: DynamoDBStack;
  autoStartCrawlers?: boolean;
  // Additional principals to register as LakeFormation admins (e.g., CDK deploy role, human admin role)
  additionalLakeFormationAdmins?: string[];
}

/**
 * Glue Catalog Stack
 * Creates Glue databases and crawlers for DynamoDB operational data
 * S3 Tables (Iceberg) manages metadata automatically, no crawler needed
 */
export class GlueCatalogStack extends cdk.Stack {
  public readonly dynamodbDatabase: glue.CfnDatabase;
  public readonly database: glue.CfnDatabase; // Default database for Lambda API (DynamoDB operational data)
  public readonly dynamodbCrawler: glue.CfnCrawler;

  constructor(scope: Construct, id: string, props: GlueCatalogStackProps) {
    super(scope, id, props);

    // IAM role for Glue crawlers with inline policy for DynamoDB access
    const crawlerRole = new iam.Role(this, 'GlueCrawlerRole', {
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      description: 'Role for Glue crawlers to access DynamoDB and S3',
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole'),
      ],
      inlinePolicies: {
        DynamoDBAccessPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: [
                'dynamodb:DescribeTable',
                'dynamodb:Scan',
              ],
              resources: [
                props.dynamodbStack.adminCodesTable.tableArn,
                props.dynamodbStack.coverageProductsTable.tableArn,
                props.dynamodbStack.coveragesTable.tableArn,
                props.dynamodbStack.financialActivitiesTable.tableArn,
                props.dynamodbStack.financialStatementsTable.tableArn,
                props.dynamodbStack.holdingsTable.tableArn,
                props.dynamodbStack.investProductsTable.tableArn,
                props.dynamodbStack.partiesTable.tableArn,
                props.dynamodbStack.policyProductsTable.tableArn,
                props.dynamodbStack.relationsTable.tableArn,
                props.dynamodbStack.ridersTable.tableArn,
                props.dynamodbStack.typeCodesTable.tableArn,
                props.dynamodbStack.metadataTable.tableArn,
              ],
            }),
          ],
        }),
      },
    });

    // Grant permissions to all DynamoDB tables
    const allTables = [
      props.dynamodbStack.adminCodesTable,
      props.dynamodbStack.coverageProductsTable,
      props.dynamodbStack.coveragesTable,
      props.dynamodbStack.financialActivitiesTable,
      props.dynamodbStack.financialStatementsTable,
      props.dynamodbStack.holdingsTable,
      props.dynamodbStack.investProductsTable,
      props.dynamodbStack.partiesTable,
      props.dynamodbStack.policyProductsTable,
      props.dynamodbStack.relationsTable,
      props.dynamodbStack.ridersTable,
      props.dynamodbStack.typeCodesTable,
      props.dynamodbStack.metadataTable,
    ];

    // Note: DynamoDB permissions (DescribeTable, Scan) are granted via inline policy in the role definition above
    // S3 Tables manages metadata automatically, no S3 bucket permissions needed

    // ============================================================================
    // Lake Formation administrators
    // Explicitly register the CDK deploy role and any additional admin roles so
    // they can create and manage LakeFormation grants via CloudFormation.
    // The deployer's current caller identity is added automatically; we also add
    // the CDK bootstrap CloudFormation execution role and any extra ARNs supplied
    // via props.additionalLakeFormationAdmins.
    // ============================================================================
    const adminArns: lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty[] = [
      // CDK CloudFormation execution role (created by cdk bootstrap)
      { dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-cfn-exec-role-${this.account}-${this.region}` },
      // Catch-all for any CDK bootstrap file-publishing / deploy roles
      { dataLakePrincipalIdentifier: `arn:aws:iam::${this.account}:role/cdk-hnb659fds-deploy-role-${this.account}-${this.region}` },
    ];

    if (props.additionalLakeFormationAdmins) {
      props.additionalLakeFormationAdmins.forEach((arn) => {
        adminArns.push({ dataLakePrincipalIdentifier: arn });
      });
    }

    new lakeformation.CfnDataLakeSettings(this, 'DataLakeSettings', {
      admins: adminArns,
    });

    // Database for DynamoDB tables (operational data)
    this.dynamodbDatabase = new glue.CfnDatabase(this, 'DynamoDBDatabase', {
      catalogId: this.account,
      databaseInput: {
        name: `${props.projectName}_dynamodb`.replace(/-/g, '_'),
        description: 'Schema catalog for current operational insurance data in DynamoDB',
      },
    });

    // Set default database for Lambda API (use DynamoDB database for operational queries)
    // Note: S3 Tables (Iceberg) analytics data is in separate namespace managed by DataLakeStack
    this.database = this.dynamodbDatabase;

    // ============================================================================
    // Lake Formation permissions for the crawler role
    // Required when Lake Formation is enabled: IAM permissions alone are not
    // sufficient — Lake Formation acts as a second authorization layer on the
    // Glue Data Catalog. Without these grants the crawler receives:
    //   "Insufficient Lake Formation permission(s) on <database>"
    // ============================================================================

    // Database-level: allows the crawler to CREATE new tables and DESCRIBE/ALTER the database
    const crawlerLFDatabasePermissions = new lakeformation.CfnPermissions(this, 'CrawlerLFDatabasePermissions', {
      dataLakePrincipal: {
        dataLakePrincipalIdentifier: crawlerRole.roleArn,
      },
      resource: {
        databaseResource: {
          catalogId: this.account,
          name: this.dynamodbDatabase.ref,
        },
      },
      permissions: ['CREATE_TABLE', 'DESCRIBE', 'ALTER'],
    });
    crawlerLFDatabasePermissions.node.addDependency(this.dynamodbDatabase);

    // Table-level (wildcard): allows the crawler to update/overwrite table metadata on
    // subsequent crawls and to drop stale entries when deleteBehavior triggers removal
    const crawlerLFTablePermissions = new lakeformation.CfnPermissions(this, 'CrawlerLFTablePermissions', {
      dataLakePrincipal: {
        dataLakePrincipalIdentifier: crawlerRole.roleArn,
      },
      resource: {
        tableResource: {
          catalogId: this.account,
          databaseName: this.dynamodbDatabase.ref,
          tableWildcard: {},
        },
      },
      permissions: ['DESCRIBE', 'ALTER', 'DROP'],
    });
    crawlerLFTablePermissions.node.addDependency(this.dynamodbDatabase);

    // Crawler for DynamoDB tables - crawl all tables for operational queries
    this.dynamodbCrawler = new glue.CfnCrawler(this, 'DynamoDBCrawler', {
      name: `${props.projectName}-dynamodb-crawler`,
      role: crawlerRole.roleArn,
      databaseName: this.dynamodbDatabase.ref,
      targets: {
        dynamoDbTargets: allTables.map((table) => ({
          path: table.tableName,
        })),
      },
      schedule: {
        scheduleExpression: 'cron(0 2 * * ? *)', // Daily at 2 AM UTC
      },
      schemaChangePolicy: {
        updateBehavior: 'UPDATE_IN_DATABASE',
        deleteBehavior: 'LOG',
      },
      configuration: JSON.stringify({
        Version: 1.0,
        CrawlerOutput: {
          Partitions: { AddOrUpdateBehavior: 'InheritFromTable' },
          Tables: { AddOrUpdateBehavior: 'MergeNewColumns' },
        },
      }),
    });

    // Auto-start DynamoDB crawler on deployment if enabled
    if (props.autoStartCrawlers !== false) {
      new GlueCrawlerTrigger(this, 'DynamoDBCrawlerTrigger', {
        crawlerName: this.dynamodbCrawler.name!,
        autoStart: true,
      });
    }

    // Apply cdk-nag suppressions for Glue crawler role
    // IAM4: AWSGlueServiceRole is the required AWS managed policy for Glue service access to catalogs
    NagSuppressions.addResourceSuppressions(crawlerRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason: 'AWSGlueServiceRole is the standard AWS managed policy required for Glue crawlers to access the Glue Data Catalog and perform metadata discovery operations',
        appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSGlueServiceRole'],
      },
    ]);

    // Outputs
    new cdk.CfnOutput(this, 'DynamoDBDatabaseName', {
      value: this.dynamodbDatabase.ref,
      description: 'Glue database for DynamoDB tables (operational data)',
      exportName: `${props.projectName}-dynamodb-database`,
    });

    new cdk.CfnOutput(this, 'DynamoDBCrawlerName', {
      value: this.dynamodbCrawler.name!,
      description: 'DynamoDB crawler name',
    });

    // LakeFormation CfnPermissions fail to delete when the underlying Glue database
    // is removed first. Retain them to avoid DELETE_FAILED during cdk destroy.
    this.node.findAll().forEach((child) => {
      if (child instanceof lakeformation.CfnPermissions) {
        child.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
      }
    });
  }
}
