/**
 * Breadcrumb trails for the authenticated shell.
 *
 * Extracted from AppShell so it can be unit-tested without rendering the whole shell
 * (which hardcodes BrowserRouter and pulls in Amplify auth, the Cloudscape chrome and
 * every lazily-loaded route page). The mapping is a pure function of the pathname —
 * it reads no feature flags and no component state.
 */

/**
 * Static pathname → trail mapping. Keys are the *parent* paths of the wizard steps,
 * which is why routes carrying an :id param need the fallback in getBreadcrumbs.
 */
const PATH_MAP = {
  "/": [{ text: "Home", href: "/" }],

  // Admin Flow
  "/admin": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
  ],
  "/admin/describe-intent": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    { text: "Describe Intent", href: "/admin/describe-intent" },
  ],
  "/admin/select-datasources": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    { text: "Select Data Sources", href: "/admin/select-datasources" },
  ],
  "/admin/review-metadata": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    { text: "Review Metadata", href: "/admin/review-metadata" },
  ],
  "/admin/select-semantic-layer-type": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    {
      text: "Select Semantic Layer Type",
      href: "/admin/select-semantic-layer-type",
    },
  ],
  "/admin/build-semantic-metadata": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    {
      text: "Build Semantic Metadata",
      href: "/admin/build-semantic-metadata",
    },
  ],
  "/admin/view-semantic-metadata": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    {
      text: "View Semantic RAG Metadata",
      href: "/admin/view-semantic-metadata",
    },
  ],
  "/admin/build-graph": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    { text: "Build Knowledge Graph", href: "/admin/build-graph" },
  ],
  "/admin/view-graph": [
    { text: "Home", href: "/" },
    { text: "Admin Dashboard", href: "/admin" },
    { text: "View Knowledge Graph", href: "/admin/view-graph" },
  ],

  "/query/ask": [
    { text: "Home", href: "/" },
    { text: "Ask Question", href: "/query/ask" },
  ],

  // Settings
  "/settings": [
    { text: "Home", href: "/" },
    { text: "Settings", href: "/settings" },
  ],
};

/**
 * Resolve the breadcrumb trail for a pathname.
 *
 * Three routes carry an :id param — /admin/select-semantic-layer-type/:id,
 * /admin/build-semantic-metadata/:id and /admin/view-semantic-metadata/:id — so their
 * pathnames never match a static key and they used to fall through to a bare "Home".
 * Stripping one trailing segment and retrying gives them the same full trail as their
 * static counterparts (e.g. /admin/view-graph).
 *
 * @param {string} pathname - location.pathname from react-router.
 * @returns {Array<{text: string, href: string}>} the trail, at minimum a single Home item.
 */
export function getBreadcrumbs(pathname) {
  if (PATH_MAP[pathname]) {
    return PATH_MAP[pathname];
  }
  const parentPath = String(pathname || "").replace(/\/[^/]+$/, "");
  return PATH_MAP[parentPath] || [{ text: "Home", href: "/" }];
}

export default getBreadcrumbs;
