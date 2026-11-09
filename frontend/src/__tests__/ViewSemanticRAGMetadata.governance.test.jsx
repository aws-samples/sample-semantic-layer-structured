/**
 * The governance tabs on the page a NON-VKG layer lands on.
 *
 * `AdminDashboard.getActionButton` routes only `type === "VKG"` to
 * `/admin/view-graph`; every other completed layer — Hybrid included — goes to
 * `/admin/view-semantic-metadata/:id`, which is this page. So this page, not
 * `ViewKnowledgeGraph`, is the only rules surface a Hybrid reviewer can reach
 * from the dashboard, and it previously mounted Governed Metrics alone.
 *
 * The distinction that matters here is Hybrid vs Semantic RAG: both arrive on
 * this page, but only Hybrid builds the ontology graph a rule binds to.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ViewSemanticRAGMetadata from "../pages/admin/ViewSemanticRAGMetadata";
import {
  ontologyAPI,
  getMetadataEnrichmentStatus,
  computeMetadataDelta,
} from "../services/api";

jest.mock("../services/api");
jest.mock("../components/DeltaPreview", () => () => <div>DeltaPreview</div>);
jest.mock("../components/LessonsLearnedTab", () => () => (
  <div>LessonsLearnedTab</div>
));
jest.mock("../components/FeedbackTab", () => () => <div>FeedbackTab</div>);
jest.mock("../components/MonitoringTab", () => () => <div>MonitoringTab</div>);
jest.mock("../pages/admin/GroundTruthDataset", () => () => (
  <div>GroundTruthDataset</div>
));
jest.mock("../pages/admin/Evaluations", () => () => <div>Evaluations</div>);

// Capture the props each governance tab is rendered with. The rules tab is
// mocked rather than rendered so its absence is unambiguous — a real render
// would also print its own internal applicability alert, and the assertions
// below are about which of the two surfaces the PAGE chose.
const rulesProps = [];
jest.mock("../components/GovernedRulesTab", () => (props) => {
  rulesProps.push(props);
  return <div>GovernedRulesTab</div>;
});
jest.mock("../components/GovernedMetricsTab", () => () => (
  <div>GovernedMetricsTab</div>
));

/**
 * Stub the three calls `loadData` makes with a given stored layer type.
 *
 * The config call must resolve `success: true` or the page throws and renders
 * only its error alert — no tabs at all, which would make every assertion here
 * vacuously pass.
 *
 * @param {string|null} type The `type` field on the ontology config payload.
 * @returns {void}
 */
function stubApis(type) {
  ontologyAPI.getOntologyConfig = jest.fn().mockResolvedValue({
    success: true,
    data: type === null ? { dataSources: [] } : { type, dataSources: [] },
  });
  ontologyAPI.getOntologyVersions = jest
    .fn()
    .mockResolvedValue({ success: true, data: { versions: [] } });
  getMetadataEnrichmentStatus.mockResolvedValue({
    success: true,
    data: { status: "completed", totalTables: 0 },
  });
  computeMetadataDelta.mockResolvedValue({ success: true, data: {} });
}

/**
 * Render the page under a route that supplies the `:id` param.
 *
 * The page reads its layer id from `useParams`, and with no id it short-circuits
 * every tab to "No semantic layer selected" — which would make each assertion
 * below pass for the wrong reason.
 *
 * @returns {void}
 */
function renderPage() {
  render(
    <MemoryRouter initialEntries={["/ont-1"]}>
      <Routes>
        <Route
          path="/:id"
          element={<ViewSemanticRAGMetadata user={{ email: "alice@x" }} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

const NOT_APPLICABLE = /apply to VKG and Hybrid layers/i;

beforeEach(() => {
  rulesProps.length = 0;
});

afterEach(() => {
  jest.clearAllMocks();
});

test("mounts the rules tab for a Hybrid layer", async () => {
  stubApis("Hybrid");
  renderPage();
  expect(await screen.findByText("GovernedRulesTab")).toBeInTheDocument();
  expect(screen.queryByText(NOT_APPLICABLE)).not.toBeInTheDocument();
});

test("explains why rules are unavailable on a Semantic RAG layer", async () => {
  // Not merely hidden: the notice states the reason. A Semantic RAG layer
  // documents tables into a Bedrock KB and builds no graph, so there is no IRI
  // for a rule's binding to resolve against — and no amount of authoring will
  // change that, which is exactly what a reviewer needs told.
  stubApis("SemanticRAG");
  renderPage();
  expect(await screen.findByText(NOT_APPLICABLE)).toBeInTheDocument();
  expect(screen.queryByText("GovernedRulesTab")).not.toBeInTheDocument();
});

test("makes no applicability claim before the layer type has loaded", async () => {
  // The type comes from a config fetch that can return a payload with no `type`
  // at all (older layers). Rendering the not-applicable notice off that would
  // tell a Hybrid reviewer something false about their own layer.
  stubApis(null);
  renderPage();
  expect(await screen.findByText("GovernedRulesTab")).toBeInTheDocument();
  expect(screen.queryByText(NOT_APPLICABLE)).not.toBeInTheDocument();
});

test("passes the layer's real type and the reviewer identity to the tab", async () => {
  // currentUser is stamped on every threshold and rule approval, and the API
  // rejects a blank approver with 422 — so a page that forgets to thread the
  // signed-in user makes approval impossible rather than anonymous.
  stubApis("Hybrid");
  renderPage();
  await waitFor(() => expect(rulesProps.at(-1)?.layerType).toBe("Hybrid"));
  expect(rulesProps.at(-1)?.currentUser).toBe("alice@x");
});
