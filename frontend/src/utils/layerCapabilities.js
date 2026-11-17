/**
 * What a semantic layer's stored type implies about the governance affordances
 * the admin tabs may offer.
 *
 * This lives in one module on purpose. The governance tabs are rendered from
 * two different pages and each used to carry its own `=== "VKG"` test, which is
 * how the tabs came to disagree about which layers rules apply to. Anything
 * that widens or narrows the supported set must be changed here, once.
 *
 * The three values are the layer types the build wizard can persist — see
 * `lambda/rest-api/ontology_api.py` (`type: 'VKG' | 'SemanticRAG' | 'Hybrid'`).
 */

export const LAYER_TYPE_VKG = "VKG";
export const LAYER_TYPE_HYBRID = "Hybrid";
export const LAYER_TYPE_SEMANTIC_RAG = "SemanticRAG";

const KNOWN_LAYER_TYPES = [
  LAYER_TYPE_VKG,
  LAYER_TYPE_HYBRID,
  LAYER_TYPE_SEMANTIC_RAG,
];

// VKG and Hybrid both build a Neptune graph and both emit the `vkg:mapsToTable`
// / `vkg:mapsToColumn` predicates that ground an ontology term in a physical
// column. Semantic RAG builds no graph at all — it documents tables into a
// Bedrock Knowledge Base — so there is nothing for an ontology IRI to bind to.
const GRAPH_BACKED_LAYER_TYPES = [LAYER_TYPE_VKG, LAYER_TYPE_HYBRID];

/**
 * Whether the stored type is one this frontend recognises.
 *
 * A layer whose type we have not read yet (config fetch in flight, or failed)
 * is NOT "known" — callers must not conclude anything from its absence.
 *
 * @param {string|null|undefined} layerType The stored layer type.
 * @returns {boolean} True only for a recognised type token.
 */
export function isKnownLayerType(layerType) {
  return KNOWN_LAYER_TYPES.includes(layerType);
}

/**
 * Whether the layer has an ontology graph in Neptune, and therefore whether
 * ontology-class-shaped inputs (a metric's `linked_class`, a rule's subject
 * class) mean anything for it.
 *
 * @param {string|null|undefined} layerType The stored layer type.
 * @returns {boolean} True for VKG and Hybrid layers.
 */
export function hasOntologyGraph(layerType) {
  return GRAPH_BACKED_LAYER_TYPES.includes(layerType);
}

/**
 * Whether we can state that governed rules do NOT apply to this layer.
 *
 * Deliberately not the negation of `hasOntologyGraph`: an unknown type must
 * produce neither claim. Telling a reviewer "rules do not apply here" because a
 * config fetch failed would be a false statement about their layer, and the one
 * they cannot check from the screen they are on.
 *
 * @param {string|null|undefined} layerType The stored layer type.
 * @returns {boolean} True only when the type is known and graph-less.
 */
export function rulesUnsupportedFor(layerType) {
  return isKnownLayerType(layerType) && !hasOntologyGraph(layerType);
}

/**
 * The one wording for "governed rules do not apply to this layer".
 *
 * Lives here, next to the predicate that decides when to show it, because two
 * surfaces render it: the rules tab's own internal alert, and the page-level
 * notice `ViewSemanticRAGMetadata` shows INSTEAD of the tab. Two copies of this
 * sentence would eventually explain the exclusion two different ways.
 */
export const RULES_UNSUPPORTED_MESSAGE =
  "Rules compile to SPARQL over the layer's ontology, so they apply to VKG " +
  "and Hybrid layers — the two that build a graph. A Semantic RAG layer has " +
  "no ontology for a rule to bind to.";
