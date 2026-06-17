import { CfnOutput, RemovalPolicy, Stack, StackProps, Duration } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as iam from 'aws-cdk-lib/aws-iam';
import { CfnWebACL } from 'aws-cdk-lib/aws-wafv2';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import {
  Distribution,
  ViewerProtocolPolicy,
  AllowedMethods,
  SecurityPolicyProtocol,
  SSLMethod,
  CachePolicy,
  CachedMethods,
} from 'aws-cdk-lib/aws-cloudfront';
import { S3BucketOrigin } from 'aws-cdk-lib/aws-cloudfront-origins';

export interface CloudFrontStorageStackProps extends StackProps {
  readonly projectName: string;
  readonly loggingBucket?: s3.IBucket;
}

/**
 * CloudFront and Storage Stack
 *
 * Creates CloudFront distribution and S3 buckets BEFORE AuthStack
 * so the CloudFront URL can be used in Cognito callback URLs.
 *
 * This follows the pattern from interview-assistant-serverless.
 */
export class CloudFrontStorageStack extends Stack {
  public readonly distribution: Distribution;
  public readonly websiteBucket: s3.Bucket;
  public readonly urls: string[];

  constructor(scope: Construct, id: string, props: CloudFrontStorageStackProps) {
    super(scope, id, props);

    // Create website bucket with proper security settings
    this.websiteBucket = new s3.Bucket(this, 'WebsiteBucket', {
      bucketName: `${props.projectName}-frontend-${this.account}-${this.region}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      versioned: false,
      serverAccessLogsBucket: props.loggingBucket,
      serverAccessLogsPrefix: 'website-bucket-access-logs/',
    });

    // Create CloudFront Origin Access Control for S3
    const oac = new cloudfront.S3OriginAccessControl(this, 'OAC', {
      signing: cloudfront.Signing.SIGV4_NO_OVERRIDE,
    });

    // Create custom cache policy for SPA
    const cachePolicy = new CachePolicy(this, 'SPACachePolicy', {
      cachePolicyName: `${props.projectName}-spa-cache-${this.region}`,
      comment: 'Cache policy for SPA with index.html bypass',
      defaultTtl: Duration.days(1),
      maxTtl: Duration.days(365),
      minTtl: Duration.seconds(0),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
      headerBehavior: cloudfront.CacheHeaderBehavior.none(),
      cookieBehavior: cloudfront.CacheCookieBehavior.none(),
      queryStringBehavior: cloudfront.CacheQueryStringBehavior.none(),
    });

    // WAF WebACL for CloudFront — CLOUDFRONT scope requires us-east-1 (this stack is already in us-east-1)
    const cloudfrontWaf = new CfnWebACL(this, 'CloudFrontWebAcl', {
      defaultAction: { allow: {} },
      scope: 'CLOUDFRONT',
      visibilityConfig: {
        metricName: 'cloudFrontWebAcl',
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
      },
      rules: [
        {
          // Rate limit: 2000 requests per 5-minute window per IP — blocks credential stuffing and scraping
          name: 'cfIpRateLimitingRule',
          priority: 0,
          statement: {
            rateBasedStatement: {
              limit: 2000,
              aggregateKeyType: 'IP',
            },
          },
          action: { block: {} },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'cfIpRateLimitingRule',
          },
        },
        {
          name: 'cfAWSCommonRuleSet',
          priority: 1,
          overrideAction: { none: {} }, // none = use rule's own BLOCK action
          statement: {
            managedRuleGroupStatement: {
              name: 'AWSManagedRulesCommonRuleSet',
              vendorName: 'AWS',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'cfAWSCommonRuleSet',
          },
        },
        {
          name: 'cfAWSKnownBadInputs',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
              vendorName: 'AWS',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'cfAWSKnownBadInputs',
          },
        },
      ],
    });

    // Create CloudFront distribution
    this.distribution = new Distribution(this, 'Distribution', {
      webAclId: cloudfrontWaf.attrArn,
      comment: `${props.projectName} Frontend Distribution`,
      defaultRootObject: 'index.html',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      minimumProtocolVersion: SecurityPolicyProtocol.TLS_V1_2_2021,
      sslSupportMethod: SSLMethod.SNI,
      enableLogging: props.loggingBucket !== undefined,
      logBucket: props.loggingBucket,
      logFilePrefix: 'cloudfront-logs/',
      logIncludesCookies: true,
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(this.websiteBucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        cachedMethods: CachedMethods.CACHE_GET_HEAD_OPTIONS,
        cachePolicy: cachePolicy,
        compress: true,
      },
      // SPA routing: serve index.html for 404s
      errorResponses: [
        {
          httpStatus: 404,
          responsePagePath: '/index.html',
          responseHttpStatus: 200,
          ttl: Duration.minutes(5),
        },
        {
          httpStatus: 403,
          responsePagePath: '/index.html',
          responseHttpStatus: 200,
          ttl: Duration.minutes(5),
        },
      ],
    });

    // Grant CloudFront OAC access to S3 bucket
    this.websiteBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${this.websiteBucket.bucketArn}/*`],
        principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${this.distribution.distributionId}`,
          },
        },
      })
    );

    // Apply cdk-nag suppressions for CloudFront distribution
    // CFR4: Using TLS 1.2 (2021) is acceptable; TLS 1.3 not available for all viewers globally
    NagSuppressions.addResourceSuppressions(this.distribution, [
      {
        id: 'AwsSolutions-CFR4',
        reason:
          'Using SecurityPolicyProtocol.TLS_V1_2_2021 which enforces minimum TLS 1.2, blocking SSLv3 and TLS 1.0/1.1; this is the current security policy standard that maintains compatibility with modern browsers',
      },
    ]);

    // Build URLs array immediately using distribution domain
    // This is available at synthesis time as a CloudFormation token
    this.urls = [
      `https://${this.distribution.distributionDomainName}`,
      'http://localhost:3000',
      'http://localhost:3001',
    ];

    // Outputs
    new CfnOutput(this, 'CloudFrontURL', {
      value: `https://${this.distribution.distributionDomainName}`,
      description: 'CloudFront Distribution URL',
      exportName: `${this.stackName}-CloudFrontURL`,
    });

    new CfnOutput(this, 'WebsiteBucketName', {
      value: this.websiteBucket.bucketName,
      description: 'S3 Bucket for Website Assets',
      exportName: `${this.stackName}-WebsiteBucket`,
    });

    new CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      description: 'CloudFront Distribution ID',
      exportName: `${this.stackName}-DistributionId`,
    });

    new CfnOutput(this, 'DistributionDomainName', {
      value: this.distribution.distributionDomainName,
      description: 'CloudFront Distribution Domain Name',
      exportName: `${this.stackName}-DistributionDomain`,
    });
  }
}
