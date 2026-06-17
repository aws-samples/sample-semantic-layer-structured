/**
 * Shared auth constants for the AgentCore JWT/OAuth unification.
 *
 * Single source of truth so the Cognito resource-server scope, the gateway
 * `allowedScopes`, the MCP OAuth proxy `GATEWAY_SCOPE`, and the runtime
 * authorizer scope cannot drift apart. Changing the scope here changes it
 * everywhere it is referenced.
 */

/** Cognito resource-server identifier for the semantic-layer MCP/AgentCore APIs. */
export const MCP_RESOURCE_SERVER_ID = 'semantic-layer-mcp';

/** The single custom scope name on the resource server. */
export const MCP_INVOKE_SCOPE_NAME = 'invoke';

/**
 * Fully-qualified scope string (`<resourceServerId>/<scopeName>`) that appears
 * in access-token `scope` claims and is validated by the gateways + runtimes.
 */
export const MCP_INVOKE_SCOPE = `${MCP_RESOURCE_SERVER_ID}/${MCP_INVOKE_SCOPE_NAME}`;

/** SSM parameter name (per project) where mcp-server publishes the MCP gateway URL. */
export const mcpGatewayUrlSsmParam = (projectName: string): string =>
  `/${projectName}/mcp/gateway-url`;

/**
 * Static callback URLs for the PKCE 3LO client (Claude Code / VSCode / Cursor
 * loopback + vscode.dev redirects). The mcp-proxy stack appends its own
 * `/callback` URL to these at deploy time. Shared so the auth stack and the
 * proxy stack agree on the base set.
 */
export const MCP_PKCE_CALLBACK_URLS: string[] = [
  'https://vscode.dev/redirect',
  'https://insiders.vscode.dev/redirect',
  'http://127.0.0.1:33418',
  'http://localhost:33418',
];
