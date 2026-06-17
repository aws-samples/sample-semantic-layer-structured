/**
 * ChatTranscript — ordered list of user/assistant turns with reasoning panels.
 *
 * Each user turn is a right-aligned bubble; each assistant turn is left-aligned
 * with an attached <ReasoningPanel> driven by AG-UI tool_call events. While a
 * turn is streaming, the in-flight assistant text accumulates from
 * message_chunk deltas; tool_call events stack up in `streamingToolCalls`.
 *
 * Thinking pill: while streaming and before any message_chunk has arrived, a
 * compact "<verb>…" indicator renders directly below the last user bubble
 * (mirrors the lightcast prototype). The verb is derived from the most
 * recent in-flight tool call so the user sees progress instead of a silent
 * 30–60s wait.
 */
import React, { useEffect, useRef } from "react";
import {
  Box,
  CopyToClipboard,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from "@cloudscape-design/components";
import FeedbackBar from "./FeedbackBar";
import MessageMarkdown from "./MessageMarkdown";
import ReasoningPanel from "./ReasoningPanel";
import ResultPanel from "./ResultPanel";
import GraphTraversalPanel from "./GraphTraversalPanel";
import ClarificationOptions from "./ClarificationOptions";
import ProvenanceBadge from "./ProvenanceBadge";

// Map tool names → short, present-tense user-facing verbs. Anything not
// listed falls back to the raw tool name so we never lose information.
const TOOL_VERBS = {
  get_graph_summary: "Reading the schema",
  get_graph_classes: "Reading the schema",
  get_graph_properties: "Reading the schema",
  get_graph_stats: "Reading the schema",
  execute_sparql_query: "Querying the knowledge graph",
  execute_sql_query: "Running the SQL query",
  disambiguate_query_terms: "Disambiguating terms",
  map_sql_results_to_rdf: "Mapping results back to entities",
};

function deriveThinkingLabel(toolCalls) {
  if (!toolCalls || toolCalls.length === 0) return "Thinking";
  const last = toolCalls[toolCalls.length - 1];
  return TOOL_VERBS[last.toolName] || last.toolName || "Thinking";
}

function ThinkingPill({ label }) {
  return (
    <Box margin={{ left: "s", top: "xxs", bottom: "s" }}>
      <SpaceBetween direction="horizontal" size="xs">
        <Spinner size="normal" />
        <Box variant="small" color="text-body-secondary">
          {label}…
        </Box>
      </SpaceBetween>
    </Box>
  );
}

function Bubble({ role, children, headerExtra = null }) {
  const isUser = role === "user";
  // Role label sits above the bubble — left-aligned for assistant,
  // right-aligned for user — so a reader can scan who said what without
  // relying solely on alignment / colour cues. ``headerExtra`` (e.g. the
  // provenance/advisory badge) stacks DIRECTLY UNDERNEATH the role label
  // (same left edge), so a user sees the answer's source immediately while the
  // badge pill stays cleanly aligned with the label rather than sitting beside
  // it on a mismatched baseline.
  return (
    <Box float={isUser ? "right" : "left"} margin={{ bottom: "xs" }}>
      <Box textAlign={isUser ? "right" : "left"} margin={{ bottom: "xxs" }}>
        <Box variant="awsui-key-label">{isUser ? "User" : "Assistant"}</Box>
        {headerExtra ? <Box margin={{ top: "xxs" }}>{headerExtra}</Box> : null}
      </Box>
      <Box
        variant="div"
        padding={{ vertical: "xs", horizontal: "s" }}
        color={isUser ? "inherit" : "text-body-secondary"}
      >
        <Box variant="div" padding="xs" fontSize="body-m">
          {children}
        </Box>
      </Box>
    </Box>
  );
}

export default function ChatTranscript({
  messages = [],
  streamingTurnId = null,
  streamingText = "",
  streamingThinking = "",
  streamingToolCalls = [],
  streamingPhases = [],
  streamingError = null,
  sessionId = null,
  ontologyId = null,
  mode = null,
  onSelectClarification = null,
}) {
  const scrollRef = useRef(null);

  // Pair each assistant turn with the question that triggered it so the
  // feedback bar can include question+answer in the AgentCore Memory event
  // (helps the SemanticStrategy mine more useful long-term lessons).
  const questionByTurnId = {};
  for (const m of messages) {
    if (m.role === "user" && m.turnId) {
      questionByTurnId[m.turnId] = m.text || "";
    }
  }

  useEffect(() => {
    // Auto-scroll on every new chunk. The scroll container is now the parent
    // (flex: 1; overflow-y: auto), so walk up to the nearest scrollable
    // ancestor and pin it to the bottom rather than scrolling this element.
    const el = scrollRef.current;
    if (!el) return;
    let node = el.parentElement;
    while (node) {
      const oy = window.getComputedStyle(node).overflowY;
      if (
        (oy === "auto" || oy === "scroll") &&
        node.scrollHeight > node.clientHeight
      ) {
        node.scrollTop = node.scrollHeight;
        return;
      }
      node = node.parentElement;
    }
  }, [messages, streamingText, streamingToolCalls, streamingPhases]);

  return (
    // The parent ChatView wraps this in a `flex: 1; overflow-y: auto` region,
    // so the transcript should flow naturally and let THAT container own the
    // scroll. A nested fixed height here produced a premature inner scrollbar
    // even with plenty of page space, so we don't constrain height — only pad.
    <Box>
      <div
        ref={scrollRef}
        style={{
          padding: "0 4px",
        }}
        aria-live="polite"
      >
        <SpaceBetween direction="vertical" size="m">
          {messages.map((m) => (
            <div key={`${m.turnId}-${m.role}`}>
              <Bubble
                role={m.role}
                headerExtra={
                  m.role === "assistant" ? (
                    <ProvenanceBadge provenance={m.totals?.provenance} />
                  ) : null
                }
              >
                <MessageMarkdown text={m.text} />
                {m.text ? (
                  <Box
                    margin={{ top: "xxs" }}
                    textAlign={m.role === "user" ? "right" : "left"}
                  >
                    <CopyToClipboard
                      variant="icon"
                      copyButtonText="Copy message"
                      copyErrorText="Failed to copy"
                      copySuccessText="Message copied"
                      textToCopy={m.text}
                    />
                  </Box>
                ) : null}
                {m.role === "assistant" && (
                  <>
                    {/* ProvenanceBadge now renders next to the "Assistant"
                        header (Bubble headerExtra) so the answer's source is
                        visible immediately, not at the bottom of the turn. */}
                    {m.totals?.clarification?.options?.length ? (
                      <ClarificationOptions
                        clarification={m.totals.clarification}
                        disabled={!!streamingTurnId}
                        onSelect={onSelectClarification}
                      />
                    ) : null}
                    <ReasoningPanel
                      toolCalls={m.reasoningSteps || []}
                      phases={m.phaseTimeline || m.totals?.phaseTimeline || []}
                      thinking={m.thinking || ""}
                      turnId={m.turnId}
                    />
                    <GraphTraversalPanel totals={m.totals} mode={mode} />
                    <ResultPanel totals={m.totals} turnId={m.turnId} />
                    <FeedbackBar
                      sessionId={sessionId}
                      ontologyId={ontologyId}
                      turnId={m.turnId}
                      question={questionByTurnId[m.turnId]}
                      answer={m.text}
                    />
                  </>
                )}
              </Bubble>
            </div>
          ))}

          {streamingTurnId &&
            (streamingError ? (
              <div>
                <Bubble role="assistant">
                  <StatusIndicator type="error">
                    {streamingError}
                  </StatusIndicator>
                </Bubble>
              </div>
            ) : streamingText ? (
              // Once the answer starts arriving, drop the spinner and render
              // the in-flight assistant bubble like a finalized one.
              <div>
                <Bubble role="assistant">
                  <MessageMarkdown text={streamingText} />
                  <ReasoningPanel
                    toolCalls={streamingToolCalls}
                    phases={streamingPhases}
                    thinking={streamingThinking}
                    turnId={streamingTurnId}
                  />
                </Bubble>
              </div>
            ) : (
              // Pre-text phase: tool calls may already be running. Show a
              // compact pill below the user bubble that reflects what the
              // agent is currently doing, then upgrade to the assistant
              // bubble once message_chunks start flowing.
              <ThinkingPill label={deriveThinkingLabel(streamingToolCalls)} />
            ))}
        </SpaceBetween>
      </div>
    </Box>
  );
}
