/**
 * Breadcrumb trails, with emphasis on the :id routes.
 *
 * Three admin wizard steps are declared in AppShell as `/:id` routes, but the trail
 * mapping is keyed on their PARENT paths. So `/admin/view-semantic-metadata/abc-123`
 * matched nothing and the page rendered a bare "Home" — no way back to the dashboard —
 * while its VKG counterpart `/admin/view-graph` (a static route) showed the full trail.
 */
import { getBreadcrumbs } from "../utils/breadcrumbs";

/** The three routes AppShell declares with a trailing :id param. */
const ID_ROUTES = [
  ["/admin/select-semantic-layer-type", "Select Semantic Layer Type"],
  ["/admin/build-semantic-metadata", "Build Semantic Metadata"],
  ["/admin/view-semantic-metadata", "View Semantic RAG Metadata"],
];

describe("getBreadcrumbs", () => {
  test.each(ID_ROUTES)(
    "%s/:id gets the same full trail as its parent path",
    (parent, leaf) => {
      const trail = getBreadcrumbs(
        `${parent}/0fbb1c42-9a1e-4f0e-8f2b-2d0a5e7c1234`,
      );
      expect(trail.map((c) => c.text)).toEqual([
        "Home",
        "Admin Dashboard",
        leaf,
      ]);
      // The leaf href stays the PARENT path, not the :id one — clicking it must not
      // re-enter the same page with a stale id.
      expect(trail[trail.length - 1].href).toBe(parent);
    },
  );

  test("a static route is unaffected by the fallback", () => {
    expect(getBreadcrumbs("/admin/view-graph").map((c) => c.text)).toEqual([
      "Home",
      "Admin Dashboard",
      "View Knowledge Graph",
    ]);
    expect(getBreadcrumbs("/query/ask").map((c) => c.text)).toEqual([
      "Home",
      "Ask Question",
    ]);
    expect(getBreadcrumbs("/").map((c) => c.text)).toEqual(["Home"]);
  });

  test("an admin route with no mapping of its own falls back to the dashboard trail", () => {
    // /admin/ground-truth and /admin/evaluations have no entry in the map. The fallback
    // strips their last segment to /admin, so they now show "Home > Admin Dashboard"
    // instead of a bare "Home". That is a deliberate improvement, pinned here so it is
    // not mistaken for the fallback firing on the wrong route.
    for (const p of ["/admin/ground-truth", "/admin/evaluations"]) {
      expect(getBreadcrumbs(p).map((c) => c.text)).toEqual([
        "Home",
        "Admin Dashboard",
      ]);
    }
  });

  test("an entirely unknown path still yields a Home trail", () => {
    expect(getBreadcrumbs("/nope/nothing/here")).toEqual([
      { text: "Home", href: "/" },
    ]);
    // Only ONE segment is stripped, so a deep :id under an unmapped parent does not
    // walk all the way up to "/" and inherit the root trail by accident.
    expect(
      getBreadcrumbs("/admin/unknown-page/abc").map((c) => c.text),
    ).toEqual(["Home"]);
  });

  test("an empty or missing pathname does not throw", () => {
    // Defensive: location.pathname is always a string in practice, but a crash here
    // would blank the entire shell rather than just the breadcrumb.
    expect(getBreadcrumbs("")).toEqual([{ text: "Home", href: "/" }]);
    expect(getBreadcrumbs(undefined)).toEqual([{ text: "Home", href: "/" }]);
  });
});
