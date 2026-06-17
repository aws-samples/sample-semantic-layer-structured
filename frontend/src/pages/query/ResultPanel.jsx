/**
 * ResultPanel — token + runtime footer for a finalized assistant turn.
 *
 * The SQL block and results table are now rendered inline by ``ReasoningPanel``
 * (under the matching ``execute_sql_query`` tool card; SemanticRAG retrieval is
 * shown via the Phase 3 slice detail) so this panel only carries the usage
 * footer that summarizes the whole turn.
 */
import React from "react";
import { Box } from "@cloudscape-design/components";

function formatNumber(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return null;
  return n.toLocaleString();
}

function formatRuntime(ms) {
  if (typeof ms !== "number" || ms <= 0) return null;
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export default function ResultPanel({ totals }) {
  if (!totals) return null;
  const { usage, runtimeMs } = totals;
  const runtime = formatRuntime(runtimeMs);

  // Bedrock Converse with prompt caching reports cache-read/write tokens
  // SEPARATELY from inputTokens, while totalTokens is the cache-INCLUSIVE grand
  // total: total = input + output + cacheRead + cacheWrite. Surface the cache
  // component so the breakdown reconciles with the total (previously the footer
  // showed "61,427 tokens (10,108 in / 1,887 out)" and the numbers didn't add
  // up — the ~49k cache-read portion was hidden).
  const inN = usage?.inputTokens;
  const outN = usage?.outputTokens;
  const totalN = usage?.totalTokens;
  const cacheN =
    (usage?.cacheReadInputTokens || 0) + (usage?.cacheWriteInputTokens || 0);

  const inTokens = formatNumber(inN);
  const outTokens = formatNumber(outN);
  const totalTokens = formatNumber(totalN);
  const cacheTokens = formatNumber(cacheN);

  const parts = [];
  if (runtime) parts.push(runtime);
  if (totalTokens) {
    // Build "X in / Y out[ / Z cached]" so the components sum to the total.
    const detailBits = [];
    if (inTokens) detailBits.push(`${inTokens} in`);
    if (outTokens) detailBits.push(`${outTokens} out`);
    if (cacheN > 0 && cacheTokens) detailBits.push(`${cacheTokens} cached`);
    const detail = detailBits.length ? ` (${detailBits.join(" / ")})` : "";
    parts.push(`${totalTokens} tokens${detail}`);
  } else if (inTokens || outTokens) {
    parts.push(`${inTokens || 0} in / ${outTokens || 0} tokens`);
  }
  if (parts.length === 0) return null;
  return (
    <Box variant="small" color="text-body-secondary">
      {parts.join(" · ")}
    </Box>
  );
}
