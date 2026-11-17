/**
 * Build-step copy for the two layer types this page serves.
 *
 * /admin/build-graph handles BOTH VKG and Hybrid builds — POST /ontology/build
 * routes on the stored type — but every word on the page described the VKG
 * ontology agent. A Hybrid build therefore claimed to be retrieving ontology
 * design patterns, generating an OWL ontology and saving a Turtle backup, none
 * of which the Hybrid creation agent does; and its two phases (documentation
 * into the Bedrock Knowledge Base, then a deterministic structural mapping into
 * Neptune) were invisible.
 *
 * These tests pin the branch, both phase explainers, and the phase-specific
 * failure copy — the last one matters because a mapping failure leaves the
 * Knowledge Base documents written while a documentation failure leaves nothing.
 */
import React from "react";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import BuildKnowledgeGraph from "../pages/admin/BuildKnowledgeGraph";
import { ontologyAPI } from "../services/api";

jest.mock("../services/api");
jest.mock("../components/CancelDraftButton", () => () => <div>Cancel</div>);

jest.mock("@cloudscape-design/components", () => ({
  Container: ({ children }) => <div>{children}</div>,
  Header: ({ children, description }) => (
    <div>
      <h1>{children}</h1>
      <p>{description}</p>
    </div>
  ),
  SpaceBetween: ({ children }) => <div>{children}</div>,
  Button: ({ children, ...props }) => <button {...props}>{children}</button>,
  Alert: ({ children, header }) => (
    <div>
      {header}
      {children}
    </div>
  ),
  Box: ({ children }) => <div>{children}</div>,
  ProgressBar: ({ description }) => <div>{description}</div>,
  StatusIndicator: ({ children }) => <div>{children}</div>,
}));

/**
 * Render the build step for a layer of the given type.
 *
 * @param {string} type The stored layer type ("VKG" | "Hybrid").
 * @returns {Promise<void>} Resolves once the type lookup has settled.
 */
async function renderForType(type) {
  ontologyAPI.getOntologyConfig = jest
    .fn()
    .mockResolvedValue({ success: true, data: { id: "lay-1", type } });
  await act(async () => {
    render(
      <MemoryRouter initialEntries={["/admin/build-graph?id=lay-1"]}>
        <BuildKnowledgeGraph user={{}} />
      </MemoryRouter>,
    );
  });
}

/**
 * Start the build and let one poll land with the given status payload.
 *
 * @param {object} status The build-status fields the poll should report.
 * @returns {Promise<void>} Resolves once the poll's state updates are applied.
 */
async function pollOnce(status) {
  ontologyAPI.buildOntology = jest.fn().mockResolvedValue({ success: true });
  ontologyAPI.getBuildStatus = jest
    .fn()
    .mockResolvedValue({ success: true, data: status });
  await act(async () => {
    fireEvent.click(screen.getByText("Start Build"));
  });
  await act(async () => {
    jest.advanceTimersByTime(3000);
  });
}

beforeEach(() => {
  jest.useFakeTimers();
});

afterEach(() => {
  jest.useRealTimers();
  jest.clearAllMocks();
});

describe("BuildKnowledgeGraph copy per layer type", () => {
  it("describes both Hybrid phases before the build starts", async () => {
    await renderForType("Hybrid");
    expect(
      screen.getByText(/Document each table in the Bedrock Knowledge Base/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Phase 1 — Documentation/)).toBeInTheDocument();
    expect(screen.getByText(/Phase 2 — Structure/)).toBeInTheDocument();
    // Hybrid measures its join keys in Athena rather than trusting the names.
    expect(screen.getByText(/measuring it in Athena/)).toBeInTheDocument();
  });

  it("does not claim Hybrid does any of the VKG agent's work", async () => {
    await renderForType("Hybrid");
    // The Hybrid agent retrieves no design patterns, writes no Turtle, and uses
    // no model at all in the phase that produces the graph.
    expect(screen.queryByText(/ontology design patterns/)).toBeNull();
    expect(screen.queryByText(/Turtle format backup/)).toBeNull();
    expect(screen.queryByText(/business terminology/)).toBeNull();
  });

  it("leaves the VKG copy unchanged", async () => {
    await renderForType("VKG");
    expect(
      screen.getByText(/Generate metadata with Virtual Knowledge Graph/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Turtle format backup/)).toBeInTheDocument();
    expect(screen.queryByText(/Phase 1 — Documentation/)).toBeNull();
  });

  it("names the documentation phase while it runs", async () => {
    await renderForType("Hybrid");
    await pollOnce({
      status: "buildingKB",
      progressPercent: 17,
      tablesProcessed: 2,
      totalTables: 6,
      currentTable: "cards",
    });
    expect(
      screen.getByText(/Phase 1 of 2: documenting your tables/),
    ).toBeInTheDocument();
    // Both phases count the same tables, so the verb is what distinguishes them.
    expect(screen.getByText("2 of 6 tables documented")).toBeInTheDocument();
    expect(
      screen.getByText("Documenting table 2 of 6: cards"),
    ).toBeInTheDocument();
  });

  it("names the mapping phase while it runs", async () => {
    await renderForType("Hybrid");
    await pollOnce({
      status: "buildingVKG",
      progressPercent: 95,
      tablesProcessed: 6,
      totalTables: 6,
      currentTable: "sets",
    });
    expect(
      screen.getByText(/Phase 2 of 2: mapping structure/),
    ).toBeInTheDocument();
    expect(screen.getByText("6 of 6 tables mapped")).toBeInTheDocument();
  });

  it("says the Knowledge Base survived a mapping failure", async () => {
    await renderForType("Hybrid");
    await pollOnce({
      status: "failed:vkg",
      tablesProcessed: 6,
      totalTables: 6,
      error: "gateway timeout",
    });
    expect(
      screen.getByText(/failed while mapping their structure into Neptune/),
    ).toBeInTheDocument();
  });

  it("says nothing was written when the documentation phase failed", async () => {
    await renderForType("Hybrid");
    await pollOnce({
      status: "failed:kb",
      tablesProcessed: 1,
      totalTables: 6,
      error: "throttled",
    });
    expect(screen.getByText(/no graph was created/)).toBeInTheDocument();
  });
});
