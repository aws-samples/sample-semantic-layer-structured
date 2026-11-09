/**
 * ViewKnowledgeGraph must hand the governance tabs the layer's REAL type.
 *
 * It used to pass the literal "VKG" to both, which defeated each tab's own
 * applicability guard. That mattered because the page is reachable for a Hybrid
 * layer (`BuildKnowledgeGraph.handleViewGraph` navigates here unconditionally),
 * so a Hybrid layer rendered as if it were VKG.
 *
 * Separate file from `ViewKnowledgeGraph.test.jsx` because asserting the props
 * requires mocking the two tab components, and that file deliberately renders
 * them for real.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ViewKnowledgeGraph from "../pages/admin/ViewKnowledgeGraph";
import { neptuneAPI, ontologyAPI } from "../services/api";

jest.mock("../services/api");
jest.mock("../components/OntologyEditor", () => () => (
  <div>OntologyEditor</div>
));
jest.mock("../components/GraphVisualization", () => () => (
  <div>GraphVisualization</div>
));
jest.mock("../components/LessonsLearnedTab", () => () => (
  <div>LessonsLearnedTab</div>
));
jest.mock("../components/FeedbackTab", () => () => <div>FeedbackTab</div>);
jest.mock("../components/MonitoringTab", () => () => <div>MonitoringTab</div>);
jest.mock("../pages/admin/GroundTruthDataset", () => () => (
  <div>GroundTruthDataset</div>
));
jest.mock("../pages/admin/Evaluations", () => () => <div>Evaluations</div>);

// Capture the props each governance tab is rendered with.
const rulesProps = [];
const metricsProps = [];
jest.mock("../components/GovernedRulesTab", () => (props) => {
  rulesProps.push(props);
  return <div>GovernedRulesTab</div>;
});
jest.mock("../components/GovernedMetricsTab", () => (props) => {
  metricsProps.push(props);
  return <div>GovernedMetricsTab</div>;
});

// No local Cloudscape mock: `setupTests.js` already stubs the library globally,
// and its `Tabs` renders every tab's content, so both governance tabs mount.

/**
 * Stub the four calls `loadGraphData` makes, with a given stored layer type.
 *
 * All four must be stubbed: the page reads `.success` off each result, so an
 * automocked `undefined` throws and the type never reaches state.
 *
 * @param {string|null} type The `type` field on the ontology config payload.
 * @returns {void}
 */
function stubApis(type) {
  neptuneAPI.getGraphSummary = jest
    .fn()
    .mockResolvedValue({ success: true, data: { entities: [] } });
  neptuneAPI.getGraphStats = jest.fn().mockResolvedValue({
    success: true,
    data: { totalEdges: 0, totalClasses: 0, totalProperties: 0 },
  });
  ontologyAPI.getOntologyConfig = jest
    .fn()
    .mockResolvedValue({ success: true, data: type === null ? {} : { type } });
  ontologyAPI.getOntologyVersions = jest
    .fn()
    .mockResolvedValue({ success: true, data: { versions: [] } });
}

beforeEach(() => {
  rulesProps.length = 0;
  metricsProps.length = 0;
});

afterEach(() => {
  jest.clearAllMocks();
});

test.each(["VKG", "Hybrid", "SemanticRAG"])(
  "passes a stored type of %s straight through to both governance tabs",
  async (type) => {
    stubApis(type);
    render(
      <MemoryRouter initialEntries={["/?id=abc"]}>
        <ViewKnowledgeGraph user={{ email: "alice@x" }} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(rulesProps.at(-1)?.layerType).toBe(type));
    expect(metricsProps.at(-1)?.layerType).toBe(type);
  },
);

test("passes null rather than guessing when the config carries no type", async () => {
  stubApis(null);
  render(
    <MemoryRouter initialEntries={["/?id=abc"]}>
      <ViewKnowledgeGraph user={{ email: "alice@x" }} />
    </MemoryRouter>,
  );
  await screen.findByText("GovernedRulesTab");
  expect(rulesProps.at(-1)?.layerType ?? null).toBeNull();
  expect(metricsProps.at(-1)?.layerType ?? null).toBeNull();
});

test("records the reviewer identity on the rules tab", async () => {
  // Every threshold/rule approval is stamped with this; an approval with no
  // named human in it is not governance.
  stubApis("Hybrid");
  render(
    <MemoryRouter initialEntries={["/?id=abc"]}>
      <ViewKnowledgeGraph user={{ email: "alice@x" }} />
    </MemoryRouter>,
  );
  await waitFor(() => expect(rulesProps.at(-1)?.currentUser).toBe("alice@x"));
});
