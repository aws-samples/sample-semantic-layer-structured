/**
 * Download helpers — trigger a browser file download from in-memory data.
 *
 * Shared by ReasoningPanel (SQL/KB result CSV + JSON, VKG Turtle slice) and
 * GraphTraversalPanel (n-quads JSON) so the blob-and-anchor boilerplate and
 * CSV escaping live in one place.
 */

// Build a CSV string from columns/rows (shared by the download + copy controls).
export function csvStringFromColumnsRows(columns, rows) {
  const escape = (v) => {
    if (v == null) return "";
    const s = typeof v === "string" ? v : JSON.stringify(v);
    if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const header = columns.join(",");
  const body = rows.map((r) => r.map(escape).join(",")).join("\n");
  return `${header}\n${body}`;
}

// Trigger a download of the given blob content under `filename`.
function downloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function downloadCsvFromColumnsRows(columns, rows, filename) {
  downloadBlob(
    csvStringFromColumnsRows(columns, rows),
    filename,
    "text/csv;charset=utf-8",
  );
}

export function downloadJson(data, filename) {
  downloadBlob(
    JSON.stringify(data, null, 2),
    filename,
    "application/json;charset=utf-8",
  );
}

// Plain-text download — used for the VKG ontology slice, which is Turtle/RDF
// (a .ttl text document), not JSON.
export function downloadText(
  text,
  filename,
  mime = "text/turtle;charset=utf-8",
) {
  downloadBlob(text, filename, mime);
}
