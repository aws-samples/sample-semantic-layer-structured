/**
 * AppShell — the authenticated application shell.
 *
 * Everything here (react-router, the Cloudscape AppLayout/SideNavigation/
 * TopNavigation chrome, all route pages, branding, feature flags) is only ever
 * rendered once the user is authenticated. App.js loads this module lazily, so
 * the unauthenticated sign-in path never downloads react-router (~380 KB) or
 * the heavy navigation chrome — they stay out of the boot bundle entirely and
 * are fetched as one async chunk after (or concurrently with) authentication.
 */
import React, { useState, useEffect, lazy, Suspense } from "react";
import {
  BrowserRouter as Router,
  Routes,
  Route,
  Navigate,
  useNavigate,
  useLocation,
} from "react-router-dom";
import {
  AppLayout,
  TopNavigation,
  SideNavigation,
  Flashbar,
  BreadcrumbGroup,
  Button,
  Modal,
  Box,
  SpaceBetween,
  Spinner,
} from "@cloudscape-design/components";

import { useNotifications } from "./hooks/useNotifications";
import useChatSessions from "./hooks/useChatSessions";
import { useAuthz } from "./hooks/useAuthz";
import { getBreadcrumbs } from "./utils/breadcrumbs";

// Route pages are code-split with React.lazy so each route ships its own webpack
// chunk instead of one monolithic bundle. A visitor to /query/ask never
// downloads the force-graph, recharts, or markdown code that only the admin/view
// pages pull in — the browser fetches a route's chunk on demand and caches it.

// Admin Flow Pages
const AdminDashboard = lazy(() => import("./pages/admin/AdminDashboard"));
const DescribeIntent = lazy(() => import("./pages/admin/DescribeIntent"));
const SelectDataSources = lazy(() => import("./pages/admin/SelectDataSources"));
const ReviewMetadata = lazy(() => import("./pages/admin/ReviewMetadata"));
const SelectSemanticLayerType = lazy(
  () => import("./pages/admin/SelectSemanticLayerType"),
);
const BuildSemanticMetadata = lazy(
  () => import("./pages/admin/BuildSemanticMetadata"),
);
const ViewSemanticRAGMetadata = lazy(
  () => import("./pages/admin/ViewSemanticRAGMetadata"),
);
const BuildKnowledgeGraph = lazy(
  () => import("./pages/admin/BuildKnowledgeGraph"),
);
const ViewKnowledgeGraph = lazy(
  () => import("./pages/admin/ViewKnowledgeGraph"),
);
const GroundTruthDataset = lazy(
  () => import("./pages/admin/GroundTruthDataset"),
);
const Evaluations = lazy(() => import("./pages/admin/Evaluations"));

// End User Flow Pages — chat-first redesign (2026-05-24).
const AskQuestion = lazy(() => import("./pages/query/AskQuestion"));

// Settings
const Settings = lazy(() => import("./pages/Settings"));

const ENABLE_ONTOLOGY_AGENTS =
  process.env.REACT_APP_ENABLE_ONTOLOGY_AGENTS !== "false";
const ENABLE_SEMANTIC_RAG =
  process.env.REACT_APP_ENABLE_SEMANTIC_RAG === "true";
// Mirrors the `enableHybrid` CDK context flag (default true), which is what
// actually deploys the semantic-layer-query / semantic-layer-creation runtimes.
// Baked in at build time, so flipping the CDK flag only reaches the UI after the
// frontend CodeBuild project re-runs (see README.md).
const ENABLE_HYBRID = process.env.REACT_APP_ENABLE_HYBRID === "true";

// Customer branding from environment variables
const CUSTOMER_NAME =
  process.env.REACT_APP_CUSTOMER_NAME || "AWS Semantic Layer";
const CUSTOMER_LOGO = process.env.REACT_APP_CUSTOMER_LOGO || "/amazicon.svg";

function AppContent({ signOut, user }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { notifications, addNotification, removeNotification } =
    useNotifications();
  const [activeNavItem, setActiveNavItem] = useState("/");
  const {
    sessions: chatSessions,
    archive: archiveChatSession,
    archiveAll: archiveAllChatSessions,
  } = useChatSessions();
  // Whether to render the semantic-layer admin surfaces at all. Fails OPEN:
  // true while the caller's roles are unknown and whenever authorization
  // enforcement is switched off, because this is decluttering and the REST API
  // is the actual enforcement point. See hooks/useAuthz.js.
  const { isAdmin } = useAuthz();
  // "Clear all" confirm modal for the Session History section.
  const [clearAllOpen, setClearAllOpen] = useState(false);
  const [clearingAll, setClearingAll] = useState(false);

  useEffect(() => {
    setActiveNavItem(location.pathname);
  }, [location]);

  // Highlight the "Ask Question" branch when a chat session is open. Without
  // this the SideNavigation thinks /query/ask?session=X is an unknown route
  // and clears the section's active styling.
  const activeChatSessionId = (() => {
    if (location.pathname !== "/query/ask") return null;
    const params = new URLSearchParams(location.search);
    return params.get("session");
  })();
  const askActiveHref = activeChatSessionId
    ? `/query/ask?session=${activeChatSessionId}`
    : activeNavItem;

  const breadcrumbs = getBreadcrumbs(location.pathname);

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <TopNavigation
        identity={{
          href: "/",
          title: CUSTOMER_NAME,
          logo: {
            src: CUSTOMER_LOGO,
            alt: `${CUSTOMER_NAME} Logo`,
          },
        }}
        utilities={[
          {
            type: "menu-dropdown",
            text: user?.email || user?.username || "User",
            description: user?.email || "Authenticated User",
            iconName: "user-profile",
            items: [
              {
                id: "signout",
                text: "Sign out",
              },
            ],
            onItemClick: ({ detail }) => {
              if (detail.id === "signout" && signOut) {
                signOut();
              }
            },
          },
        ]}
      />

      <AppLayout
        navigation={
          <SideNavigation
            activeHref={askActiveHref}
            header={{ text: "Navigation", href: "/" }}
            onFollow={(event) => {
              if (event.detail.external) return;
              event.preventDefault();
              // Anchor-only hrefs (e.g. the "Session History" link-group header
              // "#session-history") are non-navigational headers — their action
              // lives in the hover `info` icon, so don't route on them.
              if (event.detail.href?.startsWith("#")) return;
              // "+ New chat" — clear any session search-param so AskQuestion
              // renders the LandingPage. Routing to /query/ask alone does
              // the same thing because the search param is read locally.
              navigate(event.detail.href);
            }}
            items={[
              { type: "link", text: "Home", href: "/" },
              // Hidden from callers who hold no `manageOntology` anywhere: for
              // them every page behind it 403s. Spread rather than a ternary
              // returning null so no empty slot reaches SideNavigation.
              ...(isAdmin
                ? [
                    {
                      type: "link",
                      text: "Semantic Metadata",
                      href: "/admin",
                    },
                  ]
                : []),
              { type: "link", text: "+ New chat", href: "/query/ask" },
              ...(chatSessions.length > 0
                ? [
                    {
                      type: "divider",
                    },
                    {
                      // link-group (not section) so the "Session History"
                      // header can host an `info` slot — a trash icon that is
                      // hidden until the header is hovered (the "Clear all"
                      // affordance, icon-only, on hover, shown once). href="#"
                      // is a no-op; onFollow below preventDefaults all in-app nav.
                      type: "link-group",
                      text: "Session History",
                      href: "#session-history",
                      info: (
                        <span className="chat-clear-all-btn">
                          <Button
                            iconName="remove"
                            variant="icon"
                            ariaLabel="Clear all chat history"
                            onClick={(e) => {
                              const native = e?.detail?.event || e;
                              native?.preventDefault?.();
                              native?.stopPropagation?.();
                              setClearAllOpen(true);
                            }}
                          />
                        </span>
                      ),
                      items: [
                        ...chatSessions.map((s) => ({
                          type: "link",
                          text: s.title || "Untitled",
                          href: `/query/ask?session=${s.sessionId}`,
                          // Show which semantic layer (name + id + version) this
                          // chat queried, so two sessions on same-named layers are
                          // distinguishable and the layer id is visible at a glance.
                          description: s.ontologyId
                            ? `${s.ontologyName || s.ontologyId}${
                                s.ontologyVersion
                                  ? ` (${s.ontologyVersion})`
                                  : ""
                              } · ${s.ontologyId}`
                            : undefined,
                          info: (
                            <span className="chat-archive-btn">
                              <Button
                                iconName="remove"
                                variant="icon"
                                ariaLabel={`Archive chat ${s.title || s.sessionId}`}
                                onClick={(e) => {
                                  const native = e?.detail?.event || e;
                                  native?.preventDefault?.();
                                  native?.stopPropagation?.();
                                  archiveChatSession(s.sessionId);
                                  if (activeChatSessionId === s.sessionId) {
                                    navigate("/query/ask");
                                  }
                                }}
                              />
                            </span>
                          ),
                        })),
                      ],
                    },
                  ]
                : []),
              { type: "divider" },
              { type: "link", text: "Settings", href: "/settings" },
            ]}
          />
        }
        breadcrumbs={
          <BreadcrumbGroup
            items={breadcrumbs}
            onFollow={(event) => {
              event.preventDefault();
              navigate(event.detail.href);
            }}
          />
        }
        notifications={<Flashbar items={notifications} />}
        content={
          // Suspense fallback shown briefly while a route's lazy chunk loads.
          // Chunks are tiny and browser-cached after first visit, so this is
          // near-invisible in practice.
          <Suspense
            fallback={
              <Box textAlign="center" padding="xxl">
                <Spinner size="large" />
              </Box>
            }
          >
            <Routes>
              {/* Default route - redirect to Natural Language Query */}
              <Route path="/" element={<Navigate to="/query/ask" replace />} />

              {/* Admin Flow Routes.
                  Guarded as one block rather than per route: a typed or
                  bookmarked /admin URL must not land a read-only user on a page
                  that renders and then fails every call. Non-admins get the
                  splat redirect instead — declared in the `else` branch because
                  react-router ranks a static segment above `*`, so leaving the
                  specific routes mounted would shadow it.
                  `isAdmin` is true while roles are unknown (fail open), so this
                  never locks out an admin over a transient fetch failure. */}
              {isAdmin ? (
                <>
                  <Route
                    path="/admin"
                    element={<AdminDashboard user={user} />}
                  />
                  <Route
                    path="/admin/describe-intent"
                    element={<DescribeIntent user={user} />}
                  />
                  <Route
                    path="/admin/select-datasources"
                    element={<SelectDataSources user={user} />}
                  />
                  <Route
                    path="/admin/review-metadata"
                    element={<ReviewMetadata user={user} />}
                  />
                  <Route
                    path="/admin/select-semantic-layer-type/:id"
                    element={
                      <SelectSemanticLayerType
                        enableOntologyAgents={ENABLE_ONTOLOGY_AGENTS}
                        enableSemanticRag={ENABLE_SEMANTIC_RAG}
                        enableHybrid={ENABLE_HYBRID}
                      />
                    }
                  />
                  <Route
                    path="/admin/build-semantic-metadata/:id"
                    element={
                      ENABLE_SEMANTIC_RAG ? (
                        <BuildSemanticMetadata />
                      ) : (
                        <Navigate to="/admin" replace />
                      )
                    }
                  />
                  <Route
                    path="/admin/view-semantic-metadata/:id"
                    element={
                      ENABLE_SEMANTIC_RAG ? (
                        // `user` reaches the Governed Rules tab on this page: a
                        // Hybrid layer lands here, and every rule/threshold
                        // approval records the reviewer's identity.
                        <ViewSemanticRAGMetadata user={user} />
                      ) : (
                        <Navigate to="/admin" replace />
                      )
                    }
                  />
                  <Route
                    path="/admin/build-graph"
                    element={<BuildKnowledgeGraph user={user} />}
                  />
                  <Route
                    path="/admin/view-graph"
                    element={<ViewKnowledgeGraph user={user} />}
                  />
                  <Route
                    path="/admin/ground-truth"
                    element={<GroundTruthDataset />}
                  />
                  <Route path="/admin/evaluations" element={<Evaluations />} />
                </>
              ) : (
                <Route
                  path="/admin/*"
                  element={<Navigate to="/query/ask" replace />}
                />
              )}

              {/* Query Flow Routes */}
              <Route
                path="/query"
                element={<Navigate to="/query/ask" replace />}
              />
              <Route
                path="/query/ask"
                element={
                  <AskQuestion
                    enableSemanticRag={ENABLE_SEMANTIC_RAG}
                    enableHybrid={ENABLE_HYBRID}
                  />
                }
              />
              {/* Settings */}
              <Route path="/settings" element={<Settings user={user} />} />
            </Routes>
          </Suspense>
        }
        toolsHide
      />

      <Modal
        visible={clearAllOpen}
        onDismiss={() => setClearAllOpen(false)}
        header="Delete all chat history"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                disabled={clearingAll}
                onClick={() => setClearAllOpen(false)}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={clearingAll}
                onClick={async () => {
                  setClearingAll(true);
                  try {
                    await archiveAllChatSessions();
                    setClearAllOpen(false);
                    // If the open chat was one of the cleared sessions, drop
                    // back to a fresh chat.
                    if (activeChatSessionId) {
                      navigate("/query/ask");
                    }
                  } finally {
                    setClearingAll(false);
                  }
                }}
              >
                Delete all
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Delete all {chatSessions.length} chat
        {chatSessions.length === 1 ? "" : "s"} from your history? This can't be
        undone.
      </Modal>
    </div>
  );
}

/**
 * The routed application shell wrapped in its BrowserRouter. `signOut` is
 * undefined in NO_AUTH mode (no sign-out affordance needed for local dev).
 */
export default function AppShell({ signOut, user }) {
  return (
    <Router
      future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}
    >
      <AppContent signOut={signOut} user={user} />
    </Router>
  );
}
