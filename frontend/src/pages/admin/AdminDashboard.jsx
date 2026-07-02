import React, { useState, useEffect } from "react";
import {
  Container,
  Header,
  SpaceBetween,
  Cards,
  Box,
  Button,
  StatusIndicator,
  Modal,
  Alert,
} from "@cloudscape-design/components";
import { useNavigate } from "react-router-dom";
import { ontologyAPI } from "../../services/api";

// "Use Cases" descriptions vary from one line to many; clamp them to a fixed
// number of lines (with an ellipsis) and reserve that height so every card is
// the same size. LINE_HEIGHT_EM mirrors Cloudscape's body text line-height.
const USE_CASES_MAX_LINES = 4;
const USE_CASES_LINE_HEIGHT_EM = 1.4;

export default function AdminDashboard({ user }) {
  const navigate = useNavigate();
  const [ontologies, setOntologies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [deleteModalVisible, setDeleteModalVisible] = useState(false);
  const [ontologyToDelete, setOntologyToDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  useEffect(() => {
    loadOntologies();
  }, []);

  const loadOntologies = async () => {
    setLoading(true);
    const result = await ontologyAPI.listOntologies();
    if (result.success) {
      setOntologies(result.data.ontologies || []);
    }
    setLoading(false);
  };

  const handleDeleteClick = (ontology) => {
    setOntologyToDelete(ontology);
    setDeleteModalVisible(true);
    setError(null);
  };

  const handleDeleteConfirm = async () => {
    if (!ontologyToDelete) return;

    setDeleting(true);
    setError(null);

    try {
      const result = await ontologyAPI.deleteOntology(ontologyToDelete.id);

      if (result.success) {
        setSuccess(
          `Semantic metadata "${ontologyToDelete.name || ontologyToDelete.id}" deleted successfully`,
        );
        setDeleteModalVisible(false);
        setOntologyToDelete(null);
        // Reload the ontologies list
        await loadOntologies();
        // Clear success message after 3 seconds
        setTimeout(() => setSuccess(null), 3000);
      } else {
        setError(result.error || "Failed to delete ontology");
      }
    } catch (err) {
      setError(err.message || "An error occurred while deleting");
    } finally {
      setDeleting(false);
    }
  };

  const handleDeleteCancel = () => {
    setDeleteModalVisible(false);
    setOntologyToDelete(null);
    setError(null);
  };

  const getStatusIndicator = (status) => {
    const statusMap = {
      draft: <StatusIndicator type="pending">Draft</StatusIndicator>,
      data_sources_selected: (
        <StatusIndicator type="in-progress">
          Data Sources Selected
        </StatusIndicator>
      ),
      metadata_extracted: (
        <StatusIndicator type="in-progress">Metadata Extracted</StatusIndicator>
      ),
      pending: <StatusIndicator type="loading">Pending</StatusIndicator>,
      processing: <StatusIndicator type="loading">Processing</StatusIndicator>,
      building: <StatusIndicator type="loading">Building</StatusIndicator>,
      built: <StatusIndicator type="success">Completed</StatusIndicator>,
      completed: <StatusIndicator type="success">Completed</StatusIndicator>,
      failed: <StatusIndicator type="error">Failed</StatusIndicator>,
    };
    return (
      statusMap[status] || (
        <StatusIndicator type="info">{status}</StatusIndicator>
      )
    );
  };

  const getActionButton = (item) => {
    const { status, id, type } = item;

    // Completed: show View button routed to the correct view page
    if (status === "completed" || status === "built") {
      const viewPath =
        type === "VKG"
          ? `/admin/view-graph?id=${id}`
          : `/admin/view-semantic-metadata/${id}`;
      return <Button onClick={() => navigate(viewPath)}>View</Button>;
    }

    // Processing states: no action button
    if (
      status === "processing" ||
      status === "building" ||
      status === "pending"
    ) {
      return null;
    }

    // All other statuses: link to the specific workflow page
    const continuePathMap = {
      draft: `/admin/describe-intent?id=${id}`,
      data_sources_selected: `/admin/review-metadata?id=${id}`,
      metadata_extracted: `/admin/select-semantic-layer-type/${id}`,
      failed: `/admin/describe-intent?id=${id}`,
    };

    const continuePath = continuePathMap[status];
    if (continuePath) {
      const label = status === "failed" ? "Retry" : "Continue";
      return <Button onClick={() => navigate(continuePath)}>{label}</Button>;
    }

    return null;
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Manage your semantic metadata layers"
        actions={
          <Button
            variant="primary"
            onClick={() => navigate("/admin/describe-intent")}
          >
            Create New Semantic Metadata
          </Button>
        }
      >
        Admin Dashboard
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

      <Container header={<Header variant="h2">Semantic Metadata</Header>}>
        {loading ? (
          <Box textAlign="center" padding="l">
            <StatusIndicator type="loading">
              Loading semantic metadata...
            </StatusIndicator>
          </Box>
        ) : ontologies.length === 0 ? (
          <Box textAlign="center" padding="l" color="text-status-inactive">
            No semantic metadata created yet. Click "Create New Semantic
            Metadata" to get started.
          </Box>
        ) : (
          <Cards
            cardDefinition={{
              header: (item) => (
                <SpaceBetween size="xxs">
                  <span>{item.name || item.id}</span>
                  <Box fontSize="body-s" color="text-status-inactive">
                    ID: {item.id}
                  </Box>
                </SpaceBetween>
              ),
              sections: [
                {
                  id: "version",
                  header: "Version",
                  content: (item) => item.latestVersion || "v1",
                },
                {
                  id: "status",
                  header: "Status",
                  content: (item) => getStatusIndicator(item.status),
                },
                {
                  id: "use-cases",
                  header: "Use Cases",
                  content: (item) => (
                    // Clamp to a fixed number of lines so long descriptions are
                    // abbreviated with an ellipsis, and reserve that height (minHeight)
                    // so short descriptions occupy the same space — keeping every card
                    // the same size regardless of text length. Full text on hover.
                    <div
                      title={item.useCasesDescription || undefined}
                      style={{
                        display: "-webkit-box",
                        WebkitLineClamp: USE_CASES_MAX_LINES,
                        WebkitBoxOrient: "vertical",
                        overflow: "hidden",
                        minHeight: `${USE_CASES_MAX_LINES * USE_CASES_LINE_HEIGHT_EM}em`,
                        lineHeight: `${USE_CASES_LINE_HEIGHT_EM}em`,
                      }}
                    >
                      {item.useCasesDescription || "No use cases specified"}
                    </div>
                  ),
                },
                {
                  id: "updated",
                  header: "Last Updated",
                  content: (item) =>
                    item.updatedAt
                      ? new Date(item.updatedAt).toLocaleString()
                      : "Unknown",
                },
                {
                  id: "actions",
                  content: (item) => (
                    <SpaceBetween direction="horizontal" size="xs">
                      {/* Ground Truth + Evaluations moved into the layer's
                          detail screen (View → tabs) so the overview stays
                          focused on View / Continue / Delete. */}
                      {getActionButton(item)}
                      <Button
                        onClick={() => handleDeleteClick(item)}
                        variant="normal"
                      >
                        Delete
                      </Button>
                    </SpaceBetween>
                  ),
                },
              ],
            }}
            cardsPerRow={[{ cards: 1 }, { minWidth: 500, cards: 2 }]}
            items={ontologies}
          />
        )}
      </Container>

      {/* Delete Confirmation Modal */}
      <Modal
        visible={deleteModalVisible}
        onDismiss={handleDeleteCancel}
        header="Delete Semantic Metadata"
        closeAriaLabel="Close modal"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleDeleteCancel}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDeleteConfirm}
                loading={deleting}
                disabled={deleting}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          <Box>
            Are you sure you want to delete the semantic metadata{" "}
            <strong>{ontologyToDelete?.name || ontologyToDelete?.id}</strong>?
          </Box>
          <Alert type="warning">
            This action cannot be undone. All associated data, including:
            <ul>
              <li>Semantic metadata configuration</li>
              <li>Generated metadata files</li>
              <li>Uploaded files</li>
              <li>Metadata</li>
            </ul>
            will be permanently deleted from both DynamoDB and S3.
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
