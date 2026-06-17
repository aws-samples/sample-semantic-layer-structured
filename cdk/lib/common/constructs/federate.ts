import {
  UserPool,
  UserPoolProps,
  UserPoolClient,
  UserPoolClientProps,
  UserPoolDomain,
} from "aws-cdk-lib/aws-cognito";
import { Construct } from "constructs";

/**
 * Extended UserPool with optional domain support
 */
export class FederateUserPool extends UserPool {
  public readonly userPoolDomain?: UserPoolDomain;

  constructor(scope: Construct, id: string, props: UserPoolProps) {
    super(scope, id, props);

    // Optionally create a domain if configured
    // This can be extended to support custom domains or Cognito-hosted UI
  }
}

/**
 * Extended UserPoolClient with OAuth configuration support
 */
export class FederateUserPoolClient extends UserPoolClient {
  constructor(scope: Construct, id: string, props: UserPoolClientProps) {
    super(scope, id, props);
  }
}
