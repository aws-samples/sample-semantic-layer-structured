import {
  Stack,
  StackProps,
  Duration,
  CfnOutput,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_logs as logs,
  aws_apigatewayv2 as apigwv2,
} from 'aws-cdk-lib';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cr from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import * as path from 'path';
import { MCP_INVOKE_SCOPE, mcpGatewayUrlSsmParam } from '../../common/auth-constants';

export interface McpProxyStackProps extends StackProps {
  readonly projectName: string;
  /** Cognito hosted-UI base URL (https://xxx.auth.<region>.amazoncognito.com). */
  readonly cognitoDomainUrl: string;
  /** PKCE 3LO client id the proxy authenticates MCP clients against. */
  readonly mcpClientId: string;
  /** The PKCE client (for appending the proxy /callback URL post-hoc). */
  readonly mcpClient: cognito.IUserPoolClient;
  /** User pool (for the callback-URL custom resource scope). */
  readonly userPool: cognito.IUserPool;
  /** Existing callback URLs on the PKCE client (preserved when appending /callback). */
  readonly existingCallbackUrls: string[];
}

/**
 * HTTP API + OAuth proxy Lambda enabling Claude Code / VS Code / Cursor to reach
 * the semantic-layer MCP query gateway over MCP OAuth (browser login). The proxy:
 *   - serves RFC 8414 / RFC 9728 OAuth metadata,
 *   - handles /authorize (scope injection), /callback (compound state), /token,
 *     /register (Dynamic Client Registration),
 *   - forwards authenticated MCP traffic to the gateway (URL resolved from SSM),
 *     rewriting the WWW-Authenticate resource_metadata URL to point at the proxy.
 *
 * The proxy Lambda is pure stdlib Python (urllib + boto3) — no pip deps — so it
 * deploys from a plain asset, no Docker/CodeBuild round trip.
 */
export class McpProxyStack extends Stack {
  public readonly httpApi: apigwv2.HttpApi;
  public readonly proxyFn: lambda.IFunction;

  constructor(scope: Construct, id: string, props: McpProxyStackProps) {
    super(scope, id, props);

    const ssmParamName = mcpGatewayUrlSsmParam(props.projectName);

    const role = new iam.Role(this, 'ProxyRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      description: `Execution role for ${props.projectName} MCP OAuth proxy Lambda`,
    });
    // Least privilege: only read the one SSM param holding the gateway URL.
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ['ssm:GetParameter'],
        resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${ssmParamName}`],
      })
    );

    this.proxyFn = new lambda.Function(this, 'McpProxyFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../../../lambda/mcp-proxy')),
      role,
      timeout: Duration.seconds(60),
      memorySize: 256,
      environment: {
        COGNITO_DOMAIN: props.cognitoDomainUrl,
        CLIENT_ID: props.mcpClientId,
        CLIENT_SECRET: '',
        GATEWAY_URL_SSM_PARAM: ssmParamName,
        GATEWAY_SCOPE: MCP_INVOKE_SCOPE,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
      description: 'OAuth proxy forwarding MCP traffic to the semantic-layer MCP gateway',
    });

    this.httpApi = new apigwv2.HttpApi(this, 'HttpApi', {
      apiName: `${props.projectName}-mcp-proxy`,
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.OPTIONS,
          apigwv2.CorsHttpMethod.DELETE,
        ],
        allowHeaders: ['*'],
        exposeHeaders: ['Mcp-Session-Id', 'WWW-Authenticate'],
      },
    });

    const integration = new HttpLambdaIntegration('ProxyIntegration', this.proxyFn);

    const routes: { path: string; methods: apigwv2.HttpMethod[] }[] = [
      { path: '/.well-known/oauth-authorization-server', methods: [apigwv2.HttpMethod.GET] },
      { path: '/.well-known/oauth-protected-resource', methods: [apigwv2.HttpMethod.GET] },
      { path: '/authorize', methods: [apigwv2.HttpMethod.GET] },
      { path: '/callback', methods: [apigwv2.HttpMethod.GET] },
      { path: '/token', methods: [apigwv2.HttpMethod.POST] },
      { path: '/register', methods: [apigwv2.HttpMethod.POST] },
    ];
    for (const r of routes) {
      this.httpApi.addRoutes({ path: r.path, methods: r.methods, integration });
    }
    // Catch-all for MCP traffic — the proxy forwards to the gateway.
    this.httpApi.addRoutes({
      path: '/{proxy+}',
      methods: [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
      integration,
    });

    // Append the proxy's /callback URL to the PKCE client's callback list so the
    // OAuth code flow can round-trip through this proxy. Preserves existing URLs.
    const callbackUrls = [...props.existingCallbackUrls, `${this.httpApi.apiEndpoint}/callback`];
    const callbackParams = {
      UserPoolId: props.userPool.userPoolId,
      ClientId: props.mcpClient.userPoolClientId,
      CallbackURLs: callbackUrls,
      AllowedOAuthFlows: ['code'],
      AllowedOAuthScopes: ['openid', 'profile', 'email', MCP_INVOKE_SCOPE],
      AllowedOAuthFlowsUserPoolClient: true,
      SupportedIdentityProviders: ['COGNITO'],
    };
    new cr.AwsCustomResource(this, 'AddCallbackUrl', {
      onCreate: {
        service: 'CognitoIdentityServiceProvider',
        action: 'updateUserPoolClient',
        parameters: callbackParams,
        physicalResourceId: cr.PhysicalResourceId.of('mcp-proxy-callback-url'),
      },
      onUpdate: {
        service: 'CognitoIdentityServiceProvider',
        action: 'updateUserPoolClient',
        parameters: callbackParams,
        physicalResourceId: cr.PhysicalResourceId.of('mcp-proxy-callback-url'),
      },
      installLatestAwsSdk: false,
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['cognito-idp:UpdateUserPoolClient'],
          resources: [props.userPool.userPoolArn],
        }),
      ]),
    });

    new CfnOutput(this, 'McpProxyApiUrl', {
      value: this.httpApi.apiEndpoint,
      description: 'HTTP API endpoint — point Claude Code / VSCode MCP clients here',
    });
    new CfnOutput(this, 'McpClientConfig', {
      value: JSON.stringify({ type: 'http', url: this.httpApi.apiEndpoint }),
      description: 'Paste into Claude Code: claude mcp add --transport http semantic-layer <url>',
    });

    // cdk-nag: managed basic-execution policy + the SSM read are intentional and
    // least-privilege; the AwsCustomResource SDK call is scoped to the pool ARN.
    NagSuppressions.addStackSuppressions(this, [
      {
        id: 'AwsSolutions-IAM4',
        reason: 'AWSLambdaBasicExecutionRole is the standard least-privilege logging policy.',
      },
      {
        id: 'AwsSolutions-IAM5',
        reason:
          'AwsCustomResource provider + Lambda log-retention use scoped wildcards on log streams / SDK calls.',
      },
      {
        id: 'AwsSolutions-L1',
        reason: 'Proxy Lambda pinned to Python 3.12 (latest supported runtime at authoring).',
      },
      {
        id: 'AwsSolutions-APIG4',
        reason:
          'The OAuth proxy routes are INTENTIONALLY unauthenticated at the API Gateway layer: the ' +
          'metadata/authorize/callback/token/register endpoints ARE the OAuth mechanism (RFC 8414/9728 + ' +
          'PKCE) and must be public; the catch-all forwards the caller-supplied Bearer token to the ' +
          'AgentCore gateway, whose CUSTOM_JWT authorizer enforces auth downstream.',
      },
      {
        id: 'AwsSolutions-APIG1',
        reason:
          'Access logging is not enabled on this thin OAuth-proxy HTTP API; the proxy Lambda logs each ' +
          'request to CloudWatch and the gateway records its own access logs.',
      },
    ]);
  }
}
