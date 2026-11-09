/**
 * A semantic layer must be QUERIED by the same kind of engine that BUILT it.
 *
 * The resolver in LandingPage was a two-way branch (`type === "SemanticRAG" ?
 * "semantic-rag" : "vkg"`) for as long as there were only two layer types. When
 * Hybrid was added, "Hybrid" is not "SemanticRAG", so it fell through to the
 * "vkg" arm: a Hybrid layer was silently answered by the VKG agent and labelled
 * "Knowledge Graph (VKG)" in the UI. Nothing failed — the answer was simply
 * produced by the wrong engine, which is the hardest class of bug to notice.
 *
 * These tests pin the (layer type -> chat mode) mapping for all three types by
 * asserting on the navigation state LandingPage packages up, because that state
 * is what ChatView feeds to useChatStream and ultimately to CHAT_TARGETS.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LandingPage from "../pages/query/LandingPage";
import { ontologyAPI, queryAPI } from "../services/api";

jest.mock("../services/api");

const mockNavigate = jest.fn();
jest.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

// The composer is the submit surface; stub it down to a single button that
// submits a fixed question so the test does not depend on Cloudscape internals.
jest.mock("../pages/query/Composer", () => ({ onSubmit, disabled }) => (
  <button disabled={disabled} onClick={() => onSubmit("how many claims?")}>
    submit
  </button>
));

jest.mock("@cloudscape-design/components", () => ({
  Alert: ({ children, header }) => (
    <div>
      {header}
      {children}
    </div>
  ),
  Box: ({ children }) => <div>{children}</div>,
  Button: ({ children, ...props }) => <button {...props}>{children}</button>,
  Container: ({ children }) => <div>{children}</div>,
  Header: ({ children }) => <h1>{children}</h1>,
  // Only one layer is ever offered in these tests, and LandingPage auto-selects
  // the first option, so the Select needs no interaction — just render the
  // current selection so the "Mode:" label is assertable.
  Select: ({ selectedOption }) => <div>{selectedOption?.label}</div>,
  SpaceBetween: ({ children }) => <div>{children}</div>,
  StatusIndicator: ({ children }) => <div>{children}</div>,
}));

/**
 * Arrange a single completed layer of the given type and render the page.
 *
 * @param {string} type Stored `type` on the layer config ("VKG" | "SemanticRAG"
 *   | "Hybrid", or a bogus value to exercise the unmapped path).
 * @param {object} flags Feature flags passed to LandingPage.
 */
async function renderWithLayerType(
  type,
  flags = {},
  { expectVisible = true } = {},
) {
  ontologyAPI.listOntologies.mockResolvedValue({
    success: true,
    data: {
      ontologies: [
        { id: "L1", name: "ACME", status: "completed", latestVersion: "v3" },
      ],
    },
  });
  ontologyAPI.getOntologyConfig.mockResolvedValue({
    success: true,
    data: { type, version: "v3" },
  });
  queryAPI.getSuggestedQuestions.mockResolvedValue({
    success: true,
    data: { suggestions: [] },
  });

  render(
    <LandingPage enableSemanticRag enableHybrid={flags.enableHybrid ?? true} />,
  );
  if (expectVisible) {
    await waitFor(() => expect(screen.getByText("ACME")).toBeInTheDocument());
  } else {
    // The layer is expected to be FILTERED OUT, so waiting for it would always
    // time out. Wait for the settled empty state instead.
    await waitFor(() =>
      expect(
        screen.getByText(/No semantic layers available/i),
      ).toBeInTheDocument(),
    );
  }
}

/** @returns {object} The navigation state from the single navigate() call. */
function navState() {
  expect(mockNavigate).toHaveBeenCalledTimes(1);
  return mockNavigate.mock.calls[0][1].state;
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe.each([
  ["VKG", "vkg", "Knowledge Graph (VKG)"],
  ["SemanticRAG", "semantic-rag", "Semantic RAG"],
  ["Hybrid", "hybrid", "Hybrid"],
])("a %s layer", (layerType, expectedMode, expectedLabel) => {
  it(`is queried in "${expectedMode}" mode`, async () => {
    await renderWithLayerType(layerType);
    await userEvent.click(screen.getByText("submit"));

    const state = navState();
    expect(state.mode).toBe(expectedMode);
    expect(state.ontologyId).toBe("L1");
  });

  it(`is labelled "${expectedLabel}" to the user`, async () => {
    // The label is rendered as "Mode: " + the label in separate text nodes, so
    // match on the container's textContent rather than an exact-node lookup.
    // A Hybrid layer used to read "Knowledge Graph (VKG)" here.
    await renderWithLayerType(layerType);
    expect(document.body.textContent).toContain(`Mode: ${expectedLabel}`);
  });
});

it("refuses to start a chat for a layer type with no query agent", async () => {
  // The regression guard: an unmapped type must NOT quietly pick an engine.
  await renderWithLayerType("SomeFutureType");
  await userEvent.click(screen.getByText("submit"));

  expect(mockNavigate).not.toHaveBeenCalled();
  expect(screen.getByText(/no query agent/i)).toBeInTheDocument();
});

it("hides Hybrid layers when the hybrid runtime is not deployed", async () => {
  // enableHybrid=false means the semantic-layer-query gateway target does not
  // exist, so offering the layer would fail on the first question.
  await renderWithLayerType(
    "Hybrid",
    { enableHybrid: false },
    { expectVisible: false },
  );
  expect(screen.queryByText("ACME")).not.toBeInTheDocument();
});
