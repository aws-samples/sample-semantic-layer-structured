import * as cdk from 'aws-cdk-lib';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3_assets from 'aws-cdk-lib/aws-s3-assets';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as logs from 'aws-cdk-lib/aws-logs';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';

export interface ArmBuildConstructProps {
  /**
   * Path to the directory containing Dockerfile and source code
   */
  readonly sourcePath: string;

  /**
   * AWS region for ECR repository
   */
  readonly region: string;

  /**
   * Optional: Name prefix for resources
   */
  readonly namePrefix?: string;

  /**
   * Optional: Build timeout in minutes (default: 30)
   */
  readonly buildTimeoutMinutes?: number;

  /**
   * Optional: Dockerfile name (default: Dockerfile)
   */
  readonly dockerfileName?: string;
}

/**
 * Construct that builds ARM64 Docker images using a dedicated CodeBuild project
 * with native ARM64 compute, avoiding cross-compilation issues.
 */
export class ArmBuildConstruct extends Construct {
  /**
   * The ECR repository where the image is pushed
   */
  public readonly repository: ecr.Repository;

  /**
   * The image tag (based on source hash for cache invalidation)
   */
  public readonly imageTag: string;

  /**
   * Full ECR image URI (repository:tag)
   */
  public readonly imageUri: string;

  /**
   * Custom resource that triggers and waits for the build
   * Lambda functions should depend on this to ensure image exists
   */
  public readonly buildCompletion: cdk.CustomResource;

  constructor(scope: Construct, id: string, props: ArmBuildConstructProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);
    const namePrefix = props.namePrefix || id.toLowerCase();

    // Resolve source path to absolute path
    const absoluteSourcePath = path.resolve(props.sourcePath); // nosemgrep: path-join-resolve-traversal - CDK synth-time path, not runtime user input

    // Calculate source hash for cache invalidation
    this.imageTag = this.calculateSourceHash(absoluteSourcePath);

    // Create ECR repository
    this.repository = new ecr.Repository(this, 'Repository', {
      repositoryName: `${namePrefix}-repository`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        {
          maxImageCount: 5,
          description: 'Keep only last 5 images',
        },
      ],
    });

    this.imageUri = `${this.repository.repositoryUri}:${this.imageTag}`;

    // Create source asset (zips the source directory and uploads to S3)
    const sourceAsset = new s3_assets.Asset(this, 'SourceAsset', {
      path: absoluteSourcePath,
    });

    // Create CodeBuild project with ARM64 environment
    const buildProject = new codebuild.Project(this, 'ArmBuildProject', {
      projectName: `${namePrefix}-arm-build`,
      description: `ARM64 Docker build for ${namePrefix}`,
      environment: {
        privileged: true, // Required for Docker builds
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
      },
      source: codebuild.Source.s3({
        bucket: sourceAsset.bucket,
        path: sourceAsset.s3ObjectKey,
      }),
      timeout: cdk.Duration.minutes(props.buildTimeoutMinutes || 30),
      environmentVariables: {
        ECR_REPO_URI: { value: this.repository.repositoryUri },
        IMAGE_TAG: { value: this.imageTag },
        AWS_ACCOUNT_ID: { value: stack.account },
        AWS_REGION: { value: props.region || stack.region },
        DOCKERFILE_NAME: { value: props.dockerfileName || 'Dockerfile' },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: 0.2,
        phases: {
          pre_build: {
            commands: [
              "echo 'Logging in to Amazon ECR Public for base images...'",
              'aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws',
              "echo 'Logging in to Amazon ECR Private...'",
              'aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com',
              "echo 'Checking if image already exists...'",
              "REPO_NAME=$(echo $ECR_REPO_URI | cut -d'/' -f2) && if aws ecr describe-images --repository-name $REPO_NAME --image-ids imageTag=$IMAGE_TAG --region $AWS_REGION 2>/dev/null; then echo 'Image already exists, skipping build'; export SKIP_BUILD=true; else export SKIP_BUILD=false; fi",
            ],
          },
          build: {
            commands: [
              'if [ "$SKIP_BUILD" = "false" ]; then echo \'Building Docker image...\'; docker build -f $DOCKERFILE_NAME -t $ECR_REPO_URI:$IMAGE_TAG -t $ECR_REPO_URI:latest .; else echo \'Skipping build - image exists\'; fi',
            ],
          },
          post_build: {
            commands: [
              // Push the unique-digest tag — that's the one runtimes pin to.
              // The `:latest` push is best-effort: ECR repos here are
              // imageTagMutability=IMMUTABLE, so re-pushing `:latest` after
              // the first successful build returns "tag invalid" and exits 1.
              // Failing the whole build for a convenience tag would leave
              // the runtime pinned to the previous digest. `|| true` lets
              // the unique-tag push (the one that matters) be the source
              // of truth for build success.
              'if [ "$SKIP_BUILD" = "false" ]; then echo \'Pushing Docker image to ECR...\'; docker push $ECR_REPO_URI:$IMAGE_TAG && (docker push $ECR_REPO_URI:latest || echo \'WARN: :latest tag is immutable, skipping\'); fi',
              'echo "Build completed on $(date)"',
              'echo "Image URI: $ECR_REPO_URI:$IMAGE_TAG"',
            ],
          },
        },
      }),
    });

    // Suppress CDK Nag rule for KMS encryption - build artifacts are transient
    NagSuppressions.addResourceSuppressions(
      buildProject,
      [
        {
          id: 'AwsSolutions-CB4',
          reason: 'KMS encryption not required for transient Docker build artifacts',
        },
      ],
      true
    );

    // Grant CodeBuild permissions
    this.repository.grantPullPush(buildProject.role!);
    sourceAsset.grantRead(buildProject.role!);

    buildProject.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ecr:DescribeImages', 'ecr:BatchGetImage'],
        resources: [this.repository.repositoryArn],
      })
    );

    buildProject.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ecr-public:GetAuthorizationToken', 'sts:GetServiceBearerToken'],
        resources: ['*'],
      })
    );

    // Create Lambda function that starts the build (onEvent handler)
    const onEventFn = new lambda.Function(this, 'OnEventFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(1),
      // nosemgrep: missing-template-string-indicator - {event} below is valid Python f-string inside backtick, not a JS template literal
      code: lambda.Code.fromInline(`
import boto3

codebuild = boto3.client('codebuild')

def handler(event, context):
    print(f"Event: {event}")

    request_type = event['RequestType']
    props = event['ResourceProperties']
    project_name = props['ProjectName']
    image_tag = props['ImageTag']

    if request_type == 'Delete':
        return {'PhysicalResourceId': event.get('PhysicalResourceId', f"{project_name}-{image_tag}")}

    print(f"Starting build for project: {project_name}")
    response = codebuild.start_build(projectName=project_name)
    build_id = response['build']['id']
    print(f"Build started with ID: {build_id}")

    return {
        'PhysicalResourceId': f"{project_name}-{image_tag}",
        'Data': {'BuildId': build_id}
    }
`),
      logRetention: logs.RetentionDays.ONE_DAY,
    });

    // Create Lambda function that checks if build is complete (isComplete handler)
    const isCompleteFn = new lambda.Function(this, 'IsCompleteFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: cdk.Duration.minutes(1),
      // nosemgrep: missing-template-string-indicator — literal/template string is intentional; no untrusted interpolation
      code: lambda.Code.fromInline(`
import boto3

codebuild = boto3.client('codebuild')

def handler(event, context):
    print(f"Event: {event}")

    request_type = event['RequestType']

    if request_type == 'Delete':
        return {'IsComplete': True}

    build_id = event.get('Data', {}).get('BuildId')
    if not build_id:
        raise Exception("BuildId not found in event data")

    build_response = codebuild.batch_get_builds(ids=[build_id])
    builds = build_response.get('builds', [])

    if not builds:
        raise Exception(f"Build {build_id} not found")

    build = builds[0]
    status = build['buildStatus']
    print(f"Build status: {status}")

    if status == 'SUCCEEDED':
        print("Build completed successfully!")
        return {
            'IsComplete': True,
            'Data': {'BuildId': build_id, 'BuildStatus': status}
        }
    elif status in ['FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT']:
        error_msg = f"Build failed with status: {status}"
        logs_info = build.get('logs', {})
        if logs_info.get('deepLink'):
            error_msg += f" - Logs: {logs_info['deepLink']}"
        raise Exception(error_msg)

    print("Build still in progress...")
    return {'IsComplete': False}
`),
      logRetention: logs.RetentionDays.ONE_DAY,
    });

    // Grant permissions
    onEventFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['codebuild:StartBuild'],
        resources: [buildProject.projectArn],
      })
    );

    isCompleteFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['codebuild:BatchGetBuilds'],
        resources: [buildProject.projectArn],
      })
    );

    // Create provider with async polling
    const provider = new cr.Provider(this, 'BuildProvider', {
      onEventHandler: onEventFn,
      isCompleteHandler: isCompleteFn,
      queryInterval: cdk.Duration.seconds(30),
      totalTimeout: cdk.Duration.minutes(props.buildTimeoutMinutes || 30),
      logRetention: logs.RetentionDays.ONE_DAY,
    });

    NagSuppressions.addResourceSuppressions(
      provider,
      [
        {
          id: 'AwsSolutions-SF1',
          reason: 'Step Function auto-generated by CDK Provider framework',
        },
        {
          id: 'AwsSolutions-SF2',
          reason: 'Step Function auto-generated by CDK Provider framework',
        },
      ],
      true
    );

    // Create custom resource
    this.buildCompletion = new cdk.CustomResource(this, 'TriggerAndWaitBuild', {
      serviceToken: provider.serviceToken,
      properties: {
        ProjectName: buildProject.projectName,
        ImageTag: this.imageTag,
        SourceHash: this.imageTag,
      },
    });

    this.buildCompletion.node.addDependency(buildProject);

    new cdk.CfnOutput(this, 'ImageUri', {
      value: this.imageUri,
      description: 'ARM64 Docker image URI',
    });
  }

  private calculateSourceHash(sourcePath: string): string {
    const hash = crypto.createHash('sha256');

    const addFileToHash = (filePath: string) => {
      // nosemgrep: detect-non-literal-fs-filename — CDK build dir / static repo path, not user input
      if (fs.statSync(filePath).isDirectory()) {
        // nosemgrep: detect-non-literal-fs-filename - CDK synth-time file read by app developer
        const files = fs.readdirSync(filePath); // nosemgrep: detect-non-literal-fs-filename — CDK synth-time path set by app developer
        for (const file of files) {
          if (
            file === 'node_modules' ||
            file === '__pycache__' ||
            file === '.git' ||
            file === '.venv'
          ) {
            continue;
          }
          addFileToHash(path.join(filePath, file)); // nosemgrep: path-join-resolve-traversal - CDK synth-time path, not runtime user input
        }
      } else {
        const content = fs.readFileSync(filePath); // nosemgrep: detect-non-literal-fs-filename - CDK synth-time file read by app developer
        hash.update(content);
      }
    };

    addFileToHash(sourcePath);
    return hash.digest('hex').substring(0, 12);
  }
}
