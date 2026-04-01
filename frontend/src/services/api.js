import axios from 'axios';

const API_BASE_URL = process.env.REACT_APP_API_URL || '/api';

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
    'Content-Type': 'application/json',
  },
});

// Request interceptor
apiClient.interceptors.request.use(
  (config) => {
    // Add auth token if available
    const token = localStorage.getItem('authToken');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
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
        const { fetchAuthSession } = await import('aws-amplify/auth');
        const session = await fetchAuthSession({ forceRefresh: true });
        const newToken = session?.tokens?.idToken?.toString();
        if (newToken) {
          localStorage.setItem('authToken', newToken);
          // Retry the original request once with the refreshed token
          const retryConfig = { ...error.config };
          retryConfig.headers = { ...retryConfig.headers, Authorization: `Bearer ${newToken}` };
          retryConfig._retried = true; // Prevent infinite retry loop
          if (!error.config._retried) {
            return apiClient(retryConfig);
          }
        }
      } catch (_refreshErr) {
        // Refresh token also expired — force sign out
      }
      window.dispatchEvent(new CustomEvent('auth-expired'));
    }
    // Enhanced error handling
    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      console.error('API Request Timeout:', error.config?.url);
      error.isTimeout = true;
      error.userMessage =
        'Request timed out. The operation is taking longer than expected. Please try again.';
    } else if (error.response) {
      console.error('API Error:', error.response.status, error.response.data);
      error.userMessage =
        error.response.data?.message || 'An error occurred processing your request.';
    } else if (error.request) {
      console.error('API No Response:', error.message);
      error.userMessage =
        'Unable to connect to server. Please check your connection.';
    } else {
      console.error('API Error:', error.message);
      error.userMessage = 'An unexpected error occurred.';
    }
    return Promise.reject(error);
  }
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
    return handleResponse(apiClient.post('/ontology/config', data));
  },

  // Get ontology configuration
  getOntologyConfig: async (id) => {
    return handleResponse(apiClient.get(`/ontology/config/${id}`));
  },

  // List all ontologies
  listOntologies: async () => {
    return handleResponse(apiClient.get('/ontology/list'));
  },

  // Build ontology (trigger generation)
  buildOntology: async (id) => {
    return handleResponse(
      apiClient.post(`/ontology/build/${id}`, {}, {
        timeout: TIMEOUTS.LONG_RUNNING,
      })
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
      apiClient.post(`/ontology/revise/${id}/${baseVersion}`,
        { annotations },
        { timeout: TIMEOUTS.LONG_RUNNING }
      )
    );
  },

  // Upload ontology file (markdown)
  uploadOntologyFile: async (file, id) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('id', id);

    return handleResponse(
      apiClient.post('/ontology/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: TIMEOUTS.UPLOAD,
      })
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
    return handleResponse(apiClient.get('/datasource/glue/databases'));
  },

  // List Glue tables in a database
  // catalogId is optional; omit for AWSDataCatalog (e.g. pass 's3tablescatalog/<bucket>' for S3 Tables)
  listGlueTables: async (databaseName, catalogId) => {
    const params = catalogId ? `?catalogId=${encodeURIComponent(catalogId)}` : '';
    return handleResponse(apiClient.get(`/datasource/glue/tables/${databaseName}${params}`));
  },

  // Get table metadata
  getTableMetadata: async (databaseName, tableName, catalogId) => {
    const params = catalogId ? `?catalogId=${encodeURIComponent(catalogId)}` : '';
    return handleResponse(
      apiClient.get(`/datasource/glue/metadata/${databaseName}/${tableName}${params}`)
    );
  },

  // Extract metadata for selected data sources
  extractMetadata: async (dataSources) => {
    return handleResponse(
      apiClient.post('/datasource/extract-metadata', { dataSources })
    );
  },

  // Start Glue crawler
  startCrawler: async (crawlerName) => {
    return handleResponse(
      apiClient.post(`/datasource/glue/crawler/${crawlerName}/start`)
    );
  },

  // Get crawler status
  getCrawlerStatus: async (crawlerName) => {
    return handleResponse(
      apiClient.get(`/datasource/glue/crawler/${crawlerName}/status`)
    );
  },
};

// ============================================================================
// QUERY APIs
// ============================================================================
export const queryAPI = {
  // Submit natural language query (returns immediately with queryId; poll for result)
  submitQuery: async (question, id) => {
    return handleResponse(
      apiClient.post('/query/submit', { question, id: id }, {
        timeout: TIMEOUTS.DEFAULT,
      })
    );
  },

  // Get query result
  getQueryResult: async (queryId) => {
    return handleResponse(apiClient.get(`/query/result/${queryId}`));
  },

  // Get query status
  getQueryStatus: async (queryId) => {
    return handleResponse(apiClient.get(`/query/status/${queryId}`));
  },

  // Poll query until complete
  pollQueryUntilComplete: async (queryId, maxAttempts = 60, intervalMs = 2000) => {
    let attempts = 0;

    while (attempts < maxAttempts) {
      const result = await queryAPI.getQueryStatus(queryId);

      if (!result.success) {
        return result;
      }

      const status = result.data.status;

      if (status === 'COMPLETED' || status === 'SUCCEEDED' || status === 'NEEDS_CLARIFICATION') {
        // Get full result (includes clarification payload when NEEDS_CLARIFICATION)
        return await queryAPI.getQueryResult(queryId);
      } else if (status === 'FAILED' || status === 'ERROR' || status === 'BLOCKED') {
        return {
          success: false,
          error: result.data.error || 'Query failed',
        };
      }

      // Wait before next poll
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      attempts++;
    }

    return {
      success: false,
      error: 'Query timeout - exceeded maximum wait time',
    };
  },

  // Get AI-generated suggested questions for a semantic metadata layer.
  // Returns { suggestions: [{ category: string, question: string }] }
  getSuggestedQuestions: async (ontologyId) => {
    return handleResponse(
      apiClient.get(`/query/suggestions/${ontologyId}`, {
        timeout: TIMEOUTS.LONG_RUNNING,
      })
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
    apiClient.post('/metadata/enrich', { id, ...options }, { timeout: TIMEOUTS.LONG_RUNNING })
  );
};

// Start a versioned metadata revision — stamps the active record with revisionMode=True,
// invokes the metadata agent on the annotation path, and returns {nextVersion}.
// Call this instead of startMetadataEnrichment when the user submits annotations.
export const reviseMetadata = async (id, baseVersion, annotations) => {
  return handleResponse(
    apiClient.post(`/metadata/revise/${id}/${baseVersion}`, { annotations }, { timeout: TIMEOUTS.LONG_RUNNING })
  );
};

// Get metadata enrichment status
export const getMetadataEnrichmentStatus = async (jobId) => {
  return handleResponse(apiClient.get(`/metadata/enrich/status/${jobId}`));
};

// Poll metadata enrichment status until complete
export const pollMetadataEnrichmentStatus = async (jobId, onProgress, intervalMs = 3000) => {
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const result = await getMetadataEnrichmentStatus(jobId);
        if (!result.success) {
          return reject(new Error(result.error || 'Failed to get enrichment status'));
        }

        const data = result.data;
        if (onProgress) onProgress(data);

        if (data.status === 'completed') {
          return resolve(data);
        }
        if (data.status === 'failed') {
          return reject(new Error(data.error || 'Enrichment failed'));
        }

        setTimeout(poll, intervalMs);
      } catch (err) {
        reject(err);
      }
    };
    poll();
  });
};

// Submit natural language query on metadata
// The backend resolves dataSources (tables + catalogIds) from the ontology config in DynamoDB.
export const submitMetadataQuery = async (question, id) => {
  return handleResponse(
    apiClient.post('/metadata/query/submit',
      { question, id },
      { timeout: TIMEOUTS.LONG_RUNNING }
    )
  );
};

// Get metadata query status
export const getMetadataQueryStatus = async (queryId) => {
  return handleResponse(apiClient.get(`/metadata/query/status/${queryId}`));
};

// Get metadata query result
export const getMetadataQueryResult = async (queryId) => {
  return handleResponse(apiClient.get(`/metadata/query/result/${queryId}`));
};

// Get AI-enriched metadata for a single table from the Knowledge Base (S3 doc)
export const getTableKBMetadata = async (databaseName, tableName, catalogId) => {
  const params = catalogId ? `?catalog_id=${encodeURIComponent(catalogId)}` : '';
  return handleResponse(apiClient.get(`/metadata/table/${encodeURIComponent(databaseName)}/${encodeURIComponent(tableName)}${params}`));
};

// Poll metadata query until complete and fetch result
// maxAttempts * intervalMs = total wait (60 * 3000ms = 3 minutes)
export const pollMetadataQuery = async (queryId, maxAttempts = 60, intervalMs = 3000) => {
  let attempts = 0;

  while (attempts < maxAttempts) {
    const statusResult = await getMetadataQueryStatus(queryId);

    if (!statusResult.success) {
      return { success: false, error: statusResult.error || 'Failed to get query status' };
    }

    const { status } = statusResult.data;

    if (status === 'completed') {
      return await getMetadataQueryResult(queryId);
    }

    if (status === 'failed' || status === 'blocked') {
      return { success: false, error: statusResult.data.error || 'Metadata query failed' };
    }

    if (status === 'NOT_FOUND') {
      return { success: false, error: 'Query not found — the server may have restarted' };
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    attempts++;
  }

  return { success: false, error: 'Query timed out after 3 minutes' };
};

// ============================================================================
// NEPTUNE APIs
// ============================================================================
export const neptuneAPI = {
  // Execute SPARQL query
  executeSPARQL: async (query) => {
    return handleResponse(apiClient.post('/neptune/sparql', { query }));
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
  loadOntology: async (s3Path, format = 'turtle') => {
    return handleResponse(
      apiClient.post('/neptune/load', { s3Path, format }, {
        timeout: TIMEOUTS.LONG_RUNNING,
      })
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
    return handleResponse(apiClient.get('/health'));
  },

  // Get system status
  getSystemStatus: async () => {
    return handleResponse(apiClient.get('/status'));
  },
};

// Export the configured axios client for custom requests
export { apiClient };

// Export default object with all APIs
export default {
  ontologyAPI,
  dataSourceAPI,
  queryAPI,
  neptuneAPI,
  healthAPI,
};
