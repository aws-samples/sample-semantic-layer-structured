import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ViewKnowledgeGraph from '../pages/admin/ViewKnowledgeGraph';
import { neptuneAPI } from '../services/api';

jest.mock('../services/api');
jest.mock('../components/OntologyEditor', () => () => <div>OntologyEditor</div>);
jest.mock('../components/GraphVisualization', () => () => <div>GraphVisualization</div>);

jest.mock('@cloudscape-design/components', () => ({
  Container: ({ children }) => <div>{children}</div>,
  Header: ({ children }) => <h1>{children}</h1>,
  SpaceBetween: ({ children }) => <div>{children}</div>,
  Button: ({ children, ...props }) => <button {...props}>{children}</button>,
  Alert: ({ children }) => <div>{children}</div>,
  Box: ({ children, color, ...props }) => <div {...props}>{children}</div>,
  ColumnLayout: ({ children }) => <div>{children}</div>,
  StatusIndicator: ({ children }) => <div>{children}</div>,
  Badge: ({ children }) => <span>{children}</span>,
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
          <tr><td>{empty || 'Empty'}</td></tr>
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
    entities: [
      { name: 'Entity1', type: 'Type1', description: 'Desc1' },
    ],
    relationships: [
      { name: 'rel1', from: 'Entity1', to: 'Entity2' },
    ],
    properties: [
      { name: 'prop1', entity: 'Entity1', dataType: 'string', description: 'Prop desc' },
    ],
  };

  const mockGraphStats = {
    totalEdges: 100,
    totalClasses: 10,
    totalProperties: 20,
  };

  neptuneAPI.getGraphSummary = jest.fn().mockResolvedValue({ success: true, data: mockGraphSummary });
  neptuneAPI.getGraphStats = jest.fn().mockResolvedValue({ success: true, data: mockGraphStats });
});

afterEach(() => {
  jest.clearAllMocks();
});

describe('ViewKnowledgeGraph', () => {
  it('renders Edit Ontology tab', async () => {
    render(
      <MemoryRouter initialEntries={['/?id=abc']}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>
    );
    const editOntologyTab = await screen.findByText(/edit ontology/i);
    expect(editOntologyTab).toBeInTheDocument();
  });

  it('renders all five tabs', async () => {
    render(
      <MemoryRouter initialEntries={['/?id=abc']}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>
    );
    expect(await screen.findByText(/visual graph/i)).toBeInTheDocument();
    expect(screen.getByText(/entities \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText(/relationships \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText(/properties \(\d+\)/i)).toBeInTheDocument();
    expect(screen.getByText(/edit ontology/i)).toBeInTheDocument();
  });

  it('renders OntologyEditor component when id is provided', async () => {
    render(
      <MemoryRouter initialEntries={['/?id=test-ontology-id']}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>
    );
    const ontologyEditor = await screen.findByText('OntologyEditor');
    expect(ontologyEditor).toBeInTheDocument();
  });

  it('shows no ontology selected message when id is missing', async () => {
    render(
      <MemoryRouter initialEntries={['/?id=']}>
        <ViewKnowledgeGraph user={{}} />
      </MemoryRouter>
    );
    // When id is empty, the component sets an error
    expect(screen.getByText(/no ontology/i)).toBeInTheDocument();
  });
});
