import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Box,
  SpaceBetween,
  StatusIndicator,
  Alert,
  Button,
  Modal,
  Textarea,
  FormField,
  Select,
  Header,
  Table,
  Badge,
  ButtonGroup,
  ExpandableSection,
} from '@cloudscape-design/components';
import { ontologyAPI } from '../services/api';

// ─── RDF / OWL constants ────────────────────────────────────────────────────

const RDF_TYPE    = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type';
const OWL_CLASS   = 'http://www.w3.org/2002/07/owl#Class';
const OWL_DTP     = 'http://www.w3.org/2002/07/owl#DatatypeProperty';
const OWL_OBP     = 'http://www.w3.org/2002/07/owl#ObjectProperty';
const RDFS_LABEL  = 'http://www.w3.org/2000/01/rdf-schema#label';
const RDFS_COMMENT= 'http://www.w3.org/2000/01/rdf-schema#comment';
const RDFS_DOMAIN = 'http://www.w3.org/2000/01/rdf-schema#domain';
const RDFS_RANGE  = 'http://www.w3.org/2000/01/rdf-schema#range';
const VKG_DB      = 'https://semantic-layer.aws/virtual-kg/hasDatabase';
const VKG_TABLE   = 'https://semantic-layer.aws/virtual-kg/mapsToTable';
const VKG_COL     = 'https://semantic-layer.aws/virtual-kg/mapsToColumn';

// ─── N-Quads parser ──────────────────────────────────────────────────────────

// Returns Map<subjectUri, Map<predicateUri, string[]>>
function parseNQuads(content) {
  const subjects = new Map();
  for (const raw of content.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;

    // <subject> <predicate> (<object>|"literal"[^^<type>|@lang]) <graph> .
    const m = line.match(
      /^<([^>]+)>\s+<([^>]+)>\s+((?:<[^>]+>)|(?:"(?:[^"\\]|\\.)*"(?:\^\^<[^>]+>|@[\w-]+)?))\s+<[^>]+>\s*\.\s*$/
    );
    if (!m) continue;
    const [, sub, pred, obj] = m;
    if (!subjects.has(sub)) subjects.set(sub, new Map());
    const preds = subjects.get(sub);
    if (!preds.has(pred)) preds.set(pred, []);
    preds.get(pred).push(obj);
  }
  return subjects;
}

function getLiteral(preds, key) {
  const vals = preds.get(key);
  if (!vals || !vals[0]) return '';
  const m = vals[0].match(/^"((?:[^"\\]|\\.)*)"(?:\^\^<[^>]*>|@[\w-]+)?$/);
  return m ? m[1].replace(/\\"/g, '"') : '';
}

function getUri(preds, key) {
  const vals = preds.get(key);
  return vals?.[0]?.replace(/^<|>$/g, '') || '';
}

function hasType(preds, typeUri) {
  return (preds.get(RDF_TYPE) || []).includes(`<${typeUri}>`);
}

function xsdShort(uri) {
  if (!uri) return '';
  const h = uri.lastIndexOf('#');
  return h !== -1 ? `xsd:${uri.slice(h + 1)}` : uri.split('/').pop();
}

// Builds a sorted array of class objects with their properties and relationships
function buildOntologyModel(subjects) {
  const classes = new Map();
  const dtProps = [];
  const obProps = [];

  for (const [uri, preds] of subjects) {
    if (hasType(preds, OWL_CLASS)) {
      classes.set(uri, {
        uri,
        label:       getLiteral(preds, RDFS_LABEL) || uri.split('/').pop(),
        comment:     getLiteral(preds, RDFS_COMMENT),
        mapsToTable: getLiteral(preds, VKG_TABLE),
        database:    getLiteral(preds, VKG_DB),
        properties:  [],
        relationships: [],
      });
    } else if (hasType(preds, OWL_DTP)) {
      dtProps.push({
        uri,
        label:        getLiteral(preds, RDFS_LABEL) || uri.split('/').pop(),
        comment:      getLiteral(preds, RDFS_COMMENT),
        domain:       getUri(preds, RDFS_DOMAIN),
        range:        xsdShort(getUri(preds, RDFS_RANGE)),
        mapsToColumn: getLiteral(preds, VKG_COL),
      });
    } else if (hasType(preds, OWL_OBP)) {
      obProps.push({
        uri,
        label:  getLiteral(preds, RDFS_LABEL) || uri.split('/').pop(),
        comment:getLiteral(preds, RDFS_COMMENT),
        domain: getUri(preds, RDFS_DOMAIN),
        range:  getUri(preds, RDFS_RANGE),
      });
    }
  }

  for (const p of dtProps) {
    if (classes.has(p.domain)) classes.get(p.domain).properties.push(p);
  }
  for (const p of obProps) {
    if (classes.has(p.domain)) classes.get(p.domain).relationships.push(p);
  }

  return [...classes.values()].sort((a, b) => a.label.localeCompare(b.label));
}

// ─── Structured view sub-components ─────────────────────────────────────────

function TypeBadge({ type }) {
  const colors = {
    'xsd:string':   'grey',
    'xsd:integer':  'blue',
    'xsd:long':     'blue',
    'xsd:double':   'severity-medium',
    'xsd:boolean':  'severity-low',
    'xsd:date':     'green',
    'xsd:dateTime': 'green',
  };
  return <Badge color={colors[type] || 'grey'}>{type || 'string'}</Badge>;
}

function ClassCard({ cls, annotatedTargets, onAnnotate }) {
  const isClassAnnotated = annotatedTargets.has(`Class: ${cls.label}`);

  const propColumns = [
    {
      id: 'label',
      header: 'Property',
      cell: (p) => <Box fontWeight="bold">{p.label}</Box>,
      width: 160,
    },
    {
      id: 'range',
      header: 'Type',
      cell: (p) => <TypeBadge type={p.range} />,
      minWidth: 130,
      width: 140,
    },
    {
      id: 'mapsToColumn',
      header: 'Column',
      cell: (p) => <Box variant="code" fontSize="body-s">{p.mapsToColumn || '—'}</Box>,
      width: 180,
    },
    {
      id: 'comment',
      header: 'Description',
      cell: (p) => (
        <span style={{ whiteSpace: 'normal', wordBreak: 'break-word' }}>
          {p.comment || <Box color="text-status-inactive">—</Box>}
        </span>
      ),
    },
    {
      id: 'annotate',
      header: '',
      cell: (p) => {
        const key = `Property: ${cls.label}/${p.label}`;
        const annotated = annotatedTargets.has(key);
        return (
          <Button
            variant={annotated ? 'normal' : 'inline-link'}
            iconName={annotated ? 'check' : 'add-plus'}
            size="small"
            onClick={() => onAnnotate(key)}
          >
            {annotated ? 'Annotated' : 'Annotate'}
          </Button>
        );
      },
      width: 110,
    },
  ];

  const relColumns = [
    {
      id: 'label',
      header: 'Relationship',
      cell: (r) => <Box fontWeight="bold">{r.label}</Box>,
      width: 180,
    },
    {
      id: 'range',
      header: 'Target Class',
      cell: (r) => {
        const target = r.range.split('/').pop();
        return <Badge color="green">{target}</Badge>;
      },
    },
    {
      id: 'comment',
      header: 'Description',
      cell: (r) => (
        <span style={{ whiteSpace: 'normal', wordBreak: 'break-word' }}>
          {r.comment || <Box color="text-status-inactive">—</Box>}
        </span>
      ),
    },
    {
      id: 'annotate',
      header: '',
      cell: (r) => {
        const key = `Relationship: ${cls.label}/${r.label}`;
        const annotated = annotatedTargets.has(key);
        return (
          <Button
            variant={annotated ? 'normal' : 'inline-link'}
            iconName={annotated ? 'check' : 'add-plus'}
            size="small"
            onClick={() => onAnnotate(key)}
          >
            {annotated ? 'Annotated' : 'Annotate'}
          </Button>
        );
      },
      width: 110,
    },
  ];

  return (
    <ExpandableSection
      defaultExpanded
      variant="container"
      headerText={
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Badge color="blue">{cls.label}</Badge>
          {cls.mapsToTable && (
            <Box variant="code" fontSize="body-s" color="text-status-inactive">
              {cls.mapsToTable}
            </Box>
          )}
          {isClassAnnotated && <Badge color="severity-low">annotated</Badge>}
        </SpaceBetween>
      }
      headerDescription={cls.comment}
      headerActions={
        <Button
          variant={isClassAnnotated ? 'normal' : 'inline-link'}
          iconName={isClassAnnotated ? 'check' : 'add-plus'}
          size="small"
          onClick={() => onAnnotate(`Class: ${cls.label}`)}
        >
          {isClassAnnotated ? 'Annotated' : 'Annotate class'}
        </Button>
      }
    >
      <SpaceBetween size="s">
        {cls.properties.length > 0 && (
          <Table
            variant="embedded"
            columnDefinitions={propColumns}
            items={cls.properties}
            header={
              <Header variant="h3" counter={`(${cls.properties.length})`}>
                Datatype Properties
              </Header>
            }
            empty={null}
          />
        )}
        {cls.relationships.length > 0 && (
          <Table
            variant="embedded"
            columnDefinitions={relColumns}
            items={cls.relationships}
            header={
              <Header variant="h3" counter={`(${cls.relationships.length})`}>
                Relationships
              </Header>
            }
            empty={null}
          />
        )}
        {cls.properties.length === 0 && cls.relationships.length === 0 && (
          <Box color="text-status-inactive" padding={{ top: 'xs' }}>
            No properties found for this class.
          </Box>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
}

// ─── Raw view with highlight rendering ──────────────────────────────────────

function renderHighlightedContent(content, annotations) {
  if (!annotations.length) return content;
  let result = [content];
  annotations.forEach(({ selectedText }) => {
    result = result.flatMap((part) => {
      if (typeof part !== 'string') return [part];
      const idx = part.indexOf(selectedText);
      if (idx === -1) return [part];
      return [
        part.slice(0, idx),
        <mark key={`${selectedText}-${idx}`} style={{ background: '#ffc107', borderRadius: 2 }}>
          {selectedText}
        </mark>,
        part.slice(idx + selectedText.length),
      ];
    });
  });
  return result;
}

// ─── Main component ──────────────────────────────────────────────────────────

export default function OntologyEditor({ id }) {
  const [content, setContent]           = useState('');
  const [version, setVersion]           = useState('');
  const [versions, setVersions]         = useState([]);
  const [annotations, setAnnotations]   = useState([]);
  const [loading, setLoading]           = useState(true);
  const [error, setError]               = useState(null);
  const [viewMode, setViewMode]         = useState('structured');

  // Annotation modal state
  const [pendingTarget, setPendingTarget] = useState(''); // structured key or raw selection
  const [pendingSelection, setPendingSelection] = useState(''); // raw text selection only
  const [showModal, setShowModal]         = useState(false);
  const [comment, setComment]             = useState('');
  const [generating, setGenerating]       = useState(false);

  // Parse N-Quads into structured model whenever content changes
  const ontologyModel = useMemo(
    () => (content ? buildOntologyModel(parseNQuads(content)) : []),
    [content]
  );

  // Set of annotation targets for quick "is annotated?" checks
  const annotatedTargets = useMemo(
    () => new Set(annotations.map((a) => a.selectedText)),
    [annotations]
  );

  const loadVersionsAndContent = useCallback(async () => {
    setLoading(true);
    setError(null);
    const versResult = await ontologyAPI.getOntologyVersions(id);
    if (!versResult.success) {
      setError(versResult.error);
      setLoading(false);
      return;
    }
    const allVersions = versResult.data.versions || [];
    setVersions(allVersions);
    const latest = allVersions[0]?.version || 'v1';
    await loadContent(latest);
    setVersion(latest);
    setLoading(false);
  }, [id]);

  useEffect(() => { loadVersionsAndContent(); }, [loadVersionsAndContent]);

  // Raw view: capture text selections
  useEffect(() => {
    if (viewMode !== 'raw') return;
    const handler = () => {
      const sel = window.getSelection?.()?.toString?.().trim() || '';
      if (sel) setPendingSelection(sel);
    };
    document.addEventListener('mouseup', handler);
    return () => document.removeEventListener('mouseup', handler);
  }, [viewMode]);

  const loadContent = async (v) => {
    const result = await ontologyAPI.getOntologyContent(id, v);
    if (result.success) setContent(result.data.content);
    else setError(result.error);
  };

  // Open annotation modal — target is the human-readable key
  const handleAnnotate = (target) => {
    // Toggle off if already annotated
    if (annotatedTargets.has(target)) {
      setAnnotations((prev) => prev.filter((a) => a.selectedText !== target));
      return;
    }
    setPendingTarget(target);
    setShowModal(true);
  };

  const handleRawAnnotate = () => {
    if (!pendingSelection) return;
    setPendingTarget(pendingSelection);
    setShowModal(true);
  };

  const confirmAnnotation = () => {
    setAnnotations((prev) => [
      ...prev,
      { id: Date.now().toString(), selectedText: pendingTarget, comment },
    ]);
    setShowModal(false);
    setPendingTarget('');
    setPendingSelection('');
    setComment('');
  };

  const handleGenerate = async () => {
    setGenerating(true);
    const mapped = annotations.map((a) => ({
      highlightedText: a.selectedText,
      comment: a.comment,
    }));
    const result = await ontologyAPI.reviseOntology(id, version, mapped);
    if (!result.success) {
      setError(result.error);
      setGenerating(false);
      return;
    }
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      const status = await ontologyAPI.getBuildStatus(id);
      const st = status?.data?.status;
      if (st === 'completed' || st === 'failed' || attempts > 60) {
        clearInterval(poll);
        setGenerating(false);
        setAnnotations([]);
        if (st === 'failed') setError('Revision failed. Check build status for details.');
        else await loadVersionsAndContent();
      }
    }, 5000);
  };

  if (loading) {
    return (
      <Box>
        <StatusIndicator type="loading">Loading ontology...</StatusIndicator>
      </Box>
    );
  }
  if (error) return <Alert type="error">{error}</Alert>;

  const versionOptions = versions.map((v) => ({
    label: `${v.version} (${v.status})`,
    value: v.version,
  }));
  const selectedVersionOption = versionOptions.find((o) => o.value === version) || null;

  return (
    <SpaceBetween size="l">
      {/* ── Toolbar ── */}
      <Header
        variant="h3"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Select
              selectedOption={selectedVersionOption}
              options={versionOptions}
              onChange={({ detail }) => {
                setVersion(detail.selectedOption.value);
                setAnnotations([]);
                loadContent(detail.selectedOption.value);
              }}
              disabled={versions.length <= 1}
            />
            <ButtonGroup
              variant="icon"
              items={[
                {
                  type: 'icon-button',
                  id: 'structured',
                  iconName: 'list',
                  text: 'Structured view',
                  pressed: viewMode === 'structured',
                },
                {
                  type: 'icon-button',
                  id: 'raw',
                  iconName: 'script',
                  text: 'View raw N-Quads',
                  pressed: viewMode === 'raw',
                },
              ]}
              onItemClick={({ detail }) => setViewMode(detail.id === viewMode ? 'structured' : detail.id)}
            />
            <Box variant="p">
              {annotations.length} annotation{annotations.length !== 1 ? 's' : ''}
            </Box>
            <Button
              variant="primary"
              disabled={annotations.length === 0 || generating}
              loading={generating}
              onClick={handleGenerate}
            >
              Generate New Version
            </Button>
          </SpaceBetween>
        }
      >
        Metadata
      </Header>

      {/* ── Structured view ── */}
      {viewMode === 'structured' && (
        ontologyModel.length === 0 ? (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            No classes found. Switch to Raw view to inspect the N-Quads directly.
          </Box>
        ) : (
          <SpaceBetween size="m">
            {ontologyModel.map((cls) => (
              <ClassCard
                key={cls.uri}
                cls={cls}
                annotatedTargets={annotatedTargets}
                onAnnotate={handleAnnotate}
              />
            ))}
          </SpaceBetween>
        )
      )}

      {/* ── Raw N-Quads view ── */}
      {viewMode === 'raw' && (
        <SpaceBetween size="s">
          <Box variant="code">
            <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 13 }}>
              {renderHighlightedContent(content, annotations)}
            </pre>
          </Box>
          {pendingSelection && (
            <Button size="small" onClick={handleRawAnnotate}>
              Annotate selection
            </Button>
          )}
        </SpaceBetween>
      )}

      {/* ── Annotation modal ── */}
      <Modal
        visible={showModal}
        header="Add Annotation"
        onDismiss={() => {
          setShowModal(false);
          setPendingTarget('');
          setPendingSelection('');
          setComment('');
        }}
        footer={
          <Button variant="primary" disabled={!comment.trim()} onClick={confirmAnnotation}>
            Confirm
          </Button>
        }
      >
        <FormField
          label={
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                fontFamily: 'monospace',
                fontSize: 12,
                background: '#fffde7',
                padding: '6px 10px',
                borderRadius: '4px',
                borderLeft: '3px solid #ffc107',
                margin: 0,
                maxHeight: '120px',
                overflowY: 'auto',
                wordBreak: 'break-all',
              }}
            >
              {pendingTarget}
            </pre>
          }
        >
          <Textarea
            value={comment}
            onChange={({ detail }) => setComment(detail.value)}
            placeholder="Describe what to change..."
            rows={3}
          />
        </FormField>
      </Modal>

      {/* ── Annotation list ── */}
      {annotations.length > 0 && (
        <Box>
          <Header variant="h3">Annotations ({annotations.length})</Header>
          <SpaceBetween size="s">
            {annotations.map((a) => (
              <div
                key={a.id}
                style={{
                  border: '1px solid #d5dbdb',
                  borderRadius: '8px',
                  background: '#f8f8f8',
                  padding: '12px 16px',
                }}
              >
                <SpaceBetween size="xs">
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Box variant="awsui-key-label">Target</Box>
                    <Button
                      variant="icon"
                      iconName="close"
                      ariaLabel="delete annotation"
                      onClick={() => setAnnotations((prev) => prev.filter((x) => x.id !== a.id))}
                    />
                  </div>
                  <pre
                    style={{
                      whiteSpace: 'pre-wrap',
                      fontFamily: 'monospace',
                      fontSize: 12,
                      background: '#fffde7',
                      padding: '8px 12px',
                      borderRadius: '4px',
                      borderLeft: '3px solid #ffc107',
                      margin: 0,
                      maxHeight: '200px',
                      overflowY: 'auto',
                      wordBreak: 'break-all',
                    }}
                  >
                    {a.selectedText}
                  </pre>
                  <Box variant="awsui-key-label">Instruction</Box>
                  <Box variant="p">{a.comment}</Box>
                </SpaceBetween>
              </div>
            ))}
          </SpaceBetween>
        </Box>
      )}
    </SpaceBetween>
  );
}
