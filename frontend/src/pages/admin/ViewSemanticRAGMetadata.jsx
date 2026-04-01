import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  SpaceBetween, Header, Container, Button, ColumnLayout,
  Box, StatusIndicator, Alert, Tabs, Table, Badge,
  ExpandableSection, Modal, Textarea, FormField,
} from '@cloudscape-design/components';
import {
  ontologyAPI, getMetadataEnrichmentStatus, startMetadataEnrichment, reviseMetadata,
  dataSourceAPI, getTableKBMetadata,
} from '../../services/api';


// ─── Glue catalog expandable card (Data Sources tab) ─────────────────────────

const GLUE_COL_DEFS = [
  { id: 'name', header: 'Column', cell: c => <Box fontWeight="bold">{c.name}</Box> },
  { id: 'type', header: 'Type', cell: c => <Box variant="code" fontSize="body-s">{c.type}</Box>, width: 180 },
  { id: 'desc', header: 'Description', cell: c => c.comment
      ? c.comment
      : <Box color="text-status-inactive">—</Box> },
];

function GlueTableCard({ ds }) {
  const [expanded, setExpanded] = useState(false);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState(null);

  const onToggle = async ({ detail }) => {
    setExpanded(detail.expanded);
    if (detail.expanded && !meta && !loading) {
      setLoading(true);
      setFetchError(null);
      const r = await dataSourceAPI.getTableMetadata(ds.databaseName, ds.tableName, ds.catalogId);
      setLoading(false);
      if (r.success) setMeta(r.data);
      else setFetchError(r.error || 'Failed to load schema');
    }
  };

  const columns = meta ? [...(meta.columns || []), ...(meta.partitionKeys || [])] : [];

  return (
    <ExpandableSection
      variant="container"
      expanded={expanded}
      onChange={onToggle}
      headerText={
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Badge color="blue">{ds.databaseName}</Badge>
          <Box fontWeight="bold">{ds.tableName}</Box>
        </SpaceBetween>
      }
      headerDescription={
        <Box variant="code" fontSize="body-s" color="text-status-inactive">
          {ds.catalogId || 'AWSDataCatalog'}
        </Box>
      }
    >
      {loading && (
        <Box padding="m">
          <StatusIndicator type="loading">Loading schema from Glue…</StatusIndicator>
        </Box>
      )}
      {fetchError && (
        <Alert type="error">{fetchError}</Alert>
      )}
      {meta && !loading && (
        <SpaceBetween size="m">
          {meta.description ? (
            <Box>
              <Box variant="awsui-key-label">Description</Box>
              <Box variant="p">{meta.description}</Box>
            </Box>
          ) : (
            <Box color="text-status-inactive" fontSize="body-s">No table description in catalog</Box>
          )}
          <Table
            variant="embedded"
            columnDefinitions={GLUE_COL_DEFS}
            items={columns}
            empty={
              <Box textAlign="center" color="text-status-inactive">No columns found</Box>
            }
          />
        </SpaceBetween>
      )}
    </ExpandableSection>
  );
}

// ─── Knowledge Base expandable card (Metadata tab) ───────────────────────────

// tableAnnotations: [{ id, tableKey, target, instruction }] filtered for this table
function KBTableCard({ ds, tableAnnotations, onAddAnnotation, onRemoveAnnotation, versionKey }) {
  const [expanded, setExpanded] = useState(false);
  const [meta, setMeta] = useState(null);
  const [loading, setLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [fetchError, setFetchError] = useState(null);

  // Modal state (stays local)
  const [pendingTarget, setPendingTarget] = useState('');
  const [showModal, setShowModal] = useState(false);
  const [comment, setComment] = useState('');

  // Reset when a new version has been created
  useEffect(() => {
    setMeta(null);
    setNotFound(false);
    setExpanded(false);
  }, [versionKey]);

  const onToggle = async ({ detail }) => {
    setExpanded(detail.expanded);
    if (detail.expanded && !meta && !loading && !notFound) {
      setLoading(true);
      setFetchError(null);
      const r = await getTableKBMetadata(ds.databaseName, ds.tableName, ds.catalogId);
      setLoading(false);
      if (r.success) setMeta(r.data);
      else if (r.error?.includes('not yet generated') || r.details?.detail?.includes('not yet generated')) setNotFound(true);
      else setFetchError(r.error || 'Failed to load metadata');
    }
  };

  const openAnnotate = (target) => {
    setPendingTarget(target);
    setShowModal(true);
    setComment('');
  };

  const closeModal = () => {
    setShowModal(false);
    setPendingTarget('');
    setComment('');
  };

  const confirmAnnotation = () => {
    onAddAnnotation({ id: Date.now().toString(), target: pendingTarget, instruction: comment });
    closeModal();
  };

  const annotatedTargets = new Set(tableAnnotations.map(a => a.target));

  const kbColDefs = [
    { id: 'name', header: 'Column', cell: c => <Box fontWeight="bold">{c.name}</Box> },
    { id: 'type', header: 'Type', cell: c => <Box variant="code" fontSize="body-s">{c.type}</Box>, width: 180 },
    {
      id: 'desc',
      header: 'Description',
      cell: c => c.description ? c.description : <Box color="text-status-inactive">—</Box>,
    },
    {
      id: 'annotate',
      header: '',
      width: 150,
      minWidth: 150,
      cell: c => {
        const key = `column:${c.name}`;
        const isAnnotated = annotatedTargets.has(key);
        return (
          <div style={{ whiteSpace: 'nowrap' }}>
            <Button
              variant={isAnnotated ? 'normal' : 'inline-link'}
              iconName={isAnnotated ? 'check' : 'add-plus'}
              size="small"
              onClick={() => openAnnotate(key)}
            >
              {isAnnotated ? 'Annotated' : 'Annotate'}
            </Button>
          </div>
        );
      },
    },
  ];

  const tableAnnotationCount = tableAnnotations.length;

  return (
    <>
      <ExpandableSection
        variant="container"
        expanded={expanded}
        onChange={onToggle}
        headerText={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <Badge color="blue">{ds.databaseName}</Badge>
            <Box fontWeight="bold">{ds.tableName}</Box>
            {tableAnnotationCount > 0 && (
              <Badge color="severity-medium">{tableAnnotationCount} pending</Badge>
            )}
          </SpaceBetween>
        }
        headerDescription={
          <Box variant="code" fontSize="body-s" color="text-status-inactive">
            {ds.catalogId || 'AWSDataCatalog'}
          </Box>
        }
      >
        {loading && (
          <Box padding="m">
            <StatusIndicator type="loading">Loading Knowledge Base metadata…</StatusIndicator>
          </Box>
        )}
        {notFound && (
          <Alert type="info">
            Metadata not yet generated for this table. Run enrichment to populate the Knowledge Base.
          </Alert>
        )}
        {fetchError && (
          <Alert type="error">{fetchError}</Alert>
        )}
        {meta && !loading && (
          <SpaceBetween size="m">
            <div>
              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <Box variant="awsui-key-label">Table Description</Box>
                <Button
                  variant={annotatedTargets.has('table_description') ? 'normal' : 'inline-link'}
                  iconName={annotatedTargets.has('table_description') ? 'check' : 'add-plus'}
                  size="small"
                  onClick={() => openAnnotate('table_description')}
                >
                  {annotatedTargets.has('table_description') ? 'Annotated' : 'Annotate'}
                </Button>
              </SpaceBetween>
              {meta.description
                ? <Box variant="p">{meta.description}</Box>
                : <Box color="text-status-inactive" fontSize="body-s">No description in Knowledge Base</Box>
              }
            </div>
            <Table
              variant="embedded"
              columnDefinitions={kbColDefs}
              items={meta.columns || []}
              empty={
                <Box textAlign="center" color="text-status-inactive">No columns found</Box>
              }
            />
          </SpaceBetween>
        )}
      </ExpandableSection>

      {/* ── Annotation modal ── */}
      <Modal
        visible={showModal}
        header={`Annotate: ${pendingTarget}`}
        onDismiss={closeModal}
        footer={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={closeModal}>Cancel</Button>
            <Button variant="primary" disabled={!comment.trim()} onClick={confirmAnnotation}>
              Confirm
            </Button>
          </SpaceBetween>
        }
      >
        <FormField label="Instruction — what should the agent do differently?">
          <Textarea
            value={comment}
            onChange={({ detail }) => setComment(detail.value)}
            rows={4}
            placeholder="e.g. This table stores monthly snapshots, not daily transactions"
          />
        </FormField>
      </Modal>
    </>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────

export default function ViewSemanticRAGMetadata() {
  const { id } = useParams();
  const navigate = useNavigate();

  const [config, setConfig] = useState(null);
  const [enrichmentStatus, setEnrichmentStatus] = useState(null);
  const [currentVersion, setCurrentVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [reenriching, setReenriching] = useState(false);

  // Global annotation state: [{ id, tableKey, target, instruction }]
  const [annotations, setAnnotations] = useState([]);
  const [versionKey, setVersionKey] = useState(0);

  const uniqueDatabases = useMemo(
    () => [...new Set((config?.dataSources || []).map(ds => ds.databaseName).filter(Boolean))].length,
    [config]
  );

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [configResult, statusResult, versionsResult] = await Promise.all([
        ontologyAPI.getOntologyConfig(id),
        getMetadataEnrichmentStatus(id),
        ontologyAPI.getOntologyVersions(id),
      ]);

      if (!configResult.success) throw new Error(configResult.error);
      setConfig(configResult.data);

      if (statusResult.success) setEnrichmentStatus(statusResult.data);

      if (versionsResult.success) {
        const versions = versionsResult.data?.versions ?? [];
        if (versions.length > 0) setCurrentVersion(versions[0].version);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { loadData(); }, [loadData]);

  // ── Global annotation helpers ──

  const handleAddAnnotation = useCallback((tableKey, annotation) => {
    setAnnotations(prev => [...prev, { ...annotation, tableKey }]);
  }, []);

  const handleRemoveAnnotation = useCallback((annotationId) => {
    setAnnotations(prev => prev.filter(a => a.id !== annotationId));
  }, []);

  // ── Create New Version (all annotations across all tables) ──

  const handleCreateNewVersion = async () => {
    setReenriching(true);
    setError(null);
    const result = await reviseMetadata(
      id,
      currentVersion,
      annotations.map(({ target, instruction, tableKey }) => ({ tableKey, target, instruction })),
    );
    if (!result.success) {
      setError(result.error);
      setReenriching(false);
      return;
    }
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      const status = await getMetadataEnrichmentStatus(id);
      if (status.success) setEnrichmentStatus(status.data);
      const st = status?.data?.status;
      if (st === 'completed' || st === 'failed' || attempts > 60) {
        clearInterval(poll);
        setReenriching(false);
        if (st === 'completed') {
          setAnnotations([]);
          setVersionKey(k => k + 1);
          const versResult = await ontologyAPI.getOntologyVersions(id);
          if (versResult.success) {
            const versions = versResult.data?.versions ?? [];
            if (versions.length > 0) setCurrentVersion(versions[0].version);
          }
        } else {
          setError('Re-enrichment failed. Check status for details.');
        }
      }
    }, 5000);
  };

  // ── Render ──

  const totalTables = enrichmentStatus?.totalTables || 0;
  const status = enrichmentStatus?.status || 'unknown';

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Step 5 of 5: View and iterate on your enriched semantic metadata"
        info={currentVersion && <Badge color="blue">{currentVersion}</Badge>}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" onClick={loadData} loading={loading}>
              Refresh
            </Button>
            <Button variant="primary" iconName="search" onClick={() => navigate('/query')}>
              Query this data
            </Button>
          </SpaceBetween>
        }
      >
        Semantic Metadata — {config?.name ?? '…'}
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading ? (
        <Container>
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">Loading semantic metadata…</StatusIndicator>
          </Box>
        </Container>
      ) : (
        <SpaceBetween size="l">
          {/* ── Name ── */}
          {config && (
            <Container>
              <Box variant="awsui-key-label">Name</Box>
              <Box variant="h2">{config.name ?? '—'}</Box>
            </Container>
          )}

          {/* ── Stats ── */}
          <Container
            header={
              <Header variant="h2" info={<Badge color={status === 'completed' ? 'green' : 'grey'}>{status}</Badge>}>
                Enrichment Summary
              </Header>
            }
          >
            <ColumnLayout columns={2} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Databases enriched</Box>
                <Box variant="h2" fontSize="display-l">{uniqueDatabases}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Tables enriched</Box>
                <Box variant="h2" fontSize="display-l">{totalTables}</Box>
              </div>
            </ColumnLayout>
          </Container>

          {/* ── Tabs ── */}
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
                        <Box variant="p">{config?.name ?? '—'}</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Use Case Details</Box>
                        {config?.useCasesDescription
                          ? <Box variant="p">{config.useCasesDescription}</Box>
                          : <Box color="text-status-inactive">No use case description provided</Box>
                        }
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Data Source Description</Box>
                        {config?.dataSourcesDescription
                          ? <Box variant="p">{config.dataSourcesDescription}</Box>
                          : <Box color="text-status-inactive">No data source description provided</Box>
                        }
                      </div>
                    </SpaceBetween>
                  </Container>
                ),
              },
              {
                label: `Data Sources (${config?.dataSources?.length ?? 0})`,
                id: 'datasources',
                content: (
                  <SpaceBetween size="s">
                    {(config?.dataSources ?? []).length === 0 ? (
                      <Container>
                        <Box textAlign="center" color="text-status-inactive" padding="l">
                          No data sources configured
                        </Box>
                      </Container>
                    ) : (
                      (config?.dataSources ?? []).map((ds) => (
                        <GlueTableCard key={`${ds.databaseName}.${ds.tableName}`} ds={ds} />
                      ))
                    )}
                  </SpaceBetween>
                ),
              },
              {
                label: 'Metadata',
                id: 'metadata',
                content: (
                  <SpaceBetween size="m">
                    {/* ── Pending annotations + Create New Version ── */}
                    {annotations.length > 0 && (
                      <Container
                        header={
                          <Header
                            variant="h3"
                            actions={
                              <Button
                                variant="primary"
                                loading={reenriching}
                                onClick={handleCreateNewVersion}
                              >
                                Create New Version ({annotations.length})
                              </Button>
                            }
                          >
                            Pending Annotations ({annotations.length})
                          </Header>
                        }
                      >
                        <SpaceBetween size="xs">
                          {annotations.map(a => (
                            <div key={a.id} style={{
                              display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
                              border: '1px solid #d5dbdb', borderRadius: 8,
                              background: '#f8f8f8', padding: '8px 12px',
                            }}>
                              <SpaceBetween size="xxs">
                                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                                  <Badge color="blue">{a.tableKey}</Badge>
                                  <Box variant="code" fontSize="body-s">{a.target}</Box>
                                </SpaceBetween>
                                <Box variant="p" fontSize="body-s">{a.instruction}</Box>
                              </SpaceBetween>
                              <Button
                                variant="icon"
                                iconName="close"
                                ariaLabel="Remove annotation"
                                onClick={() => handleRemoveAnnotation(a.id)}
                              />
                            </div>
                          ))}
                        </SpaceBetween>
                      </Container>
                    )}

                    {/* ── Table cards ── */}
                    <SpaceBetween size="s">
                      {(config?.dataSources ?? []).length === 0 ? (
                        <Container>
                          <Box textAlign="center" color="text-status-inactive" padding="l">
                            No data sources configured
                          </Box>
                        </Container>
                      ) : (
                        (config?.dataSources ?? []).map((ds) => {
                          const tableKey = `${ds.databaseName}.${ds.tableName}`;
                          return (
                            <KBTableCard
                              key={tableKey}
                              ds={ds}
                              tableAnnotations={annotations.filter(a => a.tableKey === tableKey)}
                              onAddAnnotation={(annotation) => handleAddAnnotation(tableKey, annotation)}
                              onRemoveAnnotation={handleRemoveAnnotation}
                              versionKey={versionKey}
                            />
                          );
                        })
                      )}
                    </SpaceBetween>
                  </SpaceBetween>
                ),
              },
            ]}
          />

          {status === 'completed' && (
            <Alert type="success" header="Semantic Metadata Ready">
              Your metadata has been enriched and synced to the Knowledge Base.
              You can now query your data using natural language.
            </Alert>
          )}
        </SpaceBetween>
      )}

    </SpaceBetween>
  );
}
