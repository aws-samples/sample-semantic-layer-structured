import React, {
  useRef,
  useState,
  useCallback,
  useEffect,
  useMemo,
} from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import {
  Box,
  SpaceBetween,
  Button,
  Toggle,
  Badge,
  ColumnLayout,
  FormField,
  Multiselect,
} from '@cloudscape-design/components';
import { filterGraphData, getRelationshipTypes } from '../utils/graphTransform';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HIGHLIGHT_LINK_COLOR = '#FF9900'; // AWS orange for selected links
const HIGHLIGHT_RING_COLOR = '#FF9900';
const DIM_OPACITY = 0.15;
const NODE_RADIUS = 8;
const SELECTED_RADIUS = 12;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Interactive 2-D force-directed knowledge graph.
 *
 * Props:
 *   graphData  { nodes: [...], links: [...] }  from transformGraphData()
 *   height     canvas height in px (default 580)
 *   summary    full API summary object (for the details panel properties list)
 */
export default function GraphVisualization({ graphData, height = 580, summary }) {
  const fgRef = useRef(null);
  const containerRef = useRef(null);
  const [canvasWidth, setCanvasWidth] = useState(0);

  // Selected node & its immediate neighbours/links
  const [selectedNode, setSelectedNode] = useState(null);
  const [showLabels, setShowLabels] = useState(true);

  // Relationship type filter
  const allRelTypes = useMemo(
    () => getRelationshipTypes(graphData).map((t) => ({ label: t, value: t })),
    [graphData]
  );
  const [activeRelTypes, setActiveRelTypes] = useState([]);

  // Derived: filtered graph fed to ForceGraph
  const filteredGraph = useMemo(() => {
    if (activeRelTypes.length === 0) return graphData;
    const activeSet = new Set(activeRelTypes.map((o) => o.value));
    return filterGraphData(graphData, activeSet);
  }, [graphData, activeRelTypes]);

  // Highlight sets — recompute whenever selection changes
  const { highlightNodes, highlightLinks } = useMemo(() => {
    if (!selectedNode) return { highlightNodes: new Set(), highlightLinks: new Set() };
    const hn = new Set([selectedNode.id]);
    const hl = new Set();
    for (const link of filteredGraph.links) {
      const srcId = typeof link.source === 'object' ? link.source.id : link.source;
      const tgtId = typeof link.target === 'object' ? link.target.id : link.target;
      if (srcId === selectedNode.id || tgtId === selectedNode.id) {
        hl.add(link);
        hn.add(srcId);
        hn.add(tgtId);
      }
    }
    return { highlightNodes: hn, highlightLinks: hl };
  }, [selectedNode, filteredGraph]);

  // Properties for the selected node (from the summary)
  const nodeProperties = useMemo(() => {
    if (!selectedNode || !summary?.properties) return [];
    return summary.properties.filter((p) => p.entity === selectedNode.id);
  }, [selectedNode, summary]);

  // Connected relationships for details panel
  const nodeRelationships = useMemo(() => {
    if (!selectedNode) return [];
    return filteredGraph.links
      .map((l) => {
        const srcId = typeof l.source === 'object' ? l.source.id : l.source;
        const tgtId = typeof l.target === 'object' ? l.target.id : l.target;
        if (srcId !== selectedNode.id && tgtId !== selectedNode.id) return null;
        return {
          label: l.label,
          direction: srcId === selectedNode.id ? 'out' : 'in',
          other: srcId === selectedNode.id ? tgtId : srcId,
        };
      })
      .filter(Boolean);
  }, [selectedNode, filteredGraph]);

  // Track the actual rendered width of the canvas container
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width;
      if (w > 0) setCanvasWidth(Math.floor(w));
    });
    ro.observe(containerRef.current);
    // Seed initial value
    setCanvasWidth(Math.floor(containerRef.current.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);

  // Fit graph to canvas on data change
  useEffect(() => {
    if (fgRef.current && filteredGraph.nodes.length > 0) {
      setTimeout(() => fgRef.current?.zoomToFit(400, 40), 300);
    }
  }, [filteredGraph]);

  // ---------------------------------------------------------------------------
  // Render callbacks
  // ---------------------------------------------------------------------------

  const paintNode = useCallback(
    (node, ctx, globalScale) => {
      const isSelected = selectedNode?.id === node.id;
      const isHighlighted = highlightNodes.has(node.id);
      const hasSelection = !!selectedNode;

      const radius = isSelected ? SELECTED_RADIUS : NODE_RADIUS;
      const alpha = hasSelection && !isHighlighted ? DIM_OPACITY : 1;

      ctx.save();
      ctx.globalAlpha = alpha;

      // Ring for selected / highlighted nodes
      if (isSelected) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 3, 0, 2 * Math.PI);
        ctx.fillStyle = HIGHLIGHT_RING_COLOR;
        ctx.fill();
      } else if (isHighlighted) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 2, 0, 2 * Math.PI);
        ctx.fillStyle = HIGHLIGHT_RING_COLOR;
        ctx.fill();
      }

      // Node body
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = node.color || '#4A90E2';
      ctx.fill();

      // Dashed border for stub nodes (referenced but not yet processed)
      if (node.isStub) {
        ctx.save();
        ctx.setLineDash([2, 2]);
        ctx.strokeStyle = '#6b7280';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.restore();
      }

      // Label
      if (showLabels || isSelected || isHighlighted) {
        const fontSize = Math.max(10 / globalScale, 3);
        ctx.font = `${isSelected ? 'bold ' : ''}${fontSize}px sans-serif`;
        ctx.fillStyle = hasSelection && !isHighlighted ? `rgba(0,0,0,${DIM_OPACITY})` : '#232F3E';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(node.label, node.x, node.y + radius + fontSize + 1);
      }

      ctx.restore();
    },
    [selectedNode, highlightNodes, showLabels]
  );

  const getLinkColor = useCallback(
    (link) => {
      if (!selectedNode) return 'rgba(100,100,120,0.4)';
      return highlightLinks.has(link)
        ? HIGHLIGHT_LINK_COLOR
        : `rgba(100,100,120,${DIM_OPACITY})`;
    },
    [selectedNode, highlightLinks]
  );

  const getLinkWidth = useCallback(
    (link) => (highlightLinks.has(link) ? 2.5 : 1),
    [highlightLinks]
  );

  const paintLink = useCallback(
    (link, ctx, globalScale) => {
      if (!showLabels && !highlightLinks.has(link)) return;

      const srcX = typeof link.source === 'object' ? link.source.x : 0;
      const srcY = typeof link.source === 'object' ? link.source.y : 0;
      const tgtX = typeof link.target === 'object' ? link.target.x : 0;
      const tgtY = typeof link.target === 'object' ? link.target.y : 0;

      const label = link.label;
      if (!label) return;

      const midX = (srcX + tgtX) / 2;
      const midY = (srcY + tgtY) / 2;

      const fontSize = Math.max(8 / globalScale, 2);
      ctx.font = `${fontSize}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      const isHighlighted = highlightLinks.has(link);
      const hasSelection = !!selectedNode;
      const alpha = hasSelection && !isHighlighted ? DIM_OPACITY : 1;

      // Background pill for readability
      const textWidth = ctx.measureText(label).width;
      const padding = fontSize * 0.4;
      ctx.save();
      ctx.globalAlpha = alpha * 0.85;
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(midX - textWidth / 2 - padding, midY - fontSize / 2 - padding * 0.5, textWidth + padding * 2, fontSize + padding);
      ctx.restore();

      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.fillStyle = isHighlighted ? HIGHLIGHT_LINK_COLOR : '#555566';
      ctx.fillText(label, midX, midY);
      ctx.restore();
    },
    [showLabels, highlightLinks, selectedNode]
  );

  const handleNodeClick = useCallback(
    (node) => {
      setSelectedNode((prev) => (prev?.id === node.id ? null : node));
    },
    []
  );

  const handleBackgroundClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  const handleZoomIn = () => fgRef.current?.zoom(fgRef.current.zoom() * 1.4, 300);
  const handleZoomOut = () => fgRef.current?.zoom(fgRef.current.zoom() / 1.4, 300);
  const handleFitView = () => fgRef.current?.zoomToFit(400, 40);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  if (!graphData.nodes.length) {
    return (
      <Box textAlign="center" color="text-status-inactive" padding="xl">
        No graph data available to visualize.
      </Box>
    );
  }

  return (
    <SpaceBetween size="m">
      {/* Toolbar */}
      <Box>
        <SpaceBetween direction="horizontal" size="s">
          <Button iconName="add-plus" variant="icon" onClick={handleZoomIn} />
          <Button iconName="subtract-minus" variant="icon" onClick={handleZoomOut} />
          <Button iconName="expand" variant="icon" onClick={handleFitView} />
          <Toggle
            checked={showLabels}
            onChange={({ detail }) => setShowLabels(detail.checked)}
          >
            Labels
          </Toggle>
          {allRelTypes.length > 0 && (
            <Box style={{ minWidth: 260 }}>
              <Multiselect
                selectedOptions={activeRelTypes}
                onChange={({ detail }) => setActiveRelTypes(detail.selectedOptions)}
                options={allRelTypes}
                placeholder="Filter relationships…"
                deselectAriaLabel={(o) => `Remove ${o.label}`}
              />
            </Box>
          )}
          {selectedNode && (
            <Button
              variant="link"
              onClick={() => setSelectedNode(null)}
            >
              Clear selection
            </Button>
          )}
        </SpaceBetween>
      </Box>

      {/* Canvas + Details side-by-side */}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        {/* Graph canvas */}
        <div
          ref={containerRef}
          style={{
            flex: 1,
            border: '1px solid #d1d5db',
            borderRadius: 8,
            overflow: 'hidden',
            background: '#f9fafb',
            cursor: 'grab',
            minWidth: 0,
          }}
        >
          <ForceGraph2D
            ref={fgRef}
            graphData={filteredGraph}
            width={canvasWidth || undefined}
            height={height}
            nodeCanvasObject={paintNode}
            nodeCanvasObjectMode={() => 'replace'}
            linkColor={getLinkColor}
            linkWidth={getLinkWidth}
            linkCanvasObjectMode={() => 'after'}
            linkCanvasObject={paintLink}
            linkDirectionalArrowLength={5}
            linkDirectionalArrowRelPos={1}
            onNodeClick={handleNodeClick}
            onBackgroundClick={handleBackgroundClick}
            cooldownTicks={80}
            d3AlphaDecay={0.03}
            d3VelocityDecay={0.3}
          />
        </div>

        {/* Details panel — only when a node is selected */}
        {selectedNode && (
          <div
            style={{
              width: 280,
              flexShrink: 0,
              border: '1px solid #d1d5db',
              borderRadius: 8,
              padding: 16,
              background: '#ffffff',
              maxHeight: height,
              overflowY: 'auto',
            }}
          >
            <SpaceBetween size="s">
              <Box variant="h3">{selectedNode.label}</Box>

              <ColumnLayout columns={2} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Type</Box>
                  <Box>{selectedNode.type}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Connections</Box>
                  <Box>{selectedNode.degree}</Box>
                </div>
              </ColumnLayout>

              {selectedNode.description && selectedNode.description !== '-' && (
                <div>
                  <Box variant="awsui-key-label">Description</Box>
                  <Box color="text-body-secondary">{selectedNode.description}</Box>
                </div>
              )}

              {nodeRelationships.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">Relationships</Box>
                  <SpaceBetween size="xxs">
                    {nodeRelationships.map((r, i) => (
                      <Box key={i} fontSize="body-s">
                        <Badge color={r.direction === 'out' ? 'green' : 'blue'}>
                          {r.direction === 'out' ? '→' : '←'}
                        </Badge>
                        {' '}<strong>{r.label}</strong>{' '}
                        {r.direction === 'out' ? `→ ${r.other}` : `← ${r.other}`}
                      </Box>
                    ))}
                  </SpaceBetween>
                </div>
              )}

              {nodeProperties.length > 0 && (
                <div>
                  <Box variant="awsui-key-label">Properties ({nodeProperties.length})</Box>
                  <SpaceBetween size="xxs">
                    {nodeProperties.map((p, i) => (
                      <Box key={i} fontSize="body-s">
                        <strong>{p.name}</strong>
                        {' '}<Badge>{p.dataType || 'string'}</Badge>
                        {p.mapsToColumn && (
                          <Box color="text-status-inactive" fontSize="body-s">
                            ↦ {p.mapsToColumn}
                          </Box>
                        )}
                      </Box>
                    ))}
                  </SpaceBetween>
                </div>
              )}
            </SpaceBetween>
          </div>
        )}
      </div>

      {/* Legend */}
      <Box color="text-status-inactive" fontSize="body-s">
        Darker blue = more relationships. Grey dashed nodes are referenced entities whose ontology has not been generated yet.
        Click a node to highlight its connections. Scroll to zoom, drag to pan.
      </Box>
    </SpaceBetween>
  );
}
