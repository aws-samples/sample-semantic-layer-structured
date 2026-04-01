import * as cdk from 'aws-cdk-lib';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { DynamoDBStack } from '../dynamodb-stack';

export interface ZeroEtlStackProps extends cdk.StackProps {
  projectName: string;
  dynamodbStack: DynamoDBStack;
  /** ARN of the existing S3 Tables bucket (from DataLakeStack.tableBucketArn) */
  tableBucketArn: string;
  /** Name of the S3 Tables bucket (used to build the Glue federated catalog ARN) */
  tableBucketName: string;
  /** Namespace name from DataLakeStack (informational — Zero-ETL always creates its own
   *  UUID namespace per integration; the namespace-level ARN is not a valid targetArn). */
  tableBucketNamespace: string;
}

/**
 * Zero-ETL Stack
 *
 * Creates one Glue Zero-ETL integration per DynamoDB table targeting the
 * existing S3 Tables bucket. Each integration replicates the full table
 * and keeps it in sync incrementally via DynamoDB point-in-time exports.
 *
 * Prerequisites managed here:
 *  - Target IAM role (glue.amazonaws.com assumes it, writes to S3 Tables)
 *  - Glue Data Catalog resource policy (AuthorizeInboundIntegration on the
 *    s3tablescatalog federated catalog)
 *  - One CfnIntegration per DynamoDB table
 *
 * DynamoDB source prerequisites managed in DynamoDBStack:
 *  - TableEncryption.DEFAULT (AWS managed key is not supported for Zero-ETL)
 *  - Resource-based policy granting glue.amazonaws.com DescribeTable /
 *    ExportTableToPointInTime / DescribeExport on each table
 */
export class ZeroEtlStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ZeroEtlStackProps) {
    super(scope, id, props);

    // -------------------------------------------------------------------------
    // Target IAM role — assumed by Glue to write to the S3 Tables bucket
    // Permissions follow the AWS docs:
    //   https://docs.aws.amazon.com/glue/latest/dg/zero-etl-target.html
    // -------------------------------------------------------------------------
    const targetRole = new iam.Role(this, 'ZeroEtlTargetRole', {
      roleName: `${props.projectName}-zeroetl-target-role`,
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      description: 'Assumed by Glue Zero-ETL integrations to write to S3 Tables',
    });

    targetRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        's3tables:ListTableBuckets',
        's3tables:GetTableBucket',
        's3tables:GetTableBucketEncryption',
        's3tables:GetNamespace',
        's3tables:CreateNamespace',
        's3tables:ListNamespaces',
        's3tables:CreateTable',
        's3tables:GetTable',
        's3tables:GetTableEncryption',
        's3tables:ListTables',
        's3tables:GetTableMetadataLocation',
        's3tables:UpdateTableMetadataLocation',
        's3tables:GetTableData',
        's3tables:PutTableData',
      ],
      resources: [props.tableBucketArn, `${props.tableBucketArn}/*`],
    }));

    targetRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'cloudwatch:namespace': 'AWS/Glue/ZeroETL' },
      },
    }));

    targetRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
      ],
      resources: [
        `arn:aws:logs:${this.region}:${this.account}:log-group:/aws-glue/jobs/*`,
        `arn:aws:logs:${this.region}:${this.account}:log-group:/aws-glue/jobs/*:log-stream:*`,
      ],
    }));

    // -------------------------------------------------------------------------
    // Glue Data Catalog resource policy
    //
    // CfnResourcePolicy is not available in this CDK version — call the Glue
    // PutResourcePolicy API directly via AwsCustomResource (same pattern used
    // by DataLakeStack for CreateCatalog).
    //
    // Note: PutResourcePolicy is a full-replace operation. Existing manual
    // statements (e.g. for the zero_etl Glue database) are re-declared here
    // so they are preserved on every deploy.
    // -------------------------------------------------------------------------
    const catalogPolicy = {
      Version: '2012-10-17',
      Statement: [
        // --- S3 Tables catalog: allow account root to create integrations ---
        {
          Sid: 'AllowCreateInboundIntegrationS3Tables',
          Effect: 'Allow',
          Principal: { AWS: `arn:aws:iam::${this.account}:root` },
          Action: 'glue:CreateInboundIntegration',
          Resource: [
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
          ],
          Condition: {
            StringLike: {
              'aws:SourceArn': `arn:aws:dynamodb:${this.region}:${this.account}:table/*`,
            },
          },
        },
        // --- S3 Tables catalog: allow Glue service to authorize integrations ---
        {
          Sid: 'AllowAuthorizeInboundIntegrationS3Tables',
          Effect: 'Allow',
          Principal: { Service: 'glue.amazonaws.com' },
          Action: 'glue:AuthorizeInboundIntegration',
          Resource: [
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/*`,
          ],
          Condition: {
            StringLike: {
              'aws:SourceArn': `arn:aws:dynamodb:${this.region}:${this.account}:table/*`,
            },
          },
        },
        // --- zero_etl Glue database: preserve existing manual integration ---
        {
          Sid: 'AllowCreateInboundIntegrationZeroEtlDb',
          Effect: 'Allow',
          Principal: { AWS: `arn:aws:iam::${this.account}:root` },
          Action: 'glue:CreateInboundIntegration',
          Resource: [
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/zero_etl`,
          ],
        },
        {
          Sid: 'AllowAuthorizeInboundIntegrationZeroEtlDb',
          Effect: 'Allow',
          Principal: { Service: 'glue.amazonaws.com' },
          Action: 'glue:AuthorizeInboundIntegration',
          Resource: [
            `arn:aws:glue:${this.region}:${this.account}:catalog`,
            `arn:aws:glue:${this.region}:${this.account}:database/zero_etl`,
          ],
        },
      ],
    };

    // The Glue ZeroETL integration target ARN must be the BUCKET-level federated catalog ARN.
    // A namespace-level ARN is rejected with "Provided target id is not valid" (confirmed via
    // CloudTrail). Zero-ETL always auto-creates one UUID namespace per integration — this
    // cannot be redirected to an existing namespace via targetArn.
    // Format: arn:aws:glue:{region}:{account}:catalog/s3tablescatalog/{bucket-name}
    const glueCatalogTargetArn = `arn:aws:glue:${this.region}:${this.account}:catalog/s3tablescatalog/${props.tableBucketName}`;

    const catalogPolicyResource = new cr.AwsCustomResource(this, 'CatalogResourcePolicy', {
      onCreate: {
        service: 'Glue',
        action: 'putResourcePolicy',
        parameters: {
          PolicyInJson: JSON.stringify(catalogPolicy),
          EnableHybrid: 'TRUE',
        },
        physicalResourceId: cr.PhysicalResourceId.of('glue-catalog-resource-policy'),
      },
      onUpdate: {
        service: 'Glue',
        action: 'putResourcePolicy',
        parameters: {
          PolicyInJson: JSON.stringify(catalogPolicy),
          EnableHybrid: 'TRUE',
        },
        physicalResourceId: cr.PhysicalResourceId.of('glue-catalog-resource-policy'),
      },
      onDelete: {
        service: 'Glue',
        action: 'deleteResourcePolicy',
        parameters: {},
        ignoreErrorCodesMatching: 'EntityNotFoundException',
      },
      // Include ALL Glue actions needed by this stack's custom resources so the
      // shared singleton Lambda has the right permissions regardless of ordering.
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: [
            'glue:PutResourcePolicy',
            'glue:DeleteResourcePolicy',
            'glue:CreateIntegrationResourceProperty',
            'glue:UpdateIntegrationResourceProperty',
          ],
          resources: ['*'],
        }),
        // CreateIntegrationResourceProperty internally calls iam:PassRole to
        // register the target role with the Glue catalog target resource.
        new iam.PolicyStatement({
          actions: ['iam:PassRole'],
          resources: [targetRole.roleArn],
        }),
      ]),
    });

    // -------------------------------------------------------------------------
    // Register the target IAM role with the Glue catalog target resource.
    //
    // For DynamoDB → S3 Tables ZeroETL, the role ARN is NOT part of the
    // CfnIntegration resource — it must be set separately via
    // CreateIntegrationResourceProperty on the target catalog ARN.
    // Without this, integrations fail with UNRECOVERABLE_ACCESS_DENIED.
    // -------------------------------------------------------------------------
    const integrationResourceProperty = new cr.AwsCustomResource(this, 'IntegrationResourceProperty', {
      // Register the target IAM role with the Glue catalog so integrations can
      // write to the S3 Tables bucket. onCreate uses create; onUpdate patches it.
      // No onDelete — DeleteIntegrationResourceProperty is not available in the
      // bundled Lambda SDK and the resource is harmless to leave on stack delete.
      onCreate: {
        service: 'Glue',
        action: 'createIntegrationResourceProperty',
        parameters: {
          ResourceArn: glueCatalogTargetArn,
          TargetProcessingProperties: { RoleArn: targetRole.roleArn },
        },
        physicalResourceId: cr.PhysicalResourceId.of(`glue-integration-resource-property-${props.tableBucketName}`),
        ignoreErrorCodesMatching: 'AlreadyExistsException',
      },
      onUpdate: {
        service: 'Glue',
        action: 'updateIntegrationResourceProperty',
        parameters: {
          ResourceArn: glueCatalogTargetArn,
          TargetProcessingProperties: { RoleArn: targetRole.roleArn },
        },
        physicalResourceId: cr.PhysicalResourceId.of(`glue-integration-resource-property-${props.tableBucketName}`),
      },
      // Reuse the singleton Lambda that was created for CatalogResourcePolicy —
      // its role already has glue:CreateIntegrationResourceProperty (added above).
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: [
            'glue:CreateIntegrationResourceProperty',
            'glue:UpdateIntegrationResourceProperty',
          ],
          resources: ['*'],
        }),
      ]),
    });
    integrationResourceProperty.node.addDependency(targetRole);

    // -------------------------------------------------------------------------
    // Propagation wait: create a lightweight SSM parameter that depends on the
    // IntegrationResourceProperty custom resource. CloudFormation waits for this
    // resource before creating any integration, giving IAM role registration time
    // to propagate and avoiding transient UNRECOVERABLE_ACCESS_DENIED errors.
    // -------------------------------------------------------------------------
    const propagationWait = new cr.AwsCustomResource(this, 'PropagationWait', {
      onCreate: {
        service: 'SSM',
        action: 'putParameter',
        parameters: {
          Name: `/${props.projectName}/zeroetl/propagation-wait`,
          Value: new Date().toISOString(),
          Type: 'String',
          Overwrite: true,
        },
        physicalResourceId: cr.PhysicalResourceId.of('zeroetl-propagation-wait'),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
    });
    propagationWait.node.addDependency(integrationResourceProperty);

    // -------------------------------------------------------------------------
    // One Zero-ETL integration per DynamoDB table → S3 Tables bucket
    // -------------------------------------------------------------------------
    const tables: Array<{ suffix: string; tableArn: string }> = [
      { suffix: 'admin-codes',           tableArn: props.dynamodbStack.adminCodesTable.tableArn },
      { suffix: 'coverage-products',     tableArn: props.dynamodbStack.coverageProductsTable.tableArn },
      { suffix: 'coverages',             tableArn: props.dynamodbStack.coveragesTable.tableArn },
      { suffix: 'financial-activities',  tableArn: props.dynamodbStack.financialActivitiesTable.tableArn },
      { suffix: 'financial-statements',  tableArn: props.dynamodbStack.financialStatementsTable.tableArn },
      { suffix: 'holdings',              tableArn: props.dynamodbStack.holdingsTable.tableArn },
      { suffix: 'invest-products',       tableArn: props.dynamodbStack.investProductsTable.tableArn },
      { suffix: 'parties',               tableArn: props.dynamodbStack.partiesTable.tableArn },
      { suffix: 'policy-products',       tableArn: props.dynamodbStack.policyProductsTable.tableArn },
      { suffix: 'relations',             tableArn: props.dynamodbStack.relationsTable.tableArn },
      { suffix: 'riders',                tableArn: props.dynamodbStack.ridersTable.tableArn },
      { suffix: 'type-codes',            tableArn: props.dynamodbStack.typeCodesTable.tableArn },
    ];

    // Create integrations in batches of 4, each batch depending on the last
    // integration of the previous batch. This avoids hitting AWS service rate
    // limits from 12 concurrent creates (which cause "Re-try exhausted").
    const BATCH_SIZE = 4;
    let prevBatchLastIntegration: glue.CfnIntegration | undefined;

    for (let i = 0; i < tables.length; i++) {
      const { suffix, tableArn } = tables[i];
      const logicalId = suffix
        .split('-')
        .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
        .join('');

      const integration = new glue.CfnIntegration(this, `${logicalId}Integration`, {
        integrationName: `${props.projectName}-zeroetl-${suffix}`,
        description: `Zero-ETL replication from DynamoDB ${props.projectName}-${suffix} to S3 Tables`,
        sourceArn: tableArn,
        // Must use the Glue federated catalog ARN, not the S3 Tables bucket ARN directly.
        targetArn: glueCatalogTargetArn,
        // dataFilter is NOT supported for DynamoDB sources (ValidationException).
        // DynamoDB Zero-ETL replicates the full table automatically.
      });

      // Prerequisites: catalog policy + propagation wait (which depends on targetRole
      // and integrationResourceProperty).
      integration.node.addDependency(catalogPolicyResource);
      integration.node.addDependency(propagationWait);

      // Each batch depends on the last integration of the previous batch so at
      // most BATCH_SIZE integrations are created concurrently.
      if (i >= BATCH_SIZE && prevBatchLastIntegration) {
        integration.node.addDependency(prevBatchLastIntegration);
      }
      if ((i + 1) % BATCH_SIZE === 0 || i === tables.length - 1) {
        prevBatchLastIntegration = integration;
      }
    }

    // Outputs
    new cdk.CfnOutput(this, 'TargetRoleArn', {
      value: targetRole.roleArn,
      description: 'IAM role ARN for Glue Zero-ETL target (S3 Tables)',
      exportName: `${props.projectName}-zeroetl-target-role-arn`,
    });

    new cdk.CfnOutput(this, 'IntegrationCount', {
      value: String(tables.length),
      description: 'Number of Zero-ETL integrations created',
    });
  }
}
