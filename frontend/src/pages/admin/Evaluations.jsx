/**
 * Evaluations — admin tab showing OnDemand evaluation-pipeline runs for one
 * semantic layer.
 *
 * Runs are produced by the eval-runner that fires when a layer version reaches
 * ``completed`` (the build agents emit an ``evaluation.requested`` EventBridge
 * event; see agents/shared/eval_trigger.py). Each run is evaluated against the
 * layer's maintained ground-truth dataset and captures per-question accuracy /
 * latency / token metrics plus a roll-up summary.
 *
 * Layout: a runs table (one row per run, newest first) and, when a run is
 * selected, a detail table of its per-question metric rows. Reads ?id=.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { useSearchParams } from "react-router-dom";
import { evaluationsAPI } from "../../services/api";

function pct(x) {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : "—";
}
function num(x) {
  return typeof x === "number" && Number.isFinite(x) ? x.toLocaleString() : "—";
}
function secs(x) {
  return typeof x === "number" ? `${x.toFixed(1)}s` : "—";
}

export default function Evaluations({ id = null }) {
  const [searchParams] = useSearchParams();
  // Prefer an explicit prop (embedded as a detail-screen tab); fall back to
  // the ?id= query param (standalone /admin/evaluations route).
  const ontologyId = id || searchParams.get("id");

  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null); // full run envelope
  const [loadingDetail, setLoadingDetail] = useState(false);

  const loadRuns = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    const res = await evaluationsAPI.listRuns(ontologyId);
    if (res.success) {
      setRuns(res.data.runs || []);
    } else {
      setError(res.error || "Failed to load evaluation runs");
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const openRun = useCallback(
    async (runId) => {
      if (!ontologyId) return;
      setLoadingDetail(true);
      setError(null);
      const res = await evaluationsAPI.getRun(ontologyId, runId);
      if (res.success) {
        setSelectedRun(res.data);
      } else {
        setError(res.error || "Failed to load run detail");
      }
      setLoadingDetail(false);
    },
    [ontologyId],
  );

  const runColumns = [
    {
      id: "createdAt",
      header: "Run",
      cell: (r) =>
        r.createdAt ? new Date(r.createdAt).toLocaleString() : r.runId,
    },
    { id: "version", header: "Version", cell: (r) => r.version || "—" },
    { id: "layerType", header: "Type", cell: (r) => r.layerType || "—" },
    {
      id: "passRate",
      header: "Pass rate",
      cell: (r) => pct(r.summary?.passRate),
    },
    {
      id: "avgAccuracy",
      header: "Avg accuracy",
      cell: (r) => pct(r.summary?.avgAccuracy),
    },
    {
      id: "avgLatency",
      header: "Avg latency",
      cell: (r) => secs(r.summary?.avgLatencyS),
    },
    {
      id: "tokens",
      header: "Tokens (in / out)",
      cell: (r) =>
        `${num(r.summary?.totalInputTokens)} / ${num(r.summary?.totalOutputTokens)}`,
    },
    {
      id: "actions",
      header: "",
      cell: (r) => (
        <Button variant="inline-link" onClick={() => openRun(r.runId)}>
          View questions
        </Button>
      ),
    },
  ];

  const detailColumns = [
    { id: "question", header: "Question", cell: (r) => r.question || "—" },
    {
      id: "passed",
      header: "Result",
      cell: (r) =>
        r.passed ? (
          <StatusIndicator type="success">pass</StatusIndicator>
        ) : (
          <StatusIndicator type="error">{r.verdict || "fail"}</StatusIndicator>
        ),
    },
    { id: "accuracy", header: "Accuracy", cell: (r) => pct(r.accuracy) },
    { id: "latency", header: "Latency", cell: (r) => secs(r.latency_s) },
    {
      id: "tokens",
      header: "Tokens (in / out)",
      cell: (r) => `${num(r.agent_in_tokens)} / ${num(r.agent_out_tokens)}`,
    },
  ];

  if (!ontologyId) {
    return (
      <Alert type="error" header="Missing semantic layer id">
        Open this tab from a specific semantic layer (the URL must include
        <code> ?id=&lt;ontologyId&gt;</code>).
      </Alert>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="OnDemand evaluation runs for this semantic layer, triggered when a new version completes and evaluated against the maintained ground-truth dataset."
        actions={
          <Button iconName="refresh" onClick={loadRuns} loading={loading}>
            Refresh
          </Button>
        }
      >
        Evaluations
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header variant="h2" counter={`(${runs.length})`}>
            Evaluation runs
          </Header>
        }
      >
        {loading ? (
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">Loading runs…</StatusIndicator>
          </Box>
        ) : (
          <Table
            columnDefinitions={runColumns}
            items={runs}
            variant="embedded"
            resizableColumns
            wrapLines
            empty={
              <Box textAlign="center" color="text-status-inactive">
                No evaluation runs yet. A run is created automatically when a
                new version of this layer completes (and a ground-truth dataset
                is configured).
              </Box>
            }
          />
        )}
      </Container>

      {selectedRun && (
        <Container
          header={
            <Header
              variant="h2"
              description={
                selectedRun.createdAt
                  ? `Run ${selectedRun.runId} · ${new Date(selectedRun.createdAt).toLocaleString()}`
                  : `Run ${selectedRun.runId}`
              }
              actions={
                <Button onClick={() => setSelectedRun(null)}>Close</Button>
              }
            >
              Per-question metrics
            </Header>
          }
        >
          {loadingDetail ? (
            <Box textAlign="center" padding="l">
              <StatusIndicator type="loading">Loading…</StatusIndicator>
            </Box>
          ) : (
            <Table
              columnDefinitions={detailColumns}
              items={selectedRun.results || []}
              variant="embedded"
              resizableColumns
              wrapLines
              empty={
                <Box textAlign="center" color="text-status-inactive">
                  This run has no per-question rows.
                </Box>
              }
            />
          )}
        </Container>
      )}
    </SpaceBetween>
  );
}
