/**
 * Minimal static file server with SPA fallback, used only for perf measurement.
 *
 * Serves the CRA production build directory. Any request that does not map to a
 * real file falls back to index.html so client-side routes (e.g. /admin) resolve
 * to the SPA shell — mirroring how CloudFront serves this app in production.
 *
 * Zero dependencies (Node core http/fs) so it runs against the project's frozen
 * lockfile without installing anything.
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

const BUILD_DIR = process.argv[2];
const PORT = Number(process.argv[3] || 8099);

if (!BUILD_DIR || !fs.existsSync(BUILD_DIR)) {
  console.error(`build dir not found: ${BUILD_DIR}`);
  process.exit(1);
}

// Map file extension -> content type. Only the types the build actually emits.
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".png": "image/png",
  ".map": "application/json; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

/**
 * Resolve a URL path to an on-disk file inside BUILD_DIR, guarding against
 * path traversal. Returns null when the resolved path escapes the build dir.
 */
function resolveSafe(urlPath) {
  const clean = decodeURIComponent(urlPath.split("?")[0]);
  const resolved = path.normalize(path.join(BUILD_DIR, clean));
  if (!resolved.startsWith(BUILD_DIR)) return null;
  return resolved;
}

const server = http.createServer((req, res) => {
  let filePath = resolveSafe(req.url);
  if (!filePath) {
    res.writeHead(403);
    res.end("forbidden");
    return;
  }

  // Directory or missing file -> SPA fallback to index.html.
  let exists = fs.existsSync(filePath) && fs.statSync(filePath).isFile();
  if (!exists) {
    filePath = path.join(BUILD_DIR, "index.html");
  }

  const ext = path.extname(filePath);
  const body = fs.readFileSync(filePath);
  res.writeHead(200, {
    "Content-Type": MIME[ext] || "application/octet-stream",
    // No caching, so every measured navigation is a true cold load.
    "Cache-Control": "no-store",
  });
  res.end(body);
});

server.listen(PORT, () => {
  console.log(
    `perf-server listening on http://127.0.0.1:${PORT} (root: ${BUILD_DIR})`,
  );
});
