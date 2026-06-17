import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface BedrockKbIngestionTriggerProps {
  knowledgeBaseId: string;
  dataSourceId: string;
  autoStart?: boolean;
}

/**
 * Custom resource to trigger Bedrock Knowledge Base ingestion
 */
export class BedrockKbIngestionTrigger extends Construct {
  public readonly ingestionJobId: string;

  constructor(scope: Construct, id: string, props: BedrockKbIngestionTriggerProps) {
    super(scope, id);

    const ingestionPolicy = new iam.PolicyStatement({
      actions: [
        'bedrock:StartIngestionJob',
        'bedrock:GetIngestionJob',
        'bedrock:ListIngestionJobs',
      ],
      resources: ['*'],
    });

    const ingestionTrigger = new cr.AwsCustomResource(this, 'IngestionTrigger', {
      onCreate: props.autoStart !== false ? {
        service: 'BedrockAgent',
        action: 'startIngestionJob',
        parameters: {
          knowledgeBaseId: props.knowledgeBaseId,
          dataSourceId: props.dataSourceId,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse('ingestionJob.ingestionJobId'),
      } : undefined,
      policy: cr.AwsCustomResourcePolicy.fromStatements([ingestionPolicy]),
      logRetention: logs.RetentionDays.ONE_DAY,
    });

    this.ingestionJobId = ingestionTrigger.getResponseField('ingestionJob.ingestionJobId');

    new cdk.CfnOutput(this, 'IngestionJobId', {
      value: this.ingestionJobId,
      description: 'Knowledge Base ingestion job ID',
    });
  }
}
