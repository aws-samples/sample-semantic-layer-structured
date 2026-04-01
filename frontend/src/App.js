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

// End User Flow Pages
import NaturalLanguageQuery from "./pages/query/NaturalLanguageQuery";

// Settings
import Settings from "./pages/Settings";

// Hooks
import { useNotifications } from "./hooks/useNotifications";

// Authentication
import { CustomSignIn } from "./pages/CustomAuthComponents";

import { Amplify } from "aws-amplify";
import { Hub } from "aws-amplify/utils";
import amplifyConfig from "./config/amplify-config";

Amplify.configure(amplifyConfig);

// Check if NO_AUTH mode is enabled
const NO_AUTH = process.env.REACT_APP_NO_AUTH === "true";

const ENABLE_ONTOLOGY_AGENTS = process.env.REACT_APP_ENABLE_ONTOLOGY_AGENTS !== "false";

// Customer branding from environment variables
const CUSTOMER_NAME = process.env.REACT_APP_CUSTOMER_NAME || "AWS Semantic Layer";
const CUSTOMER_LOGO = process.env.REACT_APP_CUSTOMER_LOGO || "/amazicon.svg";

function AppContent({ signOut, user }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { notifications, addNotification, removeNotification } = useNotifications();
  const [activeNavItem, setActiveNavItem] = useState("/");

  useEffect(() => {
    setActiveNavItem(location.pathname);
  }, [location]);

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
        { text: "Select Semantic Layer Type", href: "/admin/select-semantic-layer-type" },
      ],
      "/admin/build-semantic-metadata": [
        { text: "Home", href: "/" },
        { text: "Admin Dashboard", href: "/admin" },
        { text: "Build Semantic Metadata", href: "/admin/build-semantic-metadata" },
      ],
      "/admin/view-semantic-metadata": [
        { text: "Home", href: "/" },
        { text: "Admin Dashboard", href: "/admin" },
        { text: "View Semantic RAG Metadata", href: "/admin/view-semantic-metadata" },
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
            activeHref={activeNavItem}
            header={{ text: "Navigation", href: "/" }}
            onFollow={(event) => {
              if (!event.detail.external) {
                event.preventDefault();
                navigate(event.detail.href);
              }
            }}
            items={[
              { type: "link", text: "Home", href: "/" },
              { type: "divider" },
              {
                type: "section",
                text: "Admin",
                items: [
                  { type: "link", text: "Semantic Metadata", href: "/admin" },

                ],
              },
              { type: "divider" },
              {
                type: "section",
                text: "Business User",
                items: [
                  { type: "link", text: "Ask Question", href: "/query/ask" },
                ],
              },
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
        notifications={
          <Flashbar items={notifications} />
        }
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
              element={<SelectSemanticLayerType enableOntologyAgents={ENABLE_ONTOLOGY_AGENTS} />}
            />
            <Route
              path="/admin/build-semantic-metadata/:id"
              element={<BuildSemanticMetadata />}
            />
            <Route
              path="/admin/view-semantic-metadata/:id"
              element={<ViewSemanticRAGMetadata />}
            />
            <Route
              path="/admin/build-graph"
              element={<BuildKnowledgeGraph user={user} />}
            />
            <Route
              path="/admin/view-graph"
              element={<ViewKnowledgeGraph user={user} />}
            />

            {/* Query Flow Routes */}
            <Route path="/query" element={<Navigate to="/query/ask" replace />} />
            <Route
              path="/query/ask"
              element={<NaturalLanguageQuery user={user} addNotification={addNotification} />}
            />
            {/* Settings */}
            <Route path="/settings" element={<Settings user={user} />} />
          </Routes>
        }
        toolsHide
      />
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
      const { fetchAuthSession, getCurrentUser } = await import("aws-amplify/auth");

      const session = await fetchAuthSession();

      if (session?.tokens) {
        setIsAuthenticated(true);

        const idToken = session.tokens.idToken?.toString();
        if (idToken) {
          localStorage.setItem("authToken", idToken);
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
      localStorage.removeItem("authToken");
      setIsAuthenticated(false);
      setUser(null);
    } catch (error) {
      console.error("Sign out error:", error);
    }
  };

  // Sign out on expired token — triggered by axios 401 interceptor or Amplify Hub
  useEffect(() => {
    const onAuthExpired = () => handleSignOut();
    window.addEventListener('auth-expired', onAuthExpired);

    const hubUnsub = Hub.listen('auth', ({ payload }) => {
      if (payload.event === 'tokenRefresh_failure') {
        handleSignOut();
      }
    });

    return () => {
      window.removeEventListener('auth-expired', onAuthExpired);
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
