import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface GlueCrawlerTriggerProps {
  crawlerName: string;
  autoStart?: boolean;
}

/**
 * Custom resource to trigger Glue crawler on deployment
 */
export class GlueCrawlerTrigger extends Construct {
  constructor(scope: Construct, id: string, props: GlueCrawlerTriggerProps) {
    super(scope, id);

    const onEventPolicy = new iam.PolicyStatement({
      actions: [
        'glue:StartCrawler',
        'glue:GetCrawler',
        'glue:GetCrawlerMetrics',
      ],
      resources: ['*'],
    });

    const crawlerTrigger = new cr.AwsCustomResource(this, 'CrawlerTrigger', {
      onCreate: props.autoStart !== false ? {
        service: 'Glue',
        action: 'startCrawler',
        parameters: {
          Name: props.crawlerName,
        },
        physicalResourceId: cr.PhysicalResourceId.of(`${props.crawlerName}-trigger`),
      } : undefined,
      onUpdate: props.autoStart !== false ? {
        service: 'Glue',
        action: 'startCrawler',
        parameters: {
          Name: props.crawlerName,
        },
        physicalResourceId: cr.PhysicalResourceId.of(`${props.crawlerName}-trigger`),
      } : undefined,
      policy: cr.AwsCustomResourcePolicy.fromStatements([onEventPolicy]),
      logRetention: logs.RetentionDays.ONE_DAY,
    });

    new cdk.CfnOutput(this, 'CrawlerName', {
      value: props.crawlerName,
      description: `Glue crawler: ${props.crawlerName}`,
    });
  }
}
