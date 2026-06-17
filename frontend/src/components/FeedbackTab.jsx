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
import { feedbackAPI } from "../services/api";

const PAGE_SIZE = 25;

// Render an ISO timestamp as a human-readable local date+time (e.g.
// "6/5/2026, 4:09:04 AM"), matching the Evaluations tab. Falls back to the raw
// value if it isn't a parseable date so we never show "Invalid Date".
function formatCreated(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

/**
 * Admin tab — lists and deletes per-turn 👍/👎 feedback for one ontology.
 *
 * Reads from the DynamoDB feedback table (PII-redacted via Bedrock Guardrails
 * before write — see services/feedback_service.py). There is no edit/create
 * surface here: feedback is captured below each assistant turn in the chat UI.
 */
export default function FeedbackTab({ ontologyId }) {
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
    // feedbackAPI.list resolves to the handleResponse envelope
    // ``{success, data}`` — the backend body ``{feedback: [...]}`` lives at
    // ``res.data.feedback`` (NOT ``res.feedback``). handleResponse also never
    // throws, so check ``success`` rather than relying on try/catch.
    const res = await feedbackAPI.list(ontologyId, { limit: 200 });
    if (res.success) {
      setItems(res.data?.feedback ?? []);
    } else {
      setError(res.error ?? "Failed to load feedback");
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
    const res = await feedbackAPI.remove(ontologyId, confirmDelete.feedbackId);
    if (res.success) {
      setItems((prev) =>
        prev.filter((it) => it.feedbackId !== confirmDelete.feedbackId),
      );
      setConfirmDelete(null);
    } else {
      setError(res.error ?? "Failed to delete feedback");
    }
    setDeleting(false);
  };

  const filtered = filterText
    ? items.filter((it) => {
        const hay = `${it.comment ?? ""} ${it.question ?? ""} ${it.answer ?? ""}`;
        return hay.toLowerCase().includes(filterText.toLowerCase());
      })
    : items;
  const pageStart = (currentPage - 1) * PAGE_SIZE;
  const paginated = filtered.slice(pageStart, pageStart + PAGE_SIZE);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="User 👍/👎 ratings for assistant turns. Comments are PII-redacted by Bedrock Guardrails before being written to DynamoDB."
          actions={
            <Button iconName="refresh" onClick={refresh} disabled={loading}>
              Refresh
            </Button>
          }
        >
          Feedback
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
          loadingText="Loading feedback"
          items={paginated}
          columnDefinitions={[
            {
              id: "rating",
              header: "Rating",
              cell: (item) => {
                const up = item.rating === "up";
                // Fixed-size pill so every row's Rating cell has identical
                // height/width — the previous Badge sized itself to its content
                // (and the emoji + "down" wrapped), making row heights jump.
                // This locks the icon + box dimensions; the column is widened below.
                return (
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: "4px",
                      width: "72px",
                      height: "24px",
                      boxSizing: "border-box",
                      padding: "0 8px",
                      borderRadius: "12px",
                      fontSize: "12px",
                      lineHeight: "1",
                      whiteSpace: "nowrap",
                      color: "#ffffff",
                      backgroundColor: up ? "#037f0c" : "#d91515",
                    }}
                  >
                    {up ? "👍 up" : "👎 down"}
                  </span>
                );
              },
              width: 130,
              minWidth: 130,
            },
            {
              id: "comment",
              header: "Comment",
              cell: (item) => item.comment || "—",
            },
            {
              id: "question",
              header: "Question",
              cell: (item) => item.question || "—",
            },
            {
              id: "createdAt",
              header: "Created",
              cell: (item) => formatCreated(item.createdAt),
              width: 220,
            },
            {
              id: "user",
              header: "User",
              // Prefer the human-readable email; old rows written before the
              // email was persisted fall back to the Cognito sub (userId).
              cell: (item) => item.userEmail || item.userId || "anonymous",
              width: 220,
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
                No feedback recorded yet for this ontology.
              </Box>
            )
          }
          filter={
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Search comments, questions, answers"
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
          header="Delete feedback?"
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
          This permanently removes the feedback row from DynamoDB.
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
