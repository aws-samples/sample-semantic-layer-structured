/**
 * Tests for ProvenanceBadge — the per-tier "where did this answer come from?"
 * badge on each assistant turn.
 *
 * Asserts: each tier renders its distinct label + copy; an absent/empty
 * provenance renders nothing (back-compat with legacy turns); an unknown tier
 * fails safe to a neutral badge showing the raw tier; a degraded run is flagged.
 */
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
