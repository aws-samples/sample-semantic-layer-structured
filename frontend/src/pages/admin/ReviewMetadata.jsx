import React, { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  Table,
  ExpandableSection,
  StatusIndicator,
  Badge,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { dataSourceAPI, ontologyAPI } from '../../services/api';

export default function ReviewMetadata({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get('id');

  const [metadata, setMetadata] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!id) {
      setError('No ID provided. Please complete previous steps first.');
      return;
    }
    loadMetadata();
  }, [id]);

  const loadMetadata = async () => {
    setLoading(true);
    setError(null);

    try {
      // Get ontology config to retrieve selected data sources
      const configResult = await ontologyAPI.getOntologyConfig(id);
      if (!configResult.success) {
        setError('Failed to load semantic layer configuration');
        setLoading(false);
        return;
      }

      // Backend stores as 'dataSources' (not 'selectedDataSources')
      const dataSources = configResult.data.dataSources || [];

      if (dataSources.length === 0) {
        setError('No data sources found. Please go back and select data sources.');
        setLoading(false);
        return;
      }

      // Extract metadata from selected tables
      const extractResult = await dataSourceAPI.extractMetadata(dataSources);

      if (extractResult.success) {
        // Transform backend response to match frontend expectations
        const backendData = extractResult.data;
        const transformedMetadata = {
          tables: backendData.dataSources?.map(ds => ({
            tableName: `${ds.database}.${ds.table}`,
            database: ds.database,
            table: ds.table,
            description: ds.metadata?.description || ds.description || null,
            columns: ds.metadata?.columns || []
          })) || [],
          relationships: backendData.relationships || [],
          uploadedDocuments: configResult.data.uploadedDocuments || [],
          totalTables: backendData.totalTables || 0,
          totalColumns: backendData.totalColumns || 0
        };

        setMetadata(transformedMetadata);
      } else {
        setError(extractResult.error || 'Failed to extract metadata');
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const handleContinue = () => {
    navigate(`/admin/select-semantic-layer-type/${id}`);
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 3 of 5: Review metadata extracted from your selected data sources"
      >
        Review Metadata
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
              Extracting metadata from selected data sources...
            </StatusIndicator>
          </Box>
        </Container>
      ) : !metadata ? (
        <Container>
          <Alert type="warning">
            No metadata available. Please go back and select data sources.
          </Alert>
        </Container>
      ) : (
        <SpaceBetween size="l">
          <Container
            header={
              <Header
                variant="h2"
                info={
                  <Badge color="blue">
                    {metadata.tables ? metadata.tables.length : 0} Tables
                  </Badge>
                }
              >
                Discovered Metadata
              </Header>
            }
          >
            {metadata.tables && metadata.tables.length > 0 ? (
              <SpaceBetween size="m">
                {metadata.tables.map((table) => (
                  <ExpandableSection
                    key={table.tableName}
                    headerText={table.tableName}
                    headerDescription={`${table.columns ? table.columns.length : 0} columns`}
                    defaultExpanded={false}
                  >
                    <SpaceBetween size="s">
                      {table.description && (
                        <Box color="text-body-secondary">{table.description}</Box>
                      )}
                    <Table
                      columnDefinitions={[
                        {
                          id: 'name',
                          header: 'Column Name',
                          cell: (item) => item.name || '-',
                        },
                        {
                          id: 'type',
                          header: 'Data Type',
                          cell: (item) => <Badge>{item.type || 'unknown'}</Badge>,
                        },
                        {
                          id: 'comment',
                          header: 'Description',
                          cell: (item) => item.comment || '-',
                        },
                      ]}
                      items={table.columns || []}
                      variant="embedded"
                      empty={
                        <Box textAlign="center" color="text-status-inactive">
                          No columns found
                        </Box>
                      }
                    />
                    </SpaceBetween>
                  </ExpandableSection>
                ))}
              </SpaceBetween>
            ) : (
              <Box textAlign="center" color="text-status-inactive" padding="l">
                No tables found in metadata
              </Box>
            )}
          </Container>

          {metadata.uploadedDocuments && metadata.uploadedDocuments.length > 0 && (
            <Container
              header={
                <Header
                  variant="h2"
                  info={<Badge color="blue">{metadata.uploadedDocuments.length} Files</Badge>}
                >
                  Uploaded Reference Documents
                </Header>
              }
            >
              <Table
                columnDefinitions={[
                  {
                    id: 'filename',
                    header: 'File Name',
                    cell: (item) => item.filename || '-',
                  },
                  {
                    id: 'size',
                    header: 'Size',
                    cell: (item) => item.size ? `${(item.size / 1024).toFixed(2)} KB` : '-',
                  },
                  {
                    id: 'path',
                    header: 'Location',
                    cell: (item) => (
                      <Box fontSize="body-s" color="text-body-secondary">
                        {item.path || '-'}
                      </Box>
                    ),
                  },
                ]}
                items={metadata.uploadedDocuments}
                variant="embedded"
                empty={
                  <Box textAlign="center" color="text-status-inactive">
                    No documents uploaded
                  </Box>
                }
              />
            </Container>
          )}

          {metadata.relationships && metadata.relationships.length > 0 && (
            <Container
              header={
                <Header
                  variant="h2"
                  info={<Badge color="green">{metadata.relationships.length} Detected</Badge>}
                >
                  Potential Table Relationships
                </Header>
              }
            >
              <Alert type="info">
                Relationships are detected based on column naming patterns (e.g., columns ending with "_id").
                These are suggestions and may require verification.
              </Alert>
              <Box margin={{ top: 's' }}>
                <Table
                  columnDefinitions={[
                    {
                      id: 'source',
                      header: 'Source Table',
                      cell: (item) => item.sourceTable || '-',
                    },
                    {
                      id: 'column',
                      header: 'Source Column',
                      cell: (item) => <Badge>{item.sourceColumn || '-'}</Badge>,
                    },
                    {
                      id: 'target',
                      header: 'Possible Target',
                      cell: (item) => item.targetTable || '-',
                    },
                    {
                      id: 'type',
                      header: 'Type',
                      cell: (item) => (
                        <Badge color="blue">{item.relationship || 'unknown'}</Badge>
                      ),
                    },
                  ]}
                  items={metadata.relationships}
                  variant="embedded"
                  empty={
                    <Box textAlign="center" color="text-status-inactive">
                      No relationships detected
                    </Box>
                  }
                />
              </Box>
            </Container>
          )}

          <Container>
            <SpaceBetween size="m">
              <Alert type="success">
                Metadata extraction completed successfully!
              </Alert>
              <Box>
                <SpaceBetween size="xs">
                  <Box variant="h4">Summary</Box>
                  <Box variant="p">
                    • <strong>{metadata.totalTables || 0} tables</strong> selected with{' '}
                    <strong>{metadata.totalColumns || 0} columns</strong> in total
                  </Box>
                  {metadata.uploadedDocuments && metadata.uploadedDocuments.length > 0 && (
                    <Box variant="p">
                      • <strong>{metadata.uploadedDocuments.length} reference documents</strong> uploaded
                      to provide additional context
                    </Box>
                  )}
                  {metadata.relationships && metadata.relationships.length > 0 && (
                    <Box variant="p">
                      • <strong>{metadata.relationships.length} potential relationships</strong> detected
                      based on naming patterns
                    </Box>
                  )}
                </SpaceBetween>
              </Box>
            </SpaceBetween>
          </Container>

          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate(`/admin/select-datasources?id=${id}`)}>
                Back
              </Button>
              <Button variant="primary" onClick={handleContinue}>
                Continue
              </Button>
            </SpaceBetween>
          </Box>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
