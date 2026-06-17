# Cowork ↔ Semantic-Layer MCP Gateway

Connect **Cowork** (Claude Desktop 3P) to the Semantic-Layer AgentCore Gateway's
MCP tools — `ListOntologies`, `OntologyQuery`, `MetadataQuery`,
`QuerySuggestions`. This reuses the **same Gateway and Cognito auth** that
Claude Code uses (see [`../assets/guides/MCP_SERVER.md`](../assets/guides/MCP_SERVER.md));
**no new infrastructure is required.**

## Why Cowork needs its own setup (the Claude Code config is not portable)

|             | Claude Code                                               | Cowork                                                                                  |
| ----------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Config file | `~/.claude.json` → `mcpServers`                           | `~/Library/Application Support/Claude-3p/configLibrary/<id>.json` → `managedMcpServers` |
| Endpoint    | the OAuth **proxy** (`…execute-api…`)                     | the **Gateway** directly (`…gateway.bedrock-agentcore…/mcp`)                            |
| Auth        | Claude Code's built-in MCP OAuth drives the Cognito proxy | a **headersHelper** script injects `Authorization: Bearer …`                            |

Cowork's native MCP OAuth fallback is non-functional, so Cowork relies on a
headersHelper to supply the bearer token itself.

## Architecture

```
Cowork (3P mode, Bedrock inference)
  ↓ Streamable HTTP + Bearer JWT (via managedMcpServers + headersHelper)
AgentCore Gateway (CUSTOM_JWT authorizer — validates Cognito aud + semantic-layer-mcp/invoke)
  ↓ Lambda invoke
mcp-tools Lambda → query agents (ListOntologies / OntologyQuery / MetadataQuery / QuerySuggestions)
```

Auth is the deployed Cognito **MCP PKCE client** (`McpPkceClientId`) — a public
client (no secret) that already has loopback redirect `http://127.0.0.1:33418`
registered and the `semantic-layer-mcp/invoke` scope. This is the same per-user
3LO + PKCE path Claude Code uses; we just inject the resulting bearer via the
headersHelper instead of letting Cowork run the OAuth flow.

## Files

| File                 | Purpose                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `setup-cognito.sh`   | One-shot setup: derive values from CloudFormation, browser PKCE login (port 33418), install headersHelper, write `managedMcpServers`. |
| `agentcore-token.sh` | headersHelper: refresh + JSON `Authorization` header. Installed as `/usr/local/bin/agentcore-token-semantic-layer.sh`.                |
| `config.env.example` | Template for `~/.cowork-semantic-layer/config.env`.                                                                                   |

Sidecar files (not in repo): `~/.cowork-semantic-layer/{config.env,tokens.json}`
(both chmod 600). A distinct sidecar dir + installed-helper name lets this
coexist with other Cowork connectors (e.g. the agentcore_gateway one).

## Prerequisites

1. **3P Bedrock inference configured in Cowork** — Developer Mode → _Configure
   third-party inference_ → Bedrock + region + API key → _Apply locally_.
   Without `inferenceProvider`, `managedMcpServers` is **silently ignored**
   (`setup-cognito.sh` warns if it's missing).
2. **A Cognito pool user** to sign in as. Confirmed user present:
   `huthmac@amazon.com` (pool `us-east-1_KyXradR5G`).
3. **AWS creds** (optional) — if `aws sts get-caller-identity` works,
   `setup-cognito.sh` auto-fills endpoints from CloudFormation. Otherwise copy
   `config.env.example` to `~/.cowork-semantic-layer/config.env` first.

## Setup

```bash
cd cowork
./setup-cognito.sh            # opens browser → sign in with the Cognito user
# Restart Cowork (Cmd+Q, reopen)
```

Tools appear under **Customize > Connectors > Semantic Layer MCP**. Because we
use `managedMcpServers`, tools can be pre-approved there (`toolPolicy` only
applies in `managedMcpServers`, not plain `mcpServers`/`.mcp.json`).

Re-authenticate when the refresh token expires:

```bash
./setup-cognito.sh --force-login
```

## WAF note (specific to this pool)

This pool's hosted UI is fronted by a **WAFv2 WebACL** (Bot Control +
rate limiting) — the agentcore_gateway pool has none. This does **not** break
the flow:

- `/oauth2/authorize` runs in a **real browser**, which passes Bot Control.
- `/oauth2/token` (the script's curl exchange + refresh) is **explicitly
  excluded** from Bot Control by a scope-down rule, so it is allowed.

Scripted/headless hits on `/oauth2/authorize` (e.g. a `curl` smoke test) will
get a generic `403 Forbidden` from WAF — that is expected and is **not** a
config error.

## Header rules (inherited from Cowork)

- Emit **only** the `Authorization` header from the helper. Never emit
  `Mcp-Protocol-Version` — Cowork adds it, and duplicating it causes
  `Unsupported MCP protocol version`.

## Troubleshooting

| Symptom                                          | Cause                       | Fix                                           |
| ------------------------------------------------ | --------------------------- | --------------------------------------------- |
| No connector after restart                       | `inferenceProvider` not set | Configure 3P Bedrock inference, then re-run   |
| `403 Forbidden` from a curl test of `/authorize` | WAF Bot Control (expected)  | Log in via the browser, not curl              |
| `invalid_scope` at token exchange                | `offline_access` present    | Remove it from `SCOPES` in `config.env`       |
| Tools appear but calls fail                      | JWT expired mid-session     | Restart Cowork (helper re-fetches on startup) |
| `invalid_grant` on refresh                       | Refresh token expired       | `./setup-cognito.sh --force-login`            |
