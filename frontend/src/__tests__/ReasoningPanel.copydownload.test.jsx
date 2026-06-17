/**
 * Tests the copy/download controls added to the ReasoningPanel phase timeline:
 *   - Phase 4/5 inline query rows get a "Copy SQL" / "Copy SPARQL" control.
 *   - Phase 5 execution results get a "Download CSV" + "Copy results" control.
 *
 * PhaseDetail / PhaseTimeline are internal, so we drive them through the
 * default-exported ReasoningPanel with a crafted `phases` prop.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import ReasoningPanel from "../pages/query/ReasoningPanel";

// Cloudscape's CopyToClipboard carries a 'use client' directive whose named
// re-export resolves to `undefined` under CRA's Jest resolver, and
// jest.requireActual on the barrel hits an ESM dir-import that fails to parse —
// both are test-env quirks; the real webpack build renders these fine. Rather
// than fight the resolver, stub the small set of Cloudscape primitives
// ReasoningPanel uses with minimal DOM passthroughs that preserve the bits the
// assertions read (button/section text). The component's OWN logic — when a
// copy/download control renders and with what label — is what we're testing.
jest.mock("@cloudscape-design/components", () => {
  const react = require("react");
  const passthrough = (tag) => (props) =>
    react.createElement(tag, null, props.children);
  return {
    __esModule: true,
    Box: passthrough("div"),
    SpaceBetween: passthrough("div"),
    StatusIndicator: passthrough("span"),
    ExpandableSection: ({ headerText, children }) =>
      react.createElement("section", null, headerText, children),
    Button: ({ children, onClick, disabled }) =>
      react.createElement(
        "button",
        { type: "button", onClick, disabled },
        children,
      ),
    Table: ({ empty }) => react.createElement("div", null, empty),
    CopyToClipboard: ({ copyButtonText }) =>
      react.createElement("button", { type: "button" }, copyButtonText),
  };
});

test("Phase 4 SQL row exposes a Copy SQL control", () => {
  const phases = [
    {
      phase: 4,
      step: null,
      round: 1,
      status: "success",
      result: { sql: "SELECT COUNT(DISTINCT admin_code_id) FROM admin_codes" },
    },
  ];
  render(<ReasoningPanel phases={phases} turnId="t1" />);
  // The generated SQL is shown inline...
  expect(
    screen.getByText(/SELECT COUNT\(DISTINCT admin_code_id\)/i),
  ).toBeInTheDocument();
  // ...with a dialect-labelled copy control (CopyToClipboard renders its
  // button text as the accessible name).
  expect(screen.getByRole("button", { name: /Copy SQL/i })).toBeInTheDocument();
});

test("Phase 4 SPARQL row labels the copy control SPARQL", () => {
  const phases = [
    {
      phase: 4,
      step: null,
      round: 1,
      status: "success",
      result: {
        sparql:
          "SELECT (COUNT(DISTINCT ?adminCodeId) AS ?n) WHERE { ?x a ex:AdminCode }",
      },
    },
  ];
  render(<ReasoningPanel phases={phases} turnId="t2" />);
  expect(
    screen.getByRole("button", { name: /Copy SPARQL/i }),
  ).toBeInTheDocument();
});

test("Phase 5 results row exposes Download CSV + Copy results", () => {
  const phases = [
    {
      phase: 5,
      step: null,
      round: 1,
      status: "success",
      result: {
        rowCount: 1,
        columns: ["unique_admin_codes_count"],
        rows: [[10]],
      },
    },
  ];
  render(<ReasoningPanel phases={phases} turnId="t3" />);
  expect(screen.getByText(/Results \(1 row\)/i)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /Download CSV/i }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /Copy results/i }),
  ).toBeInTheDocument();
});
