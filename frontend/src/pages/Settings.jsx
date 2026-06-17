import React from "react";
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  FormField,
  Input,
  Alert,
  Link,
  CopyToClipboard,
} from "@cloudscape-design/components";

// GitHub repository for this sample.
const GITHUB_URL =
  "https://github.com/aws-samples/sample-semantic-layer-structured";

// Backend connectivity. The app talks to two complementary gateways:
//  - REST API Gateway (REACT_APP_API_URL): the primary backend — ontology,
//    data sources, sessions, feedback, metadata, and graph operations.
//  - AgentCore Chat Gateway (REACT_APP_CHAT_GATEWAY_URL): streaming chat only
//    (queryAPI.streamChat → /metadata-query|ontology-query/invocations).
// Both are injected at build time by the CDK frontend stack. The chat gateway
// is empty in local dev (streaming unavailable until deployed).
const REST_API_URL = process.env.REACT_APP_API_URL || "/api";
const CHAT_GATEWAY_URL = process.env.REACT_APP_CHAT_GATEWAY_URL || "";

// MCP endpoints. Injected at build time (CDK frontend stack wires the
// mcp-proxy / mcp-server stack outputs). The proxy URL is what an MCP client
// (Claude Code / Cursor / VS Code) connects to; it runs the OAuth flow and
// forwards to the gateway. Empty when not configured for this deployment.
const MCP_PROXY_URL = process.env.REACT_APP_MCP_PROXY_URL || "";
const MCP_GATEWAY_URL = process.env.REACT_APP_MCP_GATEWAY_URL || "";

export default function Settings({ user }) {
  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Configure your semantic layer preferences"
      >
        Settings
      </Header>

      <Container header={<Header variant="h2">User Profile</Header>}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input value={user?.email || "Not available"} disabled />
          </FormField>
          <FormField label="Username">
            <Input value={user?.username || "Not available"} disabled />
          </FormField>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="The app uses two complementary backend gateways."
          >
            Connectivity
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">
            These endpoints are managed in the backend. Contact
            your administrator for changes.
          </Alert>
          <FormField
            label="REST API Gateway"
            description="Ontology, data sources, sessions, feedback, metadata & graph operations."
          >
            <Input value={REST_API_URL} readOnly />
          </FormField>
          {CHAT_GATEWAY_URL && (
            <FormField
              label="AgentCore Chat Gateway"
              description="Real-time streaming chat (SemanticRAG / VKG query agents)."
            >
              <Input value={CHAT_GATEWAY_URL} readOnly />
            </FormField>
          )}
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Connect an MCP client (Claude Code, Cursor, VS Code) to query your semantic layers as tools."
          >
            MCP Server
          </Header>
        }
      >
        <SpaceBetween size="m">
          {MCP_PROXY_URL ? (
            <>
              <FormField
                label="MCP endpoint (OAuth proxy)"
                description="Point your MCP client here. On first use it runs a browser Cognito login (OAuth 2.0 + PKCE)."
              >
                <SpaceBetween direction="horizontal" size="xs">
                  <Input value={MCP_PROXY_URL} readOnly />
                  <CopyToClipboard
                    copyButtonText="Copy"
                    copyErrorText="Failed to copy"
                    copySuccessText="Copied"
                    textToCopy={MCP_PROXY_URL}
                  />
                </SpaceBetween>
              </FormField>

              <FormField
                label="Add to Claude Code"
                description="Run this, then /mcp in Claude Code and complete the browser login."
              >
                <SpaceBetween direction="horizontal" size="xs">
                  <Input
                    value={`claude mcp add --transport http semantic-layer ${MCP_PROXY_URL}`}
                    readOnly
                  />
                  <CopyToClipboard
                    copyButtonText="Copy"
                    copyErrorText="Failed to copy"
                    copySuccessText="Copied"
                    textToCopy={`claude mcp add --transport http semantic-layer ${MCP_PROXY_URL}`}
                  />
                </SpaceBetween>
              </FormField>

              {MCP_GATEWAY_URL && (
                <FormField
                  label="MCP Gateway (advanced)"
                  description="The AgentCore Gateway behind the proxy (CUSTOM_JWT). Most clients should use the proxy URL above."
                >
                  <SpaceBetween direction="horizontal" size="xs">
                    <Input value={MCP_GATEWAY_URL} readOnly />
                    <CopyToClipboard
                      copyButtonText="Copy"
                      copyErrorText="Failed to copy"
                      copySuccessText="Copied"
                      textToCopy={MCP_GATEWAY_URL}
                    />
                  </SpaceBetween>
                </FormField>
              )}

              <Box variant="small" color="text-body-secondary">
                Tools exposed: <strong>ListOntologies</strong> (call first to
                discover layers), <strong>OntologyQuery</strong> (VKG),{" "}
                <strong>MetadataQuery</strong> (Semantic RAG), and{" "}
                <strong>QuerySuggestions</strong>.
              </Box>
            </>
          ) : (
            <Alert type="info">
              The MCP server endpoint is not configured for this deployment.
              Retrieve the URL from the <code>semantic-layer-*-mcp-proxy</code>{" "}
              stack output (<code>McpProxyApiUrl</code>), or see the MCP server
              guide in the repository.
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">About</Header>}>
        <Box variant="p">
          <strong>AWS Semantic Layer</strong>
        </Box>
        <Box variant="p" color="text-body-secondary">
          Version 1.0.0
        </Box>
        <Box variant="p" color="text-body-secondary" margin={{ top: "s" }}>
          A unified semantic layer for querying operational and historical data
          using natural language, powered by Amazon Bedrock, Amazon Neptune, and
          AWS Glue.
        </Box>
        <Box variant="p" margin={{ top: "s" }}>
          <Link external href={GITHUB_URL}>
            View the source on GitHub
          </Link>
        </Box>
      </Container>
    </SpaceBetween>
  );
}
