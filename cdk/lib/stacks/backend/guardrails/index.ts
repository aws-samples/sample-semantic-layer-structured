import { CfnOutput, StackProps } from "aws-cdk-lib";
import { CfnGuardrail } from "aws-cdk-lib/aws-bedrock";
import { Construct } from "constructs";
import { CommonStack } from "../../../common/constructs/stack";

export class GuardrailsStack extends CommonStack {
  public readonly guardrails: CfnGuardrail;
  public readonly guardrailId: string;
  public readonly guardrailVersion: string;

  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    const resourcePrefix = this.resourcePrefix;

    // Create a Bedrock Guardrail
    // Note: Name must be 1-50 chars, pattern: ^[0-9a-zA-Z-_]+$
    this.guardrails = new CfnGuardrail(this, "BedrockGuardrail", {
      name: `${resourcePrefix}-guardrail`,
      description: "AI safety guardrails for semantic layer application.",
      blockedInputMessaging:
        "Your input contains content that violates our usage policies.",
      blockedOutputsMessaging:
        "The response contains content that violates our usage policies.",

      // Content policy configuration
      contentPolicyConfig: {
        filtersConfig: [
          {
            type: "SEXUAL",
            inputStrength: "HIGH",
            outputStrength: "HIGH",
          },
          {
            type: "VIOLENCE",
            inputStrength: "HIGH",
            outputStrength: "HIGH",
          },
          {
            type: "HATE",
            inputStrength: "HIGH",
            outputStrength: "HIGH",
          },
          {
            type: "INSULTS",
            inputStrength: "HIGH",
            outputStrength: "HIGH",
          },
          {
            type: "MISCONDUCT",
            inputStrength: "HIGH",
            outputStrength: "HIGH",
          },
        ],
      },

      // Sensitive information policy configuration
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          {
            type: "ADDRESS",
            action: "ANONYMIZE",
          },
        ],
        regexesConfig: [
          {
            name: "CustomerIDPattern",
            description: "Pattern for customer IDs",
            pattern: "^[A-Z]{2}\\d{6}$",
            action: "ANONYMIZE",
          },
        ],
      },

      // // Contextual grounding policy configuration
      // contextualGroundingPolicyConfig: {
      //     filtersConfig: [
      //         {
      //             type: 'GROUNDING',
      //             threshold: 0.5
      //         },
      //         {
      //             type: 'RELEVANCE',
      //             threshold: 0.5
      //         }
      //     ]
      // },

      // Topic policy configuration
      topicPolicyConfig: {
        topicsConfig: [
          {
            name: "FINANCIAL_ADVICE",
            type: "DENY",
            definition:
              "Offering guidance or suggestions on financial investments, financial planning, or financial decisions.",
          },
          {
            name: "LEGAL_ADVICE",
            type: "DENY",
            definition:
              "Offering guidance or suggestions on legal matters, legal actions, interpretation of laws, or legal rights and responsibilities.",
            examples: [
              "Can I sue someone for this?",
              "What are my legal rights in this situation?",
              "Is this action against the law?",
              "What should I do to file a legal complaint?",
              "Can you explain this law to me?",
            ],
          },
        ],
      },

      // Word policy configuration
      wordPolicyConfig: {
        wordsConfig: [
          {
            text: "drugs",
          },
        ],
        managedWordListsConfig: [
          {
            type: "PROFANITY",
          },
        ],
      },
    });

    this.guardrailId = this.guardrails.ref;
    this.guardrailVersion = this.guardrails.attrVersion;

    // Export the guardrail ARN
    new CfnOutput(this, "GuardrailArn", {
      value: this.guardrails.attrGuardrailArn,
      description: "The ARN of the Bedrock Guardrail",
      exportName: `${resourcePrefix}-guardrail-arn`,
    });

    // Export the guardrail identifier
    new CfnOutput(this, "GuardrailIdentifier", {
      value: this.guardrails.ref,
      description: "The identifier of the Bedrock Guardrail",
      exportName: `${resourcePrefix}-guardrail-identifier`,
    });

    // Export the guardrail version
    new CfnOutput(this, "GuardrailVersion", {
      value: this.guardrails.attrVersion,
      description: "The version of the Bedrock Guardrail",
      exportName: `${resourcePrefix}-guardrail-version`,
    });
  }
}
