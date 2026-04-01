import * as s3 from "aws-cdk-lib/aws-s3";
import { RemovalPolicy } from "aws-cdk-lib";
import { Construct } from "constructs";

/**
 * CommonBucket provides standard S3 bucket configuration with security best practices
 */
export class CommonBucket extends s3.Bucket {
  constructor(scope: Construct, id: string, props?: s3.BucketProps) {
    super(scope, id, {
      // Security defaults
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: false,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      // Merge with provided props (allows override)
      ...props,
    });
  }
}
