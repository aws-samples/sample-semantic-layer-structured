/**
 * GroundTruthDataset — admin tab to upload and inspect a semantic layer's
 * ground-truth evaluation dataset.
 *
 * The dataset is the AgentCore ground-truth evaluation format: a JSON array of
 * records, each with Natural_Language_Question / Expected_Answer /
 * Expected_SQL_Query / Expected_SQL_Result. It is stored per semantic layer
 * (ontology id, read from ?id=) and drives the OnDemand eval pipeline that the
 * Evaluations tab surfaces.
 *
 * Mirrors UploadSupplementaryDocs' upload + table layout and Cloudscape idioms.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Container,
  FileUpload,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { useSearchParams } from "react-router-dom";
import { groundtruthAPI } from "../../services/api";

const REQUIRED_COLUMNS = [
  "Natural_Language_Question",
  "Expected_Answer",
  "Expected_SQL_Query",
  "Expected_SQL_Result",
];

// Compact a cell value for table display — objects/arrays (e.g.
// Expected_SQL_Result) render as single-line JSON, truncated.
function compact(value, max = 120) {
  if (value == null) return "";
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export default function GroundTruthDataset({ id = null }) {
  const [searchParams] = useSearchParams();
  // Prefer an explicit prop (embedded as a detail-screen tab); fall back to
  // the ?id= query param (standalone /admin/ground-truth route).
  const ontologyId = id || searchParams.get("id");

  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [records, setRecords] = useState([]);
  const [meta, setMeta] = useState(null); // {recordCount, uploadedAt}
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const loadDataset = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    const res = await groundtruthAPI.get(ontologyId);
    if (res.success) {
      setRecords(res.data.records || []);
      setMeta({
        recordCount: res.data.recordCount || 0,
        uploadedAt: res.data.uploadedAt || null,
      });
    } else {
      setError(res.error || "Failed to load dataset");
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    loadDataset();
  }, [loadDataset]);

  const handleUpload = useCallback(async () => {
    if (!files.length || !ontologyId) return;
    setUploading(true);
    setError(null);
    setSuccess(null);
    const res = await groundtruthAPI.upload(ontologyId, files[0]);
    if (res.success) {
      setSuccess(
        `Uploaded ${res.data.recordCount} record(s) for ${ontologyId}.`,
      );
      setFiles([]);
      await loadDataset();
      setTimeout(() => setSuccess(null), 4000);
    } else {
      // Surface the backend's validation detail (422) so the admin knows
      // exactly which record/column is wrong.
      setError(res.error || "Upload failed");
    }
    setUploading(false);
  }, [files, ontologyId, loadDataset]);

  const handleDelete = useCallback(async () => {
    if (!ontologyId) return;
    setError(null);
    const res = await groundtruthAPI.delete(ontologyId);
    if (res.success) {
      setRecords([]);
      setMeta({ recordCount: 0, uploadedAt: null });
      setSuccess("Dataset deleted.");
      setTimeout(() => setSuccess(null), 3000);
    } else {
      setError(res.error || "Delete failed");
    }
  }, [ontologyId]);

  const columns = REQUIRED_COLUMNS.map((col) => ({
    id: col,
    header: col.replace(/_/g, " "),
    cell: (item) => compact(item[col]),
  }));

  if (!ontologyId) {
    return (
      <Alert type="error" header="Missing semantic layer id">
        Open this tab from a specific semantic layer (the URL must include
        <code> ?id=&lt;ontologyId&gt;</code>).
      </Alert>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Upload and review the ground-truth evaluation dataset for this semantic layer."
      >
        Ground truth dataset
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess(null)}>
          {success}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={
              <Box variant="small" color="text-status-inactive">
                Required columns: {REQUIRED_COLUMNS.join(", ")}. Format: a JSON
                array of records (AgentCore ground-truth evaluation format).
              </Box>
            }
          >
            Upload dataset
          </Header>
        }
      >
        <SpaceBetween size="m">
          <FileUpload
            onChange={({ detail }) => setFiles(detail.value)}
            value={files}
            accept="application/json,.json"
            i18nStrings={{
              uploadButtonText: (multiple) =>
                multiple ? "Choose files" : "Choose file",
              dropzoneText: (multiple) =>
                multiple ? "Drop files to upload" : "Drop file to upload",
              removeFileAriaLabel: (idx) => `Remove file ${idx + 1}`,
              limitShowFewer: "Show fewer files",
              limitShowMore: "Show more files",
              errorIconAriaLabel: "Error",
            }}
            showFileLastModified
            showFileSize
            constraintText="A single .json file containing the ground-truth records."
          />
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="primary"
              onClick={handleUpload}
              loading={uploading}
              disabled={uploading || files.length === 0}
            >
              Upload
            </Button>
            <Button
              onClick={handleDelete}
              disabled={!meta || meta.recordCount === 0}
            >
              Delete dataset
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            counter={meta ? `(${meta.recordCount})` : undefined}
            description={
              meta?.uploadedAt
                ? `Last uploaded ${new Date(meta.uploadedAt).toLocaleString()}`
                : "No dataset uploaded yet."
            }
          >
            Dataset contents
          </Header>
        }
      >
        {loading ? (
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">Loading dataset…</StatusIndicator>
          </Box>
        ) : (
          <Table
            columnDefinitions={columns}
            items={records}
            variant="embedded"
            resizableColumns
            wrapLines
            empty={
              <Box textAlign="center" color="text-status-inactive">
                No records. Upload a dataset to populate this table.
              </Box>
            }
          />
        )}
      </Container>
    </SpaceBetween>
  );
}
