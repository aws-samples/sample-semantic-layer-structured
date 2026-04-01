import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export interface NeptuneLoaderTriggerProps {
  neptuneEndpoint: string;
  neptunePort: number;
  s3Path: string;
  iamRoleArn: string;
  vpc: ec2.Vpc;
  securityGroup: ec2.SecurityGroup;
  format?: 'turtle' | 'rdfxml' | 'ntriples' | 'nquads';
}

/**
 * Custom resource to load RDF data into Neptune
 */
export class NeptuneLoaderTrigger extends Construct {
  constructor(scope: Construct, id: string, props: NeptuneLoaderTriggerProps) {
    super(scope, id);

    // Lambda function to trigger Neptune bulk load
    const loaderFunction = new lambda.Function(this, 'LoaderFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json
import urllib3
import os

http = urllib3.PoolManager()

def handler(event, context):
    request_type = event['RequestType']

    if request_type == 'Delete':
        return {'PhysicalResourceId': 'neptune-loader'}

    neptune_endpoint = os.environ['NEPTUNE_ENDPOINT']
    neptune_port = os.environ['NEPTUNE_PORT']
    s3_path = os.environ['S3_PATH']
    iam_role_arn = os.environ['IAM_ROLE_ARN']
    format_type = os.environ.get('FORMAT', 'turtle')

    loader_url = f'https://{neptune_endpoint}:{neptune_port}/loader'

    payload = {
        'source': s3_path,
        'format': format_type,
        'iamRoleArn': iam_role_arn,
        'region': os.environ['AWS_REGION'],
        'failOnError': 'FALSE',
        'parallelism': 'MEDIUM'
    }

    try:
        response = http.request(
            'POST',
            loader_url,
            body=json.dumps(payload),
            headers={'Content-Type': 'application/json'}
        )

        response_data = json.loads(response.data.decode('utf-8'))
        load_id = response_data.get('payload', {}).get('loadId', 'unknown')

        return {
            'PhysicalResourceId': f'neptune-load-{load_id}',
            'Data': {
                'LoadId': load_id,
                'Status': response_data.get('status', 'unknown')
            }
        }
    except Exception as e:
        print(f'Error loading data to Neptune: {str(e)}')
        return {
            'PhysicalResourceId': 'neptune-loader-error',
            'Data': {'Error': str(e)}
        }
      `),
      timeout: cdk.Duration.minutes(5),
      vpc: props.vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [props.securityGroup],
      environment: {
        NEPTUNE_ENDPOINT: props.neptuneEndpoint,
        NEPTUNE_PORT: props.neptunePort.toString(),
        S3_PATH: props.s3Path,
        IAM_ROLE_ARN: props.iamRoleArn,
        FORMAT: props.format || 'turtle',
      },
    });

    // Grant Neptune access
    loaderFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['neptune-db:*'],
        resources: ['*'],
      })
    );

    // Custom resource provider
    const provider = new cr.Provider(this, 'LoaderProvider', {
      onEventHandler: loaderFunction,
    });

    new cdk.CustomResource(this, 'LoaderResource', {
      serviceToken: provider.serviceToken,
    });
  }
}
