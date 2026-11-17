/**
 * rulesAPI URL construction + payload shape.
 *
 * The point of these tests is that a path typo is caught here rather than as a
 * 404 in the browser — the rules router is feature-flagged, so a wrong path and
 * a disabled flag produce the same symptom (an empty rule list) and would be
 * indistinguishable at runtime.
 *
 * Uses the axios-mock harness from api.delta.test.js rather than the vitest
 * style in the plan: this app is CRA/jest, and `apiClient` is a module-scoped
 * axios.create() instance, so the interception point is axios itself.
 */

jest.mock("axios");

describe("rulesAPI", () => {
  let getMock;
  let postMock;
  let putMock;
  let deleteMock;

  beforeEach(() => {
    jest.resetModules();
    getMock = jest.fn().mockResolvedValue({ data: [] });
    postMock = jest.fn().mockResolvedValue({ data: {} });
    putMock = jest.fn().mockResolvedValue({ data: {} });
    deleteMock = jest.fn().mockResolvedValue({ data: null });
    const axios = require("axios");
    axios.create.mockReturnValue({
      get: getMock,
      post: postMock,
      put: putMock,
      delete: deleteMock,
      interceptors: {
        request: { use: jest.fn() },
        response: { use: jest.fn() },
      },
    });
  });

  it("lists rules for a namespace", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.list("ont-1");
    expect(getMock).toHaveBeenCalledWith("/rules/namespaces/ont-1/rules");
  });

  it("encodes namespace and rule id", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.get("ont/1", "RULE 4");
    expect(getMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont%2F1/rules/RULE%204",
    );
  });

  it("creates a rule by POSTing the collection", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.create("ont-1", { rule_id: "RULE-004", namespace: "ont-1" });
    expect(postMock).toHaveBeenCalledWith("/rules/namespaces/ont-1/rules", {
      rule_id: "RULE-004",
      namespace: "ont-1",
    });
  });

  it("imports a mined artefact with the :import verb suffix", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.importMined("ont-1", "mined-rules/doc-7.json");
    expect(postMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules:import",
      { key: "mined-rules/doc-7.json" },
    );
  });

  it("passes the artefact key through unencoded", async () => {
    // The key contains a slash and must reach the body verbatim — encoding it
    // would produce a key the bucket does not contain, i.e. a 404 that reads
    // as "this document mined nothing".
    const { rulesAPI } = require("../services/api");
    await rulesAPI.importMined("ont-1", "mined-rules/a/b.json");
    expect(postMock.mock.calls[0][1]).toEqual({
      key: "mined-rules/a/b.json",
    });
  });

  it("updates a rule with PUT", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.update("ont-1", "RULE-004", { rule_id: "RULE-004" });
    expect(putMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004",
      { rule_id: "RULE-004" },
    );
  });

  it("posts the approver when approving a threshold", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.approveThreshold("ont-1", "RULE-004", "THR-011", "alice@x");
    expect(postMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004/thresholds/THR-011:approve",
      { approver: "alice@x" },
    );
  });

  it("posts the approver when approving a rule", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.approve("ont-1", "RULE-004", "alice@x");
    expect(postMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004:approve",
      { approver: "alice@x" },
    );
  });

  it("uses the :publish verb suffix", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.publish("ont-1", "RULE-004");
    expect(postMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004:publish",
    );
  });

  it("uses the :validate verb suffix", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.validate("ont-1", "RULE-004");
    expect(postMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004:validate",
    );
  });

  it("deletes a rule", async () => {
    const { rulesAPI } = require("../services/api");
    await rulesAPI.remove("ont-1", "RULE-004");
    expect(deleteMock).toHaveBeenCalledWith(
      "/rules/namespaces/ont-1/rules/RULE-004",
    );
  });
});
