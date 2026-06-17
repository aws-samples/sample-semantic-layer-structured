/**
 * Composer — multi-line message input for the AG-UI chat.
 *
 * Cloudscape Textarea + send Button. Enter submits, Shift+Enter inserts a
 * newline. Disabled while a turn is streaming so the user can't pile up
 * concurrent in-flight requests on the same session.
 */
import React, { useCallback, useState } from "react";
import {
  Box,
  Button,
  SpaceBetween,
  Textarea,
} from "@cloudscape-design/components";

export default function Composer({
  disabled = false,
  onSubmit,
  streaming = false,
  onStop,
}) {
  const [value, setValue] = useState("");

  const send = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSubmit?.(trimmed);
    setValue("");
  }, [value, disabled, onSubmit]);

  // Cloudscape Textarea passes the native event through; we intercept Enter
  // before the textarea inserts a newline so the chat feels like a chat.
  const handleKeyDown = useCallback(
    (e) => {
      const native = e.detail?.nativeEvent || e.nativeEvent || e;
      if (native?.key === "Enter" && !native?.shiftKey) {
        native.preventDefault?.();
        send();
      }
    },
    [send],
  );

  return (
    <Box>
      <SpaceBetween direction="vertical" size="xs">
        <Textarea
          value={value}
          placeholder="Ask a question…"
          rows={3}
          disabled={disabled}
          onChange={({ detail }) => setValue(detail.value)}
          onKeyDown={handleKeyDown}
          ariaLabel="Chat message input"
        />
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {streaming && (
              <Button iconName="close" onClick={onStop}>
                Stop
              </Button>
            )}
            <Button
              variant="primary"
              disabled={disabled || !value.trim()}
              onClick={send}
              iconName="send"
            >
              Send
            </Button>
          </SpaceBetween>
        </Box>
      </SpaceBetween>
    </Box>
  );
}
