import { CfnWebACL } from "aws-cdk-lib/aws-wafv2";

/**
 * Managed rule configuration for AWS WAF
 */
export interface ManagedRuleConfig {
  name: string;
  overrideAction?: {
    count?: {};
    none?: {};
  };
  ruleActionOverrides?: Array<{
    name: string;
    actionToUse: {
      count?: {};
      allow?: {};
      block?: {};
    };
  }>;
}

/**
 * Create AWS managed rules for WAF configuration
 * @param scope - "regional" or "cloudfront"
 * @param startPriority - Starting priority number for rules
 * @param rules - Array of managed rule configurations
 * @returns Array of WAF rule configurations
 */
export function createManagedRules(
  scope: "regional" | "cloudfront",
  startPriority: number,
  rules: ManagedRuleConfig[]
): CfnWebACL.RuleProperty[] {
  return rules.map((rule, index) => ({
    name: rule.name,
    priority: startPriority + index,
    statement: {
      managedRuleGroupStatement: {
        vendorName: "AWS",
        name: rule.name,
        ...(rule.ruleActionOverrides && {
          ruleActionOverrides: rule.ruleActionOverrides,
        }),
      },
    },
    overrideAction: rule.overrideAction || { none: {} },
    visibilityConfig: {
      sampledRequestsEnabled: true,
      cloudWatchMetricsEnabled: true,
      metricName: rule.name,
    },
  }));
}
