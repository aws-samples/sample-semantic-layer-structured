import { useEffect, useState, useCallback } from "react";
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  Modal,
  Pagination,
  SpaceBetween,
  Spinner,
  Table,
  TextFilter,
} from "@cloudscape-design/components";
import { lessonsAPI } from "../services/api";

const PAGE_SIZE = 25;

// Render an ISO timestamp as a human-readable local date+time (e.g.
// "6/7/2026, 5:10:51 PM"), matching the Feedback tab. Falls back to the raw
// value if it isn't a parseable date so we never show "Invalid Date".
function formatCreated(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/**
 * Admin tab — read & delete long-term lessons-learned records pulled from
 * Bedrock AgentCore Memory. There is no edit/create surface here: agents
 * write turns into memory through the Strands hook (PII-redacted by
 * Bedrock Guardrails), and AgentCore extracts the lessons asynchronously.
 */
export default function LessonsLearnedTab({ ontologyId }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [filterText, setFilterText] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError(null);
    // lessonsAPI.list resolves to the handleResponse envelope
    // ``{success, data}`` — the backend body ``{lessons: [...]}`` lives at
    // ``res.data.lessons`` (NOT ``res.lessons``). handleResponse also never
    // throws, so check ``success`` rather than relying on try/catch.
    // AgentCore's ListMemoryRecords caps a page at 100; requesting more trips
    // a ValidationException the backend swallows into an empty list.
    const res = await lessonsAPI.list(ontologyId, { limit: 100 });
    if (res.success) {
      setItems(res.data?.lessons ?? []);
    } else {
      setError(res.error ?? "Failed to load lessons");
    }
    setLoading(false);
  }, [ontologyId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleDelete = async () => {
    if (!confirmDelete) return;
    setDeleting(true);
    // remove() also returns the {success, error} envelope and never throws —
    // only drop the row optimistically once the delete actually succeeded.
    const res = await lessonsAPI.remove(
      ontologyId,
      confirmDelete.memoryRecordId,
    );
    if (res.success) {
      setItems((prev) =>
        prev.filter((it) => it.memoryRecordId !== confirmDelete.memoryRecordId),
      );
      setConfirmDelete(null);
    } else {
      setError(res.error ?? "Failed to delete lesson");
    }
    setDeleting(false);
  };

  const filtered = filterText
    ? items.filter((it) =>
        (it.content ?? "").toLowerCase().includes(filterText.toLowerCase()),
      )
    : items;
  const pageStart = (currentPage - 1) * PAGE_SIZE;
  const paginated = filtered.slice(pageStart, pageStart + PAGE_SIZE);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Long-term lessons extracted from chat sessions by Bedrock AgentCore Memory. PII is redacted by Bedrock Guardrails before any record is written."
          actions={
            <Button iconName="refresh" onClick={refresh} disabled={loading}>
              Refresh
            </Button>
          }
        >
          Lessons Learned
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
          loadingText="Loading lessons"
          items={paginated}
          columnDefinitions={[
            {
              id: "content",
              header: "Lesson",
              cell: (item) => item.content || "—",
            },
            {
              id: "createdAt",
              header: "Created",
              cell: (item) => formatCreated(item.createdAt),
              width: 200,
            },
            {
              id: "actions",
              header: "Actions",
              cell: (item) => (
                <Button
                  variant="inline-link"
                  onClick={() => setConfirmDelete(item)}
                >
                  Delete
                </Button>
              ),
              width: 120,
            },
          ]}
          empty={
            loading ? (
              <Spinner />
            ) : (
              <Box textAlign="center" color="text-status-inactive">
                No lessons recorded yet for this ontology.
              </Box>
            )
          }
          filter={
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Search lessons"
              onChange={({ detail }) => {
                setFilterText(detail.filteringText);
                setCurrentPage(1);
              }}
            />
          }
          pagination={
            <Pagination
              currentPageIndex={currentPage}
              pagesCount={Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))}
              onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
            />
          }
        />

        <Modal
          visible={!!confirmDelete}
          header="Delete lesson?"
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
                <Button
                  variant="primary"
                  onClick={handleDelete}
                  loading={deleting}
                >
                  Delete
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          This permanently removes the record from AgentCore Memory.
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
