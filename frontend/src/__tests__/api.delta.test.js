jest.mock("axios");

describe("delta API", () => {
  let postMock;
  beforeEach(() => {
    jest.resetModules();
    postMock = jest.fn().mockResolvedValue({ data: { rebuildSet: ["db.a"] } });
    const axios = require("axios");
    axios.create.mockReturnValue({
      get: jest.fn(),
      post: postMock,
      delete: jest.fn(),
      interceptors: {
        request: { use: jest.fn() },
        response: { use: jest.fn() },
      },
    });
  });

  it("computeMetadataDelta POSTs /metadata/delta/{id}", async () => {
    const { computeMetadataDelta } = require("../services/api");
    // handleResponse wraps the axios response as { success, data }, so the
    // rebuildSet lives under out.data.
    const out = await computeMetadataDelta("L1");
    expect(postMock).toHaveBeenCalledWith("/metadata/delta/L1");
    expect(out.success).toBe(true);
    expect(out.data.rebuildSet).toEqual(["db.a"]);
  });

  it("computeOntologyDelta POSTs /ontology/delta/{id}", async () => {
    const { computeOntologyDelta } = require("../services/api");
    await computeOntologyDelta("L1");
    expect(postMock).toHaveBeenCalledWith("/ontology/delta/L1");
  });

  it("buildOntology forwards a delta options body", async () => {
    const { ontologyAPI } = require("../services/api");
    await ontologyAPI.buildOntology("L1", {
      scope: "delta",
      targetTables: ["db.a"],
    });
    expect(postMock).toHaveBeenCalledWith(
      "/ontology/build/L1",
      { scope: "delta", targetTables: ["db.a"] },
      expect.objectContaining({ timeout: expect.any(Number) }),
    );
  });
});
