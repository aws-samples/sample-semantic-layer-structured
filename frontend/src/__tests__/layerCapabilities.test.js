/**
 * Layer-capability predicates.
 *
 * The interesting cases are the two asymmetries: Hybrid is graph-backed (it was
 * excluded for a year by an accident of copy-paste), and an UNKNOWN type must
 * produce no claim in either direction.
 */
import {
  hasOntologyGraph,
  isKnownLayerType,
  rulesUnsupportedFor,
} from "../utils/layerCapabilities";

describe("hasOntologyGraph", () => {
  it("is true for VKG and Hybrid", () => {
    expect(hasOntologyGraph("VKG")).toBe(true);
    expect(hasOntologyGraph("Hybrid")).toBe(true);
  });

  it("is false for Semantic RAG, which builds no graph", () => {
    expect(hasOntologyGraph("SemanticRAG")).toBe(false);
  });

  it("is false for an unread type", () => {
    expect(hasOntologyGraph(null)).toBe(false);
    expect(hasOntologyGraph(undefined)).toBe(false);
  });
});

describe("isKnownLayerType", () => {
  it("accepts the three types the wizard persists", () => {
    ["VKG", "Hybrid", "SemanticRAG"].forEach((t) =>
      expect(isKnownLayerType(t)).toBe(true),
    );
  });

  it("rejects anything else, including a lowercased token", () => {
    [null, undefined, "", "vkg", "hybrid", "Graph"].forEach((t) =>
      expect(isKnownLayerType(t)).toBe(false),
    );
  });
});

describe("rulesUnsupportedFor", () => {
  it("is true only for Semantic RAG", () => {
    expect(rulesUnsupportedFor("SemanticRAG")).toBe(true);
    expect(rulesUnsupportedFor("VKG")).toBe(false);
    expect(rulesUnsupportedFor("Hybrid")).toBe(false);
  });

  it("makes no claim about a layer whose type failed to load", () => {
    // Not the negation of hasOntologyGraph — an unknown type must not render as
    // "rules do not apply here".
    expect(rulesUnsupportedFor(null)).toBe(false);
    expect(rulesUnsupportedFor(undefined)).toBe(false);
  });
});
