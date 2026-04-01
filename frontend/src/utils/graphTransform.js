/**
 * Graph data transformation utilities for the interactive knowledge graph visualization.
 *
 * Neptune stores class URIs as full IRIs, e.g.:
 *   http://example.com/ontology/{id}/PolicyHolder
 * Entity names returned by the API are short labels (rdfs:label), e.g. "PolicyHolder".
 * Relationship from/to values come directly from Neptune and may be full URIs.
 * normalizeRef strips the URI prefix so links resolve against entity names.
 */

// ---------------------------------------------------------------------------
// URI normalization
// ---------------------------------------------------------------------------

/**
 * Convert a full URI or a plain local name into the short local name that
 * matches entity.name values returned by the API.
 *
 * Examples:
 *   "http://example.com/ontology/my-ont/PolicyHolder" → "PolicyHolder"
 *   "http://example.com/ontology/my-ont/Holding/hasParty" → "hasParty"
 *   "PolicyHolder" → "PolicyHolder"  (already short)
 */
export function normalizeRef(ref) {
  if (!ref || ref === '-') return ref;
  const hashIdx = ref.lastIndexOf('#');
  if (hashIdx !== -1) return ref.slice(hashIdx + 1);
  const slashIdx = ref.lastIndexOf('/');
  if (slashIdx !== -1) return ref.slice(slashIdx + 1);
  return ref;
}

// ---------------------------------------------------------------------------
// Color coding by connectivity
// ---------------------------------------------------------------------------

const BASE_HUE = 213; // blue family — matches Cloudscape primary palette

/**
 * Build a mapping of entity name → connection count (degree).
 * Both from and to are counted.
 */
function buildDegreeMap(relationships) {
  const degree = {};
  for (const r of relationships) {
    const from = normalizeRef(r.from);
    const to = normalizeRef(r.to);
    if (from && from !== '-') degree[from] = (degree[from] || 0) + 1;
    if (to && to !== '-') degree[to] = (degree[to] || 0) + 1;
  }
  return degree;
}

/**
 * Map a degree value to an HSL color string.
 * More connections → deeper/darker blue, fewer → lighter blue.
 */
function degreeToColor(degree, maxDegree) {
  if (maxDegree === 0) return `hsl(${BASE_HUE}, 65%, 55%)`;
  // Lightness: isolated nodes → 75%, highly connected → 30%
  const t = Math.min(degree / maxDegree, 1);
  const lightness = Math.round(75 - t * 45);
  const saturation = Math.round(55 + t * 25);
  return `hsl(${BASE_HUE}, ${saturation}%, ${lightness}%)`;
}

// ---------------------------------------------------------------------------
// Main transform
// ---------------------------------------------------------------------------

/**
 * Transform the API summary response into the { nodes, links } format
 * expected by react-force-graph-2d.
 *
 * @param {Object} summary  Response from GET /neptune/graph/summary/{id}
 * @returns {{ nodes: Array, links: Array }}
 */
export function transformGraphData(summary) {
  if (!summary) return { nodes: [], links: [] };

  const entities = summary.entities || [];
  const relationships = summary.relationships || [];

  // Build a set of known entity names for link validation
  const entityNames = new Set(entities.map((e) => e.name));

  // Compute per-node degree
  const degreeMap = buildDegreeMap(relationships);
  const maxDegree = Math.max(0, ...Object.values(degreeMap));

  const nodes = entities.map((e) => ({
    id: e.name,
    label: e.name,
    type: e.type || 'Class',
    description: e.description || '',
    degree: degreeMap[e.name] || 0,
    color: degreeToColor(degreeMap[e.name] || 0, maxDegree),
    isStub: false,
  }));

  // Build links — keep any link with valid from/to values regardless of whether
  // the endpoint has a full entity entry (stub nodes are added below for missing ones).
  const links = relationships
    .map((r) => ({
      source: normalizeRef(r.from),
      target: normalizeRef(r.to),
      label: r.name || '',
    }))
    .filter((l) => l.source && l.target && l.source !== '-' && l.target !== '-');

  // Add grey stub nodes for any relationship endpoint not in the main entity set.
  // These represent referenced entities whose ontology was not yet generated.
  const stubNames = new Set();
  for (const l of links) {
    if (!entityNames.has(l.source)) stubNames.add(l.source);
    if (!entityNames.has(l.target)) stubNames.add(l.target);
  }
  const stubNodes = [...stubNames].map((name) => ({
    id: name,
    label: name,
    type: 'Class',
    description: 'Referenced entity — ontology not yet generated',
    degree: degreeMap[name] || 0,
    color: '#9ca3af', // grey — visually distinct from processed entities
    isStub: true,
  }));

  return { nodes: [...nodes, ...stubNodes], links };
}

/**
 * Filter graph data by a set of active relationship labels.
 * Nodes with no remaining links are kept (isolated nodes remain visible).
 *
 * @param {{ nodes, links }} graphData
 * @param {Set<string>|null} activeRelTypes  null means show all
 * @returns {{ nodes, links }}
 */
export function filterGraphData(graphData, activeRelTypes) {
  if (!activeRelTypes) return graphData;
  const links = graphData.links.filter((l) => activeRelTypes.has(l.label));
  return { nodes: graphData.nodes, links };
}

/**
 * Returns the set of unique relationship labels in the graph.
 * @param {{ nodes, links }} graphData
 * @returns {string[]}
 */
export function getRelationshipTypes(graphData) {
  return [...new Set((graphData.links || []).map((l) => l.label))].sort();
}
