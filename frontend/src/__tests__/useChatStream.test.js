/**
 * Tests for useChatStream session rehydration on session switch (todo item 1).
 *
 * Regression: switching sessionId (sidebar click) must load the new session's
 * transcript. A prior `messages.length > 0` guard (commit 4977b99) left stale
 * messages on screen because ChatView doesn't remount on switch. The fix must
 * still preserve an optimistic user bubble on a brand-new (empty) session.
 */
import { act, renderHook, waitFor } from "@testing-library/react";

// Prefixed with `mock` so jest.mock's factory may reference them (jest hoists
// the factory above imports and forbids out-of-scope refs that aren't mock*).
const mockGetSession = jest.fn();
const mockStreamChat = jest.fn(async () => ({ abort: jest.fn() }));

jest.mock("../services/api", () => ({
  chatAPI: { getSession: (...a) => mockGetSession(...a) },
  queryAPI: { streamChat: (...a) => mockStreamChat(...a) },
}));

// Mock useChatSessions to avoid pulling its (api-dependent) module graph in.
jest.mock("../hooks/useChatSessions", () => ({
  CHAT_SESSIONS_CHANGED_EVENT: "chat-sessions-changed",
}));

import useChatStream from "../hooks/useChatStream";

const turn = (role, text, turnId) => ({
  role,
  text,
  turnId,
  reasoningSteps: [],
});

beforeEach(() => {
  mockGetSession.mockReset();
  mockStreamChat.mockReset();
  mockStreamChat.mockResolvedValue({ abort: jest.fn() });
});

describe("session rehydration", () => {
  it("loads the new session transcript when sessionId switches", async () => {
    // Session A has 2 turns; B has 1. Start on A, then switch to B.
    mockGetSession.mockImplementation(async (id) =>
      id === "A"
        ? {
            success: true,
            data: {
              messages: [
                turn("user", "qa", "t1"),
                turn("assistant", "aa", "t1"),
              ],
            },
          }
        : {
            success: true,
            data: { messages: [turn("assistant", "bb", "t9")] },
          },
    );

    const { result, rerender } = renderHook(
      ({ sid }) =>
        useChatStream({ sessionId: sid, ontologyId: "o1", mode: "vkg" }),
      { initialProps: { sid: "A" } },
    );

    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    rerender({ sid: "B" });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(result.current.messages[0].text).toBe("bb");
  });

  it("clears stale messages when switching to an empty session", async () => {
    mockGetSession.mockImplementation(async (id) =>
      id === "A"
        ? { success: true, data: { messages: [turn("assistant", "aa", "t1")] } }
        : { success: true, data: { messages: [] } },
    );

    const { result, rerender } = renderHook(
      ({ sid }) =>
        useChatStream({ sessionId: sid, ontologyId: "o1", mode: "vkg" }),
      { initialProps: { sid: "A" } },
    );
    await waitFor(() => expect(result.current.messages).toHaveLength(1));

    rerender({ sid: "B" });
    await waitFor(() => expect(result.current.messages).toHaveLength(0));
  });

  it("preserves an optimistic user bubble on a fresh empty session", async () => {
    // Brand-new session: getSession 404/empty. sendMessage appends optimistically
    // BEFORE the rehydrate resolves; the empty fetch must NOT clobber it.
    mockGetSession.mockResolvedValue({ success: true, data: { messages: [] } });

    const { result } = renderHook(() =>
      useChatStream({ sessionId: "NEW", ontologyId: "o1", mode: "vkg" }),
    );

    await act(async () => {
      await result.current.sendMessage("hello");
    });
    await waitFor(() => expect(result.current.messages).toHaveLength(1));
    expect(result.current.messages[0].text).toBe("hello");
  });
});
