import { useEffect, useState, useCallback } from "react";
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  KeyValuePairs,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { monitoringAPI } from "../services/api";

// Per-bucket bar color. The agentic layer is greyed out because it is not
// implemented yet (the backend always reports it as 0 with implemented=false).
const BUCKET_COLORS = {
  metric: "#0972d5", // blue   — Tier 1 governed metric
  semantic: "#037f0c", // green  — Tier 2 graph (slice → SQL / VKG)
  advisory: "#8c4fff", // purple — schema / "what can I ask" questions
  agentic: "#b5b9c0", // grey   — planned, not implemented
};

/**
 * Admin tab — production-signal monitoring for one semantic layer.
 *
 * Surfaces two signals over LIVE query-agent traffic (read from the
 * chat-sessions store, bucketed by each answer's provenance tier):
 *   1. Resolution breakdown across the four layers (metric / semantic /
 *      advisory / agentic-not-yet-implemented).
 *   2. Share of user turns that used correction language ("that's the wrong
 *      table", "you're missing the fraud filter"), correlated with the count
 *      of lessons AgentCore Memory has extracted for the layer.
 */
export default function MonitoringTab({ ontologyId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    // monitoringAPI.get resolves to the handleResponse envelope {success, data};
    // the backend body (the breakdown) lives at res.data. handleResponse never
    // throws, so branch on success rather than try/catch.
    const res = await monitoringAPI.get(ontologyId);
    if (res.success) {
      setData(res.data ?? null);
    } else {
      setError(res.error ?? "Failed to load monitoring data");
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const resolution = data?.resolution ?? { totalAnswered: 0, buckets: [] };
  const corrections = data?.corrections ?? {
    userTurns: 0,
    correctionTurns: 0,
    pct: 0,
    examples: [],
    lessonsExtracted: 0,
    lessonsCapped: false,
  };
  // AgentCore caps the lessons page at 100; show "100+" rather than a clamped
  // "100" so the figure isn't read as an exact total when it's a floor.
  const lessonsLabel = corrections.lessonsCapped
    ? `${corrections.lessonsExtracted}+`
    : `${corrections.lessonsExtracted ?? 0}`;
  const buckets = resolution.buckets ?? [];
  // recharts wants a plain array; carry the key through for per-bar coloring.
  const chartData = buckets.map((b) => ({
    name: b.label,
    pct: b.pct,
    key: b.key,
  }));

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="How live queries for this semantic layer resolve across the metric, semantic, advisory, and (planned) agentic layers — plus how often users correct the agent. Aggregated from chat, MCP, and eval sessions (rolling 24h window)."
          actions={
            <Button iconName="refresh" onClick={refresh} disabled={loading}>
              Refresh
            </Button>
          }
        >
          Monitoring
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {data && !data.configured && (
          <Alert type="info">
            Chat-session telemetry is not wired in this environment, so no
            production traffic can be aggregated yet.
          </Alert>
        )}

        {loading && !data ? (
          <Box textAlign="center" padding="l">
            <Spinner size="large" />
          </Box>
        ) : (
          <>
            {/* ── Top-line counts ─────────────────────────────────────── */}
            <KeyValuePairs
              columns={3}
              items={[
                { label: "Sessions analyzed", value: data?.sessionCount ?? 0 },
                {
                  label: "Answered queries",
                  value: resolution.totalAnswered ?? 0,
                },
                {
                  label: "Lessons extracted",
                  value: lessonsLabel,
                },
              ]}
            />

            {/* ── Resolution-layer breakdown ──────────────────────────── */}
            <Box>
              <Box variant="h3" padding={{ bottom: "s" }}>
                Resolution layer breakdown
              </Box>
              {resolution.totalAnswered > 0 ? (
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart
                    data={chartData}
                    margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="name" tick={{ fontSize: 12 }} />
                    <YAxis unit="%" domain={[0, 100]} tick={{ fontSize: 12 }} />
                    <Tooltip formatter={(v) => `${v}%`} />
                    <Bar
                      dataKey="pct"
                      name="Share of queries"
                      radius={[4, 4, 0, 0]}
                    >
                      {chartData.map((entry) => (
                        <Cell
                          key={entry.key}
                          fill={BUCKET_COLORS[entry.key] ?? "#0972d5"}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <Box color="text-status-inactive" padding="m">
                  No answered queries recorded yet for this layer.
                </Box>
              )}

              <Table
                variant="embedded"
                items={buckets}
                columnDefinitions={[
                  {
                    id: "label",
                    header: "Resolution layer",
                    cell: (b) => (
                      <SpaceBetween direction="horizontal" size="xs">
                        <span>{b.label}</span>
                        {!b.implemented && (
                          <StatusIndicator type="pending" colorOverride="grey">
                            not implemented
                          </StatusIndicator>
                        )}
                      </SpaceBetween>
                    ),
                  },
                  {
                    id: "count",
                    header: "Queries",
                    cell: (b) => b.count,
                    width: 120,
                  },
                  {
                    id: "pct",
                    header: "Share",
                    cell: (b) => `${b.pct}%`,
                    width: 120,
                  },
                ]}
                empty={
                  <Box textAlign="center" color="text-status-inactive">
                    No data
                  </Box>
                }
              />
            </Box>

            {/* ── Correction-language signal ──────────────────────────── */}
            <Box>
              <Box variant="h3" padding={{ bottom: "s" }}>
                Correction language
              </Box>
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Correction rate</Box>
                  <Box
                    fontSize="display-l"
                    fontWeight="bold"
                    color={
                      corrections.pct >= 20
                        ? "text-status-error"
                        : corrections.pct >= 10
                          ? "text-status-warning"
                          : "text-status-success"
                    }
                  >
                    {corrections.pct}%
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Correction turns</Box>
                  <Box fontSize="display-l">
                    {corrections.correctionTurns}
                    <Box variant="span" color="text-status-inactive">
                      {" "}
                      / {corrections.userTurns}
                    </Box>
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Lessons extracted</Box>
                  <Box fontSize="display-l">{lessonsLabel}</Box>
                </div>
              </ColumnLayout>
              <Box
                color="text-status-inactive"
                fontSize="body-s"
                padding={{ top: "xs" }}
              >
                Share of user turns that corrected the agent (e.g.
                &ldquo;that&rsquo;s the wrong table&rdquo;, &ldquo;you&rsquo;re
                missing the fraud filter&rdquo;). Each correction is a candidate
                lesson; compare against lessons extracted into AgentCore Memory
                to see whether corrections are being captured durably.
              </Box>

              {corrections.examples?.length > 0 && (
                <Box padding={{ top: "s" }}>
                  <Box variant="awsui-key-label">Recent correction phrases</Box>
                  <SpaceBetween size="xxs">
                    {corrections.examples.map((ex, i) => (
                      <Box
                        key={i}
                        fontSize="body-s"
                        color="text-body-secondary"
                      >
                        &ldquo;{ex}&rdquo;
                      </Box>
                    ))}
                  </SpaceBetween>
                </Box>
              )}
            </Box>
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}
