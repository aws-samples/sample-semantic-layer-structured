/**
 * useChatSessions — owns the caller's chat-session list for the consolidated
 * sidebar. Used by App.js to render chat history inline in the global
 * SideNavigation (replacing the standalone ConversationsRail introduced in
 * 2026-05-24 and removed in 2026-05-27).
 *
 * Listens for a window-level "chat-sessions-changed" event so that
 * useChatStream can trigger a refresh on run_finished without threading a
 * callback through the AppLayout. Same hook handles optimistic archive.
 */
import { useCallback, useEffect, useState } from "react";
import { chatAPI } from "../services/api";

export const CHAT_SESSIONS_CHANGED_EVENT = "chat-sessions-changed";

export default function useChatSessions() {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const result = await chatAPI.listSessions({ limit: 50 });
    if (result.success && Array.isArray(result.data?.sessions)) {
      setSessions(result.data.sessions);
    } else {
      setError(result.error || "Could not load chats");
      setSessions([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const handler = () => refresh();
    window.addEventListener(CHAT_SESSIONS_CHANGED_EVENT, handler);
    return () =>
      window.removeEventListener(CHAT_SESSIONS_CHANGED_EVENT, handler);
  }, [refresh]);

  const archive = useCallback(
    async (sessionId) => {
      setSessions((prev) => prev.filter((s) => s.sessionId !== sessionId));
      const result = await chatAPI.deleteSession(sessionId);
      if (!result.success) {
        refresh();
      }
    },
    [refresh],
  );

  // Archive every session at once (sidebar "Clear all"). Optimistically clears
  // the list, then reconciles with the server if the bulk call fails.
  const archiveAll = useCallback(async () => {
    setSessions([]);
    const result = await chatAPI.deleteAllSessions();
    if (!result.success) {
      refresh();
    }
    return result;
  }, [refresh]);

  return { sessions, loading, error, refresh, archive, archiveAll };
}
