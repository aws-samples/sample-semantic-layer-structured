/**
 * LandingPage — empty-state home for the chat-first redesign (2026-05-24).
 *
 * Centered "ask anything" composer; picks an ontology via a single dropdown,
 * mode (VKG vs SemanticRAG) is derived from the ontology's type, not selected
 * separately. Mints a new sessionId on first submit and routes to
 * ``/query/ask?session=<id>`` so streaming continues in ChatView. The chat
 * list lives in the global SideNavigation as of 2026-05-27.
 *
 * Design note: the composer here is intentionally NOT a stream owner. It
 * just packages (ontologyId, mode, message) into navigation state; the
 * actual AG-UI streaming is started by ChatView's useChatStream hook. This
 * keeps two stream owners from coexisting during the route transition.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  Select,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { ontologyAPI, queryAPI } from "../../services/api";
import Composer from "./Composer";

function uuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// The chat mode is determined by the picked layer's type — not user-selectable.
// VKG layers route to the ontology-query agent; SemanticRAG layers route to the
// metadata-query agent. Surfacing it as a separate dropdown lets users pick a
// nonsensical (layer, mode) pair and crashes the agent invocation.
function modeForOntologyType(type) {
  return type === "SemanticRAG" ? "semantic-rag" : "vkg";
}

function modeLabel(mode) {
  return mode === "semantic-rag" ? "Semantic RAG" : "Knowledge Graph (VKG)";
}

export default function LandingPage({ enableSemanticRag = false }) {
  const navigate = useNavigate();

  const [ontologies, setOntologies] = useState([]);
  const [loadingOntologies, setLoadingOntologies] = useState(false);
  const [selectedOntology, setSelectedOntology] = useState(null);
  const [suggestions, setSuggestions] = useState([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [error, setError] = useState(null);

  // Load completed ontologies into the dropdown. Filters out SemanticRAG
  // when the deployment-time flag disables it (matches NaturalLanguageQuery).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoadingOntologies(true);
      const result = await ontologyAPI.listOntologies();
      if (cancelled) return;
      if (!result.success) {
        setError(result.error || "Could not load ontologies");
        setLoadingOntologies(false);
        return;
      }
      const completed = (result.data?.ontologies || []).filter(
        (o) => o.status === "completed",
      );
      const opts = [];
      for (const o of completed) {
        const cfg = await ontologyAPI.getOntologyConfig(o.id);
        if (cancelled) return;
        if (cfg.success) {
          opts.push({
            label: o.name || o.id,
            value: o.id,
            type: cfg.data?.type || "VKG",
            // Carry the active version so ChatView can show it in the header
            // from the first turn (o.latestVersion comes from the list;
            // cfg.data.version is the active record's version).
            version: cfg.data?.version || o.latestVersion || "v1",
          });
        }
      }
      const filtered = enableSemanticRag
        ? opts
        : opts.filter((o) => o.type !== "SemanticRAG");
      setOntologies(filtered);
      if (filtered.length > 0) {
        setSelectedOntology(filtered[0]);
      }
      setLoadingOntologies(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [enableSemanticRag]);

  // Refresh suggestions whenever the picked ontology changes.
  useEffect(() => {
    if (!selectedOntology?.value) {
      setSuggestions([]);
      return undefined;
    }
    let cancelled = false;
    setLoadingSuggestions(true);
    (async () => {
      const result = await queryAPI.getSuggestedQuestions(
        selectedOntology.value,
      );
      if (cancelled) return;
      if (result.success && Array.isArray(result.data?.suggestions)) {
        // Cap at 3 — matches the agent's prompt + server-side cap; defensive
        // against a stale/cached response carrying more.
        setSuggestions(result.data.suggestions.slice(0, 3));
      } else {
        setSuggestions([]);
      }
      setLoadingSuggestions(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedOntology?.value]);

  const startChat = useCallback(
    (text) => {
      if (!selectedOntology?.value || !text) return;
      const sessionId = uuid();
      const mode = modeForOntologyType(selectedOntology.type);
      // ChatView reads the seed message from navigation state and dispatches
      // it on mount. Persisted via the rail thereafter.
      navigate(`/query/ask?session=${sessionId}`, {
        state: {
          ontologyId: selectedOntology.value,
          ontologyName: selectedOntology.label,
          ontologyVersion: selectedOntology.version,
          mode,
          seedMessage: text,
        },
      });
    },
    [navigate, selectedOntology],
  );

  const noOntologies = !loadingOntologies && ontologies.length === 0;

  return (
    <div style={{ display: "flex", height: "calc(100vh - 56px)" }}>
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: "24px",
          overflow: "auto",
        }}
      >
        <div style={{ width: "100%", maxWidth: "720px" }}>
          <SpaceBetween size="l">
            <Header variant="h1">What would you like to know?</Header>

            {error && <Alert type="error">{error}</Alert>}

            {noOntologies && (
              <Alert type="info" header="No semantic layers available">
                An admin must publish a semantic layer before you can start a
                chat.
              </Alert>
            )}

            {!noOntologies && (
              <Container>
                <SpaceBetween size="m">
                  <div style={{ minWidth: "260px" }}>
                    <Box variant="awsui-key-label">Semantic layer</Box>
                    <Select
                      selectedOption={selectedOntology}
                      onChange={({ detail }) =>
                        setSelectedOntology(detail.selectedOption)
                      }
                      options={ontologies}
                      placeholder={
                        loadingOntologies ? "Loading…" : "Pick a layer"
                      }
                      disabled={loadingOntologies}
                    />
                    {selectedOntology && (
                      <Box variant="small" color="text-status-inactive">
                        Mode:{" "}
                        {modeLabel(modeForOntologyType(selectedOntology.type))}
                      </Box>
                    )}
                  </div>

                  <Composer disabled={!selectedOntology} onSubmit={startChat} />

                  <Box>
                    {loadingSuggestions ? (
                      <StatusIndicator type="loading">
                        Generating suggestions…
                      </StatusIndicator>
                    ) : suggestions.length > 0 ? (
                      <SpaceBetween size="xs">
                        <Box variant="small" color="text-status-inactive">
                          Try one of these to get started:
                        </Box>
                        <SpaceBetween direction="horizontal" size="xs">
                          {suggestions.map((s, i) => (
                            <Button
                              key={`${s.category}-${i}`}
                              variant="normal"
                              onClick={() => startChat(s.question)}
                            >
                              {s.question}
                            </Button>
                          ))}
                        </SpaceBetween>
                      </SpaceBetween>
                    ) : null}
                  </Box>
                </SpaceBetween>
              </Container>
            )}
          </SpaceBetween>
        </div>
      </div>
    </div>
  );
}
