/**
 * UploadSupplementaryDocs — admin page for the creation-time doc pipeline (item #3).
 *
 * Lets a steward upload PDF / Markdown / DOCX / text files at semantic-layer
 * creation. Uploads land in S3 via the documents API, which kicks off the
 * Step Functions pipeline (chunk → embed → link → index). The UI polls per
 * 4 seconds for stage status until all five booleans flip.
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
import { documentsAPI } from "../../services/api";

const STAGE_ORDER = ["chunked", "ner", "embedded", "linked", "indexed"];

function stageBadge(stages, name, errors) {
  if (errors && errors[name]) {
    return (
      <StatusIndicator type="error">
        {name} — {String(errors[name]).slice(0, 60)}
      </StatusIndicator>
    );
  }
  if (stages && stages[name] === true) {
    return <StatusIndicator type="success">{name}</StatusIndicator>;
  }
  return <StatusIndicator type="pending">{name}</StatusIndicator>;
}

export default function UploadSupplementaryDocs({ ontologyId }) {
  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [docs, setDocs] = useState([]);
  const [error, setError] = useState(null);

  const loadDocs = useCallback(async () => {
    if (!ontologyId) return;
    const res = await documentsAPI.list(ontologyId);
    if (res.success) {
      setDocs(res.data.documents || []);
    } else {
      setError(res.error || "Failed to load docs");
    }
  }, [ontologyId]);

  useEffect(() => {
    loadDocs();
  }, [loadDocs]);

  // Poll every 4s while any doc has an unfinished stage.
  useEffect(() => {
    const anyPending = docs.some((d) =>
      STAGE_ORDER.some((s) => !(d.stages && d.stages[s] === true)),
    );
    if (!anyPending) return undefined;
    const t = setInterval(loadDocs, 4000);
    return () => clearInterval(t);
  }, [docs, loadDocs]);

  const handleUpload = useCallback(async () => {
    if (!ontologyId || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of files) {
        const res = await documentsAPI.upload(ontologyId, file);
        if (!res.success) {
          setError(res.error || `Upload failed for ${file.name}`);
          break;
        }
      }
      setFiles([]);
      await loadDocs();
    } finally {
      setUploading(false);
    }
  }, [files, ontologyId, loadDocs]);

  const handleDelete = useCallback(
    async (docId) => {
      if (!ontologyId) return;
      const res = await documentsAPI.delete(ontologyId, docId);
      if (!res.success) {
        setError(res.error || "Delete failed");
      }
      await loadDocs();
    },
    [ontologyId, loadDocs],
  );

  const columns = [
    {
      id: "filename",
      header: "Filename",
      cell: (item) => item.filename,
      sortingField: "filename",
    },
    {
      id: "size",
      header: "Size",
      cell: (item) =>
        item.sizeBytes ? `${(item.sizeBytes / 1024).toFixed(1)} KB` : "",
    },
    {
      id: "stages",
      header: "Pipeline status",
      cell: (item) => (
        <SpaceBetween size="xxs" direction="horizontal">
          {STAGE_ORDER.map((s) => (
            <Box key={s}>{stageBadge(item.stages, s, item.errors)}</Box>
          ))}
        </SpaceBetween>
      ),
    },
    {
      id: "actions",
      header: "Actions",
      cell: (item) => (
        <Button variant="inline-link" onClick={() => handleDelete(item.docId)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <SpaceBetween size="m">
      <Container
        header={
          <Header
            variant="h2"
            description="Upload PDF / Markdown / DOCX / text files describing your business domain."
          >
            Supplementary documents
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          <FileUpload
            value={files}
            onChange={({ detail }) => setFiles(detail.value)}
            multiple
            accept=".pdf,.md,.markdown,.docx,.txt"
            i18nStrings={{
              uploadButtonText: (e) => (e ? "Choose files" : "Choose file"),
              dropzoneText: (e) =>
                e ? "Drop files to upload" : "Drop file to upload",
              removeFileAriaLabel: (i) => `Remove file ${i + 1}`,
              limitShowFewer: "Show fewer files",
              limitShowMore: "Show more files",
              errorIconAriaLabel: "Error",
            }}
            constraintText="Max 50 MB per file. Supported: .pdf .md .docx .txt"
          />
          <Box>
            <Button
              variant="primary"
              onClick={handleUpload}
              loading={uploading}
              disabled={files.length === 0 || uploading || !ontologyId}
            >
              Upload {files.length > 1 ? `${files.length} files` : "file"}
            </Button>
          </Box>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h3">Uploaded documents</Header>}>
        <Table
          columnDefinitions={columns}
          items={docs}
          variant="embedded"
          empty={
            <Box textAlign="center" color="text-status-inactive">
              No documents uploaded yet.
            </Box>
          }
        />
      </Container>
    </SpaceBetween>
  );
}
