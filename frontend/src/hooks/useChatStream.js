/**
 * useChatStream — encapsulates the AG-UI streaming state machine.
 *
 * Owned by the chat-first redesign (2026-05-24): the same state machine is
 * needed by both the dedicated ChatView page (one row from the sidebar) and
 * the LandingPage's empty-state composer (which mints a session on first
 * send and immediately routes into ChatView). Lifting the logic out of
 * ChatPanel into a hook keeps both call sites identical without copy-paste.
 *
 * Responsibilities:
 *  - Mint / persist a sessionId (provided by caller) and rehydrate the
 *    transcript on mount via chatAPI.getSession.
 *  - Drive the AG-UI event stream (queryAPI.streamChat) and accumulate
 *    streamingText + tool calls as they arrive.
 *  - Expose `sendMessage(text)`, `cancel()`, and `reset()` so the caller
 *    can wire a Composer + "New chat" button without re-implementing the
 *    event loop.
 *
 * Returns `{messages, streaming, streamingTurnId, streamingText,
 * streamingToolCalls, streamingError, sendMessage, cancel, reset}`.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { chatAPI, queryAPI } from "../services/api";
import { CHAT_SESSIONS_CHANGED_EVENT } from "./useChatSessions";

function uuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export default function useChatStream({ sessionId, ontologyId, mode }) {
  const [messages, setMessages] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingTurnId, setStreamingTurnId] = useState(null);
  const [streamingText, setStreamingText] = useState("");
  const [streamingThinking, setStreamingThinking] = useState("");
  const [streamingToolCalls, setStreamingToolCalls] = useState([]);
  const [streamingPhases, setStreamingPhases] = useState([]);
  const [streamingError, setStreamingError] = useState(null);

  // AbortController for the in-flight fetch + a generation counter so a
  // late-arriving event from a cancelled stream can't overwrite fresh state.
  const controllerRef = useRef(null);
  const generationRef = useRef(0);

  // Tracks the session id the rehydrate effect last applied. Lets us tell a
  // genuine session SWITCH (clear + replace unconditionally) apart from the
  // optimistic-send race on a brand-new session (preserve the local bubble).
  const loadedSessionRef = useRef(null);

  // Restore transcript on session change. 404 is expected for a fresh
  // session and silently produces an empty transcript.
  //
  // Race fix: only overwrite local messages when the server actually returns
  // some — a 404 / empty body for a brand-new session must NOT clobber the
  // optimistic user bubble that sendMessage appended a tick earlier (e.g. the
  // LandingPage seedMessage flow dispatches before the rehydrate fetch
  // resolves).
  useEffect(() => {
    if (!sessionId) return undefined;
    // A switch to a different session must not leave the previous session's
    // transcript on screen while the fetch is in flight — clear it immediately.
    // The first load for a session counts as a switch too; the optimistic
    // user bubble that sendMessage appends a tick later is protected below by
    // only preserving local messages on an EMPTY fetch for the SAME session.
    const isSwitch = loadedSessionRef.current !== sessionId;
    if (isSwitch) {
      setMessages([]);
      loadedSessionRef.current = sessionId;
    }
    let cancelled = false;
    (async () => {
      const result = await chatAPI.getSession(sessionId);
      if (cancelled) return;
      if (result.success && Array.isArray(result.data?.messages)) {
        // A NON-empty transcript always wins — it's the persisted server state.
        // An EMPTY fetch is intentionally a no-op: on a genuine switch we ALREADY
        // cleared to [] above, so the new session correctly shows empty; on a
        // brand-new session the empty body must NOT clobber the optimistic user
        // bubble sendMessage appended while this fetch was in flight (the
        // LandingPage seedMessage race — commit 4977b99).
        if (result.data.messages.length > 0) {
          setMessages(result.data.messages);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const cancel = useCallback(() => {
    if (controllerRef.current) {
      controllerRef.current.abort();
      controllerRef.current = null;
    }
    setStreaming(false);
    setStreamingTurnId(null);
    setStreamingText("");
    setStreamingThinking("");
    setStreamingToolCalls([]);
    setStreamingPhases([]);
  }, []);

  const reset = useCallback(() => {
    cancel();
    generationRef.current += 1;
    setMessages([]);
    setStreamingError(null);
  }, [cancel]);

  const sendMessage = useCallback(
    async (text) => {
      if (!sessionId || !ontologyId || !text) return;
      const turnId = uuid();
      const generation = ++generationRef.current;

      // Optimistic append so the user sees their bubble instantly.
      setMessages((prev) => [
        ...prev,
        { role: "user", text, turnId, reasoningSteps: [] },
      ]);
      setStreaming(true);
      setStreamingTurnId(turnId);
      setStreamingText("");
      setStreamingThinking("");
      setStreamingToolCalls([]);
      setStreamingPhases([]);
      setStreamingError(null);

      let accumulated = "";
      let thinkingAccumulated = "";
      const toolCalls = new Map();
      // Per-phase trace rows. Keyed by phase+step+round so a loop-back round
      // (judge expand / grounding re-check) updates its own row instead of
      // duplicating an earlier phase. Insertion order = display order.
      const phases = [];
      const phaseIndex = new Map();
      const phaseKey = (evt) =>
        `${evt.phase}:${evt.step || ""}:${evt.round || evt.groundingRound || 1}`;

      const stillCurrent = () => generationRef.current === generation;

      const handleEvent = (evt) => {
        if (!stillCurrent()) return;
        switch (evt.type) {
          case "run_started":
            break;
          case "tool_call_start":
            toolCalls.set(evt.callId, {
              callId: evt.callId,
              toolName: evt.toolName,
              args: evt.args,
              startedAt: evt.startedAt,
              status: "running",
            });
            setStreamingToolCalls(Array.from(toolCalls.values()));
            break;
          case "tool_call_end": {
            const existing = toolCalls.get(evt.callId) || {};
            toolCalls.set(evt.callId, {
              ...existing,
              callId: evt.callId,
              result: evt.result,
              durationMs: evt.durationMs,
              endedAt: evt.endedAt,
              status: "success",
            });
            setStreamingToolCalls(Array.from(toolCalls.values()));
            break;
          }
          case "tier_event": {
            // Per-phase trace from the Tier 2 graph workflow. phase_start
            // appends a row; the matching phase_result patches it in place.
            const key = phaseKey(evt);
            if (evt.action === "phase_start") {
              if (!phaseIndex.has(key)) {
                phaseIndex.set(key, phases.length);
                phases.push({
                  phase: evt.phase,
                  step: evt.step || null,
                  round: evt.round || evt.groundingRound || 1,
                  status: "running",
                  startedAt: evt.startedAt || Date.now(),
                });
              }
            } else if (evt.action === "phase_result") {
              const idx = phaseIndex.has(key)
                ? phaseIndex.get(key)
                : (() => {
                    phaseIndex.set(key, phases.length);
                    phases.push({
                      phase: evt.phase,
                      step: evt.step || null,
                      round: evt.round || evt.groundingRound || 1,
                      startedAt: evt.startedAt || Date.now(),
                    });
                    return phases.length - 1;
                  })();
              phases[idx] = {
                ...phases[idx],
                status: "success",
                endedAt: evt.endedAt || Date.now(),
                result: {
                  candidateCount: evt.candidateCount,
                  candidates: evt.candidates,
                  candidateKind: evt.candidateKind,
                  mappings: evt.mappings,
                  ambiguities: evt.ambiguities,
                  judgeRounds: evt.judgeRounds,
                  status: evt.status,
                  sufficient: evt.sufficient,
                  ambiguous: evt.ambiguous,
                  repaired: evt.repaired,
                  regenerated: evt.regenerated,
                  grounded: evt.grounded,
                  missing: evt.missing,
                  rowCount: evt.rowCount,
                  overLimit: evt.overLimit,
                  degraded: evt.degraded,
                  sql: evt.sql,
                  sparql: evt.sparql,
                  // Phase 3 (RAG) slice JSON string — drives the slice
                  // view/download in ReasoningPanel (todo item 2).
                  slice: evt.slice,
                  // Phase 5 emits the actual SQL/SPARQL result so ReasoningPanel
                  // can render the "Results (N rows)" table. Without capturing
                  // these the tabular result is silently dropped and the user
                  // only sees the prose answer + reasoning panels.
                  columns: evt.columns,
                  rows: evt.rows,
                  inputTokens: evt.inputTokens,
                  outputTokens: evt.outputTokens,
                },
              };
            }
            setStreamingPhases([...phases]);
            break;
          }
          case "message_chunk":
            accumulated += evt.delta || "";
            setStreamingText(accumulated);
            break;
          case "thinking_chunk":
            thinkingAccumulated += evt.delta || "";
            setStreamingThinking(thinkingAccumulated);
            break;
          case "run_finished":
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant",
                text: accumulated,
                turnId,
                reasoningSteps: Array.from(toolCalls.values()),
                phaseTimeline: phases.slice(),
                totals: evt.totals,
                thinking: thinkingAccumulated || undefined,
              },
            ]);
            setStreaming(false);
            setStreamingTurnId(null);
            setStreamingText("");
            setStreamingThinking("");
            setStreamingToolCalls([]);
            setStreamingPhases([]);
            // Nudge any listeners (consolidated sidebar) so a freshly-titled
            // session appears in the global nav without a manual refresh.
            window.dispatchEvent(new CustomEvent(CHAT_SESSIONS_CHANGED_EVENT));
            break;
          case "run_error":
            setStreamingError(evt.error || "agent error");
            setStreaming(false);
            break;
          default:
            break;
        }
      };

      const handleError = (err) => {
        if (!stillCurrent()) return;
        setStreamingError(err?.message || String(err));
        setStreaming(false);
      };

      const handleClose = () => {
        if (!stillCurrent()) return;
        controllerRef.current = null;
      };

      const controller = await queryAPI.streamChat({
        sessionId,
        ontologyId,
        mode,
        message: text,
        turnId,
        onEvent: handleEvent,
        onError: handleError,
        onClose: handleClose,
      });
      controllerRef.current = controller;
    },
    [sessionId, ontologyId, mode],
  );

  // Best-effort cleanup on unmount.
  useEffect(() => {
    return () => {
      if (controllerRef.current) {
        controllerRef.current.abort();
      }
    };
  }, []);

  return {
    messages,
    streaming,
    streamingTurnId,
    streamingText,
    streamingThinking,
    streamingToolCalls,
    streamingPhases,
    streamingError,
    sendMessage,
    cancel,
    reset,
  };
}
