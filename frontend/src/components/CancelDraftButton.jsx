/**
 * CancelDraftButton — a "Cancel" action for the semantic-layer creation
 * workflow steps (DescribeIntent → SelectDataSources → ReviewMetadata →
 * SelectSemanticLayerType → BuildSemanticMetadata / BuildKnowledgeGraph).
 *
 * Clicking it opens a confirm modal; on confirm it deletes the in-draft
 * semantic layer (DELETE /ontology/config/{id}) and navigates to the admin
 * dashboard. Rendered only on the workflow steps (NOT on AdminDashboard).
 *
 * The draft id may be absent on the very first step (before the draft is
 * created) — in that case Cancel just navigates away without a delete call.
 */
import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Modal,
  Box,
  SpaceBetween,
} from "@cloudscape-design/components";
import { ontologyAPI } from "../services/api";

export default function CancelDraftButton({ ontologyId, variant = "link" }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const confirmCancel = async () => {
    setBusy(true);
    try {
      // Delete the in-draft layer if it has been created yet. Best-effort:
      // navigate away even if the delete fails (the draft is excluded from the
      // dashboard anyway, and a stale draft is harmless).
      if (ontologyId) {
        try {
          await ontologyAPI.deleteOntology(ontologyId);
        } catch (e) {
          // swallow — leaving a draft behind is preferable to trapping the user
          console.warn("Cancel: failed to delete draft layer", e);
        }
      }
      setOpen(false);
      navigate("/admin");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button variant={variant} onClick={() => setOpen(true)}>
        Cancel
      </Button>
      <Modal
        visible={open}
        onDismiss={() => setOpen(false)}
        header="Cancel semantic layer creation"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                disabled={busy}
                onClick={() => setOpen(false)}
              >
                Keep editing
              </Button>
              <Button variant="primary" loading={busy} onClick={confirmCancel}>
                Discard draft
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Discard this in-progress semantic layer? The draft and any metadata
        generated so far will be deleted. This can't be undone.
      </Modal>
    </>
  );
}
