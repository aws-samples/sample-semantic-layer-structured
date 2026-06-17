/**
 * Tests the Phase 3 slice view/download in ReasoningPanel (todo item 2).
 *
 * PhaseDetail is internal; we render the default-exported ReasoningPanel with a
 * `phases` prop whose Phase 3 row carries `result.slice` (the JSON string the
 * slice builder produced) and assert the slice summary header + a Download
 * control appear.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import ReasoningPanel from "../pages/query/ReasoningPanel";

const sliceJson = JSON.stringify({
  tables: ["normalized.admin_codes"],
  columns: [{ table_id: "normalized.admin_codes", name: "code_value" }],
  joins: [],
});

test("renders a downloadable slice summary for a Phase 3 row", () => {
  const phases = [
    {
      phase: 3,
      step: null,
      round: 1,
      status: "success",
      result: { sufficient: true, tableCount: 1, slice: sliceJson },
    },
  ];
  render(<ReasoningPanel phases={phases} turnId="t1" />);
  // ExpandableSection header shows table + column counts.
  expect(screen.getByText(/Slice \(1 table/i)).toBeInTheDocument();
  // A download control is present.
  expect(screen.getByText(/Download slice/i)).toBeInTheDocument();
});

// The VKG (ontology_query) agent emits a Turtle slice, not JSON. The same Phase
// 3 row should render a triple-count summary + a Turtle download control.
const sliceTurtle = `@prefix ex: <http://example.org/> .
ex:Party a owl:Class .
ex:hasParty a owl:ObjectProperty .
ex:partyId a owl:DatatypeProperty .
`;

test("renders a downloadable Turtle slice summary for a VKG Phase 3 row", () => {
  const phases = [
    {
      phase: 3,
      step: null,
      round: 1,
      status: "success",
      result: { sufficient: true, classCount: 1, slice: sliceTurtle },
    },
  ];
  render(<ReasoningPanel phases={phases} turnId="t2" />);
  // Header reflects Turtle, not tables/columns.
  expect(
    screen.getByText(/Ontology slice \(3 triples, Turtle\)/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/Download slice \(Turtle\)/i)).toBeInTheDocument();
});
