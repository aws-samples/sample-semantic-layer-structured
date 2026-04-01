import { Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";

/**
 * CommonStack provides shared utilities and naming conventions for all stacks
 */
export class CommonStack extends Stack {
  public readonly resourcePrefix: string;
  public readonly environmentName: string;

  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // Extract resource prefix from stack ID (e.g., "semantic-layer-auth" -> "semantic-layer")
    const parts = id.split("-");
    if (parts.length > 1) {
      this.resourcePrefix = parts.slice(0, -1).join("-");
    } else {
      this.resourcePrefix = id;
    }

    // Use region or default to "dev"
    this.environmentName = props?.env?.region || "dev";
  }
}
