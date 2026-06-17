/**
 * FeedbackBar — 👍/👎 affordance below an assistant turn.
 *
 * Inspired by lightcast's chat UI. Once the user picks a rating, an optional
 * comment box appears so they can explain why; submitting writes the feedback
 * to the per-ontology DynamoDB table via ``POST /query/feedback``. The
 * Feedback admin tab lists/deletes those rows; comments are PII-redacted
 * via Bedrock Guardrails before persistence.
 *
 * Local state only — once submitted, the bar collapses to a "Thanks" line.
 * No history is read back per turn (the lessons tab is the canonical view).
 */
import React, { useState } from "react";
import {
  Box,
  Button,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from "@cloudscape-design/components";
import { queryAPI } from "../../services/api";

export default function FeedbackBar({
  sessionId,
  ontologyId,
  turnId,
  question,
  answer,
}) {
  // `stage` controls which UI is shown ("thumbs" | "comment"). It is decoupled
  // from the chosen rating so a thumbs-up that fails to submit stays on the
  // thumbs row with an inline error instead of falling through to the
  // thumbs-down comment box.
  const [stage, setStage] = useState("thumbs");
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState(null);

  if (!turnId || !sessionId || !ontologyId) return null;

  const submit = async (chosenRating) => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await queryAPI.submitFeedback({
        sessionId,
        ontologyId,
        turnId,
        rating: chosenRating,
        comment,
        question: question || "",
        answer: answer || "",
      });
      if (result.success) {
        setSubmitted(true);
      } else {
        setError(result.error || "Failed to record feedback");
      }
    } catch (err) {
      setError(err?.message || "Failed to record feedback");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <Box variant="small" color="text-body-secondary" padding={{ top: "xxs" }}>
        <StatusIndicator type="success">
          Thanks — feedback recorded
        </StatusIndicator>
      </Box>
    );
  }

  // Stage 1: just thumbs. Picking 👍 submits immediately (no comment needed
  // for positive feedback). Picking 👎 opens the comment box so the user can
  // explain — that's the highest-signal data for the lessons tab.
  if (stage === "thumbs") {
    return (
      <SpaceBetween direction="horizontal" size="xs">
        <Button
          variant="inline-icon"
          iconName="thumbs-up"
          ariaLabel="Helpful"
          disabled={submitting}
          onClick={() => submit("up")}
        />
        <Button
          variant="inline-icon"
          iconName="thumbs-down"
          ariaLabel="Not helpful"
          disabled={submitting}
          onClick={() => setStage("comment")}
        />
        {error && (
          <Box variant="small" color="text-status-error">
            {error}
          </Box>
        )}
      </SpaceBetween>
    );
  }

  // Stage 2: thumbs-down was clicked — show comment box.
  return (
    <SpaceBetween direction="vertical" size="xxs">
      <Box variant="small" color="text-body-secondary">
        What was wrong with this answer? (optional)
      </Box>
      <Textarea
        value={comment}
        onChange={({ detail }) => setComment(detail.value)}
        placeholder="e.g. wrong table, missing filter, ignored my follow-up"
        rows={2}
        disabled={submitting}
      />
      <SpaceBetween direction="horizontal" size="xs">
        <Button
          variant="primary"
          loading={submitting}
          onClick={() => submit("down")}
        >
          Submit feedback
        </Button>
        <Button
          variant="link"
          disabled={submitting}
          onClick={() => {
            setStage("thumbs");
            setComment("");
          }}
        >
          Cancel
        </Button>
        {error && (
          <Box variant="small" color="text-status-error">
            {error}
          </Box>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}
