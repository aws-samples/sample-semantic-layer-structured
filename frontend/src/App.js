import React, { useState, useEffect } from "react";
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
} from "@cloudscape-design/components";
import "./App.css";

// Admin Flow Pages
import AdminDashboard from "./pages/admin/AdminDashboard";
import DescribeIntent from "./pages/admin/DescribeIntent";
import SelectDataSources from "./pages/admin/SelectDataSources";
import ReviewMetadata from "./pages/admin/ReviewMetadata";
import SelectSemanticLayerType from "./pages/admin/SelectSemanticLayerType";
import BuildSemanticMetadata from "./pages/admin/BuildSemanticMetadata";
import ViewSemanticRAGMetadata from "./pages/admin/ViewSemanticRAGMetadata";
import BuildKnowledgeGraph from "./pages/admin/BuildKnowledgeGraph";
import ViewKnowledgeGraph from "./pages/admin/ViewKnowledgeGraph";
import GroundTruthDataset from "./pages/admin/GroundTruthDataset";
import Evaluations from "./pages/admin/Evaluations";

// End User Flow Pages — chat-first redesign (2026-05-24).
import AskQuestion from "./pages/query/AskQuestion";

// Settings
import Settings from "./pages/Settings";

// Hooks
import { useNotifications } from "./hooks/useNotifications";
import useChatSessions from "./hooks/useChatSessions";

// Authentication
import { CustomSignIn } from "./pages/CustomAuthComponents";

import { Amplify } from "aws-amplify";
import { Hub } from "aws-amplify/utils";
import amplifyConfig from "./config/amplify-config";

Amplify.configure(amplifyConfig);

// Check if NO_AUTH mode is enabled
const NO_AUTH = process.env.REACT_APP_NO_AUTH === "true";

const ENABLE_ONTOLOGY_AGENTS =
  process.env.REACT_APP_ENABLE_ONTOLOGY_AGENTS !== "false";
const ENABLE_SEMANTIC_RAG =
  process.env.REACT_APP_ENABLE_SEMANTIC_RAG === "true";

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

  const getBreadcrumbs = () => {
    const pathMap = {
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

    return pathMap[location.pathname] || [{ text: "Home", href: "/" }];
  };

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
              { type: "link", text: "Semantic Metadata", href: "/admin" },
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
            items={getBreadcrumbs()}
            onFollow={(event) => {
              event.preventDefault();
              navigate(event.detail.href);
            }}
          />
        }
        notifications={<Flashbar items={notifications} />}
        content={
          <Routes>
            {/* Default route - redirect to Natural Language Query */}
            <Route path="/" element={<Navigate to="/query/ask" replace />} />

            {/* Admin Flow Routes */}
            <Route path="/admin" element={<AdminDashboard user={user} />} />
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
                  <ViewSemanticRAGMetadata />
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

            {/* Query Flow Routes */}
            <Route
              path="/query"
              element={<Navigate to="/query/ask" replace />}
            />
            <Route
              path="/query/ask"
              element={<AskQuestion enableSemanticRag={ENABLE_SEMANTIC_RAG} />}
            />
            {/* Settings */}
            <Route path="/settings" element={<Settings user={user} />} />
          </Routes>
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

function App() {
  if (NO_AUTH) {
    console.warn(
      "⚠️ NO_AUTH mode is enabled - Authentication is BYPASSED for local development",
    );
    return (
      <Router
        future={{
          v7_startTransition: true,
          v7_relativeSplatPath: true,
        }}
      >
        <AppContent user={{ username: "Dev User", email: "dev@local" }} />
      </Router>
    );
  }

  return <CustomAuthenticatedApp />;
}

function CustomAuthenticatedApp() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    checkAuthStatus();
  }, []);

  const checkAuthStatus = async () => {
    try {
      const { fetchAuthSession, getCurrentUser } =
        await import("aws-amplify/auth");

      const session = await fetchAuthSession();

      if (session?.tokens) {
        setIsAuthenticated(true);

        const idToken = session.tokens.idToken?.toString();
        if (idToken) {
          localStorage.setItem("authToken", idToken);
        }

        // The AgentCore chat Gateway's CUSTOM_JWT authorizer validates
        // `allowedClients` against the token's `client_id` claim. Only the
        // Cognito ACCESS token carries `client_id` (the ID token uses `aud`),
        // so the gateway requires the access token — stored under a separate
        // key so the REST API keeps using the ID token.
        const accessToken = session.tokens.accessToken?.toString();
        if (accessToken) {
          localStorage.setItem("chatGatewayToken", accessToken);
        }

        try {
          const authUser = await getCurrentUser();

          if (idToken) {
            try {
              const payload = JSON.parse(atob(idToken.split(".")[1]));

              const email = payload.email || null;
              const username =
                authUser.username ||
                authUser.signInDetails?.loginId ||
                payload["cognito:username"] ||
                "User";
              const sub = payload.sub || null;

              setUser({
                email,
                username,
                sub,
              });
            } catch (jwtError) {
              setUser({
                username: authUser.username || authUser.signInDetails?.loginId,
              });
            }
          } else {
            setUser({
              username: authUser.username || authUser.signInDetails?.loginId,
            });
          }
        } catch (userError) {
          try {
            if (idToken) {
              const payload = JSON.parse(atob(idToken.split(".")[1]));

              const email = payload.email || null;
              const username =
                payload.preferred_username ||
                payload["cognito:username"] ||
                "User";
              const sub = payload.sub || null;

              setUser({
                email,
                username,
                sub,
              });
            } else {
              setUser({ username: "User" });
            }
          } catch (jwtError) {
            setUser({ username: "User" });
          }
        }
      } else {
        setIsAuthenticated(false);
      }
    } catch (error) {
      console.error("Auth error:", error);
      setIsAuthenticated(false);
    } finally {
      setLoading(false);
    }
  };

  const handleSignOut = async () => {
    try {
      const { signOut } = await import("aws-amplify/auth");
      await signOut();
      // Session-binding lifecycle (clear-local policy): drop every locally
      // cached credential so the next sign-in mints a fresh session and never
      // reuses another (or a stale) principal's token. Chat transcripts are
      // intentionally KEPT server-side — a returning user still sees prior
      // chats. The active sessionId lives only in the URL/React state, which
      // resets when the authed app unmounts on isAuthenticated=false below.
      localStorage.removeItem("authToken");
      localStorage.removeItem("chatGatewayToken");
      setIsAuthenticated(false);
      setUser(null);
    } catch (error) {
      console.error("Sign out error:", error);
    }
  };

  // Sign out on expired token — triggered by axios 401 interceptor or Amplify Hub
  useEffect(() => {
    const onAuthExpired = () => handleSignOut();
    window.addEventListener("auth-expired", onAuthExpired);

    const hubUnsub = Hub.listen("auth", ({ payload }) => {
      if (payload.event === "tokenRefresh_failure") {
        handleSignOut();
      }
    });

    return () => {
      window.removeEventListener("auth-expired", onAuthExpired);
      hubUnsub();
    };
  }, []);

  if (loading) {
    return (
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          height: "100vh",
        }}
      >
        <div style={{ textAlign: "center" }}>
          <h2>Loading...</h2>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <CustomSignIn />;
  }

  return (
    <Router
      future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}
    >
      <AppContent signOut={handleSignOut} user={user} />
    </Router>
  );
}

export default App;
