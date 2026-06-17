/**
 * ChatView — full-screen chat for one session (chat-first redesign).
 *
 * Owns the AG-UI streaming via the shared ``useChatStream`` hook, renders the
 * transcript + composer. The chat list / "+ New chat" UI lives in the global
 * SideNavigation (App.js) since the 2026-05-27 sidebar consolidation, so this
 * page no longer renders an inner rail.
 *
 * If the route was navigated to from LandingPage with a ``seedMessage`` in
 * location.state, we dispatch it on first paint so the user's typed question
 * doesn't get lost across the route transition.
 *
 * Recovers ontologyId/mode in three increasing-cost steps:
 *   1. location.state (set by LandingPage or the SideNavigation chat link).
 *   2. ``GET /query/sessions/{id}`` response.
 *   3. Bail out with an error if neither resolved (corrupt URL).
 */
import React, { useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import {
  Alert,
  Box,
  Header,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { chatAPI } from "../../services/api";
import useChatStream from "../../hooks/useChatStream";
import ChatTranscript from "./ChatTranscript";
import Composer from "./Composer";

export default function ChatView({ sessionIdOverride }) {
  const sessionId = sessionIdOverride;
  const navigate = useNavigate();
  const location = useLocation();

  const [ontologyId, setOntologyId] = useState(
    location.state?.ontologyId || null,
  );
  const [ontologyName, setOntologyName] = useState(
    location.state?.ontologyName || "",
  );
  const [ontologyVersion, setOntologyVersion] = useState(
    location.state?.ontologyVersion || "",
  );
  const [mode, setMode] = useState(location.state?.mode || "vkg");
  const [resolveError, setResolveError] = useState(null);
  const [resolving, setResolving] = useState(!location.state?.ontologyId);

  // If we landed here from a deep link / page refresh, the ontologyId/mode
  // need to be recovered from the persisted session row.
  useEffect(() => {
    if (location.state?.ontologyId) {
      return;
    }
    let cancelled = false;
    (async () => {
      const result = await chatAPI.getSession(sessionId);
      if (cancelled) return;
      if (result.success && result.data?.ontologyId) {
        setOntologyId(result.data.ontologyId);
        setMode(result.data.mode || "vkg");
        // Backend hydrates name + version onto the session row (best-effort).
        if (result.data.ontologyName) {
          setOntologyName(result.data.ontologyName);
        } else if (!ontologyName) {
          setOntologyName(result.data.ontologyId);
        }
        if (result.data.ontologyVersion) {
          setOntologyVersion(result.data.ontologyVersion);
        }
      } else {
        setResolveError(
          "Could not load this chat — it may have been archived.",
        );
      }
      setResolving(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, location.state, ontologyName]);

  const {
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
  } = useChatStream({ sessionId, ontologyId, mode });

  // Dispatch the seed message exactly once when the hook is ready.
  // Tracking via ref so a re-render with the same state.seedMessage doesn't
  // re-send the same prompt.
  const seedDispatchedRef = useRef(false);
  useEffect(() => {
    if (seedDispatchedRef.current) return;
    if (!ontologyId || !sessionId) return;
    const seed = location.state?.seedMessage;
    if (!seed) {
      seedDispatchedRef.current = true;
      return;
    }
    seedDispatchedRef.current = true;
    sendMessage(seed);
    // Wipe the seed from history so a back-forward navigation doesn't replay.
    navigate(`${location.pathname}?session=${sessionId}`, {
      replace: true,
      state: {
        ontologyId,
        ontologyName,
        ontologyVersion,
        mode,
      },
    });
  }, [
    ontologyId,
    sessionId,
    location.state,
    location.pathname,
    sendMessage,
    navigate,
    ontologyName,
    ontologyVersion,
    mode,
  ]);

  // The global SideNavigation now owns the chat list and "+ New chat" — when
  // the user navigates away mid-stream we still need to abort the in-flight
  // fetch, so listen for sessionId/path changes and cancel.
  useEffect(() => {
    return () => {
      cancel();
    };
  }, [sessionId, cancel]);

  return (
    <div style={{ display: "flex", height: "calc(100vh - 56px)" }}>
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          padding: "16px 24px",
          overflow: "hidden",
        }}
      >
        <Header
          variant="h2"
          description={
            // Surface the semantic-layer id + version and the session id so
            // users/oncall can tell WHICH layer (and which version of it) a
            // chat is querying, and copy the ids when correlating against
            // backend logs/traces. The layer id + version disambiguate two
            // layers that happen to share a display name.
            ontologyId || sessionId ? (
              <Box variant="small" color="text-status-inactive">
                <SpaceBetween direction="vertical" size="xxxs">
                  {ontologyId && (
                    <span style={{ fontFamily: "monospace" }}>
                      Semantic layer: {ontologyId}
                      {ontologyVersion ? ` (${ontologyVersion})` : ""}
                      {mode ? ` · ${mode}` : ""}
                    </span>
                  )}
                  {sessionId && (
                    <span style={{ fontFamily: "monospace" }}>
                      Session: {sessionId}
                    </span>
                  )}
                </SpaceBetween>
              </Box>
            ) : null
          }
        >
          {ontologyName || "Chat"}
        </Header>
        {resolving && (
          <Box padding="m">
            <StatusIndicator type="loading">Loading chat…</StatusIndicator>
          </Box>
        )}
        {resolveError && <Alert type="error">{resolveError}</Alert>}
        {!resolving && !resolveError && (
          <>
            <div
              style={{
                flex: 1,
                overflowY: "auto",
                marginTop: "12px",
                marginBottom: "12px",
              }}
            >
              <ChatTranscript
                messages={messages}
                streamingTurnId={streamingTurnId}
                streamingText={streamingText}
                streamingThinking={streamingThinking}
                streamingToolCalls={streamingToolCalls}
                streamingPhases={streamingPhases}
                streamingError={streamingError}
                sessionId={sessionId}
                ontologyId={ontologyId}
                mode={mode}
                onSelectClarification={sendMessage}
              />
            </div>
            {/* Stop renders next to Send inside the Composer while streaming. */}
            <Composer
              disabled={streaming}
              onSubmit={sendMessage}
              streaming={streaming}
              onStop={cancel}
            />
          </>
        )}
      </div>
    </div>
  );
}
