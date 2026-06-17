/**
 * ClarificationOptions — clickable disambiguation chips for a clarification turn.
 *
 * When the query agent can't resolve an ambiguous term (RAG Phase 2 / VKG
 * disambiguation), it returns a `needs_clarification` payload whose structured
 * options are persisted on the assistant turn as
 * `m.totals.clarification = { original_question, options: [{ id, label }] }`.
 * This renders those options as Cloudscape buttons (reusing the suggestion-chip
 * pattern from LandingPage). Clicking one submits the option `id` — the exact
 * key `resolve_clarification_reply` matches first — through the normal
 * `sendMessage` path, so the next turn streams exactly as typing the choice
 * would.
 */
import React from "react";
import { Box, Button, SpaceBetween } from "@cloudscape-design/components";

export default function ClarificationOptions({
  clarification,
  disabled = false,
  onSelect,
}) {
  const options = clarification?.options;
  if (!Array.isArray(options) || options.length === 0) {
    return null;
  }

  return (
    <Box margin={{ top: "xs" }}>
      <SpaceBetween size="xs">
        <Box variant="small" color="text-status-inactive">
          Pick one to continue:
        </Box>
        <SpaceBetween direction="horizontal" size="xs">
          {options.map((opt, i) => {
            // Submit the option id (the exact-match key the backend's
            // resolve_clarification_reply checks first); fall back to label.
            const value = opt?.id || opt?.label || "";
            const text = opt?.label || opt?.id || "";
            if (!value) return null;
            return (
              <Button
                key={`${value}-${i}`}
                variant="normal"
                disabled={disabled}
                onClick={() => onSelect && onSelect(value)}
              >
                {text}
              </Button>
            );
          })}
        </SpaceBetween>
      </SpaceBetween>
    </Box>
  );
}
