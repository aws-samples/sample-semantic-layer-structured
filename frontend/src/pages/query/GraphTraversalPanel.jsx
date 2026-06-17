/**
 * GraphTraversalPanel — VKG-only view of the knowledge-graph sub-graph the
 * ontology query agent traversed to answer a turn.
 *
 * The ontology query agent surfaces two related signals in its run_finished
 * ``totals`` (see agents/ontology_query_agent/main.py):
 *   - ``graphTraversal``: a human-readable summary of the term → class (table)
 *     mappings the agent resolved, e.g.
 *     "insured → Policy (normalized.policy), party → Party (normalized.party)".
 *   - ``nQuads`` / ``kbSources``: the actual RDF/OWL triples of the retrieved
 *     sub-graph (capped server-side).
 *
 * We render the traversal summary as a list of mapping chips and the n-quads
 * as a collapsible code block with a JSON download. Renders nothing when the
 * turn carried no graph data (e.g. SemanticRAG turns, or a clarification).
 */
import React from "react";
import {
  Box,
  Button,
  ExpandableSection,
  SpaceBetween,
} from "@cloudscape-design/components";

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

/**
 * Split the agent's ``graphTraversal`` summary string into individual mapping
 * entries. The agent joins entries with ", " — but each entry can itself
 * contain a parenthesised "(db.table)", which never contains a comma, so a
 * plain comma split is safe. Returns [] for the generic placeholder the agent
 * emits when no specific mappings were resolved.
 */
function parseTraversal(summary) {
  if (!summary || typeof summary !== "string") return [];
  const trimmed = summary.trim();
  if (!trimmed || trimmed.toLowerCase() === "ontology mappings applied") {
    return [];
  }
  return trimmed
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function GraphTraversalPanel({ totals, mode }) {
  if (!totals) return null;
  const summary = totals.graphTraversal || "";
  // The raw n-quad sub-graph is only meaningful in VKG mode, where it holds
  // real ontology triples. In Semantic-RAG mode the same field carries the
  // retrieved markdown KB docs inlined as triples — an unreadable wall of
  // escaped text — so we suppress it there and let the KB-chunk citations
  // (ReasoningPanel) be the human-readable view instead.
  const isVkg = mode === "vkg";
  // nQuads is the canonical field; kbSources carried the same payload before
  // graphTraversal existed, so fall back to it for older persisted turns.
  const nQuads = isVkg
    ? Array.isArray(totals.nQuads)
      ? totals.nQuads
      : Array.isArray(totals.kbSources)
        ? totals.kbSources
        : []
    : [];

  const mappings = parseTraversal(summary);
  const hasSummary = mappings.length > 0;
  const hasNQuads = nQuads.length > 0;
  if (!hasSummary && !hasNQuads) return null;

  const headerLabel = `Graph traversal${
    hasSummary
      ? ` (${mappings.length} mapping${mappings.length === 1 ? "" : "s"})`
      : ""
  }`;

  return (
    <ExpandableSection headerText={headerLabel} variant="footer">
      <SpaceBetween direction="vertical" size="xs">
        {hasSummary && (
          <SpaceBetween direction="vertical" size="xxs">
            <Box variant="small" color="text-body-secondary">
              Query terms mapped to ontology classes / tables:
            </Box>
            <SpaceBetween direction="horizontal" size="xs">
              {mappings.map((mapEntry, i) => (
                <Box
                  key={i}
                  variant="code"
                  fontSize="body-s"
                  padding={{ horizontal: "xs" }}
                >
                  {mapEntry}
                </Box>
              ))}
            </SpaceBetween>
          </SpaceBetween>
        )}
        {hasNQuads && (
          // The raw n-quad triples are unreadable inline (long IRIs, escaped
          // literals). Expose them via JSON download only — the readable
          // mapping chips above are the human-facing view.
          <Box>
            <Button
              iconName="download"
              onClick={() => downloadJson(nQuads, "subgraph-nquads.json")}
            >
              Download sub-graph ({nQuads.length} triple
              {nQuads.length === 1 ? "" : "s"}, JSON)
            </Button>
          </Box>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
}
