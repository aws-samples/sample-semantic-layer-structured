import {
  CfnOutput,
  RemovalPolicy,
  Stack,
  StackProps,
  Duration,
  CustomResource,
  SecretValue,
} from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3Assets from 'aws-cdk-lib/aws-s3-assets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { Provider } from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as path from 'path';
import {
  Distribution,
  ViewerProtocolPolicy,
  AllowedMethods,
  OriginRequestPolicy,
  SecurityPolicyProtocol,
  SSLMethod,
  CachePolicy,
  CachedMethods,
} from 'aws-cdk-lib/aws-cloudfront';
import { S3BucketOrigin } from 'aws-cdk-lib/aws-cloudfront-origins';
import { CloudFrontIntegration } from '../backend/cloudfront-integration';

export interface FrontendStackProps extends StackProps {
  readonly apiUrl: string;
  readonly userPoolId?: string;
  readonly userPoolClientId?: string;
  readonly userPoolDomain?: string;
  readonly projectName?: string;
  readonly apiGatewayEndpoint?: string;
  readonly cloudFrontHeaderSecret?: string;
  // Existing CloudFront distribution and S3 bucket from CloudFrontStorageStack
  readonly distribution: Distribution;
  readonly websiteBucket: s3.Bucket;
  readonly enableOntologyAgents?: boolean;
}

export class FrontendStack extends Stack {
  public readonly distribution: Distribution;
  public readonly websiteBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    // Use existing distribution and bucket from CloudFrontStorageStack
    this.distribution = props.distribution;
    this.websiteBucket = props.websiteBucket;

    // Build environment variables for React
    const buildEnvironment = {
      REACT_APP_API_URL: props.apiUrl,
      REACT_APP_USER_POOL_ID: props.userPoolId || '',
      REACT_APP_USER_POOL_CLIENT_ID: props.userPoolClientId || '',
      REACT_APP_USER_POOL_DOMAIN: props.userPoolDomain || '',
      REACT_APP_CUSTOMER_NAME: 'AWS Semantic Layer',
      REACT_APP_CUSTOMER_LOGO: '/amazicon.svg',
      REACT_APP_NO_AUTH: 'false',
      REACT_APP_AUTH_MODE: 'direct', // Show username/password fields directly
      REACT_APP_ENABLE_ONTOLOGY_AGENTS: String(props.enableOntologyAgents ?? true),
    };

    // Create S3 asset from frontend source code (creates a zip file)
    const frontendPath = path.join(__dirname, '../../../../frontend');
    const frontendAsset = new s3Assets.Asset(this, 'FrontendSourceAsset', {
      path: frontendPath,
      exclude: [
        'node_modules',
        'node_modules/**',
        'build',
        'build/**',
        'dist',
        'dist/**',
        '.git',
        '.git/**',
        '.DS_Store',
        '**/.DS_Store',
        'coverage',
        'coverage/**',
        '.env',
        '.env.*',
        '**/.env',
        '**/.env.*',
      ],
    });

    // Create CodeBuild project to build and deploy the React app
    const frontendBuildProject = new codebuild.Project(this, 'FrontendBuild', {
      projectName: `${this.stackName}-frontend-build`,
      description: 'Build and deploy React frontend to S3',
      source: codebuild.Source.s3({
        bucket: frontendAsset.bucket,
        path: frontendAsset.s3ObjectKey,
      }),
      // Use CodeBuild Artifacts to deploy directly to S3
      artifacts: codebuild.Artifacts.s3({
        bucket: this.websiteBucket,
        includeBuildId: false,
        packageZip: false,
        name: '/',
        encryption: false,
      }),
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.MEDIUM,
        environmentVariables: {
          DISTRIBUTION_ID: {
            value: this.distribution.distributionId,
          },
          ...Object.entries(buildEnvironment).reduce((acc, [key, value]) => {
            acc[key] = { value };
            return acc;
          }, {} as Record<string, codebuild.BuildEnvironmentVariable>),
        },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          install: {
            'runtime-versions': {
              nodejs: '20',
            },
            commands: [
              'echo "Installing dependencies..."',
              'echo "Node version: $(node --version)"',
              'echo "NPM version: $(npm --version)"',
              'npm install --legacy-peer-deps',
            ],
          },
          build: {
            commands: [
              'echo "Building React application..."',
              'CI=false npm run build',
            ],
          },
          post_build: {
            commands: [
              'echo "Invalidating CloudFront cache..."',
              'aws cloudfront create-invalidation --distribution-id $DISTRIBUTION_ID --paths "/*"',
            ],
          },
        },
        artifacts: {
          files: ['**/*'],
          'base-directory': 'build',
        },
      }),
      logging: {
        cloudWatch: {
          logGroup: new logs.LogGroup(this, 'FrontendBuildLogs', {
            logGroupName: `/aws/codebuild/${this.stackName}-frontend-build`,
            retention: logs.RetentionDays.ONE_WEEK,
            removalPolicy: RemovalPolicy.DESTROY,
          }),
        },
      },
      timeout: Duration.minutes(15),
    });

    // Grant CodeBuild permissions to write to S3 bucket and invalidate CloudFront
    this.websiteBucket.grantReadWrite(frontendBuildProject);
    frontendBuildProject.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudfront:CreateInvalidation'],
        resources: [
          `arn:aws:cloudfront::${this.account}:distribution/${this.distribution.distributionId}`,
        ],
      })
    );

    // Create Lambda functions for the Provider pattern
    const providerPath = path.join(__dirname, 'provider');

    // Handler that starts the CodeBuild project
    const onEventHandler = new NodejsFunction(this, 'OnEventHandler', {
      entry: `${providerPath}.ts`,
      handler: 'onEventHandler',
      timeout: Duration.seconds(60),
      // Bundle AWS SDK since it's not included in Node.js 20 runtime by default
      bundling: {
        minify: true,
      },
      initialPolicy: [
        new iam.PolicyStatement({
          actions: ['codebuild:StartBuild'],
          resources: [frontendBuildProject.projectArn],
        }),
      ],
    });

    // Handler that checks if the CodeBuild project completed
    const isCompleteHandler = new NodejsFunction(this, 'IsCompleteHandler', {
      entry: `${providerPath}.ts`,
      handler: 'isCompleteHandler',
      timeout: Duration.seconds(60),
      // Bundle AWS SDK since it's not included in Node.js 20 runtime by default
      bundling: {
        minify: true,
      },
      initialPolicy: [
        new iam.PolicyStatement({
          actions: ['codebuild:BatchGetBuilds'],
          resources: [frontendBuildProject.projectArn],
        }),
      ],
    });

    // Create the Provider that manages the Custom Resource lifecycle
    const provider = new Provider(this, 'BuildProvider', {
      onEventHandler,
      isCompleteHandler,
      queryInterval: Duration.seconds(15),
      totalTimeout: Duration.minutes(15),
    });

    // Create Custom Resource to trigger the build during deployment
    const buildTrigger = new CustomResource(this, 'BuildTrigger', {
      serviceToken: provider.serviceToken,
      properties: {
        projectName: frontendBuildProject.projectName,
        assetHash: frontendAsset.assetHash, // Force update when source changes
      },
    });

    // Grant CodeBuild permission to read the source asset
    frontendAsset.grantRead(frontendBuildProject);

    // Ensure build happens after distribution is created
    buildTrigger.node.addDependency(this.distribution);

    // Store CloudFront domain in Secrets Manager for Lambda API to read
    const cloudFrontDomainSecret = new secretsmanager.Secret(this, 'CloudFrontDomainSecret', {
      secretName: '/semantic-layer/cloudfront-domain',
      description: 'CloudFront distribution domain name for CORS configuration',
      secretStringValue: SecretValue.unsafePlainText(this.distribution.distributionDomainName),
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Outputs
    new CfnOutput(this, 'FrontendBuildProjectName', {
      value: frontendBuildProject.projectName!,
      description: 'CodeBuild project name for frontend deployment',
    });

    new CfnOutput(this, 'BuildEnvironmentVariables', {
      value: JSON.stringify(buildEnvironment, null, 2),
      description: 'Environment variables used for React build',
    });

    // CloudFront Integration - Connect API Gateway to CloudFront
    // This allows the frontend to call APIs through CloudFront (same domain, no CORS issues)
    if (props.apiGatewayEndpoint && props.cloudFrontHeaderSecret) {
      new CloudFrontIntegration(this, 'CloudFrontIntegration', {
        distribution: this.distribution,
        apiGatewayEndpoint: props.apiGatewayEndpoint,
        resourcePrefix: props.projectName || 'semantic-layer',
        cloudFrontHeaderSecret: props.cloudFrontHeaderSecret,
      });
    }

    // Apply cdk-nag suppressions for frontend build infrastructure
    // CB4: CodeBuild project encryption not required for build artifacts (ephemeral)
    // SF1/SF2: Step Function state machine created by Provider pattern for custom resource orchestration
    // SMG4: CloudFront domain secret does not require rotation (generated by CDK, not user-managed credentials)
    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-CB4',
        reason: 'Frontend CodeBuild project builds React artifacts which are temporary and stored in S3 with encryption enabled; KMS encryption not required for ephemeral build outputs',
      },
      {
        id: 'AwsSolutions-SF1',
        reason: 'Step Function state machine is auto-generated by CDK custom resource Provider for build orchestration; logging not required for internal CDK-managed resources',
      },
      {
        id: 'AwsSolutions-SF2',
        reason: 'Step Function state machine is auto-generated by CDK custom resource Provider for build orchestration; X-Ray tracing not required for internal CDK-managed resources',
      },
      {
        id: 'AwsSolutions-SMG4',
        reason: 'CloudFront domain secret contains the distribution domain name (derived from CloudFormation) and does not require rotation; this is computed infrastructure metadata, not user credentials',
      },
    ]);
  }
}
