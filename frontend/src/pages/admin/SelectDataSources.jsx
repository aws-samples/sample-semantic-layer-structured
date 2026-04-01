import React, { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Alert,
  Box,
  Badge,
  Checkbox,
  ColumnLayout,
  FormField,
  FileUpload,
  ExpandableSection,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { dataSourceAPI, ontologyAPI } from '../../services/api';

// Derive a short human-readable label and badge colour from a catalogId string.
// AWSDataCatalog  → "DynamoDB Catalog"   blue
// s3tablescatalog/<bucket> → "S3 Tables (<bucket>)"  green
function catalogLabel(catalogId) {
  if (!catalogId || catalogId === 'AWSDataCatalog') {
    return { text: 'AWS Data Catalog', color: 'blue' };
  }
  if (catalogId.startsWith('s3tablescatalog/')) {
    const bucket = catalogId.replace('s3tablescatalog/', '');
    return { text: `S3 Tables · ${bucket}`, color: 'green' };
  }
  return { text: catalogId, color: 'grey' };
}

// Build a stable unique key for a table across all catalogs.
function tableKey(catalogId, databaseName, tableName) {
  return `${catalogId || 'AWSDataCatalog'}::${databaseName}.${tableName}`;
}

// Derive the Athena data source from a catalogId.
// - undefined / 'AWSDataCatalog'     → 'AwsDataCatalog'  (built-in Glue catalog)
// - starts with 's3tablescatalog/'   → 'AwsDataCatalog'  (S3 Tables sub-catalog)
// - anything else (e.g. 'dynamodb_catalog') → catalogId  (federated connector IS the data source)
function dataSourceForCatalog(catalogId) {
  if (!catalogId || catalogId === 'AWSDataCatalog') return 'AwsDataCatalog';
  if (catalogId.startsWith('s3tablescatalog/')) return 'AwsDataCatalog';
  return catalogId;
}

export default function SelectDataSources({ user }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const id = searchParams.get('id');

  const [databases, setDatabases] = useState([]);
  const [selectedTables, setSelectedTables] = useState([]);
  const [ontologyFile, setOntologyFile] = useState([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (!id) {
      setError('No ID provided. Please complete Step 1 first.');
      return;
    }
    loadDataSources();
  }, [id]);

  const loadDataSources = async () => {
    setLoading(true);
    const result = await dataSourceAPI.listGlueDatabases();

    if (result.success) {
      // Load tables for each database, passing its catalogId so the backend
      // queries the correct catalog.
      const databasesWithTables = await Promise.all(
        result.data.databases.map(async (db) => {
          const tablesResult = await dataSourceAPI.listGlueTables(db.name, db.catalogId);
          return {
            ...db,
            tables: tablesResult.success ? tablesResult.data.tables : [],
          };
        })
      );
      setDatabases(databasesWithTables);
    } else {
      setError(result.error || 'Failed to load data sources');
    }
    setLoading(false);
  };

  const handleDatabaseSelection = (catalogId, databaseName, tables, checked) => {
    const resolvedCatalogId = catalogId || 'AWSDataCatalog';
    // Remove any existing entries for this catalog+database
    const others = selectedTables.filter(
      (t) => !(t.databaseName === databaseName && t.catalogId === resolvedCatalogId)
    );
    if (checked) {
      // Expand to individual table entries so the agent always receives explicit table names
      const tableEntries = (tables || []).map((table) => ({
        dataSource: dataSourceForCatalog(resolvedCatalogId),
        catalogId: resolvedCatalogId,
        databaseName,
        tableName: table.name,
        tableId: tableKey(resolvedCatalogId, databaseName, table.name),
      }));
      setSelectedTables([...others, ...tableEntries]);
    } else {
      setSelectedTables(others);
    }
  };

  const handleTableSelection = (catalogId, databaseName, tableName, checked) => {
    const resolvedCatalogId = catalogId || 'AWSDataCatalog';
    const key = tableKey(resolvedCatalogId, databaseName, tableName);
    // If entire database was selected, remove the database-level entry first
    const withoutDbLevel = selectedTables.filter(
      (t) => !(t.databaseName === databaseName && t.catalogId === resolvedCatalogId && t.tableName === null)
    );
    if (checked) {
      setSelectedTables([
        ...withoutDbLevel.filter((t) => t.tableId !== key),
        {
          dataSource: dataSourceForCatalog(resolvedCatalogId),
          catalogId: resolvedCatalogId,
          databaseName,
          tableName,
          tableId: key,
        },
      ]);
    } else {
      setSelectedTables(withoutDbLevel.filter((t) => t.tableId !== key));
    }
  };

  const isTableSelected = (catalogId, databaseName, tableName) => {
    const resolvedCatalogId = catalogId || 'AWSDataCatalog';
    // Selected if individual entry exists OR database-level entry covers it
    return selectedTables.some(
      (t) =>
        t.catalogId === resolvedCatalogId &&
        t.databaseName === databaseName &&
        (t.tableName === tableName || t.tableName === null)
    );
  };

  const isDatabaseSelected = (catalogId, databaseName, tables) => {
    const resolvedCatalogId = catalogId || 'AWSDataCatalog';
    // True if there's a database-level entry OR all individual tables are selected
    const hasDbLevel = selectedTables.some(
      (t) => t.databaseName === databaseName && t.catalogId === resolvedCatalogId && t.tableName === null
    );
    if (hasDbLevel) return true;
    if (!tables || tables.length === 0) return false;
    const inDb = selectedTables.filter(
      (t) => t.databaseName === databaseName && t.catalogId === resolvedCatalogId && t.tableName !== null
    );
    return inDb.length === tables.length;
  };

  const isDatabaseIndeterminate = (catalogId, databaseName, tables) => {
    const resolvedCatalogId = catalogId || 'AWSDataCatalog';
    // Not indeterminate if entire database is selected
    const hasDbLevel = selectedTables.some(
      (t) => t.databaseName === databaseName && t.catalogId === resolvedCatalogId && t.tableName === null
    );
    if (hasDbLevel) return false;
    if (!tables || tables.length === 0) return false;
    const inDb = selectedTables.filter(
      (t) => t.databaseName === databaseName && t.catalogId === resolvedCatalogId && t.tableName !== null
    );
    return inDb.length > 0 && inDb.length < tables.length;
  };

  const handleSubmit = async () => {
    if (selectedTables.length === 0) {
      setError('Please select at least one data source');
      return;
    }

    setError(null);
    setSuccess(false);
    setSubmitting(true);

    try {
      // Upload multiple files if provided
      const uploadedFilePaths = [];
      if (ontologyFile.length > 0) {
        for (let i = 0; i < ontologyFile.length; i++) {
          const file = ontologyFile[i];
          const uploadResult = await ontologyAPI.uploadOntologyFile(file, id);
          if (uploadResult.success) {
            uploadedFilePaths.push({
              filename: file.name,
              path: uploadResult.data.path,
              size: file.size,
            });
          } else {
            setError(`File upload failed for "${file.name}": ${uploadResult.error}`);
            setSubmitting(false);
            return;
          }
        }
      }

      const updateData = {
        id: id,
        selectedDataSources: selectedTables,
        uploadedDocuments: uploadedFilePaths,
        status: 'data_sources_selected',
      };

      const result = await ontologyAPI.createOntologyConfig(updateData);

      if (result.success) {
        setSuccess(true);
        setTimeout(() => {
          navigate(`/admin/review-metadata?id=${id}`);
        }, 1500);
      } else {
        setError(result.error || 'Failed to save data source selection');
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setSubmitting(false);
    }
  };

  // Group databases by catalogId for display
  const groupedByCatalog = databases.reduce((acc, db) => {
    const key = db.catalogId || 'AWSDataCatalog';
    if (!acc[key]) acc[key] = [];
    acc[key].push(db);
    return acc;
  }, {});

  const uniqueCatalogs = Object.keys(groupedByCatalog).sort();

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 2 of 5: Select data sources from AWS Glue Catalog and optionally upload existing documentation"
      >
        Select Data Sources
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {success && (
        <Alert type="success">
          Data sources saved successfully! Redirecting to next step...
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description="Select entire databases or individual tables from the AWS Glue Data Catalog"
          >
            Available Data Sources
          </Header>
        }
      >
        {loading ? (
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">
              Loading data sources from Glue Catalog...
            </StatusIndicator>
          </Box>
        ) : databases.length === 0 ? (
          <Box textAlign="center" padding="l" color="text-status-inactive">
            No databases found in Glue Catalog. Please ensure your Glue crawlers have run.
          </Box>
        ) : (
          <SpaceBetween size="l">
            {uniqueCatalogs.map((catId) => {
              const { text: catText, color: catColor } = catalogLabel(catId);
              return (
                <SpaceBetween key={catId} size="s">
                  {/* Catalog section header */}
                  <Box>
                    <Badge color={catColor}>{catText}</Badge>
                  </Box>

                  {groupedByCatalog[catId].map((database) => (
                    <Container key={`${catId}::${database.name}`}>
                      <SpaceBetween size="s">
                        {/* Database-level checkbox */}
                        <Checkbox
                          checked={isDatabaseSelected(database.catalogId, database.name, database.tables)}
                          indeterminate={isDatabaseIndeterminate(database.catalogId, database.name, database.tables)}
                          onChange={({ detail }) =>
                            handleDatabaseSelection(
                              database.catalogId,
                              database.name,
                              database.tables,
                              detail.checked
                            )
                          }
                        >
                          <SpaceBetween size="xxxs">
                            <Box variant="strong" fontSize="heading-m">
                              {database.name}
                            </Box>
                            <Box variant="small" color="text-body-secondary">
                              {database.description || 'No description'} •{' '}
                              {database.tables?.length || 0} tables
                            </Box>
                          </SpaceBetween>
                        </Checkbox>

                        {/* Tables within database */}
                        <ExpandableSection
                          headerText="View individual tables"
                          variant="footer"
                          defaultExpanded={false}
                        >
                          {database.tables && database.tables.length > 0 ? (
                            <Box padding={{ top: 's' }}>
                              <ColumnLayout columns={2}>
                                {database.tables.map((table) => (
                                  <Checkbox
                                    key={tableKey(database.catalogId, database.name, table.name)}
                                    checked={isTableSelected(database.catalogId, database.name, table.name)}
                                    onChange={({ detail }) =>
                                      handleTableSelection(
                                        database.catalogId,
                                        database.name,
                                        table.name,
                                        detail.checked
                                      )
                                    }
                                  >
                                    <SpaceBetween size="xxxs">
                                      <Box variant="strong">{table.name}</Box>
                                      <Box variant="small" color="text-body-secondary">
                                        {table.description || 'No description'}
                                      </Box>
                                    </SpaceBetween>
                                  </Checkbox>
                                ))}
                              </ColumnLayout>
                            </Box>
                          ) : (
                            <Box color="text-status-inactive">No tables found</Box>
                          )}
                        </ExpandableSection>
                      </SpaceBetween>
                    </Container>
                  ))}
                </SpaceBetween>
              );
            })}
          </SpaceBetween>
        )}

        {selectedTables.length > 0 && (
          <Box margin={{ top: 'l' }}>
            <Header variant="h3">
              Selected sources ({selectedTables.length} entr{selectedTables.length !== 1 ? 'ies' : 'y'})
            </Header>
            <SpaceBetween size="m">
              {selectedTables.map((t) => (
                <Box key={t.tableId}>
                  <Table
                    variant="embedded"
                    columnDefinitions={[
                      { id: 'dataSource', header: 'Data source', cell: (item) => item.dataSource || 'AwsDataCatalog' },
                      { id: 'catalogId',  header: 'Catalog',     cell: (item) => item.catalogId || 'AWSDataCatalog' },
                      { id: 'database',   header: 'Database',    cell: (item) => item.databaseName },
                      { id: 'table',      header: 'Table',       cell: (item) => item.tableName || <Box color="text-status-inactive">All tables</Box> },
                    ]}
                    items={[t]}
                    empty={<Box color="text-status-inactive">No sources selected</Box>}
                  />
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Upload reference documentation to provide additional context (optional)"
          >
            Upload Additional Documentation
          </Header>
        }
      >
        <FormField
          description="Upload existing semantic metadata, taxonomies, data dictionaries, or any documentation that provides context about your data sources"
        >
          <FileUpload
            value={ontologyFile}
            onChange={({ detail }) => setOntologyFile(detail.value)}
            accept=".md,.markdown,.txt,.pdf,.docx"
            constraintText="Supported formats: Markdown (.md), plain text (.txt), PDF, Word (.docx)"
            multiple
            showFileSize
            showFileLastModified
            i18nStrings={{
              uploadButtonText: e => e ? "Choose files" : "Choose file",
              dropzoneText: e => e ? "Drop files to upload" : "Drop file to upload",
              removeFileAriaLabel: e => `Remove file ${e + 1}`,
              limitShowFewer: "Show fewer files",
              limitShowMore: "Show more files",
              errorIconAriaLabel: "Error"
            }}
          >
            Choose files
          </FileUpload>
        </FormField>
      </Container>

      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => navigate(`/admin/describe-intent?id=${id}`)}>
            Back
          </Button>
          <Button
            variant="primary"
            onClick={handleSubmit}
            loading={submitting}
            disabled={selectedTables.length === 0 || submitting}
          >
            Next: Review Metadata
          </Button>
        </SpaceBetween>
      </Box>
    </SpaceBetween>
  );
}
