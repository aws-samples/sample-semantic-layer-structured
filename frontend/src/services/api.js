import axios from "axios";

const API_BASE_URL = process.env.REACT_APP_API_URL || "/api";

// Base URL of the AgentCore chat Gateway (injected at build time). Streaming
// chat goes directly through this gateway — it is the only chat transport (the
// legacy /query/chat proxy was removed). Empty only pre-deploy / local dev,
// where streaming chat is unavailable until the gateway URL is wired.
const CHAT_GATEWAY_URL = process.env.REACT_APP_CHAT_GATEWAY_URL || "";

// Static map from chat mode to the Gateway target name. Target names are fixed
// by the CDK chat-gateway definition.
const CHAT_TARGETS = {
  "semantic-rag": "metadata-query",
  vkg: "ontology-query",
};

// Timeout configurations (in milliseconds)
const TIMEOUTS = {
  DEFAULT: 30000, // 30 seconds for standard requests
  LONG_RUNNING: 120000, // 2 minutes for ontology builds and queries
  UPLOAD: 60000, // 1 minute for file uploads
};

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: TIMEOUTS.DEFAULT,
  headers: {
    "Content-Type": "application/json",
  },
});

// Request interceptor
apiClient.interceptors.request.use(
  (config) => {
    // Add auth token if available
    const token = localStorage.getItem("authToken");
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  },
);

// Response interceptor
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    // Token expired — attempt a silent token refresh first (Amplify v6).
    // If refresh succeeds, retry the original request transparently.
    // If refresh fails (refresh token also expired), fire the auth-expired
    // event so App.js can sign the user out without a direct state reference.
    if (error.response?.status === 401) {
      try {
        const { fetchAuthSession } = await import("aws-amplify/auth");
        const session = await fetchAuthSession({ forceRefresh: true });
        const newToken = session?.tokens?.idToken?.toString();
        // Keep the chat-gateway access token in sync on refresh (see streamChat).
        const newAccessToken = session?.tokens?.accessToken?.toString();
        if (newAccessToken) {
          localStorage.setItem("chatGatewayToken", newAccessToken);
        }
        if (newToken) {
          localStorage.setItem("authToken", newToken);
          // Retry the original request once with the refreshed token
          const retryConfig = { ...error.config };
          retryConfig.headers = {
            ...retryConfig.headers,
            Authorization: `Bearer ${newToken}`,
          };
          retryConfig._retried = true; // Prevent infinite retry loop
          if (!error.config._retried) {
            return apiClient(retryConfig);
          }
        }
      } catch (_refreshErr) {
        // Refresh token also expired — force sign out
      }
      window.dispatchEvent(new CustomEvent("auth-expired"));
    }
    // Enhanced error handling
    if (error.code === "ECONNABORTED" && error.message.includes("timeout")) {
      console.error("API Request Timeout:", error.config?.url);
      error.isTimeout = true;
      error.userMessage =
        "Request timed out. The operation is taking longer than expected. Please try again.";
    } else if (error.response) {
      console.error("API Error:", error.response.status, error.response.data);
      error.userMessage =
        error.response.data?.message ||
        "An error occurred processing your request.";
    } else if (error.request) {
      console.error("API No Response:", error.message);
      error.userMessage =
        "Unable to connect to server. Please check your connection.";
    } else {
      console.error("API Error:", error.message);
      error.userMessage = "An unexpected error occurred.";
    }
    return Promise.reject(error);
  },
);

// Helper function to handle API responses
const handleResponse = (promise) => {
  return promise
    .then((response) => ({
      success: true,
      data: response.data,
    }))
    .catch((error) => ({
      success: false,
      error: error.userMessage || error.message,
      details: error.response?.data,
    }));
};

// ============================================================================
// ONTOLOGY APIs
// ============================================================================
export const ontologyAPI = {
  // Create or update ontology configuration
  createOntologyConfig: async (data) => {
    return handleResponse(apiClient.post("/ontology/config", data));
  },

  // Get ontology configuration
  getOntologyConfig: async (id) => {
    return handleResponse(apiClient.get(`/ontology/config/${id}`));
  },

  // List all ontologies
  listOntologies: async () => {
    return handleResponse(apiClient.get("/ontology/list"));
  },

  // Build ontology (trigger generation)
  buildOntology: async (id) => {
    return handleResponse(
      apiClient.post(
        `/ontology/build/${id}`,
        {},
        {
          timeout: TIMEOUTS.LONG_RUNNING,
        },
      ),
    );
  },

  // Get build status
  getBuildStatus: async (id) => {
    return handleResponse(apiClient.get(`/ontology/build-status/${id}`));
  },

  // Fetch N-Quads content for a specific version
  getOntologyContent: async (id, version) => {
    return handleResponse(apiClient.get(`/ontology/content/${id}/${version}`));
  },

  // List all versions for an ontology (sorted newest first)
  getOntologyVersions: async (id) => {
    return handleResponse(apiClient.get(`/ontology/versions/${id}`));
  },

  // Submit annotation-driven revision from a base version
  reviseOntology: async (id, baseVersion, annotations) => {
    return handleResponse(
      apiClient.post(
        `/ontology/revise/${id}/${baseVersion}`,
        { annotations },
        { timeout: TIMEOUTS.LONG_RUNNING },
      ),
    );
  },

  // Upload ontology file (markdown)
  uploadOntologyFile: async (file, id) => {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("id", id);

    return handleResponse(
      apiClient.post("/ontology/upload", formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: TIMEOUTS.UPLOAD,
      }),
    );
  },

  // Delete ontology
  deleteOntology: async (id) => {
    return handleResponse(apiClient.delete(`/ontology/config/${id}`));
  },
};

// ============================================================================
// DATA SOURCE APIs
// ============================================================================
export const dataSourceAPI = {
  // List Glue databases
  listGlueDatabases: async () => {
    return handleResponse(apiClient.get("/datasource/glue/databases"));
  },

  // List Glue tables in a database
  // catalogId is optional; omit for AWSDataCatalog (e.g. pass 's3tablescatalog/<bucket>' for S3 Tables)
  listGlueTables: async (databaseName, catalogId) => {
    const params = catalogId
      ? `?catalogId=${encodeURIComponent(catalogId)}`
      : "";
    return handleResponse(
      apiClient.get(`/datasource/glue/tables/${databaseName}${params}`),
    );
  },

  // Get table metadata
  getTableMetadata: async (databaseName, tableName, catalogId) => {
    const params = catalogId
      ? `?catalogId=${encodeURIComponent(catalogId)}`
      : "";
    return handleResponse(
      apiClient.get(
        `/datasource/glue/metadata/${databaseName}/${tableName}${params}`,
      ),
    );
  },

  // Extract metadata for selected data sources
  extractMetadata: async (dataSources) => {
    return handleResponse(
      apiClient.post("/datasource/extract-metadata", { dataSources }),
    );
  },

  // Start Glue crawler
  startCrawler: async (crawlerName) => {
    return handleResponse(
      apiClient.post(`/datasource/glue/crawler/${crawlerName}/start`),
    );
  },

  // Get crawler status
  getCrawlerStatus: async (crawlerName) => {
    return handleResponse(
      apiClient.get(`/datasource/glue/crawler/${crawlerName}/status`),
    );
  },
};

// ============================================================================
// QUERY APIs
// ============================================================================
export const queryAPI = {
  // Get AI-generated suggested questions for a semantic metadata layer.
  // Returns { suggestions: [{ category: string, question: string }] }
  getSuggestedQuestions: async (ontologyId) => {
    return handleResponse(
      apiClient.get(`/query/suggestions/${ontologyId}`, {
        timeout: TIMEOUTS.LONG_RUNNING,
      }),
    );
  },

  // Submit user feedback (👍/👎 + optional comment) for one assistant turn.
  // The backend persists this into the per-ontology DynamoDB feedback table,
  // PII-redacted via Bedrock Guardrails — surfaced in the admin Feedback tab.
  submitFeedback: async ({
    sessionId,
    ontologyId,
    turnId,
    rating,
    comment = "",
    question = "",
    answer = "",
  }) => {
    return handleResponse(
      apiClient.post("/query/feedback", {
        sessionId,
        ontologyId,
        turnId,
        rating,
        comment,
        question,
        answer,
      }),
    );
  },

  // ----------------------------------------------------------------------
  // AG-UI streaming chat (item #1 — frontend chat)
  // ----------------------------------------------------------------------
  //
  // Uses fetch + ReadableStream rather than EventSource because the latter
  // doesn't support POST bodies. We parse SSE-formatted chunks ourselves and
  // invoke `onEvent({type, ...payload})` for each AG-UI event.
  //
  // Returns the AbortController so the caller can cancel an in-flight stream
  // (e.g. when the user clicks "New chat" mid-turn).
  streamChat: async ({
    sessionId,
    ontologyId,
    mode,
    message,
    turnId,
    onEvent,
    onError,
    onClose,
  }) => {
    const controller = new AbortController();
    const target = CHAT_TARGETS[mode] || CHAT_TARGETS["semantic-rag"];
    // Stream directly through the AgentCore chat Gateway (Cognito bearer JWT) to
    // bypass the buffered API Gateway/Lambda path (the 30s-timeout + Mangum buffering
    // that caused HTTP 503). The gateway is the only chat transport; when its URL
    // isn't configured (local dev before deploy) streaming chat is unavailable.
    if (!CHAT_GATEWAY_URL) {
      onError?.(
        new Error(
          "Chat gateway URL is not configured (REACT_APP_CHAT_GATEWAY_URL)",
        ),
      );
      return controller;
    }
    const url = `${CHAT_GATEWAY_URL.replace(/\/$/, "")}/${target}/invocations`;

    // The gateway's CUSTOM_JWT authorizer matches `allowedClients` against the
    // token's `client_id` claim, which only the Cognito ACCESS token carries
    // (the ID token uses `aud`), so the gateway path uses the access token.
    //
    // This path uses raw fetch (not the axios apiClient), so the axios response
    // interceptor's silent token refresh never runs here. Mint the token via
    // fetchAuthSession() at send time instead of reading the cached
    // `chatGatewayToken`: Amplify transparently refreshes the access token when
    // it is near/over its 1h expiry, so a follow-up turn sent after a pause
    // (e.g. picking a clarification option) carries a valid token rather than a
    // stale one the gateway would reject with 403.
    const freshAccessToken = async ({ forceRefresh = false } = {}) => {
      const { fetchAuthSession } = await import("aws-amplify/auth");
      const session = await fetchAuthSession({ forceRefresh });
      const accessToken = session?.tokens?.accessToken?.toString();
      if (accessToken) {
        // Keep the cache in sync for any other reader of chatGatewayToken.
        localStorage.setItem("chatGatewayToken", accessToken);
      }
      return accessToken;
    };

    const sendRequest = (token) => {
      const headers = {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      };
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }
      return fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({
          sessionId,
          ontologyId,
          mode,
          message,
          turnId,
        }),
        signal: controller.signal,
      });
    };

    // Fire and forget — caller handles events through callbacks.
    (async () => {
      try {
        let token = await freshAccessToken();
        let response = await sendRequest(token);
        // A 401/403 here means the token was rejected at the gateway auth edge
        // (expired/invalid). Force a refresh and replay the request once before
        // surfacing the error.
        if (response.status === 401 || response.status === 403) {
          token = await freshAccessToken({ forceRefresh: true });
          response = await sendRequest(token);
        }
        if (!response.ok) {
          throw new Error(`chat request failed: HTTP ${response.status}`);
        }
        if (!response.body) {
          throw new Error("chat response has no body");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        // SSE records are separated by a blank line. We split on \n\n,
        // accumulate partials in `buffer`, and parse each completed record.
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let recordEnd = buffer.indexOf("\n\n");
          while (recordEnd !== -1) {
            const record = buffer.slice(0, recordEnd);
            buffer = buffer.slice(recordEnd + 2);
            const evt = parseSseRecord(record);
            if (evt && onEvent) {
              onEvent(evt);
            }
            recordEnd = buffer.indexOf("\n\n");
          }
        }
        if (onClose) onClose();
      } catch (err) {
        // Aborted streams are not errors from the caller's perspective.
        if (err.name === "AbortError") {
          if (onClose) onClose();
          return;
        }
        if (onError) onError(err);
      }
    })();

    return controller;
  },
};

// Parse a single SSE record (the lines between blank-line separators).
// Returns the JSON-decoded data payload merged with the event type, or null
// if the record is malformed.
function parseSseRecord(record) {
  if (!record) return null;
  let eventType = "message";
  const dataLines = [];
  for (const line of record.split("\n")) {
    if (line.startsWith("event: ")) {
      eventType = line.slice(7).trim();
    } else if (line.startsWith("data: ")) {
      dataLines.push(line.slice(6));
    }
  }
  if (dataLines.length === 0) return null;
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    return { type: eventType, ...parsed };
  } catch (e) {
    return null;
  }
}

export const chatAPI = {
  // Restore a session's transcript on page refresh.
  getSession: async (sessionId) => {
    return handleResponse(apiClient.get(`/query/sessions/${sessionId}`));
  },

  // List the caller's recent sessions (chat-first sidebar).
  // Returns {sessions: [{sessionId, ontologyId, ontologyName, mode, title,
  // updatedAt, createdAt}], nextCursor}.
  listSessions: async ({ limit = 50, cursor } = {}) => {
    const params = { limit };
    if (cursor) params.cursor = cursor;
    return handleResponse(apiClient.get(`/query/sessions`, { params }));
  },

  // Soft-delete a session (sidebar "x" action). Server flips archived=true.
  deleteSession: async (sessionId) => {
    return handleResponse(apiClient.delete(`/query/sessions/${sessionId}`));
  },

  // Soft-delete ALL of the caller's sessions at once (sidebar "Clear all").
  // Server archives every session owned by the authenticated principal.
  deleteAllSessions: async () => {
    return handleResponse(apiClient.delete(`/query/sessions`));
  },
};

// ============================================================================
// FEEDBACK API — per-turn 👍/👎 rows backing the admin "Feedback" tab.
// ============================================================================

export const feedbackAPI = {
  // List feedback rows for one ontology (newest-first).
  list: async (ontologyId, { limit = 50 } = {}) => {
    return handleResponse(
      apiClient.get(`/feedback/${encodeURIComponent(ontologyId)}`, {
        params: { limit },
      }),
    );
  },

  // Delete one feedback row by feedbackId.
  remove: async (ontologyId, feedbackId) => {
    return handleResponse(
      apiClient.delete(
        `/feedback/${encodeURIComponent(ontologyId)}/${encodeURIComponent(feedbackId)}`,
      ),
    );
  },
};

// ============================================================================
// LESSONS-LEARNED API (item #2 — AgentCore Memory long-term records)
// ============================================================================

export const lessonsAPI = {
  // List long-term memory records for one ontology. Returns an array of
  // {memoryRecordId, content, namespaces, createdAt}. Empty when the
  // backend hasn't been wired with a memory id yet.
  list: async (ontologyId, { limit = 50 } = {}) => {
    return handleResponse(
      apiClient.get(`/lessons/${encodeURIComponent(ontologyId)}`, {
        params: { limit },
      }),
    );
  },

  // Delete one record. recordId is the memoryRecordId returned by list().
  remove: async (ontologyId, recordId) => {
    return handleResponse(
      apiClient.delete(
        `/lessons/${encodeURIComponent(ontologyId)}/${encodeURIComponent(recordId)}`,
      ),
    );
  },
};

// ============================================================================
// SUPPLEMENTARY DOCUMENTS API (item #3 — creation-time doc pipeline)
// ============================================================================

export const documentsAPI = {
  // Upload one supplementary doc (PDF / Markdown / DOCX / text).
  upload: async (ontologyId, file) => {
    const formData = new FormData();
    formData.append("file", file);
    return handleResponse(
      apiClient.post(`/documents/${ontologyId}/upload`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: TIMEOUTS.UPLOAD,
      }),
    );
  },

  // List uploaded docs for one ontology.
  list: async (ontologyId) => {
    return handleResponse(apiClient.get(`/documents/${ontologyId}`));
  },

  // Per-doc status (used by the upload page for progress polling).
  get: async (ontologyId, docId) => {
    return handleResponse(apiClient.get(`/documents/${ontologyId}/${docId}`));
  },

  // Cascade-delete one doc (S3 raw + DDB status row).
  delete: async (ontologyId, docId) => {
    return handleResponse(
      apiClient.delete(`/documents/${ontologyId}/${docId}`),
    );
  },
};

// ============================================================================
// GROUNDTRUTH DATASET API — per-semantic-layer eval dataset (admin tab).
// ============================================================================
//
// Backs the admin "Ground truth dataset" tab. The dataset is the AgentCore
// ground-truth evaluation format: a JSON array of records, each with
// Natural_Language_Question / Expected_Answer / Expected_SQL_Query /
// Expected_SQL_Result.
export const groundtruthAPI = {
  // Upload a dataset JSON file for one semantic layer.
  upload: async (ontologyId, file) => {
    const formData = new FormData();
    formData.append("file", file);
    return handleResponse(
      apiClient.post(`/groundtruth/${ontologyId}/upload`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: TIMEOUTS.UPLOAD,
      }),
    );
  },

  // Fetch the stored dataset (envelope {ontologyId, recordCount, uploadedAt,
  // records}). Returns recordCount:0 / records:[] when none uploaded yet.
  get: async (ontologyId) => {
    return handleResponse(apiClient.get(`/groundtruth/${ontologyId}`));
  },

  // Delete the stored dataset for one semantic layer.
  delete: async (ontologyId) => {
    return handleResponse(apiClient.delete(`/groundtruth/${ontologyId}`));
  },
};

// ============================================================================
// EVALUATIONS API — OnDemand eval-pipeline runs per semantic layer (admin tab).
// ============================================================================
//
// Backs the admin "Evaluations" tab. Runs are produced by the eval-runner
// triggered when a layer version completes (evaluation.requested event), and
// capture per-question accuracy / latency / token metrics + a roll-up summary.
export const evaluationsAPI = {
  // List run summaries (no per-question rows) for one layer, newest first.
  listRuns: async (ontologyId) => {
    return handleResponse(apiClient.get(`/evaluations/${ontologyId}`));
  },

  // Fetch one full run (incl. per-question metric rows).
  getRun: async (ontologyId, runId) => {
    return handleResponse(apiClient.get(`/evaluations/${ontologyId}/${runId}`));
  },

  // Delete one stored run.
  deleteRun: async (ontologyId, runId) => {
    return handleResponse(
      apiClient.delete(`/evaluations/${ontologyId}/${runId}`),
    );
  },
};

// ============================================================================
// GOVERNED METRICS API — Tier 1 maintained metrics per semantic layer (admin tab).
// ============================================================================
//
// Backs the admin "Governed Metrics" tab. A governed metric is a curated
// name/description/synonyms + pre-validated compiled_sql row; once PUBLISHED it
// is embedded (Titan v2) and KNN-matched (cosine ≥ 0.85) against the user's
// question by the Tier 1 lookup BEFORE the Tier 2 Strands graph runs.
//
// Routes are namespace-scoped (`/metrics/namespaces/{ns}/metrics`). The query
// agents resolve their Tier 1 namespace as `config.namespace || id`, and the
// ontology config has no `namespace` field — so the effective namespace is
// always the semantic-layer id. Every call here passes the layer id as `ns`,
// which guarantees a published metric is discoverable by Tier 1 for that layer.
//
// The router mounts only when METRICS_TABLE is set on the REST API Lambda; when
// it is unset these calls 404 and the tab surfaces an informational empty state.
export const metricsAPI = {
  // List every metric (any lifecycle) for one layer.
  list: async (ns) => {
    return handleResponse(
      apiClient.get(`/metrics/namespaces/${encodeURIComponent(ns)}/metrics`),
    );
  },

  // Fetch one metric by metric_id.
  get: async (ns, metricId) => {
    return handleResponse(
      apiClient.get(
        `/metrics/namespaces/${encodeURIComponent(ns)}/metrics/${encodeURIComponent(metricId)}`,
      ),
    );
  },

  // Create a metric. `metric` must carry `namespace === ns` (the backend 400s
  // on mismatch). SQL is validated SELECT-only with sqlglot server-side.
  create: async (ns, metric) => {
    return handleResponse(
      apiClient.post(
        `/metrics/namespaces/${encodeURIComponent(ns)}/metrics`,
        metric,
      ),
    );
  },

  // Replace a metric (bumps version server-side). Identifiers in the body must
  // match the path (`namespace === ns`, `metric_id === metricId`).
  update: async (ns, metricId, metric) => {
    return handleResponse(
      apiClient.put(
        `/metrics/namespaces/${encodeURIComponent(ns)}/metrics/${encodeURIComponent(metricId)}`,
        metric,
      ),
    );
  },

  // Publish a metric — flips lifecycle to PUBLISHED, embeds it, and makes it
  // live for the Tier 1 lookup. Path uses the gRPC-style `{id}:publish` verb.
  publish: async (ns, metricId) => {
    return handleResponse(
      apiClient.post(
        `/metrics/namespaces/${encodeURIComponent(ns)}/metrics/${encodeURIComponent(metricId)}:publish`,
      ),
    );
  },

  // Delete a metric (idempotent server-side).
  remove: async (ns, metricId) => {
    return handleResponse(
      apiClient.delete(
        `/metrics/namespaces/${encodeURIComponent(ns)}/metrics/${encodeURIComponent(metricId)}`,
      ),
    );
  },
};

// ============================================================================
// METADATA APIs
// ============================================================================

// Start metadata enrichment — pass the ontology config ID and optional enrichment parameters.
// options may contain targetTables and/or annotations.
// The backend reads dataSources (tables + catalogId) from DynamoDB and uses it as the job ID.
export const startMetadataEnrichment = async (id, options = {}) => {
  return handleResponse(
    apiClient.post(
      "/metadata/enrich",
      { id, ...options },
      { timeout: TIMEOUTS.LONG_RUNNING },
    ),
  );
};

// Start a versioned metadata revision — stamps the active record with revisionMode=True,
// invokes the metadata agent on the annotation path, and returns {nextVersion}.
// Call this instead of startMetadataEnrichment when the user submits annotations.
export const reviseMetadata = async (id, baseVersion, annotations) => {
  return handleResponse(
    apiClient.post(
      `/metadata/revise/${id}/${baseVersion}`,
      { annotations },
      { timeout: TIMEOUTS.LONG_RUNNING },
    ),
  );
};

// Get metadata enrichment status
export const getMetadataEnrichmentStatus = async (jobId) => {
  return handleResponse(apiClient.get(`/metadata/enrich/status/${jobId}`));
};

// Poll metadata enrichment status until complete
export const pollMetadataEnrichmentStatus = async (
  jobId,
  onProgress,
  intervalMs = 3000,
) => {
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const result = await getMetadataEnrichmentStatus(jobId);
        if (!result.success) {
          return reject(
            new Error(result.error || "Failed to get enrichment status"),
          );
        }

        const data = result.data;
        if (onProgress) onProgress(data);

        if (data.status === "completed") {
          return resolve(data);
        }
        if (data.status === "failed") {
          return reject(new Error(data.error || "Enrichment failed"));
        }

        setTimeout(poll, intervalMs);
      } catch (err) {
        reject(err);
      }
    };
    poll();
  });
};

// Get AI-enriched metadata for a single table from the Knowledge Base (S3 doc).
// All four scoping fields are required — the metadata agent writes documents at
//   metadata/{semantic_layer_id}/{semantic_layer_version}/{catalog_id}/{db}/{table}.md
export const getTableKBMetadata = async (
  databaseName,
  tableName,
  catalogId,
  semanticLayerId,
  semanticLayerVersion,
) => {
  const params = new URLSearchParams({
    catalog_id: catalogId,
    semantic_layer_id: semanticLayerId,
    semantic_layer_version: semanticLayerVersion,
  }).toString();
  return handleResponse(
    apiClient.get(
      `/metadata/table/${encodeURIComponent(databaseName)}/${encodeURIComponent(tableName)}?${params}`,
    ),
  );
};

// ============================================================================
// NEPTUNE APIs
// ============================================================================
export const neptuneAPI = {
  // Execute SPARQL query
  executeSPARQL: async (query) => {
    return handleResponse(apiClient.post("/neptune/sparql", { query }));
  },

  // Get knowledge graph summary
  getGraphSummary: async (id) => {
    return handleResponse(apiClient.get(`/neptune/graph/summary/${id}`));
  },

  // Get graph statistics
  getGraphStats: async (id) => {
    return handleResponse(apiClient.get(`/neptune/graph/stats/${id}`));
  },

  // Load ontology to Neptune
  loadOntology: async (s3Path, format = "turtle") => {
    return handleResponse(
      apiClient.post(
        "/neptune/load",
        { s3Path, format },
        {
          timeout: TIMEOUTS.LONG_RUNNING,
        },
      ),
    );
  },

  // Get load status
  getLoadStatus: async (loadId) => {
    return handleResponse(apiClient.get(`/neptune/load/status/${loadId}`));
  },

  // Clear graph data
  clearGraph: async (id) => {
    return handleResponse(apiClient.delete(`/neptune/graph/${id}`));
  },
};

// ============================================================================
// HEALTH & MONITORING APIs
// ============================================================================
export const healthAPI = {
  // Check API health
  checkHealth: async () => {
    return handleResponse(apiClient.get("/health"));
  },

  // Get system status
  getSystemStatus: async () => {
    return handleResponse(apiClient.get("/status"));
  },
};

// Export the configured axios client for custom requests
export { apiClient };

// Export default object with all APIs. Named before exporting so the default
// has a stable identifier (better stack traces / DevTools display, and
// satisfies eslint import/no-anonymous-default-export).
const api = {
  ontologyAPI,
  dataSourceAPI,
  queryAPI,
  neptuneAPI,
  healthAPI,
};

export default api;
