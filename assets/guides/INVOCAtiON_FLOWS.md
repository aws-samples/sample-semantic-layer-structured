# Invocation Flows

Three ways in, one runtime + Bedrock chain, all speaking JWT/OAuth after the
unification: 
(1) the **browser chat** SSE path (below), 
(2) **MCP clients**
(Claude Code / VSCode / Cursor) via the OAuth proxy, and 
(3) **another AgentCore
Gateway** invoking us as a remote `mcp_server` target.

## 1.Invocation Flow: UI ↔ AgentCore Gateway ↔ Runtime ↔ Bedrock
Streaming chat path after the JWT/OAuth unification

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 1. BROWSER UI  (React / Cloudscape — frontend/src)                             │
│    • user types a question in the chat panel                                   │
│    • useChatStream.js opens an SSE POST                                         │
│    • Authorization: Bearer <Cognito ACCESS token>  (chatGatewayToken)          │
└───────────────────────────────┬────────────────────────────────────────────--┘
                                 │  POST {chatGatewayUrl}/{target}/invocations
                                 │  Accept: text/event-stream
                                 │  target = metadata-query | ontology-query
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 2. AGENTCORE CHAT GATEWAY                                                      │
│    inbound  : CUSTOM_JWT  ── validates access token's client_id vs allowed     │
│               (Cognito SPA client) against the pool's discovery URL            │
│    outbound : JWT_PASSTHROUGH ── forwards the SAME validated token onward       │
│               (no token swap — runtime re-validates it)                         │
└───────────────────────────────┬───────────────────────────────────────────--─┘
                                 │  Authorization: Bearer <same access token>
                                 │  X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 3. AGENT on AGENTCORE RUNTIME  (agents/metadata_query | ontology_query)        │
│    inbound  : CUSTOM_JWT  ── re-validates the forwarded token                  │
│               allowedClients = [SPA, M2M]   (SgEni/VPC, Python 3.12 container)   │
│                                                                                │
│    @app.entrypoint invoke(payload, context):                                   │
│      • _user_id_from_context() decodes the JWT `sub`  ← real user identity     │
│      • Strands Graph workflow runs (Tier-2 phases)                             │
│      • each phase/tool call → Bedrock LLM (see hop 4)                          │
│      • AG-UI events streamed back up (run_started, phase_*, run_finished)      │
│      • chat turn persisted to DynamoDB under  {ontology_id}/{sub}              │
└───────────────────────────────┬───────────────────────────────────────────--─┘
                                 │  bedrock-runtime: InvokeModelWithResponseStream
                                 │  (SigV4 via the runtime's execution role)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 4. BEDROCK LLM   (Claude Sonnet 4.6 / model from agents/*/models.json)         │
│    • generates SQL / SPARQL / reasoning / final answer                         │
│    • prompt-cached worker model (CacheConfig auto) to cut token cost           │
│    • token stream flows back ▲ through 3 → 2 → 1 as SSE to the browser         │
└──────────────────────────────────────────────────────────────────────────────┘

         ▲ stream back up                         ▼ request down
         └──────────  SSE: text/event-stream  ────┘
```

## Auth at each hop

| Hop                    | Direction | Auth mode           | Credential                                      |
| ---------------------- | --------- | ------------------- | ----------------------------------------------- |
| UI → Chat Gateway      | inbound   | **CUSTOM_JWT**      | Cognito **access token** (has `client_id`)      |
| Chat Gateway → Runtime | outbound  | **JWT_PASSTHROUGH** | same token forwarded unchanged                  |
| Runtime (inbound)      | inbound   | **CUSTOM_JWT**      | re-validates token; `allowedClients=[SPA, M2M]` |
| Runtime → Bedrock      | outbound  | **SigV4**           | runtime execution-role IAM creds                |

> The bug we fixed: the runtime was silently still **IAM/SigV4** inbound (the
> in-place CDK auth change no-op'd because runtime auth is immutable), so the
> forwarded JWT was rejected with `403 Authorization method mismatch`. The clean
> redeploy creates the runtime as **CUSTOM_JWT** from the start.

## 2.Invocation Flow: MCP clients (Claude Code / VSCode / Cursor)

Same runtimes + Bedrock, reached over MCP instead of the chat SSE path:

```
Claude Code ──MCP OAuth (PKCE, bearer)──▶ HTTP API + mcp-proxy Lambda
                                             │ forwards bearer (gateway scope injected)
                                             ▼
                          AgentCore MCP Gateway (CUSTOM_JWT: PKCE client + invoke scope)
                                             │ GATEWAY_IAM_ROLE (SigV4)
                                             ▼
                          mcp-tools Lambda ── INPUT guardrail
                                             │ HTTPS + M2M OAuth Bearer (client_credentials)
                                             ▼
                          Agent on Runtime (CUSTOM_JWT) ──SigV4──▶ Bedrock LLM
```

| Hop                            | Auth mode            | Credential                                           |
| ------------------------------ | -------------------- | ---------------------------------------------------- |
| MCP client → proxy             | MCP OAuth (PKCE)     | user **access token** (PKCE 3LO client)              |
| proxy → MCP gateway            | CUSTOM_JWT           | forwarded bearer + `semantic-layer-mcp/invoke` scope |
| MCP gateway → mcp-tools Lambda | GATEWAY_IAM_ROLE     | gateway service role (SigV4)                         |
| mcp-tools → Runtime            | **M2M OAuth Bearer** | Cognito **client_credentials** token (no user)       |
| Runtime → Bedrock              | SigV4                | runtime execution-role IAM creds                     |

## 3.Invocation Flow: another AgentCore Gateway (MCP `mcp_server` target)

_Another_ project's chat agent can surface the query tools by registering the
MCP gateway as a remote `mcp_server` target on ITS own AgentCore Gateway. On the
outbound hop, that gateway's **AgentCore Identity** runs an **OAuth2 credential
provider** with the **authorization-code (3LO) grant, per-user** against our
Cognito pool — which requires a **confidential client** (the `agentcoreClient`,
`generateSecret: true`; the public PKCE client is NOT reusable here). The minted
per-user access token carries `semantic-layer-mcp/invoke` and is forwarded into
the SAME MCP gateway → mcp-tools → runtime chain as the Claude Code path:

```
Other project's chat agent (Strands, on its own AgentCore Runtime)
        │ tool call routed to its "semantic-layer" mcp_server target
        ▼
Its AgentCore Gateway ── AgentCore Identity OAuth2 credential provider
        │                  grant = AUTHORIZATION_CODE (per-user, 3LO)
        │                  client = agentcoreClient (confidential, id+secret)
        │                  endpoints = OUR Cognito /oauth2/authorize + /oauth2/token
        │                  scope = semantic-layer-mcp/invoke
        │                  1st call → browser consent → Token Vault caches the
        │                  per-user refresh token (later calls skip the popup)
        │ Bearer <user's semantic-layer Cognito access token>
        │ → OUR gateway /mcp URL directly (NOT the Claude Code OAuth proxy)
        ▼
AgentCore MCP Gateway (CUSTOM_JWT: validates client_id + invoke scope)
        │ GATEWAY_IAM_ROLE (SigV4)
        ▼
mcp-tools Lambda ── INPUT guardrail
        │ HTTPS + M2M OAuth Bearer (client_credentials)
        ▼
Agent on Runtime (CUSTOM_JWT) ──SigV4──▶ Bedrock LLM
```

| Hop                             | Auth mode            | Credential                                                                         |
| ------------------------------- | -------------------- | ---------------------------------------------------------------------------------- |
| Other gateway → our MCP gateway | CUSTOM_JWT           | per-user **access token** from **auth-code 3LO** (`agentcoreClient`, confidential) |
| MCP gateway → mcp-tools Lambda  | GATEWAY_IAM_ROLE     | gateway service role (SigV4)                                                       |
| mcp-tools → Runtime             | **M2M OAuth Bearer** | Cognito **client_credentials** token (no user)                                     |
| Runtime → Bedrock               | SigV4                | runtime execution-role IAM creds                                                   |

> **Why a _separate_ client from the PKCE one:** AgentCore Identity's credential
> provider needs a **confidential** client (client secret) — whereas the Claude Code / VSCode path uses
> the **public** PKCE client (no secret) and goes through the MCP OAuth _proxy_.
> Both ride the auth-code grant and the same `invoke` scope; they differ in
> client confidentiality and in endpoint (this path hits our Cognito + gateway
> `/mcp` **directly**, bypassing the proxy). The secret is mirrored to Secrets
> Manager at `/${resourcePrefix}/agentcore-client-secret`; the callback
> (`oauth-complete`) is a placeholder until the consuming project's credential
> provider mints the real AgentCore return URL — registered on `agentcoreClient`
> in setup Phase 3. Per-user RBAC is preserved end-to-end (no M2M token swap).

## Identity primitives (Cognito, one pool)

- **SPA client** — browser chat user token (hop 1).
- **PKCE 3LO client** — Claude Code / VSCode / Cursor login (public, no secret; MCP path).
- **Auth-code 3LO client** (`agentcoreClient`) — confidential client (with secret) for
  another AgentCore Gateway's OAuth2 credential provider to invoke us as an
  `mcp_server` target.
- **M2M client** — backend service-to-runtime calls (mcp-tools, REST jobs).
- **Resource server** `semantic-layer-mcp` → scope `semantic-layer-mcp/invoke`.
