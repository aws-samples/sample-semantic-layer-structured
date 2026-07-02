# MCP Server Guide

The MCP server is an **AgentCore Gateway** that exposes four tools to MCP clients (Claude Desktop, Cursor, other agents). The Gateway forwards each `tools/call` to a Lambda that wraps the existing query agents.

## Architecture

```
Claude Code / VSCode / Cursor
        │ MCP OAuth 2.0 (PKCE), bearer token
        ▼
HTTP API + mcp-proxy Lambda (lambda/mcp-proxy)
        │ forwards bearer token (gateway scope injected)
        ▼
AgentCore Gateway (CUSTOM_JWT authorizer: PKCE client + semantic-layer-mcp/invoke)
        │ Lambda invoke (Gateway service role)
        ▼
mcp-tools Lambda (lambda/mcp-tools)
   ├── INPUT guardrail (Bedrock)
   ├── invoke runtimes over HTTPS w/ M2M OAuth Bearer (JWT-inbound runtimes)
   └── OUTPUT guardrail
```

## Endpoints

| Resource        | URL             |
| --------------- | --------------- |
| MCP Gateway     | `<PLACEHOLDER>` |
| MCP OAuth Proxy | `<PLACEHOLDER>` |
| Chat Gateway    | `<PLACEHOLDER>` |

Retrieve the current URL from CloudFormation at any time:

```bash
aws cloudformation describe-stacks \
  --stack-name semantic-layer-dev-mcp-server \
  --query "Stacks[0].Outputs[?OutputKey=='McpGatewayUrl'].OutputValue" \
  --output text
```

## Tools

### `ListOntologies`

- `status` (string, optional) — filter to ontologies in that build status (e.g. `completed`); omit for all
- Returns: `{ ontologies: [{ id, name, mode, type, status, updatedAt, dataSourceCount, latestVersion, description }, ...], count }`
- **Call this first** to discover the available semantic layers and read each one's `mode` (VKG → `OntologyQuery`, SemanticRAG → `MetadataQuery`) before choosing a query tool.

### `OntologyQuery`

- `ontologyId` (string, required), `question` (string, required), `rowLimit` (integer, default 10, max 100)
- Returns: `answer`, `rows`, `sparql`, `sql`, `lineage`

### `MetadataQuery`

- Same shape as OntologyQuery, runs the Semantic-RAG path. Returns `retrievedChunks` instead of `lineage`.

### `QuerySuggestions`

- `ontologyId` (string, required) → `{ suggestions: [{ category, question }, ...] }`

## Authentication

The MCP query gateway authorizer is **`CUSTOM_JWT`** (Cognito): clients present a
bearer access token carrying the `semantic-layer-mcp/invoke` scope — no SigV4, no
AWS credentials on the client. Outbound auth from Gateway → Lambda stays the
Gateway service role (`GATEWAY_IAM_ROLE`).

Claude Code / VS Code / Cursor reach the gateway through the **MCP OAuth proxy**
(`semantic-layer-dev-mcp-proxy`): an HTTP API + Lambda that runs the MCP OAuth 2.0
flow (RFC 8414/9728 discovery + Authorization Code + PKCE + Dynamic Client
Registration), injects the gateway scope at `/authorize`, and forwards
authenticated MCP traffic to the gateway. The browser handles the Cognito login.

> Backend service-to-runtime calls (mcp-tools Lambda, REST generation jobs) use a
> Cognito **M2M client_credentials** token (the `semantic-layer-mcp/invoke` scope).

## Claude Code quickstart

```bash
# Add the MCP server via the OAuth proxy URL (browser Cognito login on first use).
claude mcp add --transport http semantic-layer <PLACEHOLDER>
# then run /mcp in Claude Code and complete the Cognito browser login
```

After login, the tool list shows `ListOntologies`, `OntologyQuery`, `MetadataQuery`,
`QuerySuggestions`. For interactive testing, point the **MCP Inspector**
(https://modelcontextprotocol.io/) at the proxy URL — it drives the same OAuth flow.

## Cowork (Claude Desktop 3P) quickstart

Cowork reaches the **same Gateway with the same Cognito auth** — no new
infrastructure. But the Claude Code config above is **not portable**: Cowork
reads a different config file (`configLibrary` → `managedMcpServers`, not
`~/.claude.json`), connects to the Gateway **directly** (not the OAuth proxy),
and authenticates via a **headersHelper** script that injects the bearer token
(Cowork's native MCP OAuth fallback is non-functional).

```bash
# One-time in Cowork: Developer Mode → Configure third-party inference →
# Bedrock + region + API key → Apply locally. Without inferenceProvider,
# managedMcpServers is silently ignored.

cd cowork
./setup-cognito.sh        # browser Cognito login (port 33418), installs helper,
                          # writes managedMcpServers
# Restart Cowork (Cmd+Q, reopen)
```

The setup reuses the deployed **MCP PKCE client** (`McpPkceClientId`) — already a
public client with the `http://127.0.0.1:33418` loopback redirect and the
`semantic-layer-mcp/invoke` scope. Tools appear under
**Customize > Connectors > Semantic Layer MCP**. See [`cowork/README.md`](../../cowork/README.md)
for the full setup, the WAF note (this pool's hosted UI is fronted by WAFv2 Bot
Control — harmless for the browser login flow), and troubleshooting.

## Testing

`notebooks/10_mcp_server_testing.ipynb` covers the full deployed stack:

| Step   | What it tests                                                   |
| ------ | --------------------------------------------------------------- |
| Step 2 | Cognito M2M token minting (`client_credentials`)                |
| Step 3 | Gateway `tools/list` via CUSTOM_JWT bearer token                |
| Step 4 | All 4 tools called through the Gateway (CUSTOM_JWT path)        |
| Step 5 | All 4 tools called directly against the Lambda (IAM/SigV4 path) |
| Step 6 | Bedrock Guardrails INPUT blocking                               |
| Step 7 | OAuth proxy RFC 8414/9728 discovery endpoints                   |
| Step 8 | Lambda dispatcher error handling (missing args, unknown tool)   |
| Step 9 | M2M JWT scope + expiry validation                               |

## Implementation notes

- `lambda/mcp-tools/index.py` — four tool handlers + Gateway dispatch (`bedrockAgentCoreToolName` from `context.client_context.custom`).
- `lambda/mcp-tools/Dockerfile` — Lambda Python 3.12 ARM64 base image (proper Lambda Runtime client).
- `cdk/lib/stacks/backend/mcp-server-stack.ts` — Gateway + 4 inline-schema targets + Lambda + IAM.
- Tool implementations call the JWT-inbound AgentCore Runtimes over HTTPS with a Cognito M2M OAuth Bearer token (the runtimes no longer accept SigV4), so a single source of truth for query logic is preserved.
- `lambda/mcp-proxy/lambda_function.py` + `cdk/lib/stacks/backend/mcp-proxy-stack.ts` — the OAuth proxy (HTTP API + stdlib Lambda) that makes the CUSTOM_JWT gateway reachable from Claude Code.

## Status

- [x] AgentCore Gateway with `CUSTOM_JWT` authorizer (Cognito PKCE + `semantic-layer-mcp/invoke` scope) + 4 inline-schema targets
- [x] MCP OAuth proxy (HTTP API + Lambda) for Claude Code / VSCode / Cursor login
- [x] Lambda image (proper `public.ecr.aws/lambda/python:3.12-arm64` base) bundling tool dispatch
- [x] Bedrock Guardrails INPUT/OUTPUT screening per tool call
