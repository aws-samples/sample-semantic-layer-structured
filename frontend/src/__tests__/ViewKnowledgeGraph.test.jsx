import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ViewKnowledgeGraph from "../pages/admin/ViewKnowledgeGraph";
import { neptuneAPI, metricsAPI } from "../services/api";

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
// GroundTruthDataset + Evaluations are admin tabs added after this test was
// written; stub them like the other tab components so the partial Cloudscape
// mock below doesn't need every component each tab imports (e.g. FileUpload).
jest.mock("../pages/admin/GroundTruthDataset", () => () => (
  <div>GroundTruthDataset</div>
));
jest.mock("../pages/admin/Evaluations", () => () => <div>Evaluations</div>);

jest.mock("@cloudscape-design/components", () => ({
  Container: ({ children }) => <div>{children}</div>,
  Header: ({ children }) => <h1>{children}</h1>,
  SpaceBetween: ({ children }) => <div>{children}</div>,
  Button: ({ children, ...props }) => <button {...props}>{children}</button>,
  Alert: ({ children }) => <div>{children}</div>,
  Box: ({ children, color, ...props }) => <div {...props}>{children}</div>,
  ColumnLayout: ({ children }) => <div>{children}</div>,
  StatusIndicator: ({ children }) => <div>{children}</div>,
  Badge: ({ children }) => <span>{children}</span>,
  // GovernedMetricsTab (rendered un-mocked inside the admin tabs) also imports
  // these — without stubs they resolve to undefined ("Element type is invalid").
  Form: ({ children }) => <form>{children}</form>,
  FormField: ({ children, label }) => (
    <div>
      {label}
      {children}
    </div>
  ),
  Input: ({ value, onChange, ...props }) => (
    <input
      value={value}
      onChange={(e) =>
        onChange && onChange({ detail: { value: e.target.value } })
      }
      {...props}
    />
  ),
  Textarea: ({ value, onChange, ...props }) => (
    <textarea
      value={value}
      onChange={(e) =>
        onChange && onChange({ detail: { value: e.target.value } })
      }
      {...props}
    />
  ),
  Select: ({ selectedOption, ...props }) => (
    <select {...props}>{selectedOption?.label}</select>
  ),
  Modal: ({ children, visible }) => (visible ? <div>{children}</div> : null),
  Spinner: () => <div>Loading</div>,
  Table: ({ items, empty, columnDefinitions }) => (
    <table>
      <tbody>
        {items && items.length > 0 ? (
          items.map((item, idx) => (
            <tr key={idx}>
              {columnDefinitions.map((col) => (
                <td key={col.id}>{col.cell ? col.cell(item) : item[col.id]}</td>
              ))}
            </tr>
          ))
        ) : (
          <tr>
            <td>{empty || "Empty"}</td>
          </tr>
        )}
      </tbody>
    </table>
  ),
  Tabs: ({ tabs }) => (
    <div>
      {tabs.map((tab) => (
        <div key={tab.id}>
          <div className="tab-label">{tab.label}</div>
          <div className="tab-content">{tab.content}</div>
        </div>
      ))}
    </div>
  ),
}));

beforeEach(() => {
  const mockGraphSummary = {
    entities: [{ name: "Entity1", type: "Type1", description: "Desc1" }],
    relationships: [{ name: "rel1", from: "Entity1", to: "Entity2" }],
    properties: [
      {
        name: "prop1",
        entity: "Entity1",
        dataType: "string",
        description: "Prop desc",
      },
    ],
  };

  const mockGraphStats = {
    totalEdges: 100,
    totalClasses: 10,
    totalProperties: 20,
  };

  neptuneAPI.getGraphSummary = jest
    .fn()
    .mockResolvedValue({ success: true, data: mockGraphSummary });
  neptuneAPI.getGraphStats = jest
    .fn()
    .mockResolvedValue({ success: true, data: mockGraphStats });
  // GovernedMetricsTab calls metricsAPI.list(ontologyId) on mount and reads
  // res.success; without this stub the auto-mock returns undefined and throws.
  metricsAPI.list = jest.fn().mockResolvedValue({ success: true, data: [] });
});

afterEach(() => {
  jest.clearAllMocks();
});

describe("ViewKnowledgeGraph", () => {
  it("renders Metadata tab (ontology editor)", async () => {
    render(
      <MemoryRouter initialEntries={["/?id=abc"]}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Metadata")).toBeInTheDocument();
  });

  it("renders all admin tabs", async () => {
    render(
      <MemoryRouter initialEntries={["/?id=abc"]}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/visual graph/i)).toBeInTheDocument();
    expect(screen.getByText(/entities \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText(/relationships \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText(/properties \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText("Metadata")).toBeInTheDocument();
    expect(screen.getByText(/lessons learned/i)).toBeInTheDocument();
    // "Feedback" appears as both the tab label and the FeedbackTab placeholder
    expect(screen.getAllByText(/feedback/i).length).toBeGreaterThan(0);
  });

  it("renders OntologyEditor component when id is provided", async () => {
    render(
      <MemoryRouter initialEntries={["/?id=test-ontology-id"]}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>,
    );
    const ontologyEditor = await screen.findByText("OntologyEditor");
    expect(ontologyEditor).toBeInTheDocument();
  });

  it("shows error message when id is missing", async () => {
    render(
      <MemoryRouter initialEntries={["/?id="]}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/no id provided/i)).toBeInTheDocument();
  });
});
