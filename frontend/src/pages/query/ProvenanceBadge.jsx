/**
 * ProvenanceBadge — a first-class, per-tier "where did this answer come from?"
 * badge rendered on each assistant turn.
 *
 * The trust contract differs per tier, so the badge is visually distinct:
 *   governed_metric → green  ("the system stands behind this number")
 *   governed_rule   → green  (an approved rule fired — a human signed off on the
 *                             rule AND on every numeric bound in it, so it
 *                             shares the governed green; the label distinguishes
 *                             a fired rule from a computed metric)
 *   semantic_sql    → blue   (grounded in executed SQL over the schema slice)
 *   vkg             → blue   (also a semantic-layer SQL path — SPARQL→SQL→Athena —
 *                             so it shares the blue "grounded in executed SQL" cue
 *                             with semantic_sql for visual consistency; the LABEL
 *                             distinguishes the two paths)
 *   agentic         → orange (Tier 3 supervisor loop rescued the turn; Cloudscape
 *                             has no "orange", so this is "severity-medium")
 *   advisory        → grey   ("describes the layer's metadata, not a computed result")
 *   document        → blue   (grounded in a cited passage of an uploaded file, so it
 *                             shares the "grounded" blue; the LABEL says the source
 *                             is documents rather than the data)
 *
 * Every tier in agents/shared/provenance.py:VALID_TIERS must appear in TIER_META.
 * An unmapped tier falls back to a grey badge showing the RAW tier string, which
 * does not crash but does surface a lowercase identifier to the user as if it
 * were a label — so the fallback is a safety net, not a substitute for an entry.
 *
 * Reads m.totals.provenance (see agents/shared/provenance.py). Renders nothing
 * when provenance is absent so legacy persisted turns (pre-provenance) don't
 * crash and don't show a misleading badge.
 */
import React from "react";
import { Badge, Box, SpaceBetween } from "@cloudscape-design/components";

// tier → { color, label, copy }. ``color`` is a Cloudscape Badge color.
const TIER_META = {
  governed_metric: {
    color: "green",
    label: "Governed Metric",
    copy: "Answered from a governed metric.",
  },
  governed_rule: {
    // Green like governed_metric: both carry an explicit human approval. The
    // label is what tells the user a RULE fired rather than a metric computed.
    color: "green",
    label: "Governed Rule",
    copy: "An approved rule fired; its thresholds were signed off by a human.",
  },
  semantic_sql: {
    color: "blue",
    label: "Semantic Layer (SQL)",
    copy: "Grounded in the schema slice; SQL in reasoning.",
  },
  vkg: {
    // Blue to match semantic_sql: VKG is also a semantic-layer SQL path
    // (SPARQL→SQL→Athena), so it shares the "grounded in executed SQL" colour;
    // the label keeps the two paths distinguishable.
    color: "blue",
    label: "Knowledge Graph",
    copy: "SPARQL→SQL trace in reasoning.",
  },
  agentic: {
    // Orange, not blue: the Tier 3 supervisor loop only runs after Tier 2
    // degraded, so the answer IS grounded but the path was a rescue. Colouring
    // it like semantic_sql would hide that the clean path failed.
    color: "severity-medium",
    label: "Agentic (Tier 3)",
    copy: "Tier 2 degraded; the supervisor loop recovered this answer.",
  },
  advisory: {
    color: "grey",
    label: "Advisory",
    copy: "Describes the layer's metadata & governed metrics; not a data query.",
  },
  document: {
    // Blue, like the SQL paths: a document answer IS grounded — every claim is
    // tied to a cited passage of an uploaded file, and an answer that couldn't be
    // tied to one is never returned. What differs is the SOURCE (prose the user
    // uploaded, not the data), which is what the label says.
    color: "blue",
    label: "Uploaded Documents",
    copy: "Answered from this layer's uploaded documents; sources cite file and page.",
  },
};

export default function ProvenanceBadge({ provenance }) {
  // Back-compat: legacy turns (and any error path) carry no provenance — render
  // nothing rather than a misleading or broken badge.
  if (!provenance || !provenance.tier) return null;

  const meta = TIER_META[provenance.tier];
  // Unknown tier: fail safe to a neutral badge showing the raw tier so we never
  // throw on a value the backend added that the UI hasn't mapped yet.
  const { color, label, copy } = meta || {
    color: "grey",
    label: provenance.tier,
    copy: "",
  };

  // A degraded Tier 2/VKG run still grounded its attempt in the schema — flag it
  // so the badge copy is honest about the degrade rather than implying a clean answer.
  const degradedNote = provenance.degraded
    ? ` (degraded: ${provenance.degraded})`
    : "";

  return (
    // No outer margin: the caller (ChatTranscript Bubble header) owns the
    // spacing now that the badge stacks directly under the role label.
    <Box>
      <SpaceBetween direction="horizontal" size="xs">
        <Badge color={color}>{label}</Badge>
        {copy ? (
          <Box variant="small" color="text-body-secondary" display="inline">
            {copy}
            {degradedNote}
          </Box>
        ) : null}
      </SpaceBetween>
    </Box>
  );
}
