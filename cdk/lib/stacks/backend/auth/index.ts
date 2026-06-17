import { Duration, CfnOutput, RemovalPolicy, StackProps } from 'aws-cdk-lib';
import {
  AccountRecovery,
  ClientAttributes,
  FeaturePlan,
  OAuthScope,
  ResourceServerScope,
  UserPool,
  UserPoolClient,
  UserPoolDomain,
  UserPoolGroup,
  UserPoolResourceServer,
} from 'aws-cdk-lib/aws-cognito';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { IdentityPool, UserPoolAuthenticationProvider } from 'aws-cdk-lib/aws-cognito-identitypool';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Function } from 'aws-cdk-lib/aws-lambda';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { CfnWebACL, CfnWebACLAssociation } from 'aws-cdk-lib/aws-wafv2';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { FederateUserPool, FederateUserPoolClient } from '../../../common/constructs/federate';
import { createManagedRules } from '../../../common/utilities';
import { CommonStack } from '../../../common/constructs/stack';
import { LakeFormationGroupGrants } from '../lf-groups';
import {
  MCP_RESOURCE_SERVER_ID,
  MCP_INVOKE_SCOPE_NAME,
  MCP_PKCE_CALLBACK_URLS,
} from '../../../common/auth-constants';

interface AuthStackProps extends StackProps {
  urls: string[];
  hydrationFunction?: Function;
  /** Glue databases the per-group LF roles should be granted on. When
   *  omitted the LF construct is not provisioned.
   *  Wired by app.ts after the glue-catalog stack lands. */
  lfGrantDatabases?: { readonly name: string; readonly catalogId?: string }[];
}

export class AuthStack extends CommonStack {
  public readonly userPool: UserPool;
  public readonly userPoolDomain?: UserPoolDomain;
  public readonly userPoolClient: UserPoolClient;
  public readonly identityPool: IdentityPool;
  public readonly regionalWebAclArn: string;
  public readonly authenticatedRole: iam.Role;
  public readonly authenticationMode: string;
  public readonly lfGroupGrants?: LakeFormationGroupGrants;

  // ---- AgentCore JWT/OAuth unification (2026-06-02) -----------------------
  /** Hosted-UI domain for the OAuth proxy login + token endpoint. */
  public readonly mcpHostedUiDomain: UserPoolDomain;
  /** Resource server carrying the `semantic-layer-mcp/invoke` scope. */
  public readonly mcpResourceServer: UserPoolResourceServer;
  /** Public PKCE 3LO client for Claude Code / VSCode / Cursor MCP login. */
  public readonly mcpClient: UserPoolClient;
  /** Confidential machine-to-machine client (client_credentials) for backend
   *  service-to-runtime calls (mcp-tools Lambda, REST generation jobs). */
  public readonly m2mClient: UserPoolClient;
  /** Secrets Manager secret holding the M2M client secret (backend callers read
   *  this to build the client_credentials Basic-auth header). */
  public readonly m2mClientSecret: secretsmanager.Secret;
  /** Confidential authorization-code client for the strands-agent AgentCore
   *  Gateway (mcp_server target). Mirrors mcpClient but with a secret —
   *  AgentCore Identity requires a confidential client. */
  public readonly agentcoreClient: UserPoolClient;
  /** Secrets Manager secret holding the AgentCore auth-code client secret. */
  public readonly agentcoreClientSecret: secretsmanager.Secret;
  /** Cognito hosted-UI base URL; the OAuth token endpoint is `${url}/oauth2/token`. */
  public readonly mcpHostedUiDomainUrl: string;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const { urls, hydrationFunction } = props;

    // Read feature flags from CDK context
    const enableDirectAuth = this.node.tryGetContext('enableDirectAuth') ?? true;
    const selfSignUpEnabled = this.node.tryGetContext('selfSignUpEnabled') ?? false; // default CLOSED — never allow self-signup unless explicitly set
    // default FALSE — dev/demo deletes the user pool on `cdk destroy`. Pass
    // `-c retainUserPool=true` for production so real accounts are preserved.
    const retainUserPool =
      this.node.tryGetContext('retainUserPool') === true ||
      this.node.tryGetContext('retainUserPool') === 'true';
    this.authenticationMode = enableDirectAuth ? 'direct' : 'oauth';

    // Create Cognito User Pool
    const userPool = new FederateUserPool(this, `${this.resourcePrefix}-userPool`, {
      selfSignUpEnabled: selfSignUpEnabled,
      signInAliases: {
        email: true,
      },
      autoVerify: {
        email: true,
      },
      standardAttributes: {
        email: {
          required: true,
          mutable: true,
        },
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireDigits: true,
        requireUppercase: true,
        requireSymbols: true,
      },
      accountRecovery: AccountRecovery.EMAIL_ONLY,
      featurePlan: FeaturePlan.ESSENTIALS,
      lambdaTriggers: {
        postConfirmation: hydrationFunction,
      },
    });
    // dev/demo: delete the pool on `cdk destroy`. For prod pass
    // `-c retainUserPool=true` to keep RETAIN and preserve user accounts.
    userPool.applyRemovalPolicy(retainUserPool ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY);
    NagSuppressions.addResourceSuppressions(userPool, [
      {
        id: 'AwsSolutions-COG2',
        reason: 'Cognito user pool should not require MFA for demos.',
      },
      {
        id: 'AwsSolutions-COG3',
        reason:
          "AdvancedSecurityMode is set to depreciate. Using Cognito feature plan's essential security feature.",
      },
    ]);

    new UserPoolGroup(this, 'adminUserPoolGroup', {
      userPool,
      groupName: 'Admin',
    });

    new UserPoolGroup(this, 'usersUserPoolGroup', {
      userPool,
      groupName: 'Users',
    });

    // Access and ID tokens: 1 hour — limits blast radius if a token is stolen
    const accessTokenValidity = Duration.hours(1);
    // Refresh token: 30 days — keeps users logged in; revoke via Cognito admin if compromised
    const refreshTokenValidity = Duration.days(30);

    // Create User Pool Client with OAuth configuration
    // URLs array includes CloudFront domain (from CloudFrontStorageStack) + localhost URLs
    // This ensures users can login through both CloudFront production URL and local development
    const userPoolClient = new FederateUserPoolClient(
      this,
      `${this.resourcePrefix}-userPoolClient`,
      {
        userPool,
        generateSecret: false,
        refreshTokenValidity: refreshTokenValidity,
        accessTokenValidity: accessTokenValidity,
        idTokenValidity: accessTokenValidity,
        readAttributes: new ClientAttributes().withStandardAttributes({
          email: true,
        }),
        authFlows: {
          // SRP: client proves knowledge of password without transmitting it over the wire
          userSrp: true,
          // Custom auth: enables Lambda-based challenge/response flows (e.g. OTP)
          custom: true,
          // adminUserPassword intentionally omitted — ADMIN_USER_PASSWORD_AUTH sends credentials
          // in plaintext headers; use userSrp for all user-facing authentication
        },
        oAuth: {
          callbackUrls: urls,
          logoutUrls: urls,
        },
      }
    );

    // Create identity pool
    const identityPool = new IdentityPool(this, 'identityPool', {
      allowUnauthenticatedIdentities: false,
      authenticationProviders: {
        userPools: [
          new UserPoolAuthenticationProvider({
            userPool,
            userPoolClient,
          }),
        ],
      },
    });
    identityPool.unauthenticatedRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.DENY,
        actions: ['*'],
        resources: ['*'],
      })
    );

    const regionalWebAcl = new CfnWebACL(this, 'regionalWebAcl', {
      defaultAction: { allow: {} },
      scope: 'REGIONAL',
      visibilityConfig: {
        metricName: 'regionalWebAcl',
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
      },
      rules: [
        {
          name: 'ipRateLimitingRule',
          priority: 0,
          statement: {
            rateBasedStatement: {
              limit: 3000,
              aggregateKeyType: 'IP',
            },
          },
          action: {
            block: {},
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'ipRateLimitingRule',
          },
        },
        ...createManagedRules('regional', 1, [
          {
            name: 'AWSManagedRulesCommonRuleSet',
            // No overrideAction — each rule uses its own default BLOCK action
          },
          {
            name: 'AWSManagedRulesBotControlRuleSet',
            // No overrideAction — each rule uses its own default BLOCK action
          },
          {
            name: 'AWSManagedRulesKnownBadInputsRuleSet',
          },
          {
            name: 'AWSManagedRulesUnixRuleSet',
            ruleActionOverrides: [
              {
                name: 'UNIXShellCommandsVariables_BODY',
                actionToUse: {
                  // COUNT (not block): SPARQL/ontology payloads contain $variable syntax
                  // that resembles Unix shell variables. These are legitimate API payloads.
                  count: {},
                },
              },
            ],
          },
          {
            name: 'AWSManagedRulesSQLiRuleSet',
            ruleActionOverrides: [
              {
                name: 'SQLi_BODY',
                actionToUse: {
                  // COUNT (not block): large Athena SQL query strings in request bodies
                  // trigger this rule. SQL injection protection is enforced by Athena directly.
                  count: {},
                },
              },
            ],
          },
        ]),
      ],
    });
    const regionalWebAclArn = regionalWebAcl.attrArn;

    new CfnWebACLAssociation(this, 'userPoolWebAclAssociation', {
      resourceArn: userPool.userPoolArn,
      webAclArn: regionalWebAclArn,
    });

    this.userPool = userPool;
    this.userPoolDomain = userPool.userPoolDomain;
    this.userPoolClient = userPoolClient;
    this.identityPool = identityPool;
    this.regionalWebAclArn = regionalWebAclArn;

    // ---- AgentCore JWT/OAuth unification (2026-06-02) --------------------
    // See docs/plans/2026-06-02-agentcore-jwt-oauth-unification-design.md.
    // Adds the identity primitives that let the WHOLE system speak JWT/OAuth:
    //   * a hosted-UI domain (browser login for the MCP OAuth proxy + token endpoint),
    //   * a resource server + `invoke` scope (validated by gateways + runtimes),
    //   * a public PKCE 3LO client (Claude Code / VSCode / Cursor MCP login),
    //   * a confidential M2M client (backend service-to-runtime calls, no user).
    // The existing SPA web client + chat-gateway JWT config are untouched.

    // Hosted-UI domain — prefix must be globally unique; derive from
    // resourcePrefix + account and clamp to Cognito's 63-char limit.
    const domainPrefix = `${this.resourcePrefix}-${this.account}`.toLowerCase().slice(0, 63);
    this.mcpHostedUiDomain = userPool.addDomain('McpHostedUiDomain', {
      cognitoDomain: { domainPrefix },
    });

    // Resource server + single custom scope (semantic-layer-mcp/invoke).
    const invokeScope = new ResourceServerScope({
      scopeName: MCP_INVOKE_SCOPE_NAME,
      scopeDescription: 'Invoke semantic-layer MCP tools and AgentCore runtimes',
    });
    this.mcpResourceServer = userPool.addResourceServer('McpResourceServer', {
      identifier: MCP_RESOURCE_SERVER_ID,
      scopes: [invokeScope],
    });

    // Public PKCE 3LO client — no secret; authorization-code grant; loopback +
    // vscode callbacks. The OAuth proxy appends its own /callback URL post-hoc.
    this.mcpClient = userPool.addClient('McpPkceClient', {
      userPoolClientName: `${this.resourcePrefix}-mcp`,
      generateSecret: false,
      accessTokenValidity: accessTokenValidity,
      idTokenValidity: accessTokenValidity,
      refreshTokenValidity: refreshTokenValidity,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          OAuthScope.OPENID,
          OAuthScope.PROFILE,
          OAuthScope.EMAIL,
          OAuthScope.resourceServer(this.mcpResourceServer, invokeScope),
        ],
        callbackUrls: [...MCP_PKCE_CALLBACK_URLS],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    // Confidential M2M client — client_credentials grant + invoke scope. Used by
    // backend Lambdas (mcp-tools, REST generation jobs) that invoke runtimes with
    // no end-user context. The secret is read from the auto-generated client
    // secret (exposed via the SDK at runtime through DescribeUserPoolClient, or
    // surfaced to callers per the proxy/runtime caller wiring).
    this.m2mClient = userPool.addClient('M2mClient', {
      userPoolClientName: `${this.resourcePrefix}-m2m`,
      generateSecret: true,
      accessTokenValidity: accessTokenValidity,
      authFlows: {},
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [OAuthScope.resourceServer(this.mcpResourceServer, invokeScope)],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    // Surface the M2M client secret into Secrets Manager so backend callers
    // (mcp-tools Lambda, REST) can read it at runtime with a least-privilege
    // grant. `userPoolClientSecret` is a CDK SecretValue resolvable at deploy.
    this.m2mClientSecret = new secretsmanager.Secret(this, 'M2mClientSecret', {
      secretName: `/${this.resourcePrefix}/m2m-client-secret`,
      description: 'Cognito M2M (client_credentials) app-client secret for AgentCore calls',
      secretStringValue: this.m2mClient.userPoolClientSecret,
    });
    NagSuppressions.addResourceSuppressions(this.m2mClientSecret, [
      {
        id: 'AwsSolutions-SMG4',
        reason:
          'This secret mirrors the Cognito app-client secret (a managed value); Secrets Manager ' +
          'rotation cannot regenerate a Cognito client secret, so automatic rotation is not applicable. ' +
          'Rotate by recreating the app client if ever compromised.',
      },
    ]);

    // Confidential authorization-code client for the strands-agent AgentCore
    // Gateway (mcp_server target). Like mcpClient but generateSecret: true —
    // AgentCore Identity needs a confidential client.
    this.agentcoreClient = userPool.addClient('AgentcoreAuthCodeClient', {
      userPoolClientName: `${this.resourcePrefix}-agentcore`,
      generateSecret: true,
      accessTokenValidity: accessTokenValidity,
      idTokenValidity: accessTokenValidity,
      refreshTokenValidity: refreshTokenValidity,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [OAuthScope.OPENID, OAuthScope.resourceServer(this.mcpResourceServer, invokeScope)],
        // Placeholder; the real AgentCore callback URL is added in Phase 3 of
        // SEMANTIC_LAYER_SETUP.md once the credential provider mints it.
        callbackUrls: ['https://localhost/oauth-complete'],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    // Mirror the secret into Secrets Manager (same pattern as m2mClientSecret).
    this.agentcoreClientSecret = new secretsmanager.Secret(this, 'AgentcoreClientSecret', {
      secretName: `/${this.resourcePrefix}/agentcore-client-secret`,
      description: 'Confidential auth-code client secret for the strands-agent AgentCore Gateway',
      secretStringValue: this.agentcoreClient.userPoolClientSecret,
    });
    NagSuppressions.addResourceSuppressions(this.agentcoreClientSecret, [
      {
        id: 'AwsSolutions-SMG4',
        reason:
          'This secret mirrors the Cognito app-client secret (a managed value); Secrets Manager ' +
          'rotation cannot regenerate a Cognito client secret, so automatic rotation is not applicable. ' +
          'Rotate by recreating the app client if ever compromised.',
      },
    ]);

    new CfnOutput(this, 'AgentcoreAuthCodeClientId', {
      value: this.agentcoreClient.userPoolClientId,
      description: 'Confidential auth-code client id for the strands-agent AgentCore Gateway',
    });

    this.mcpHostedUiDomainUrl = `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`;

    // Create authenticated role
    this.authenticatedRole = new iam.Role(this, `${this.resourcePrefix}-AuthenticatedRole`, {
      assumedBy: new iam.FederatedPrincipal(
        'cognito-identity.amazonaws.com',
        {
          StringEquals: {
            'cognito-identity.amazonaws.com:aud': this.identityPool.identityPoolId,
          },
          'ForAnyValue:StringLike': {
            'cognito-identity.amazonaws.com:amr': 'authenticated',
          },
        },
        'sts:AssumeRoleWithWebIdentity'
      ),
      description: 'IAM role for authenticated Cognito users',
    });

    // Add comprehensive permissions for the retail workforce management system
    this.addAuthenticatedRolePermissions(this.resourcePrefix, this.environmentName);

    // Access the underlying CFN resource to work with the identity pool at the CloudFormation level
    const cfnIdentityPool = this.identityPool.node.defaultChild as cognito.CfnIdentityPool;

    // NOTE: The CDK IdentityPool construct automatically creates a default role attachment
    // To avoid the "already exists" conflict error, we need to override or remove the auto-created attachment

    // Find and remove any auto-generated role attachments to prevent conflicts
    this.identityPool.node.findAll().forEach((child) => {
      if (
        child.node.id.includes('RoleAttachment') &&
        child.node.id !== 'CustomIdentityPoolRoleAttachment'
      ) {
        child.node.tryRemoveChild('Resource');
      }
    });

    // Create our role attachment with our custom authenticated role and unauthenticated role
    new cognito.CfnIdentityPoolRoleAttachment(this, 'CustomIdentityPoolRoleAttachment', {
      identityPoolId: cfnIdentityPool.ref,
      roles: {
        authenticated: this.authenticatedRole.roleArn,
        unauthenticated: this.identityPool.unauthenticatedRole.roleArn,
      },
      // Adding empty roleMappings to ensure this attachment has a different configuration
      // than any auto-generated one, further preventing conflicts
      roleMappings: {},
    });

    // ---- Per-group LF grants (item #4 OBO migration) ----
    if (props.lfGrantDatabases && props.lfGrantDatabases.length > 0) {
      this.lfGroupGrants = new LakeFormationGroupGrants(this, 'LfGroupGrants', {
        userPool: this.userPool,
        databases: props.lfGrantDatabases,
        identityPoolId: this.identityPool.identityPoolId,
      });
    }

    // Outputs
    new CfnOutput(this, `${this.resourcePrefix}-UserPoolId`, {
      value: this.userPool.userPoolId,
    });

    new CfnOutput(this, `${this.resourcePrefix}-UserPoolClientId`, {
      value: this.userPoolClient.userPoolClientId,
    });

    new CfnOutput(this, `${this.resourcePrefix}-IdentityPoolId`, {
      value: this.identityPool.identityPoolId,
    });

    new CfnOutput(this, `${this.resourcePrefix}-AuthenticationMode`, {
      value: enableDirectAuth ? 'direct' : 'oauth',
      description: 'Authentication mode: oauth (Midway) or direct (Cognito)',
    });

    // ---- AgentCore JWT/OAuth unification outputs ------------------------
    new CfnOutput(this, 'McpHostedUiDomainUrl', {
      value: `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`,
      description: 'Cognito hosted-UI domain base URL (login + /oauth2/token)',
    });
    new CfnOutput(this, 'McpPkceClientId', {
      value: this.mcpClient.userPoolClientId,
      description: 'Public PKCE 3LO client id for Claude Code / VSCode MCP login',
    });
    new CfnOutput(this, 'M2mClientId', {
      value: this.m2mClient.userPoolClientId,
      description: 'Confidential M2M client id (client_credentials) for backend runtime calls',
    });
  }

  private addAuthenticatedRolePermissions(resourcePrefix: string, environment: string) {
    // Basic STS permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sts:GetCallerIdentity'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'aws:RequestedRegion': this.region,
          },
        },
      })
    );

    // Cognito permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['cognito-identity:GetId', 'cognito-identity:GetCredentialsForIdentity'],
        resources: [
          `arn:aws:cognito-identity:${this.region}:${this.account}:identitypool/${this.identityPool.identityPoolId}`,
        ],
      })
    );

    // EKS permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['eks:DescribeCluster', 'eks:AccessKubernetesApi'],
        resources: [`arn:aws:eks:${this.region}:${this.account}:cluster/${resourcePrefix}-cluster`],
        conditions: {
          StringEquals: {
            'aws:RequestedRegion': this.region,
          },
        },
      })
    );

    // ECR permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
          'ecr:BatchCheckLayerAvailability',
        ],
        resources: [
          `arn:aws:ecr:${this.region}:${this.account}:repository/${resourcePrefix}-backend`,
        ],
      })
    );

    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      })
    );

    // S3 permissions for authenticated users (scoped to Get/Put only, no Delete)
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          's3:GetObject',
          's3:GetObjectVersion',
          's3:PutObject',
          's3:PutObjectAcl',
          's3:ListBucket',
          's3:GetBucketLocation',
          's3:HeadBucket',
        ],
        resources: [
          // Standard patterns
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}-databucket*`,
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}-databucket*/*`,

          // Match patterns with wildcards to cover various bucket naming formats
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}*databucket*`,
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}*databucket*/*`,

          // Match the specific format in the error message: dev-sonicintbackendstorage9f359-databucketd8691f4e-2mxitb1kuznp
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}backendstorage*-databucket*`,
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}backendstorage*-databucket*/*`,

          // Additional backup patterns to ensure coverage
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}backendstorage*`,
          `arn:aws:s3:::${environment.toLowerCase()}-${resourcePrefix.toLowerCase()}backendstorage*/*`,

          // Very broad pattern as a fallback
          `arn:aws:s3:::${environment.toLowerCase()}-*databucket*`,
          `arn:aws:s3:::${environment.toLowerCase()}-*databucket*/*`,
        ],
      })
    );

    // Bedrock permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          // Global foundation models (no region/account)
          `arn:aws:bedrock:::foundation-model/anthropic.claude-*`,
          `arn:aws:bedrock:::foundation-model/amazon.nova-*`,
          `arn:aws:bedrock:::foundation-model/amazon.titan-*`,
          // Regional models
          `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-*`,
          `arn:aws:bedrock:${this.region}::foundation-model/amazon.nova-*`,
          `arn:aws:bedrock:${this.region}::foundation-model/amazon.titan-*`,
        ],
      })
    );

    // Bedrock Guardrails permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:ApplyGuardrail'],
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`,
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail-profile/*`,
        ],
      })
    );

    // Bedrock Knowledge Base permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:Retrieve'],
        resources: [`arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/*`],
      })
    );

    // Amazon Transcribe permissions for live assistant
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'transcribe:StartStreamTranscription',
          'transcribe:StartStreamTranscriptionWebSocket',
        ],
        resources: ['*'],
      })
    );

    // DynamoDB permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchWriteItem',
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-USERROLE-${environment}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-FEEDBACK-${environment}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-DOCUMENTUPLOAD-${environment}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-SESSION-PREP-${environment}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-SESSION-PREP-${environment}/index/CategoryIndex`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-SESSION-HISTORY-${environment}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${resourcePrefix}-SESSION-HISTORY-${environment}/index/CategoryIndex`,
        ],
      })
    );

    // CloudWatch Logs permissions
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:DescribeLogGroups',
          'logs:DescribeLogStreams',
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:bedrock-agentcore-observability`,
          `arn:aws:logs:${this.region}:${this.account}:log-group:bedrock-agentcore-observability:*`,
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/eks/${resourcePrefix}-cluster:*`,
        ],
      })
    );

    // X-Ray permissions for distributed tracing
    this.authenticatedRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'xray:PutTraceSegments',
          'xray:PutTelemetryRecords',
          'xray:GetSamplingRules',
          'xray:GetSamplingTargets',
        ],
        resources: ['*'],
      })
    );
  }
}
