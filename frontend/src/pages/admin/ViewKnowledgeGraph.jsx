import React, { useState, useEffect, useMemo } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  ColumnLayout,
  StatusIndicator,
  Badge,
  Table,
  Tabs,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { neptuneAPI, ontologyAPI } from '../../services/api';
import GraphVisualization from '../../components/GraphVisualization';
import OntologyEditor from '../../components/OntologyEditor';
import { transformGraphData } from '../../utils/graphTransform';

function useSortedItems(items, sorting) {
  return useMemo(() => {
    if (!sorting.sortingColumn) return items;
    const field = sorting.sortingColumn.sortingField;
    return [...items].sort((a, b) => {
      const valA = (a[field] ?? '').toString().toLowerCase();
      const valB = (b[field] ?? '').toString().toLowerCase();
      const cmp = valA < valB ? -1 : valA > valB ? 1 : 0;
      return sorting.isDescending ? -cmp : cmp;
    });
  }, [items, sorting]);
}

// Approximate height of everything above the graph canvas:
// app nav + page header + stats container + tab bar + padding
const GRAPH_HEIGHT_OFFSET = 390;

function useGraphHeight() {
  const [height, setHeight] = useState(() =>
    Math.max(400, window.innerHeight - GRAPH_HEIGHT_OFFSET)
  );
  useEffect(() => {
    const onResize = () =>
      setHeight(Math.max(400, window.innerHeight - GRAPH_HEIGHT_OFFSET));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return height;
}

export default function ViewKnowledgeGraph({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get('id');
  const graphHeight = useGraphHeight();

  const [graphSummary, setGraphSummary] = useState(null);
  const [graphStats, setGraphStats] = useState(null);
  const [ontologyConfig, setOntologyConfig] = useState(null);
  const [currentVersion, setCurrentVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [entitiesSorting, setEntitiesSorting] = useState({});
  const [relationshipsSorting, setRelationshipsSorting] = useState({});
  const [propertiesSorting, setPropertiesSorting] = useState({});

  const sortedEntities = useSortedItems(graphSummary?.entities ?? [], entitiesSorting);
  const sortedRelationships = useSortedItems(graphSummary?.relationships ?? [], relationshipsSorting);
  const sortedProperties = useSortedItems(graphSummary?.properties ?? [], propertiesSorting);

  useEffect(() => {
    if (!id) {
      setError('No ID provided.');
      setLoading(false);
      return;
    }
    loadGraphData();
  }, [id]);

  const loadGraphData = async () => {
    setLoading(true);
    setError(null);

    try {
      const [summaryResult, statsResult, configResult, versionsResult] = await Promise.all([
        neptuneAPI.getGraphSummary(id),
        neptuneAPI.getGraphStats(id),
        ontologyAPI.getOntologyConfig(id),
        ontologyAPI.getOntologyVersions(id),
      ]);

      if (summaryResult.success) setGraphSummary(summaryResult.data);
      if (statsResult.success) setGraphStats(statsResult.data);
      if (configResult.success) setOntologyConfig(configResult.data);
      if (versionsResult.success) {
        const versions = versionsResult.data?.versions ?? [];
        if (versions.length > 0) setCurrentVersion(versions[0].version);
      }

      if (!summaryResult.success && !statsResult.success) {
        setError('Failed to load graph data');
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 5 of 5: View and iterate on your knowledge graph"
        info={currentVersion && <Badge color="blue">{currentVersion}</Badge>}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" onClick={loadGraphData} loading={loading}>
              Refresh
            </Button>
            <Button
              variant="primary"
              iconName="search"
              onClick={() => navigate('/query/ask')}
            >
              Start Querying
            </Button>
          </SpaceBetween>
        }
      >
        {ontologyConfig?.name ? `Knowledge Graph — ${ontologyConfig.name}` : 'Knowledge Graph Overview'}
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading ? (
        <Container>
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">
              Loading knowledge graph data...
            </StatusIndicator>
          </Box>
        </Container>
      ) : (
        <SpaceBetween size="l">
          {ontologyConfig && (
            <Container>
              <Box variant="awsui-key-label">Name</Box>
              <Box variant="h2">{ontologyConfig.name ?? '—'}</Box>
            </Container>
          )}

          <Container
            header={
              <Header variant="h2" info={<Badge color="green">Active</Badge>}>
                Graph Statistics
              </Header>
            }
          >
            {graphStats ? (
              <ColumnLayout columns={4} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Total Triples</Box>
                  <Box variant="h2" fontSize="display-l">{graphStats.totalEdges || 0}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Entities</Box>
                  <Box variant="h2" fontSize="display-l">{graphStats.totalClasses || 0}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Properties</Box>
                  <Box variant="h2" fontSize="display-l">{(graphSummary?.properties ?? []).length || 0}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Relationships</Box>
                  <Box variant="h2" fontSize="display-l">
                    {(graphSummary?.relationships ?? []).length}
                  </Box>
                </div>
              </ColumnLayout>
            ) : (
              <Box textAlign="center" color="text-status-inactive">
                No graph statistics available
              </Box>
            )}
          </Container>

          {graphSummary && (
            <Tabs
              tabs={[
                {
                  label: 'Details',
                  id: 'details',
                  content: (
                    <Container>
                      <SpaceBetween size="l">
                        <div>
                          <Box variant="awsui-key-label">Name</Box>
                          <Box variant="p">{ontologyConfig?.name ?? '—'}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Use Case Details</Box>
                          {ontologyConfig?.useCasesDescription
                            ? <Box variant="p">{ontologyConfig.useCasesDescription}</Box>
                            : <Box color="text-status-inactive">No use case description provided</Box>
                          }
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Data Source Description</Box>
                          {ontologyConfig?.dataSourcesDescription
                            ? <Box variant="p">{ontologyConfig.dataSourcesDescription}</Box>
                            : <Box color="text-status-inactive">No data source description provided</Box>
                          }
                        </div>
                      </SpaceBetween>
                    </Container>
                  ),
                },
                {
                  label: 'Visual Graph',
                  id: 'visual',
                  content: (
                    <Container>
                      <GraphVisualization
                        graphData={transformGraphData(graphSummary)}
                        summary={graphSummary}
                        height={graphHeight}
                      />
                    </Container>
                  ),
                },
                {
                  label: `Entities (${sortedEntities.length})`,
                  id: 'entities',
                  content: (
                    <Container>
                      <Table
                        sortingColumn={entitiesSorting.sortingColumn}
                        sortingDescending={entitiesSorting.isDescending}
                        onSortingChange={({ detail }) => setEntitiesSorting(detail)}
                        columnDefinitions={[
                          {
                            id: 'name',
                            header: 'Entity Name',
                            cell: (item) => <Badge color="blue">{item.name}</Badge>,
                            sortingField: 'name',
                          },
                          {
                            id: 'type',
                            header: 'Type',
                            cell: (item) => item.type || '-',
                            sortingField: 'type',
                          },
                          {
                            id: 'description',
                            header: 'Description',
                            cell: (item) => item.description || '-',
                            sortingField: 'description',
                          },
                        ]}
                        items={sortedEntities}
                        empty={
                          <Box textAlign="center" color="text-status-inactive">
                            No entities found
                          </Box>
                        }
                      />
                    </Container>
                  ),
                },
                {
                  label: `Relationships (${sortedRelationships.length})`,
                  id: 'relationships',
                  content: (
                    <Container>
                      <Table
                        sortingColumn={relationshipsSorting.sortingColumn}
                        sortingDescending={relationshipsSorting.isDescending}
                        onSortingChange={({ detail }) => setRelationshipsSorting(detail)}
                        columnDefinitions={[
                          {
                            id: 'name',
                            header: 'Relationship',
                            cell: (item) => <Badge color="green">{item.name}</Badge>,
                            sortingField: 'name',
                          },
                          {
                            id: 'from',
                            header: 'From Entity',
                            cell: (item) => item.from || '-',
                            sortingField: 'from',
                          },
                          {
                            id: 'to',
                            header: 'To Entity',
                            cell: (item) => item.to || '-',
                            sortingField: 'to',
                          },
                        ]}
                        items={sortedRelationships}
                        empty={
                          <Box textAlign="center" color="text-status-inactive">
                            No relationships found
                          </Box>
                        }
                      />
                    </Container>
                  ),
                },
                {
                  label: `Properties (${sortedProperties.length})`,
                  id: 'properties',
                  content: (
                    <Container>
                      <Table
                        sortingColumn={propertiesSorting.sortingColumn}
                        sortingDescending={propertiesSorting.isDescending}
                        onSortingChange={({ detail }) => setPropertiesSorting(detail)}
                        columnDefinitions={[
                          {
                            id: 'name',
                            header: 'Property Name',
                            cell: (item) => item.name || '-',
                            sortingField: 'name',
                          },
                          {
                            id: 'entity',
                            header: 'Entity',
                            cell: (item) => item.entity || '-',
                            sortingField: 'entity',
                          },
                          {
                            id: 'dataType',
                            header: 'Data Type',
                            cell: (item) => <Badge>{item.dataType || 'string'}</Badge>,
                            sortingField: 'dataType',
                          },
                          {
                            id: 'description',
                            header: 'Description',
                            cell: (item) => item.description || '-',
                            sortingField: 'description',
                          },
                        ]}
                        items={sortedProperties}
                        empty={
                          <Box textAlign="center" color="text-status-inactive">
                            No properties found
                          </Box>
                        }
                      />
                    </Container>
                  ),
                },
                {
                  label: 'Metadata',
                  id: 'edit',
                  content: id ? (
                    <OntologyEditor id={id} />
                  ) : (
                    <Box color="text-status-inactive" padding="l" textAlign="center">
                      No semantic layer selected
                    </Box>
                  ),
                },
              ]}
            />
          )}

          <Alert type="success" header="Knowledge Graph Ready">
            Your semantic layer is now fully configured and ready to use.
            You can start asking natural language questions about your data.
          </Alert>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
