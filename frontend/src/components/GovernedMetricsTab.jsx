import { useEffect, useState, useCallback } from "react";
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
  Table,
  Textarea,
} from "@cloudscape-design/components";
import { metricsAPI } from "../services/api";

// Dialects the backend knows how to compile + execute (metric_models.py
// ALLOWED_DIALECTS). Kept in sync manually — the backend is the source of truth
// and rejects anything else at create/update time.
const DIALECT_OPTIONS = [
  { label: "athena", value: "athena" },
  { label: "trino", value: "trino" },
  { label: "presto", value: "presto" },
];

// Lifecycle the steward can set at author time. PUBLISHED is reachable here too
// (the backend embeds on any write that lands PUBLISHED), but the primary path
// to publish is the dedicated Publish action so the intent is explicit.
const LIFECYCLE_OPTIONS = [
  { label: "DRAFT", value: "DRAFT" },
  { label: "APPROVED", value: "APPROVED" },
  { label: "PUBLISHED", value: "PUBLISHED" },
];

const LIFECYCLE_COLOR = {
  DRAFT: "grey",
  APPROVED: "blue",
  PUBLISHED: "green",
};

// Parse a comma-separated list field into a trimmed, de-blanked array. Used for
// synonyms / supported_dimensions / supported_filters which the model stores as
// List[str] but which are far easier to edit as a single comma-separated input.
function parseList(text) {
  return (text || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// Empty form state for a brand-new metric. namespace is injected at submit time
// from ontologyId — never edited by the user — so Tier 1 finds the metric.
function emptyForm() {
  return {
    metric_id: "",
    name: "",
    description: "",
    synonyms: "",
    compiled_sql: "",
    dialect: "athena",
    supported_dimensions: "",
    supported_filters: "",
    linked_class: "",
    lifecycle: "DRAFT",
  };
}

// Build the form state from an existing Metric record (list fields → CSV).
function formFromMetric(m) {
  return {
    metric_id: m.metric_id ?? "",
    name: m.name ?? "",
    description: m.description ?? "",
    synonyms: (m.synonyms ?? []).join(", "),
    compiled_sql: m.compiled_sql ?? "",
    dialect: m.dialect ?? "athena",
    supported_dimensions: (m.supported_dimensions ?? []).join(", "),
    supported_filters: (m.supported_filters ?? []).join(", "),
    linked_class: m.linked_class ?? "",
    lifecycle: m.lifecycle ?? "DRAFT",
  };
}

/**
 * Admin tab — authors the Tier 1 governed (maintained) metrics for one layer.
 *
 * A published metric is embedded (Titan v2) and KNN-matched against the user's
 * question BEFORE the Tier 2 Strands graph runs; a clear hit (cosine ≥ 0.85)
 * short-circuits to the metric's pre-validated `compiled_sql` on Athena.
 *
 * Every metric is authored under `namespace === ontologyId` because the query
 * agents resolve their Tier 1 namespace as `config.namespace || id` and the
 * config has no namespace field — so the layer id IS the namespace.
 *
 * @param {string} ontologyId  semantic-layer id (used as the API namespace).
 * @param {string} layerType   'VKG' | 'SemanticRAG'; VKG exposes `linked_class`.
 */
export default function GovernedMetricsTab({ ontologyId, layerType }) {
  const isVkg = layerType === "VKG";

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Create/edit modal state.
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null); // existing metric, or null = create
  const [form, setForm] = useState(emptyForm());
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);

  // Row-level action state.
  const [publishing, setPublishing] = useState(null); // metric_id in flight
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    // metricsAPI.list resolves to the handleResponse envelope {success, data}
    // and never throws — the router returns a bare array of Metric objects.
    const res = await metricsAPI.list(ontologyId);
    if (res.success) {
      // Defensive: the backend returns a list; tolerate a {metrics:[...]} shape.
      setItems(Array.isArray(res.data) ? res.data : (res.data?.metrics ?? []));
    } else {
      setError(res.error ?? "Failed to load metrics");
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm());
    setFormError(null);
    setModalOpen(true);
  };

  const openEdit = (m) => {
    setEditing(m);
    setForm(formFromMetric(m));
    setFormError(null);
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setForm(emptyForm());
    setFormError(null);
  };

  const setField = (key) => (value) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  // Build the Metric payload the router expects. namespace is forced to the
  // layer id so Tier 1 lookup (namespace = config.namespace || id) finds it.
  const buildPayload = () => ({
    metric_id: form.metric_id.trim(),
    namespace: ontologyId,
    name: form.name.trim(),
    description: form.description.trim(),
    synonyms: parseList(form.synonyms),
    compiled_sql: form.compiled_sql.trim(),
    dialect: form.dialect,
    supported_dimensions: parseList(form.supported_dimensions),
    supported_filters: parseList(form.supported_filters),
    // Send linked_class only for VKG; null clears it on the model.
    linked_class: isVkg && form.linked_class.trim() ? form.linked_class.trim() : null,
    lifecycle: form.lifecycle,
  });

  const validateForm = () => {
    if (!form.metric_id.trim()) return "Metric ID is required.";
    if (!form.name.trim()) return "Name is required.";
    if (!form.description.trim()) return "Description is required.";
    if (!form.compiled_sql.trim()) return "Compiled SQL is required.";
    // Soft SELECT-only hint — the backend (sqlglot) is the real gate, this just
    // catches the obvious mistake before a round-trip.
    const sqlHead = form.compiled_sql.trim().slice(0, 6).toUpperCase();
    if (!sqlHead.startsWith("SELECT") && !sqlHead.startsWith("WITH")) {
      return "Compiled SQL must be a SELECT statement (may start with WITH).";
    }
    return null;
  };

  const handleSave = async () => {
    const vErr = validateForm();
    if (vErr) {
      setFormError(vErr);
      return;
    }
    setSaving(true);
    setFormError(null);
    const payload = buildPayload();
    const res = editing
      ? await metricsAPI.update(ontologyId, payload.metric_id, payload)
      : await metricsAPI.create(ontologyId, payload);
    setSaving(false);
    if (res.success) {
      closeModal();
      refresh();
    } else {
      // Surface the backend 422 (sqlglot) / 400 (id mismatch) detail inline.
      setFormError(
        res.details?.detail || res.error || "Failed to save metric",
      );
    }
  };

  const handlePublish = async (m) => {
    setPublishing(m.metric_id);
    setError(null);
    const res = await metricsAPI.publish(ontologyId, m.metric_id);
    setPublishing(null);
    if (res.success) {
      refresh();
    } else {
      setError(res.error ?? "Failed to publish metric");
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) return;
    setDeleting(true);
    const res = await metricsAPI.remove(ontologyId, confirmDelete.metric_id);
    setDeleting(false);
    if (res.success) {
      setItems((prev) =>
        prev.filter((it) => it.metric_id !== confirmDelete.metric_id),
      );
      setConfirmDelete(null);
    } else {
      setError(res.error ?? "Failed to delete metric");
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={items.length ? `(${items.length})` : undefined}
          description="Curated, pre-validated metrics. A PUBLISHED metric is embedded and matched against the user's question by the Tier 1 lookup (cosine ≥ 0.85) before the Tier 2 graph runs — short-circuiting to its compiled SQL on a clear hit."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={refresh} disabled={loading}>
                Refresh
              </Button>
              <Button variant="primary" iconName="add-plus" onClick={openCreate}>
                Add metric
              </Button>
            </SpaceBetween>
          }
        >
          Governed Metrics
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <Table
          loading={loading}
          loadingText="Loading metrics"
          items={items}
          columnDefinitions={[
            {
              id: "name",
              header: "Name",
              cell: (m) => <Box fontWeight="bold">{m.name}</Box>,
              width: 200,
            },
            {
              id: "description",
              header: "Description",
              cell: (m) => m.description || "—",
            },
            {
              id: "lifecycle",
              header: "Lifecycle",
              cell: (m) => (
                <Badge color={LIFECYCLE_COLOR[m.lifecycle] ?? "grey"}>
                  {m.lifecycle}
                </Badge>
              ),
              width: 130,
            },
            {
              id: "version",
              header: "Version",
              cell: (m) => `v${m.version ?? 1}`,
              width: 90,
            },
            {
              id: "dialect",
              header: "Dialect",
              cell: (m) => (
                <Box variant="code" fontSize="body-s">
                  {m.dialect}
                </Box>
              ),
              width: 100,
            },
            {
              id: "actions",
              header: "Actions",
              cell: (m) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="inline-link" onClick={() => openEdit(m)}>
                    Edit
                  </Button>
                  {m.lifecycle !== "PUBLISHED" && (
                    <Button
                      variant="inline-link"
                      loading={publishing === m.metric_id}
                      onClick={() => handlePublish(m)}
                    >
                      Publish
                    </Button>
                  )}
                  <Button
                    variant="inline-link"
                    onClick={() => setConfirmDelete(m)}
                  >
                    Delete
                  </Button>
                </SpaceBetween>
              ),
              width: 230,
            },
          ]}
          empty={
            loading ? (
              <Spinner />
            ) : (
              <Box textAlign="center" color="text-status-inactive" padding="m">
                <SpaceBetween size="xs">
                  <span>No governed metrics yet for this layer.</span>
                  <span>
                    Add a metric, then publish it to make it available for the
                    Tier 1 lookup.
                  </span>
                </SpaceBetween>
              </Box>
            )
          }
        />
      </SpaceBetween>

      {/* ── Create / edit modal ── */}
      <Modal
        visible={modalOpen}
        size="large"
        header={editing ? `Edit metric — ${editing.name}` : "Add governed metric"}
        onDismiss={closeModal}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={closeModal} disabled={saving}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSave} loading={saving}>
                {editing ? "Save changes" : "Create metric"}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween size="m">
            {formError && <Alert type="error">{formError}</Alert>}

            <FormField
              label="Metric ID"
              description="Stable identifier (immutable after creation). e.g. total_premium_by_product"
            >
              <Input
                value={form.metric_id}
                disabled={!!editing}
                onChange={({ detail }) => setField("metric_id")(detail.value)}
                placeholder="total_premium_by_product"
              />
            </FormField>

            <FormField
              label="Name"
              description="Human-readable name; embedded for the Tier 1 match."
            >
              <Input
                value={form.name}
                onChange={({ detail }) => setField("name")(detail.value)}
                placeholder="Total premium by product"
              />
            </FormField>

            <FormField
              label="Description"
              description="What the metric answers; embedded for the Tier 1 match."
            >
              <Textarea
                value={form.description}
                onChange={({ detail }) => setField("description")(detail.value)}
                rows={2}
                placeholder="Sum of written premium grouped by policy product."
              />
            </FormField>

            <FormField
              label="Synonyms"
              description="Comma-separated. Alternate phrasings the user might ask; embedded for the Tier 1 match."
            >
              <Input
                value={form.synonyms}
                onChange={({ detail }) => setField("synonyms")(detail.value)}
                placeholder="premium total, gross premium, premium by product"
              />
            </FormField>

            <FormField
              label="Compiled SQL"
              description="Pre-validated, SELECT-only SQL run verbatim on Athena on a Tier 1 hit. Validated server-side with sqlglot."
            >
              <Textarea
                value={form.compiled_sql}
                onChange={({ detail }) =>
                  setField("compiled_sql")(detail.value)
                }
                rows={6}
                placeholder={"SELECT product, SUM(premium) AS total_premium\nFROM normalized.coverage\nGROUP BY product"}
              />
            </FormField>

            <FormField label="Dialect">
              <Select
                selectedOption={
                  DIALECT_OPTIONS.find((o) => o.value === form.dialect) ??
                  DIALECT_OPTIONS[0]
                }
                options={DIALECT_OPTIONS}
                onChange={({ detail }) =>
                  setField("dialect")(detail.selectedOption.value)
                }
              />
            </FormField>

            <FormField
              label="Supported dimensions"
              description="Comma-separated. Columns the metric can be grouped by (optional)."
            >
              <Input
                value={form.supported_dimensions}
                onChange={({ detail }) =>
                  setField("supported_dimensions")(detail.value)
                }
                placeholder="product, region, year"
              />
            </FormField>

            <FormField
              label="Supported filters"
              description="Comma-separated. Columns the metric can be filtered on (optional)."
            >
              <Input
                value={form.supported_filters}
                onChange={({ detail }) =>
                  setField("supported_filters")(detail.value)
                }
                placeholder="product, status"
              />
            </FormField>

            {isVkg && (
              <FormField
                label="Linked class (VKG)"
                description="Optional ontology class IRI this metric is associated with."
              >
                <Input
                  value={form.linked_class}
                  onChange={({ detail }) =>
                    setField("linked_class")(detail.value)
                  }
                  placeholder="http://example.com/insurance#Coverage"
                />
              </FormField>
            )}

            <FormField
              label="Lifecycle"
              description="DRAFT/APPROVED are not matched by Tier 1 — use the Publish action to go live, or set PUBLISHED here to embed on save."
            >
              <Select
                selectedOption={
                  LIFECYCLE_OPTIONS.find((o) => o.value === form.lifecycle) ??
                  LIFECYCLE_OPTIONS[0]
                }
                options={LIFECYCLE_OPTIONS}
                onChange={({ detail }) =>
                  setField("lifecycle")(detail.selectedOption.value)
                }
              />
            </FormField>
          </SpaceBetween>
        </Form>
      </Modal>

      {/* ── Delete confirm ── */}
      <Modal
        visible={!!confirmDelete}
        header="Delete metric?"
        onDismiss={() => setConfirmDelete(null)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setConfirmDelete(null)}
                disabled={deleting}
              >
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={deleting}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        This permanently removes
        {confirmDelete ? ` "${confirmDelete.name}"` : " the metric"} from
        DynamoDB. If it was published, it is no longer available to the Tier 1
        lookup.
      </Modal>
    </Container>
  );
}
