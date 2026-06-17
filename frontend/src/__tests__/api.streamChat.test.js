/**
 * Tests for the AG-UI streaming chat helper in services/api.js.
 *
 * The helper uses fetch + ReadableStream rather than EventSource (EventSource
 * doesn't accept POST bodies). We feed a synthetic stream of bytes and assert
 * that each SSE record fires onEvent with a parsed payload.
 */
jest.mock("axios");

describe("queryAPI.streamChat", () => {
  beforeEach(() => {
    jest.resetModules();
    // Streaming chat is gateway-only (the legacy /query/chat proxy was removed),
    // so a gateway URL must be present for streamChat to proceed to fetch. Set it
    // BEFORE the module is required (CHAT_GATEWAY_URL is read at module load).
    process.env.REACT_APP_CHAT_GATEWAY_URL =
      "https://gw.test.gateway.bedrock-agentcore.us-east-1.amazonaws.com";
    const axios = require("axios");
    axios.create.mockReturnValue({
      get: jest.fn(),
      post: jest.fn(),
      delete: jest.fn(),
      interceptors: {
        request: { use: jest.fn() },
        response: { use: jest.fn() },
      },
    });
  });

  afterEach(() => {
    delete process.env.REACT_APP_CHAT_GATEWAY_URL;
  });

  function makeStreamResponse(records) {
    // ReadableStream with one chunk per record.
    const encoder = new TextEncoder();
    return {
      ok: true,
      body: new ReadableStream({
        start(controller) {
          for (const r of records) {
            controller.enqueue(encoder.encode(r));
          }
          controller.close();
        },
      }),
    };
  }

  it("emits onEvent for each SSE record", async () => {
    const records = [
      'event: run_started\ndata: {"turnId":"t1","agent":"ontology_query"}\n\n',
      'event: message_chunk\ndata: {"turnId":"t1","delta":"hello"}\n\n',
      'event: run_finished\ndata: {"turnId":"t1","messageId":"m1"}\n\n',
    ];
    global.fetch = jest.fn().mockResolvedValue(makeStreamResponse(records));

    const { queryAPI } = require("../services/api");
    const events = [];
    let closed = false;
    queryAPI.streamChat({
      sessionId: "s",
      ontologyId: "o",
      mode: "vkg",
      message: "hi",
      turnId: "t1",
      onEvent: (e) => events.push(e),
      onClose: () => {
        closed = true;
      },
    });
    // Allow the async streaming loop to flush.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(events.map((e) => e.type)).toEqual([
      "run_started",
      "message_chunk",
      "run_finished",
    ]);
    expect(events[1].delta).toBe("hello");
    expect(closed).toBe(true);
  });

  it("handles records split across chunks", async () => {
    // Split a single SSE record across two byte chunks.
    const part1 = 'event: message_chunk\ndata: {"turnId":"t1","delta":"';
    const part2 = 'world"}\n\n';
    global.fetch = jest
      .fn()
      .mockResolvedValue(makeStreamResponse([part1, part2]));

    const { queryAPI } = require("../services/api");
    const events = [];
    queryAPI.streamChat({
      sessionId: "s",
      ontologyId: "o",
      mode: "vkg",
      message: "hi",
      onEvent: (e) => events.push(e),
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(events).toEqual([
      { type: "message_chunk", turnId: "t1", delta: "world" },
    ]);
  });

  describe("chat gateway URL routing", () => {
    const GATEWAY_URL =
      "https://gw123.gateway.bedrock-agentcore.us-east-1.amazonaws.com";

    afterEach(() => {
      // Restore the fallback behaviour for the other tests/suites.
      delete process.env.REACT_APP_CHAT_GATEWAY_URL;
    });

    function loadApiWithGateway() {
      // CHAT_GATEWAY_URL is read at module load, so set env BEFORE require.
      jest.resetModules();
      const axios = require("axios");
      axios.create.mockReturnValue({
        get: jest.fn(),
        post: jest.fn(),
        delete: jest.fn(),
        interceptors: {
          request: { use: jest.fn() },
          response: { use: jest.fn() },
        },
      });
      process.env.REACT_APP_CHAT_GATEWAY_URL = GATEWAY_URL;
      return require("../services/api").queryAPI;
    }

    it("routes semantic-rag to the metadata-query target", async () => {
      const queryAPI = loadApiWithGateway();
      global.fetch = jest.fn().mockResolvedValue(makeStreamResponse([]));

      queryAPI.streamChat({
        sessionId: "s",
        ontologyId: "o",
        mode: "semantic-rag",
        message: "hi",
      });
      await new Promise((r) => setTimeout(r, 0));

      expect(global.fetch).toHaveBeenCalled();
      const calledUrl = global.fetch.mock.calls[0][0];
      expect(calledUrl).toBe(`${GATEWAY_URL}/metadata-query/invocations`);
    });

    it("routes vkg to the ontology-query target", async () => {
      const queryAPI = loadApiWithGateway();
      global.fetch = jest.fn().mockResolvedValue(makeStreamResponse([]));

      queryAPI.streamChat({
        sessionId: "s",
        ontologyId: "o",
        mode: "vkg",
        message: "hi",
      });
      await new Promise((r) => setTimeout(r, 0));

      expect(global.fetch).toHaveBeenCalled();
      const calledUrl = global.fetch.mock.calls[0][0];
      expect(calledUrl).toBe(`${GATEWAY_URL}/ontology-query/invocations`);
    });
  });

  it("calls onError on non-ok response", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false, status: 500, body: null });

    const { queryAPI } = require("../services/api");
    let errored = null;
    queryAPI.streamChat({
      sessionId: "s",
      ontologyId: "o",
      mode: "vkg",
      message: "hi",
      onError: (e) => {
        errored = e;
      },
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(errored).not.toBeNull();
    expect(errored.message).toMatch(/HTTP 500/);
  });
});
