/**
 * ProvenanceBadge — a first-class, per-tier "where did this answer come from?"
 * badge rendered on each assistant turn.
 *
 * The trust contract differs per tier, so the badge is visually distinct:
 *   governed_metric → green  ("the system stands behind this number")
 *   semantic_sql    → blue   (grounded in executed SQL over the schema slice)
 *   vkg             → blue   (also a semantic-layer SQL path — SPARQL→SQL→Athena —
 *                             so it shares the blue "grounded in executed SQL" cue
 *                             with semantic_sql for visual consistency; the LABEL
 *                             distinguishes the two paths)
 *   advisory        → grey   ("describes the layer's metadata, not a computed result")
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
  advisory: {
    color: "grey",
    label: "Advisory",
    copy: "Describes the layer's metadata & governed metrics; not a data query.",
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
