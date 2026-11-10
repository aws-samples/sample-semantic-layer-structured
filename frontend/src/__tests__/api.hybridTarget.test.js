/**
 * Tests that the AG-UI streaming chat helper routes the "hybrid" chat mode to
 * the correct Gateway target. Mirrors the routing tests in
 * api.streamChat.test.js exactly (same fetch mocking + call shape).
 */
jest.mock("axios");

describe("queryAPI.streamChat hybrid routing", () => {
  const GATEWAY_URL =
    "https://gw123.gateway.bedrock-agentcore.us-east-1.amazonaws.com";

  afterEach(() => {
    // Restore the fallback behaviour for the other tests/suites.
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

  it("routes hybrid to the semantic-layer-query target", async () => {
    const queryAPI = loadApiWithGateway();
    global.fetch = jest.fn().mockResolvedValue(makeStreamResponse([]));

    queryAPI.streamChat({
      sessionId: "s",
      ontologyId: "o",
      mode: "hybrid",
      message: "hi",
    });
    await new Promise((r) => setTimeout(r, 0));

    expect(global.fetch).toHaveBeenCalled();
    const calledUrl = global.fetch.mock.calls[0][0];
    expect(calledUrl).toBe(`${GATEWAY_URL}/semantic-layer-query/invocations`);
  });
});
