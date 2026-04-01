/**
 * CloudFront Integration Module for Serverless Backend
 *
 * This module provides CloudFront integration constructs for routing traffic
 * to serverless origins (API Gateway and AgentCore Runtime).
 */

import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as custom_resources from "aws-cdk-lib/custom-resources";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";
import { CfnOutput, CustomResource, Duration, Fn } from "aws-cdk-lib";

export interface CloudFrontIntegrationProps {
  /**
   * The CloudFront distribution to update
   */
  distribution: cloudfront.Distribution;

  /**
   * The API Gateway endpoint URL for REST API
   */
  apiGatewayEndpoint: string;

  /**
   * Optional resource prefix
   */
  resourcePrefix: string;

  /**
   * Custom header secret for API Gateway origin verification
   */
  cloudFrontHeaderSecret: string;
}

/**
 * CloudFront integration construct to update a CloudFront distribution
 * with serverless origins for API Gateway (REST API)
 *
 * Note: WebSocket connections now use pre-signed URLs directly to AgentCore Runtime,
 * so no CloudFront /ws origin is needed.
 */
export class CloudFrontIntegration extends Construct {
  constructor(scope: Construct, id: string, props: CloudFrontIntegrationProps) {
    super(scope, id);

    const {
      distribution,
      apiGatewayEndpoint,
      resourcePrefix,
      cloudFrontHeaderSecret,
    } = props;

    // Create a provider to update the CloudFront distribution
    const updateCloudFrontFunction = new lambda.Function(
      this,
      "UpdateCloudFrontFunction",
      {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "index.handler",
        timeout: Duration.minutes(5),
        // nosemgrep: missing-template-string-indicator - {variable} below is valid Python f-string inside backtick, not a JS template literal
        code: lambda.Code.fromInline(`
import boto3
import cfnresponse
import time
import logging
import uuid

def handler(event, context):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    try:
        logger.info(f"Event: {event}")
        request_type = event['RequestType']
        
        if request_type == 'Delete':
            # No need to revert changes on delete as this would disrupt service
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        
        properties = event['ResourceProperties']
        distribution_id = properties['DistributionId']
        api_gateway_endpoint = properties['ApiGatewayEndpoint']
        cloudfront_header_secret = properties['CloudFrontHeaderSecret']

        if not api_gateway_endpoint or api_gateway_endpoint == 'undefined':
            logger.warning("API Gateway endpoint is not available yet. Skipping CloudFront update.")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Status": "API Gateway not ready, update skipped"})
            return

        # Extract domain name from full URL
        # API Gateway: Remove protocol prefixes and get just the domain (before any path)
        api_gateway_domain = api_gateway_endpoint.replace('https://', '').replace('http://', '').split('/')[0]

        logger.info(f"Updating CloudFront distribution {distribution_id}")
        logger.info(f"API Gateway domain: {api_gateway_domain}")
        
        cf_client = boto3.client('cloudfront')
        
        # Get current config
        response = cf_client.get_distribution_config(Id=distribution_id)
        etag = response['ETag']
        distribution_config = response['DistributionConfig']
        
        logger.info(f"Retrieved distribution config: {distribution_config.keys()}")
        
        # Check if Origins structure exists and initialize if needed
        if 'Origins' not in distribution_config:
            distribution_config['Origins'] = {
                'Quantity': 0,
                'Items': []
            }
        
        origins_config = distribution_config['Origins']

        # -------------------- Add DefaultRootObject Logic --------------------
        # Ensure DefaultRootObject is set
        if 'DefaultRootObject' not in distribution_config or not distribution_config['DefaultRootObject']:
            logger.info("Setting DefaultRootObject to index.html")
            distribution_config['DefaultRootObject'] = 'index.html'
        # ----------------------------------------------------------------------

        
        # Ensure Origins has the correct structure
        if 'Items' not in origins_config:
            origins_config['Items'] = []
        if 'Quantity' not in origins_config:
            origins_config['Quantity'] = len(origins_config['Items'])
        
        # Define origin ID
        api_gateway_origin_id = 'ApiGatewayOrigin'

        # Check if API Gateway origin already exists
        origins = origins_config['Items']
        api_gateway_origin_exists = any(
            origin.get('Id') == api_gateway_origin_id or origin.get('DomainName') == api_gateway_domain
            for origin in origins
        )

        # Add or update API Gateway origin
        # API Gateway is configured as a custom HTTPS origin
        api_gateway_origin = {
            'Id': api_gateway_origin_id,
            'DomainName': api_gateway_domain,
            'OriginPath': '',
            'CustomOriginConfig': {
                'HTTPPort': 80,
                'HTTPSPort': 443,
                'OriginProtocolPolicy': 'https-only',
                'OriginSslProtocols': {
                    'Quantity': 1,
                    'Items': ['TLSv1.2']
                },
                'OriginReadTimeout': 60,
                'OriginKeepaliveTimeout': 5
            },
            'ConnectionAttempts': 3,
            'ConnectionTimeout': 10,
            'CustomHeaders': {
                'Quantity': 1,
                'Items': [
                    {
                        'HeaderName': 'x-origin-verify',
                        'HeaderValue': cloudfront_header_secret
                    }
                ]
            }
        }

        if not api_gateway_origin_exists:
            # Add new origin
            origins_config['Items'].append(api_gateway_origin)
            origins_config['Quantity'] = len(origins_config['Items'])
            logger.info(f"Added API Gateway origin: {api_gateway_domain}")
        else:
            # Update existing origin to ensure correct configuration
            for i, origin in enumerate(origins_config['Items']):
                if origin.get('Id') == api_gateway_origin_id or origin.get('DomainName') == api_gateway_domain:
                    origins_config['Items'][i] = api_gateway_origin
                    logger.info(f"Updated API Gateway origin: {api_gateway_domain}")
                    break
        
        # Handle cache behaviors
        if 'CacheBehaviors' not in distribution_config:
            distribution_config['CacheBehaviors'] = {
                'Quantity': 0,
                'Items': []
            }
        
        cache_behaviors_config = distribution_config['CacheBehaviors']
        if 'Items' not in cache_behaviors_config:
            cache_behaviors_config['Items'] = []
        if 'Quantity' not in cache_behaviors_config:
            cache_behaviors_config['Quantity'] = len(cache_behaviors_config['Items'])
        
        cache_behaviors = cache_behaviors_config['Items']
        
        # Create cache behaviors for API and WebSocket paths
        # API Gateway behavior: Use AllViewerExceptHostHeader to avoid Host header conflicts
        api_behavior = {
            'PathPattern': 'api/*',
            'TargetOriginId': api_gateway_origin_id,
            'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {
                'Quantity': 7,
                'Items': ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'OPTIONS', 'DELETE'],
                'CachedMethods': {
                    'Quantity': 2,
                    'Items': ['GET', 'HEAD']
                }
            },
            'CachePolicyId': '4135ea2d-6df8-44a3-9df3-4b5a84be39ad',  # Managed-CachingDisabled
            'OriginRequestPolicyId': 'b689b0a8-53d0-40ab-baf2-68738e2966ac',  # Managed-AllViewerExceptHostHeader
            'ResponseHeadersPolicyId': '60669652-455b-4ae9-85a4-c4c02393f86c',  # Managed-CORS-with-preflight-and-Security
            'SmoothStreaming': False,
            'Compress': True,
            'FieldLevelEncryptionId': '',
            'LambdaFunctionAssociations': {
                'Quantity': 0,
                'Items': []
            },
            'FunctionAssociations': {
                'Quantity': 0,
                'Items': []
            },
            'TrustedSigners': {
                'Enabled': False,
                'Quantity': 0,
                'Items': []
            },
            'TrustedKeyGroups': {
                'Enabled': False,
                'Quantity': 0,
                'Items': []
            }
        }
        
        health_behavior = {
            'PathPattern': 'health',
            'TargetOriginId': api_gateway_origin_id,
            'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {
                'Quantity': 2,
                'Items': ['GET', 'HEAD'],
                'CachedMethods': {
                    'Quantity': 2,
                    'Items': ['GET', 'HEAD']
                }
            },
            'CachePolicyId': '4135ea2d-6df8-44a3-9df3-4b5a84be39ad',  # Managed-CachingDisabled
            'OriginRequestPolicyId': '216adef6-5c7f-47e4-b989-5492eafa07d3',  # Managed-AllViewer
            'ResponseHeadersPolicyId': '60669652-455b-4ae9-85a4-c4c02393f86c',  # Managed-CORS-with-preflight-and-Security
            'SmoothStreaming': False,
            'Compress': True,
            'FieldLevelEncryptionId': '',
            'LambdaFunctionAssociations': {
                'Quantity': 0,
                'Items': []
            },
            'FunctionAssociations': {
                'Quantity': 0,
                'Items': []
            },
            'TrustedSigners': {
                'Enabled': False,
                'Quantity': 0,
                'Items': []
            },
            'TrustedKeyGroups': {
                'Enabled': False,
                'Quantity': 0,
                'Items': []
            }
        }
        
        # Check if cache behaviors already exist for these paths
        api_exists = any(behavior.get('PathPattern') == 'api/*' for behavior in cache_behaviors)
        health_exists = any(behavior.get('PathPattern') == 'health' for behavior in cache_behaviors)

        new_behaviors = []
        if not api_exists:
            new_behaviors.append(api_behavior)
        if not health_exists:
            new_behaviors.append(health_behavior)
        
        # Add new behaviors
        if new_behaviors:
            cache_behaviors_config['Items'].extend(new_behaviors)
            cache_behaviors_config['Quantity'] = len(cache_behaviors_config['Items'])
        
        # Keep custom error responses for S3 origin
        # API Gateway and AgentCore Runtime use explicit cache behaviors and won't match custom error responses
        logger.info(f"CustomErrorResponses preserved for S3 static asset handling")
        
        # Update the distribution
        logger.info("Updating CloudFront distribution configuration")
        response = cf_client.update_distribution(
            Id=distribution_id,
            IfMatch=etag,
            DistributionConfig=distribution_config
        )
        
        logger.info("CloudFront distribution update initiated")
        
        # Wait for distribution to deploy (but don't block the custom resource response)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'Status': 'CloudFront distribution update initiated',
            'DistributionId': distribution_id,
            'ApiGatewayOriginId': api_gateway_origin_id
        })
        
    except Exception as e:
        logger.error(f"Error updating CloudFront distribution: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
    `),
        environment: {
          PYTHONUNBUFFERED: "1",
        },
      },
    );

    // Create IAM policy for CloudFront actions
    updateCloudFrontFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "cloudfront:GetDistribution",
          "cloudfront:GetDistributionConfig",
          "cloudfront:UpdateDistribution",
        ],
        resources: ["*"],
      }),
    );

    // Create provider
    const provider = new custom_resources.Provider(
      this,
      "CloudFrontUpdateProvider",
      {
        onEventHandler: updateCloudFrontFunction,
      },
    );

    // Create custom resource with deployment-time trigger
    // Use Fn.join to ensure the custom resource runs whenever the API Gateway endpoint changes
    const updateTrigger = Fn.join("-", [
      apiGatewayEndpoint,
      new Date().toISOString(), // Synthesis-time timestamp to force update
    ]);

    const customResource = new CustomResource(
      this,
      "UpdateCloudFrontResource",
      {
        serviceToken: provider.serviceToken,
        properties: {
          DistributionId: distribution.distributionId,
          ApiGatewayEndpoint: apiGatewayEndpoint,
          CloudFrontHeaderSecret: cloudFrontHeaderSecret,
          // Use deployment-time trigger to ensure update when endpoint changes
          UpdateTrigger: updateTrigger,
        },
      },
    );

    // Ensure the custom resource is used (suppress unused warning)
    customResource.node.addDependency(provider);

    // Output the status
    new CfnOutput(this, "CloudFrontUpdateStatus", {
      value: `CloudFront distribution ${distribution.distributionId} updated with API Gateway origin`,
      description:
        "CloudFront configured with API Gateway REST API origin (WebSocket uses pre-signed URLs)",
    });
  }
}
