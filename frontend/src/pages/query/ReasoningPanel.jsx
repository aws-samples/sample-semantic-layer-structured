/**
 * ReasoningPanel — collapsible per-turn list of AG-UI tool_call records.
 *
 * Renders one card per tool call with the tool name, args, and result.
 * The Thinking section nests INSIDE the "Show reasoning" group so all
 * intermediate output stays under one collapsible header.
 *
 * Tool-specific renderers:
 *   - ``execute_sql_query``: render args.sql in a code block matching the
 *     SQL Query section's styling, and any rows/columns in the result as
 *     a small table preview.
 *   - other tools: collapsible JSON Arguments + Result (the generic fallback view).
 *
 * SemanticRAG retrieval is no longer surfaced as a ``retrieve_kb_context`` tool
 * card; the retrieved slice is shown via the Phase 3 (Slice builder) detail in
 * ``PhaseDetail`` instead.
 */
import React from "react";
import {
  Box,
  Button,
  CopyToClipboard,
  ExpandableSection,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";

// Format an epoch-ms tool timestamp as a local wall-clock time with
// millisecond precision (e.g. "6:11:50.123 PM"). The millis matter here:
// the deterministic pipeline tools fire within the same second, so a
// seconds-only clock wouldn't disambiguate their order. Returns "" for a
// missing/invalid stamp so the card just omits the time.
function formatClockTime(epochMs) {
  if (typeof epochMs !== "number" || !Number.isFinite(epochMs)) return "";
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) return "";
  const base = d.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
  const millis = String(d.getMilliseconds()).padStart(3, "0");
  // Splice the millis in before the AM/PM suffix when present so the
  // result reads "6:11:50.123 PM" rather than "6:11:50 PM.123".
  const m = base.match(/^(.*\d{2})(\s*[AaPp][Mm])?$/);
  if (m) return `${m[1]}.${millis}${m[2] || ""}`;
  return `${base}.${millis}`;
}

// ----------------------------------------------------------------------
// Download helpers — rendered next to the SQL/KB tool cards so the user can
// pull the same data the agent saw without scrolling to a separate panel.
// ----------------------------------------------------------------------

// Build a CSV string from columns/rows (shared by the download + copy controls).
function csvStringFromColumnsRows(columns, rows) {
  const escape = (v) => {
    if (v == null) return "";
    const s = typeof v === "string" ? v : JSON.stringify(v);
    if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const header = columns.join(",");
  const body = rows.map((r) => r.map(escape).join(",")).join("\n");
  return `${header}\n${body}`;
}

function downloadCsvFromColumnsRows(columns, rows, filename) {
  const csv = csvStringFromColumnsRows(columns, rows);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Plain-text download — used for the VKG ontology slice, which is Turtle/RDF
// (a .ttl text document), not JSON.
function downloadText(text, filename, mime = "text/turtle;charset=utf-8") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ----------------------------------------------------------------------
// Helpers — extract structured payloads from Strands ToolResult content.
// ----------------------------------------------------------------------

/**
 * Strands ToolResult.content is an array of typed content blocks:
 *   [{json: {...}}, {text: "..."}, ...]
 * The Python @tool decorator wraps a ``str`` return into a {text: ...}
 * block — so a tool that ``return json.dumps(...)`` lands here as text,
 * NOT as a json block. We try {json: ...} first, then attempt to parse
 * any {text: ...} block as JSON.
 *
 * Returns the parsed object, or null if nothing parseable was found.
 */
function firstJsonBlock(result) {
  if (!Array.isArray(result)) {
    // Some tools may have already been parsed by the runner — accept a
    // bare object/array too.
    if (result && typeof result === "object") return result;
    return null;
  }
  for (const block of result) {
    if (block && typeof block === "object" && "json" in block) {
      return block.json;
    }
  }
  for (const block of result) {
    if (block && typeof block === "object" && typeof block.text === "string") {
      try {
        return JSON.parse(block.text);
      } catch (_e) {
        // Not JSON — fall through and try the next block.
      }
    }
  }
  return null;
}

/** First ``text`` block from a ToolResult.content array. */
function firstTextBlock(result) {
  if (!Array.isArray(result)) return null;
  for (const block of result) {
    if (block && typeof block === "object" && typeof block.text === "string") {
      return block.text;
    }
  }
  return null;
}

// ----------------------------------------------------------------------
// Tool-specific renderers
// ----------------------------------------------------------------------

function SqlQueryCall({ call, turnId }) {
  // ``execute_sql_query`` args carry the SQL string; the result usually
  // includes columns/rows or an Athena execution summary. We render the
  // SQL block + a full Cloudscape Table with CSV download right here so
  // the reasoning card is the single place users see the SQL execution
  // (no duplicated SQL Query / Results panels below the bubble).
  const data = firstJsonBlock(call.result) || {};
  const sql =
    call.args?.sql || call.args?.query || data.sql_query || data.sql || "";
  const columns = Array.isArray(data.columns) ? data.columns : [];
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const executionId = data.query_execution_id || data.execution_id || "";
  const database = data.database_name || data.database || "";
  const errorText = data.error || firstTextBlock(call.result) || "";

  // Build Cloudscape column defs from the result columns (positional rows).
  const columnDefinitions = columns.map((col, idx) => ({
    id: col || `col-${idx}`,
    header: col,
    cell: (r) => {
      const v = r[idx];
      if (v == null) return "";
      return typeof v === "string" ? v : JSON.stringify(v);
    },
  }));
  // Cloudscape Table requires items to be objects, not arrays — wrap each
  // positional row so the cell accessors above can read it by index.
  const tableItems = rows.map((r) => Object.assign(Array.from(r), {}));

  const rowLabel = `${rows.length} row${rows.length === 1 ? "" : "s"}`;

  return (
    <SpaceBetween direction="vertical" size="xs">
      {sql && (
        <ExpandableSection
          variant="footer"
          headerText="SQL Query"
          defaultExpanded
        >
          <SpaceBetween direction="vertical" size="xs">
            <Box>
              <CopyToClipboard
                copyButtonText="Copy SQL"
                copyErrorText="Failed to copy SQL"
                copySuccessText="SQL copied"
                textToCopy={sql}
              />
            </Box>
            <Box variant="code" fontSize="body-s">
              <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{sql}</pre>
            </Box>
          </SpaceBetween>
        </ExpandableSection>
      )}
      {(columns.length > 0 || rows.length > 0) && (
        <ExpandableSection
          variant="footer"
          headerText={`Results (${rowLabel})`}
          defaultExpanded={false}
        >
          <SpaceBetween direction="vertical" size="xs">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                iconName="download"
                onClick={() =>
                  downloadCsvFromColumnsRows(
                    columns,
                    rows,
                    `results-${turnId || "turn"}.csv`,
                  )
                }
                disabled={rows.length === 0}
              >
                Download CSV
              </Button>
              <CopyToClipboard
                copyButtonText="Copy results"
                copyErrorText="Failed to copy results"
                copySuccessText="Results copied"
                textToCopy={csvStringFromColumnsRows(columns, rows)}
                disabled={rows.length === 0}
              />
            </SpaceBetween>
            <Table
              columnDefinitions={columnDefinitions}
              items={tableItems}
              variant="embedded"
              stickyHeader={false}
              resizableColumns
              wrapLines
              empty={<Box>No rows</Box>}
            />
          </SpaceBetween>
        </ExpandableSection>
      )}
      {(executionId || database) && (
        <Box variant="small" color="text-body-secondary">
          {database ? `Database: ${database}` : ""}
          {database && executionId ? " · " : ""}
          {executionId ? `Athena execution: ${executionId}` : ""}
        </Box>
      )}
      {!sql && !columns.length && errorText && (
        <Box variant="code" fontSize="body-s">
          <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{errorText}</pre>
        </Box>
      )}
    </SpaceBetween>
  );
}

function GenericToolCall({ call }) {
  const argsText = (() => {
    try {
      return JSON.stringify(call.args ?? {}, null, 2);
    } catch (_e) {
      return String(call.args);
    }
  })();
  const resultText = (() => {
    try {
      return JSON.stringify(call.result ?? {}, null, 2);
    } catch (_e) {
      return String(call.result);
    }
  })();
  return (
    <SpaceBetween direction="vertical" size="xxs">
      <ExpandableSection variant="footer" headerText="Arguments">
        <Box variant="code" fontSize="body-s">
          <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{argsText}</pre>
        </Box>
      </ExpandableSection>
      <ExpandableSection variant="footer" headerText="Result">
        <Box variant="code" fontSize="body-s">
          <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {resultText.slice(0, 2000)}
            {resultText.length > 2000 ? "\n… (truncated)" : ""}
          </pre>
        </Box>
      </ExpandableSection>
    </SpaceBetween>
  );
}

function ToolCallCard({ call, turnId }) {
  // Pick a renderer based on the tool name. Defaults to the generic
  // JSON view so unfamiliar tools (e.g. new MCP gateway tools) still
  // surface their args + result.
  let body;
  if (call.toolName === "execute_sql_query") {
    body = <SqlQueryCall call={call} turnId={turnId} />;
  } else {
    body = <GenericToolCall call={call} />;
  }
  const startedClock = formatClockTime(call.startedAt);
  return (
    <Box variant="div" padding={{ vertical: "xs" }}>
      <SpaceBetween direction="vertical" size="xxs">
        <Box>
          <StatusIndicator type={call.status === "error" ? "error" : "success"}>
            {call.toolName || "tool"}
          </StatusIndicator>
          {startedClock && (
            <Box variant="small" color="text-body-secondary" display="inline">
              {" "}
              · {startedClock}
            </Box>
          )}
          {typeof call.durationMs === "number" && (
            <Box variant="small" color="text-body-secondary" display="inline">
              {" "}
              · {call.durationMs} ms
            </Box>
          )}
        </Box>
        {body}
      </SpaceBetween>
    </Box>
  );
}

// ----------------------------------------------------------------------
// Phase timeline (Tier 2 graph workflow tier_event trace)
// ----------------------------------------------------------------------

// Human labels for each (phase, step) the workflow emits.
function phaseLabel(row) {
  const { phase, step, result } = row;
  if (phase === 1) return "Phase 1 · Topic router";
  if (phase === 2) return "Phase 2 · Disambiguation";
  if (phase === 3 && step === "3b") return "Phase 3b · Slice disambiguation";
  if (phase === 3) return "Phase 3 · Slice builder";
  if (phase === 4) return "Phase 4 · SQL generate + validate";
  if (phase === 5) {
    // Phase 5 emits a grounding result (grounded flag present) and, once
    // grounded, an execution result (rowCount present).
    if (result && typeof result.rowCount === "number")
      return "Phase 5 · Execution";
    return "Phase 5 · Grounding";
  }
  return `Phase ${phase}`;
}

// Short result chip text derived from the phase_result payload.
function phaseResultText(row) {
  const r = row.result || {};
  if (r.degraded) return `degraded: ${r.degraded}`;
  if (typeof r.candidateCount === "number") {
    // VKG candidates are class/property IRIs, not tables — label by kind.
    const noun =
      r.candidateKind === "iri" ? "candidate IRI(s)" : "candidate table(s)";
    return `${r.candidateCount} ${noun}`;
  }
  if (r.status) return String(r.status).toLowerCase();
  if (typeof r.sufficient === "boolean")
    return r.sufficient ? "sufficient" : "insufficient";
  if (typeof r.ambiguous === "boolean")
    return r.ambiguous ? "ambiguous" : "clear";
  if (typeof r.repaired === "boolean") return r.repaired ? "repaired" : "valid";
  if (r.grounded === false)
    return `ungrounded: ${(r.missing || []).join(", ") || "missing identifiers"}`;
  if (r.grounded === true && typeof r.rowCount !== "number") return "grounded";
  if (typeof r.rowCount === "number")
    return `${r.rowCount} row(s)${r.overLimit ? " (truncated to 100)" : ""}`;
  return "";
}

// Per-phase token chip: "· 1.2k in / 340 out" when either is present.
function phaseTokenText(row) {
  const r = row.result || {};
  const inTok = r.inputTokens || 0;
  const outTok = r.outputTokens || 0;
  if (!inTok && !outTok) return "";
  const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  return `${fmt(inTok)} in / ${fmt(outTok)} out`;
}

// Expandable detail for a phase row: Phase 1 candidate list (table/IRI +
// relevance score), Phase 2 term→target disambiguation mappings, and Phase 5
// execution result table (columns + rows emitted alongside rowCount).
function PhaseDetail({ result, turnId }) {
  const r = result || {};
  const candidates = Array.isArray(r.candidates) ? r.candidates : [];
  const mappings = Array.isArray(r.mappings) ? r.mappings : [];
  const ambiguities = Array.isArray(r.ambiguities) ? r.ambiguities : [];
  const columns = Array.isArray(r.columns) ? r.columns : [];
  const rows = Array.isArray(r.rows) ? r.rows : [];

  if (candidates.length > 0) {
    const isIri = r.candidateKind === "iri";
    return (
      <ExpandableSection
        variant="footer"
        headerText={`${candidates.length} ${isIri ? "candidate IRIs" : "candidate tables"}`}
        defaultExpanded={false}
      >
        <SpaceBetween direction="vertical" size="xxs">
          {candidates.map((c, idx) => {
            const name = isIri ? c.localName || c.iri : c.table;
            const sub = isIri
              ? c.kind || ""
              : c.database
                ? `database: ${c.database}`
                : "";
            const score =
              typeof c.score === "number"
                ? isIri
                  ? `score ${c.score}`
                  : `${Math.round(c.score * 100)}%`
                : "";
            return (
              <Box key={idx} variant="small">
                {name}
                {(sub || score) && (
                  <Box color="text-body-secondary" display="inline">
                    {" "}
                    · {[sub, score].filter(Boolean).join(" · ")}
                  </Box>
                )}
              </Box>
            );
          })}
        </SpaceBetween>
      </ExpandableSection>
    );
  }

  if (mappings.length > 0 || ambiguities.length > 0) {
    return (
      <ExpandableSection
        variant="footer"
        headerText="Term mappings"
        defaultExpanded={false}
      >
        <SpaceBetween direction="vertical" size="xxs">
          {mappings.map((m, idx) => (
            <Box key={`m-${idx}`} variant="small">
              {m.term} → {m.localName || m.table || m.iri || "?"}
              {(m.database || typeof m.confidence === "number") && (
                <Box color="text-body-secondary" display="inline">
                  {" "}
                  ·{" "}
                  {[
                    m.database ? `database: ${m.database}` : "",
                    typeof m.confidence === "number"
                      ? `confidence ${m.confidence}`
                      : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </Box>
              )}
            </Box>
          ))}
          {ambiguities.map((a, idx) => (
            <Box key={`a-${idx}`} variant="small" color="text-status-warning">
              ambiguous: {a.term} (
              {(a.matches || [])
                .map((x) => x.table || x.column || "?")
                .join(", ")}
              )
            </Box>
          ))}
        </SpaceBetween>
      </ExpandableSection>
    );
  }

  // Phase 5 execution result — render when columns + rows are present.
  if (columns.length > 0 || rows.length > 0) {
    const rowLabel = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
    const columnDefinitions = columns.map((col, idx) => ({
      id: col || `col-${idx}`,
      header: col,
      cell: (r) => {
        const v = r[idx];
        if (v == null) return "";
        return typeof v === "string" ? v : JSON.stringify(v);
      },
    }));
    const tableItems = rows.map((r) => Object.assign(Array.from(r), {}));
    return (
      <ExpandableSection
        variant="footer"
        headerText={`Results (${rowLabel})`}
        defaultExpanded={false}
      >
        <SpaceBetween direction="vertical" size="xs">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              iconName="download"
              onClick={() =>
                downloadCsvFromColumnsRows(
                  columns,
                  rows,
                  `results-${turnId || "turn"}.csv`,
                )
              }
              disabled={rows.length === 0}
            >
              Download CSV
            </Button>
            <CopyToClipboard
              copyButtonText="Copy results"
              copyErrorText="Failed to copy results"
              copySuccessText="Results copied"
              textToCopy={csvStringFromColumnsRows(columns, rows)}
              disabled={rows.length === 0}
            />
          </SpaceBetween>
          <Table
            columnDefinitions={columnDefinitions}
            items={tableItems}
            variant="embedded"
            stickyHeader={false}
            resizableColumns
            wrapLines
            empty={<Box>No rows</Box>}
          />
        </SpaceBetween>
      </ExpandableSection>
    );
  }

  // Phase 3 slice — view the assembled grounding data + download it.
  // r.slice is the slice builder's output string: JSON for the RAG
  // (metadata_query) agent — {tables, columns, joins} — and Turtle/RDF for the
  // VKG (ontology_query) agent. We parse JSON first; if that fails, treat it as
  // Turtle and render a triple-count summary + a .ttl download.
  if (typeof r.slice === "string" && r.slice.trim()) {
    let parsed = null;
    try {
      parsed = JSON.parse(r.slice);
    } catch (_e) {
      parsed = null;
    }

    // VKG / Turtle slice — not JSON. Show a triple count and download as .ttl.
    if (parsed == null) {
      // Count triples heuristically: a Turtle statement ends in ' .' (';' / ','
      // continue a statement, so they don't terminate). '@prefix'/'@base'
      // directives also end in ' .' but aren't triples, so subtract them.
      const ttl = r.slice;
      const terminators = (ttl.match(/\s\.\s*(?:\n|$)/g) || []).length;
      const directives = (ttl.match(/^\s*@(?:prefix|base)\b/gim) || []).length;
      const tripleCount = Math.max(0, terminators - directives);
      const header = `Ontology slice (${tripleCount} triple${
        tripleCount === 1 ? "" : "s"
      }, Turtle)`;
      return (
        <ExpandableSection
          variant="footer"
          headerText={header}
          defaultExpanded={false}
        >
          <SpaceBetween direction="vertical" size="xs">
            <Box>
              <Button
                iconName="download"
                onClick={() =>
                  downloadText(ttl, `slice-${turnId || "turn"}.ttl`)
                }
              >
                Download slice (Turtle)
              </Button>
            </Box>
            <Box variant="code" fontSize="body-s">
              {ttl.slice(0, 4000)}
              {ttl.length > 4000
                ? "\n… (truncated — download for full slice)"
                : ""}
            </Box>
          </SpaceBetween>
        </ExpandableSection>
      );
    }

    // RAG / JSON slice — {tables, columns, joins}.
    const tables = Array.isArray(parsed?.tables) ? parsed.tables : [];
    const cols = Array.isArray(parsed?.columns) ? parsed.columns : [];
    const joins = Array.isArray(parsed?.joins) ? parsed.joins : [];
    const header = `Slice (${tables.length} table${
      tables.length === 1 ? "" : "s"
    }, ${cols.length} column${cols.length === 1 ? "" : "s"})`;
    return (
      <ExpandableSection
        variant="footer"
        headerText={header}
        defaultExpanded={false}
      >
        <SpaceBetween direction="vertical" size="xs">
          <Box>
            <Button
              iconName="download"
              onClick={() =>
                downloadJson(
                  parsed ?? r.slice,
                  `slice-${turnId || "turn"}.json`,
                )
              }
            >
              Download slice (JSON)
            </Button>
          </Box>
          {tables.length > 0 && (
            <Box variant="small" color="text-body-secondary">
              Tables: {tables.join(", ")}
            </Box>
          )}
          {cols.map((c, i) => (
            <Box key={`c-${i}`} variant="small">
              {(c.table_id ? `${c.table_id}.` : "") + (c.name || "")}
              {c.type ? ` · ${c.type}` : ""}
            </Box>
          ))}
          {joins.map((j, i) => (
            <Box key={`j-${i}`} variant="small" color="text-body-secondary">
              {j.from || "?"} → {j.to || "?"}
            </Box>
          ))}
        </SpaceBetween>
      </ExpandableSection>
    );
  }

  return null;
}

function PhaseTimeline({ phases, turnId }) {
  if (!Array.isArray(phases) || phases.length === 0) return null;
  return (
    <SpaceBetween direction="vertical" size="xxs">
      {phases.map((row, i) => {
        const running = row.status !== "success";
        const durationMs =
          typeof row.endedAt === "number" && typeof row.startedAt === "number"
            ? row.endedAt - row.startedAt
            : null;
        const resultText = phaseResultText(row);
        const tokenText = phaseTokenText(row);
        const roundBadge = row.round > 1 ? ` · round ${row.round}` : "";
        // Surface the generated query inline on the Phase 4 row so the user sees
        // the actual SQL/SPARQL even when execution is skipped (degraded path).
        const query = row.result && (row.result.sql || row.result.sparql);
        // Label the copy control by dialect: VKG (ontology) agent emits SPARQL,
        // RAG (metadata) agent emits SQL.
        const queryLang = row.result && row.result.sparql ? "SPARQL" : "SQL";
        return (
          <Box key={`${row.phase}-${row.step || ""}-${row.round}-${i}`}>
            <StatusIndicator type={running ? "in-progress" : "success"}>
              {phaseLabel(row)}
              {roundBadge}
            </StatusIndicator>
            {resultText && (
              <Box variant="small" color="text-body-secondary" display="inline">
                {" "}
                · {resultText}
              </Box>
            )}
            {tokenText && (
              <Box variant="small" color="text-body-secondary" display="inline">
                {" "}
                · {tokenText}
              </Box>
            )}
            {durationMs !== null && durationMs > 0 && (
              <Box variant="small" color="text-body-secondary" display="inline">
                {" "}
                · {durationMs} ms
              </Box>
            )}
            <PhaseDetail result={row.result} turnId={turnId} />
            {query && (
              <SpaceBetween direction="vertical" size="xxs">
                <Box margin={{ top: "xxs" }}>
                  <CopyToClipboard
                    variant="icon"
                    copyButtonText={`Copy ${queryLang}`}
                    copyErrorText={`Failed to copy ${queryLang}`}
                    copySuccessText={`${queryLang} copied`}
                    textToCopy={query}
                  />
                </Box>
                <Box variant="code" fontSize="body-s">
                  <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                    {query}
                  </pre>
                </Box>
              </SpaceBetween>
            )}
          </Box>
        );
      })}
    </SpaceBetween>
  );
}

function ThinkingBlock({ thinking }) {
  if (!thinking) return null;
  return (
    <ExpandableSection headerText="Thinking" variant="footer">
      <Box variant="code" fontSize="body-s">
        <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{thinking}</pre>
      </Box>
    </ExpandableSection>
  );
}

export default function ReasoningPanel({
  toolCalls = [],
  phases = [],
  thinking = "",
  turnId = "",
}) {
  const hasThinking = Boolean(thinking);
  const hasToolCalls = Array.isArray(toolCalls) && toolCalls.length > 0;
  const hasPhases = Array.isArray(phases) && phases.length > 0;
  if (!hasThinking && !hasToolCalls && !hasPhases) {
    return null;
  }
  // Header label: stay scannable across phases + tool calls + thinking.
  // Group everything under a single "Show reasoning" header so all
  // intermediate output collapses together.
  const parts = [];
  if (hasPhases)
    parts.push(`${phases.length} phase${phases.length === 1 ? "" : "s"}`);
  if (hasToolCalls)
    parts.push(
      `${toolCalls.length} tool ${toolCalls.length === 1 ? "call" : "calls"}`,
    );
  if (hasThinking) parts.push("thinking");
  const headerLabel = `Show reasoning (${parts.join(" + ")})`;
  return (
    <ExpandableSection headerText={headerLabel} variant="footer">
      <SpaceBetween direction="vertical" size="xs">
        <PhaseTimeline phases={phases} turnId={turnId} />
        <ThinkingBlock thinking={thinking} />
        {hasToolCalls &&
          toolCalls.map((call) => (
            <ToolCallCard
              key={call.callId || call.toolName}
              call={call}
              turnId={turnId}
            />
          ))}
      </SpaceBetween>
    </ExpandableSection>
  );
}
