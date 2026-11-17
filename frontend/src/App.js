import React, { useState, useEffect, lazy, Suspense } from "react";
import { Box, Spinner } from "@cloudscape-design/components";
import "./App.css";

// Deferred Amplify configuration — keeps the ~400 KB aws-amplify SDK off the
// boot path (loaded lazily on the auth path instead of gating first paint).
import ensureAmplifyConfigured from "./config/ensureAmplifyConfigured";

// The entire authenticated application shell — react-router, the Cloudscape
// AppLayout/SideNavigation chrome, and every route page — lives in AppShell and
// is loaded lazily. The unauthenticated sign-in path renders none of it, so
// none of that code sits in the boot bundle. AppShell and CustomSignIn are the
// two mutually-exclusive top-level views, each its own async chunk.
const AppShell = lazy(() => import("./AppShell"));

// Sign-in shell — lazy so the aws-amplify/auth code it imports statically stays
// in an async chunk instead of the boot bundle.
const CustomSignIn = lazy(() =>
  import("./pages/CustomAuthComponents").then((m) => ({
    default: m.CustomSignIn,
  })),
);

// Check if NO_AUTH mode is enabled
const NO_AUTH = process.env.REACT_APP_NO_AUTH === "true";

/** Full-viewport centered spinner used as the Suspense fallback for top-level
 * chunks (the sign-in shell and the authenticated app shell). */
function FullPageSpinner() {
  return (
    <Box textAlign="center" padding="xxl">
      <Spinner size="large" />
    </Box>
  );
}

function App() {
  if (NO_AUTH) {
    console.warn(
      "⚠️ NO_AUTH mode is enabled - Authentication is BYPASSED for local development",
    );
    return (
      <Suspense fallback={<FullPageSpinner />}>
        <AppShell user={{ username: "Dev User", email: "dev@local" }} />
      </Suspense>
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
      // Configure Amplify (deferred off the boot path) before any auth call.
      await ensureAmplifyConfigured();
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
      await ensureAmplifyConfigured();
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

    // Subscribe to Amplify's Hub lazily (aws-amplify/utils is part of the
    // deferred SDK chunk). `active` guards against the async import resolving
    // after the effect has already been cleaned up.
    let active = true;
    let hubUnsub = null;
    (async () => {
      const { Hub } = await import("aws-amplify/utils");
      if (!active) return;
      hubUnsub = Hub.listen("auth", ({ payload }) => {
        if (payload.event === "tokenRefresh_failure") {
          handleSignOut();
        }
      });
    })();

    return () => {
      active = false;
      window.removeEventListener("auth-expired", onAuthExpired);
      if (hubUnsub) hubUnsub();
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
    // CustomSignIn is lazy (its aws-amplify/auth import lives in the deferred
    // SDK chunk); Suspense covers the brief chunk fetch.
    return (
      <Suspense fallback={<FullPageSpinner />}>
        <CustomSignIn />
      </Suspense>
    );
  }

  return (
    <Suspense fallback={<FullPageSpinner />}>
      <AppShell signOut={handleSignOut} user={user} />
    </Suspense>
  );
}

export default App;
