/**
 * AskQuestion — chat-first orchestrator route (replaces NaturalLanguageQuery).
 *
 * One route ``/query/ask`` that switches between two internal modes via the
 * ``?session=<id>`` search param:
 *   - no session → <LandingPage>: empty-state composer + suggestions.
 *   - session set → <ChatView>: full transcript + composer + rail.
 *
 * The conversations rail is rendered by both children — keeping it inside
 * the children rather than here means we don't have to thread the active
 * session id through a wrapper, and routing animations stay simple.
 *
 * Why not React Router children? Because the chat rail needs to react to
 * "click an existing session" which is a same-route URL change. Reading
 * ``?session`` here lets a single component re-render on that change.
 */
import React, { useCallback } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import LandingPage from "./LandingPage";
import ChatView from "./ChatView";

export default function AskQuestion({ enableSemanticRag = false }) {
  const [searchParams] = useSearchParams();
  const sessionId = searchParams.get("session") || "";

  if (sessionId) {
    // Wrap ChatView so we can pass the sessionId through props instead of
    // re-reading the route param. Avoids a second hook call inside ChatView.
    return <ChatView sessionIdOverride={sessionId} />;
  }
  return <LandingPage enableSemanticRag={enableSemanticRag} />;
}
