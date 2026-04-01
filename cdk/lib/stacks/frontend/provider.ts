import {
  CloudFormationCustomResourceEvent,
  CloudFormationCustomResourceResponse,
  CloudFormationCustomResourceCreateEvent,
  CloudFormationCustomResourceUpdateEvent,
  CloudFormationCustomResourceDeleteEvent,
} from 'aws-lambda';
import { CodeBuildClient, StartBuildCommand, BatchGetBuildsCommand } from '@aws-sdk/client-codebuild';

const codebuild = new CodeBuildClient({});

interface ResourceProperties {
  projectName: string;
  assetHash: string;
}

interface OnEventResponse {
  PhysicalResourceId?: string;
  Data?: Record<string, any>;
}

interface IsCompleteResponse {
  IsComplete: boolean;
  Data?: Record<string, any>;
}

/**
 * Handles Create and Update events - starts the CodeBuild project
 */
export async function onEventHandler(
  event: CloudFormationCustomResourceEvent
): Promise<OnEventResponse> {
  console.log('Event:', JSON.stringify(event, null, 2));

  const requestType = event.RequestType;

  if (requestType === 'Delete') {
    // No action needed for Delete
    return {
      PhysicalResourceId: event.PhysicalResourceId || 'frontend-build',
    };
  }

  // Create or Update - start the build
  const props = event.ResourceProperties as unknown as ResourceProperties;
  const projectName = props.projectName;

  console.log(`Starting build for project: ${projectName}`);

  const command = new StartBuildCommand({
    projectName,
  });

  const response = await codebuild.send(command);
  const buildId = response.build?.id;

  if (!buildId) {
    throw new Error('Failed to start build - no build ID returned');
  }

  console.log(`Build started: ${buildId}`);

  return {
    PhysicalResourceId: buildId,
    Data: {
      BuildId: buildId,
    },
  };
}

/**
 * Checks if the CodeBuild project has completed
 * Called repeatedly by CloudFormation until IsComplete returns true
 */
export async function isCompleteHandler(
  event: CloudFormationCustomResourceEvent
): Promise<IsCompleteResponse> {
  console.log('IsComplete Event:', JSON.stringify(event, null, 2));

  const requestType = event.RequestType;

  if (requestType === 'Delete') {
    // Delete operations complete immediately
    return { IsComplete: true };
  }

  // PhysicalResourceId is populated by CloudFormation after onEventHandler returns
  // For both Create and Update events, it will contain the build ID
  const buildId = (event as CloudFormationCustomResourceUpdateEvent).PhysicalResourceId;

  if (!buildId) {
    throw new Error('No build ID found in PhysicalResourceId');
  }

  console.log(`Checking build status for: ${buildId}`);

  const command = new BatchGetBuildsCommand({
    ids: [buildId],
  });

  const response = await codebuild.send(command);
  const build = response.builds?.[0];

  if (!build) {
    throw new Error(`Build not found: ${buildId}`);
  }

  const buildStatus = build.buildStatus;
  console.log(`Build status: ${buildStatus}`);

  // Check if build is in a terminal state
  if (buildStatus === 'SUCCEEDED') {
    return {
      IsComplete: true,
      Data: {
        BuildId: buildId,
        BuildStatus: buildStatus,
      },
    };
  }

  if (['FAILED', 'FAULT', 'TIMED_OUT', 'STOPPED'].includes(buildStatus || '')) {
    const logs = build.logs;
    const logUrl = logs?.deepLink || 'No logs available';
    throw new Error(`Build failed with status: ${buildStatus}. Logs: ${logUrl}`);
  }

  // Build is still in progress
  return {
    IsComplete: false,
  };
}
