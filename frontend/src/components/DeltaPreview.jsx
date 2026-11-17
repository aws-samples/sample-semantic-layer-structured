import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";

/**
 * Bucket → row metadata. Order matters: the FIRST bucket a table appears in
 * (scanning this list top-to-bottom) wins the fallback reason/status when
 * plan.perTable has no explicit reason. "removed" is listed first so a dropped
 * table is always flagged as an error rather than a softer status.
 *
 * Each entry:
 *   key          — the plan.<bucket> array name
 *   reason       — fallback reason text when perTable[table].reason is absent
 *   statusType   — Cloudscape StatusIndicator type for the Status column
 */
const BUCKETS = [
  { key: "removed", reason: "removed", statusType: "error" },
  { key: "schemaChanged", reason: "schema", statusType: "warning" },
  { key: "dataChanged", reason: "data", statusType: "warning" },
  { key: "added", reason: "added", statusType: "warning" },
  { key: "fkNeighbors", reason: "fk-neighbor", statusType: "info" },
  { key: "unchanged", reason: "unchanged", statusType: "success" },
];

/**
 * Build the deduplicated diff rows from a delta plan.
 *
 * Walks every bucket in BUCKETS order and records each table the first time it
 * is seen, so a table that appears in multiple buckets is attributed to its
 * highest-priority bucket. The explicit plan.perTable[table] entry (if any)
 * overrides the bucket fallback for reason/detail.
 *
 * @param {object} plan - the resolved delta plan (bucket arrays + perTable map).
 * @returns {Array<{table: string, reason: string, detail: string, statusType: string}>}
 *   one row per unique table name.
 */
function buildRows(plan) {
  const perTable = plan.perTable ?? {};
  const seen = new Set();
  const rows = [];
  BUCKETS.forEach((bucket) => {
    const tables = plan[bucket.key] ?? [];
    tables.forEach((table) => {
      // Dedup: a table already attributed to a higher-priority bucket is skipped.
      if (seen.has(table)) return;
      seen.add(table);
      const meta = perTable[table] ?? {};
      rows.push({
        table,
        reason: meta.reason ?? bucket.reason,
        detail: meta.detail ?? "",
        statusType: bucket.statusType,
      });
    });
  });
  return rows;
}

/**
 * Reusable human-in-the-loop diff preview for incremental delta ingestion.
 *
 * The admin clicks "Check for updates", the component calls onCompute() to get
 * a delta plan, then renders which tables changed / were added / removed and
 * why. The admin then triggers a scoped rebuild ("Rebuild changed") or a full
 * rebuild ("Full rebuild"). The component is prop-driven so both the
 * Semantic-RAG and VKG View pages can reuse it with different bindings.
 *
 * @param {object} props
 * @param {() => Promise<object>} props.onCompute - resolves to the delta plan
 *   object directly (parent unwraps any {success, data} envelope first).
 * @param {(scope: string, plan: object) => void} props.onRebuild - invoked with
 *   "delta" or "full" plus the current plan when a rebuild button is clicked.
 * @param {number} props.totalTables - table count for the "Rebuild N of M" footer.
 * @returns {JSX.Element} the diff-preview UI.
 */
export default function DeltaPreview({ onCompute, onRebuild, totalTables }) {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  /**
   * Run onCompute and store the resolved plan. Always clears loading in the
   * finally block so a rejected onCompute never leaves the button spinning.
   *
   * @returns {Promise<void>}
   */
  const handleCheck = async () => {
    setError(null);
    setLoading(true);
    try {
      const p = await onCompute();
      setPlan(p);
    } catch (e) {
      // Fail loudly: surface the message rather than silently swallowing it.
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  const rebuildCount = plan?.rebuildSet?.length ?? 0;
  const removedCount = plan?.removed?.length ?? 0;
  // "No changes" only when there is nothing to rebuild AND nothing to remove.
  const noChanges = plan !== null && rebuildCount === 0 && removedCount === 0;
  const rows = plan ? buildRows(plan) : [];

  return (
    <SpaceBetween size="l">
      <Header
        variant="h3"
        description="Check the source for changes since the last build, review the diff, then rebuild only what changed or rebuild everything."
        actions={
          <Button variant="primary" loading={loading} onClick={handleCheck}>
            Check for updates
          </Button>
        }
      >
        Incremental updates
      </Header>

      {error && <Alert type="error">{error}</Alert>}

      {plan && (
        <>
          {noChanges ? (
            <StatusIndicator type="success">
              No changes detected since the last build.
            </StatusIndicator>
          ) : (
            <Table
              variant="embedded"
              items={rows}
              columnDefinitions={[
                {
                  id: "table",
                  header: "Table",
                  // Plain text so tests can find the table name by text.
                  cell: (row) => row.table,
                },
                {
                  id: "status",
                  header: "Status",
                  cell: (row) => (
                    <StatusIndicator type={row.statusType}>
                      {row.reason}
                    </StatusIndicator>
                  ),
                  width: 160,
                },
                {
                  id: "detail",
                  header: "Detail",
                  cell: (row) => row.detail || "-",
                },
              ]}
              empty={
                <Box textAlign="center" color="text-status-inactive">
                  No data
                </Box>
              }
            />
          )}

          <SpaceBetween direction="horizontal" size="xs">
            <Box
              variant="p"
              color="text-body-secondary"
              padding={{ top: "xxs" }}
            >
              Rebuild {rebuildCount} of {totalTables} tables
            </Box>
            <Button
              variant="primary"
              disabled={rebuildCount === 0}
              onClick={() => onRebuild("delta", plan)}
            >
              Rebuild changed
            </Button>
            <Button onClick={() => onRebuild("full", plan)}>
              Full rebuild
            </Button>
          </SpaceBetween>
        </>
      )}
    </SpaceBetween>
  );
}
