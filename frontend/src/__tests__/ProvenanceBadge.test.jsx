/**
 * Tests for ProvenanceBadge — the per-tier "where did this answer come from?"
 * badge on each assistant turn.
 *
 * Asserts: each tier renders its distinct label + copy; an absent/empty
 * provenance renders nothing (back-compat with legacy turns); an unknown tier
 * fails safe to a neutral badge showing the raw tier; a degraded run is flagged.
 */
import fs from "fs";
import path from "path";
import React from "react";
import { render, screen } from "@testing-library/react";
import ProvenanceBadge from "../pages/query/ProvenanceBadge";

// Partial Cloudscape mock — Badge/Box/SpaceBetween render their children so we
// can assert on text content without the real design-system DOM.
jest.mock("@cloudscape-design/components", () => ({
  Badge: ({ children, color }) => <span data-color={color}>{children}</span>,
  Box: ({ children }) => <div>{children}</div>,
  SpaceBetween: ({ children }) => <div>{children}</div>,
}));

test("governed_metric renders the Governed Metric badge", () => {
  render(
    <ProvenanceBadge
      provenance={{ tier: "governed_metric", sources: ["metric:x"] }}
    />,
  );
  expect(screen.getByText("Governed Metric")).toBeInTheDocument();
  expect(
    screen.getByText(/Answered from a governed metric/i),
  ).toBeInTheDocument();
});

test("semantic_sql renders the Semantic Layer (SQL) badge", () => {
  render(
    <ProvenanceBadge
      provenance={{ tier: "semantic_sql", sources: ["table:coverage"] }}
    />,
  );
  expect(screen.getByText("Semantic Layer (SQL)")).toBeInTheDocument();
});

test("vkg renders the Knowledge Graph badge in BLUE (consistent with semantic_sql)", () => {
  render(
    <ProvenanceBadge provenance={{ tier: "vkg", sources: ["class:Party"] }} />,
  );
  const badge = screen.getByText("Knowledge Graph");
  expect(badge).toBeInTheDocument();
  // VKG is also a semantic-layer SQL path (SPARQL→SQL→Athena), so its badge
  // shares the blue colour with semantic_sql for visual consistency.
  expect(badge).toHaveAttribute("data-color", "blue");
});

test("governed_rule renders its own label in the governed GREEN", () => {
  render(
    <ProvenanceBadge
      provenance={{ tier: "governed_rule", sources: ["rule:RULE-004"] }}
    />,
  );
  const badge = screen.getByText("Governed Rule");
  // Green like governed_metric — both are human-approved — but a distinct label,
  // because "an approved rule fired" is not "a governed metric was computed".
  expect(badge).toHaveAttribute("data-color", "green");
  expect(screen.getByText(/approved rule fired/i)).toBeInTheDocument();
});

test("agentic renders a Tier 3 badge rather than a raw lowercase tier", () => {
  render(<ProvenanceBadge provenance={{ tier: "agentic", sources: ["kb"] }} />);
  const badge = screen.getByText(/Agentic \(Tier 3\)/);
  expect(badge).toBeInTheDocument();
  // Not blue: the answer is grounded, but only after Tier 2 degraded. Sharing
  // semantic_sql's blue would hide that the clean path failed.
  expect(badge).toHaveAttribute("data-color", "severity-medium");
});

test("advisory renders the Advisory badge", () => {
  render(
    <ProvenanceBadge provenance={{ tier: "advisory", sources: ["kb"] }} />,
  );
  expect(screen.getByText("Advisory")).toBeInTheDocument();
  expect(screen.getByText(/not a data query/i)).toBeInTheDocument();
});

test("absent provenance renders nothing (back-compat with legacy turns)", () => {
  const { container } = render(<ProvenanceBadge provenance={undefined} />);
  expect(container).toBeEmptyDOMElement();
});

test("provenance without a tier renders nothing", () => {
  const { container } = render(
    <ProvenanceBadge provenance={{ sources: [] }} />,
  );
  expect(container).toBeEmptyDOMElement();
});

/**
 * Read the backend's tier vocabulary out of agents/shared/provenance.py.
 *
 * The vocabulary is authored in Python and consumed here, so nothing but a test
 * couples the two. Parsing the source (rather than duplicating the list in JS)
 * is the point: a duplicated list drifts exactly like the map it is meant to
 * guard. Throws if the file or the tuple can't be found — a silently-empty
 * vocabulary would make the agreement test below pass vacuously.
 */
function readBackendTiers() {
  const file = path.resolve(__dirname, "../../../agents/shared/provenance.py");
  const source = fs.readFileSync(file, "utf8");
  const tuple = source.match(/VALID_TIERS = \(([^)]*)\)/);
  if (!tuple) throw new Error(`VALID_TIERS tuple not found in ${file}`);
  const tiers = [...tuple[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  if (tiers.length === 0) throw new Error(`VALID_TIERS is empty in ${file}`);
  return tiers;
}

test("every backend provenance tier has a real badge, not the raw-tier fallback", () => {
  // This is the drift guard: a tier added on the Python side without a TIER_META
  // entry renders as a grey badge showing its own lowercase identifier as the
  // label. That does not crash, so only an assertion catches it — which is how
  // `agentic` shipped unmapped.
  for (const tier of readBackendTiers()) {
    const { container, unmount } = render(
      <ProvenanceBadge provenance={{ tier, sources: [] }} />,
    );
    // The fallback's signature is a badge whose LABEL is the raw tier string, so
    // assert on an exact whole-text match (queryByText's default) rather than a
    // substring of the whole card: legitimate copy may contain the tier word
    // itself — the `document` tier's copy says "uploaded documents" — and a
    // substring check would fail that for no reason.
    expect(screen.queryByText(tier)).toBeNull();
    expect(container.textContent.trim()).not.toBe("");
    unmount();
  }
});

test("unknown tier fails safe to a neutral badge showing the raw tier", () => {
  render(<ProvenanceBadge provenance={{ tier: "freeform", sources: [] }} />);
  expect(screen.getByText("freeform")).toBeInTheDocument();
});

test("a degraded Tier 2 run is flagged in the copy", () => {
  render(
    <ProvenanceBadge
      provenance={{
        tier: "semantic_sql",
        sources: ["kb"],
        degraded: "phase3_max_rounds",
      }}
    />,
  );
  expect(screen.getByText(/degraded: phase3_max_rounds/i)).toBeInTheDocument();
});
