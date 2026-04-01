import {
  Spinner,
  Input,
  Button,
  FormField,
  Alert,
} from "@cloudscape-design/components";

import {
  signInWithRedirect,
  fetchAuthSession,
  signIn,
  signUp,
  confirmSignUp,
  resetPassword,
  confirmResetPassword,
} from "aws-amplify/auth";
import { useState, useEffect } from "react";

// Custom Sign In component that completely replaces the default one
export const CustomSignIn = () => {
  const [loading, setLoading] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(null);
  const [view, setView] = useState("signin"); // 'signin', 'signup', 'forgot'

  // Read authentication mode from environment variable
  const authMode = process.env.REACT_APP_AUTH_MODE || "oauth";
  // Read customer logo from environment variable
  const customerLogo =
    process.env.REACT_APP_CUSTOMER_LOGO || "/amazicon.svg";

  // Check if user is already authenticated on component mount
  useEffect(() => {
    const checkAuthStatus = async () => {
      try {
        console.log("🔐 [CustomSignIn] Checking auth status on mount...");
        setLoading(true);

        // Check for existing Cognito session
        const session = await fetchAuthSession().catch(() => null);
        console.log("🔐 [CustomSignIn] Session on mount:", session);
        console.log("🔐 [CustomSignIn] Has tokens?", !!session?.tokens);

        const isAuth = !!session?.tokens;
        setIsAuthenticated(isAuth);
        console.log("🔐 [CustomSignIn] isAuthenticated set to:", isAuth);
      } catch (error) {
        console.error(
          "🔐 [CustomSignIn] Error checking authentication status:",
          error,
        );
        setIsAuthenticated(false);
      } finally {
        setLoading(false);
      }
    };

    checkAuthStatus();
  }, []);

  const handleSignIn = async () => {
    try {
      console.log("🔐 [CustomSignIn] Sign in button clicked");
      console.log("🔐 [CustomSignIn] isAuthenticated state:", isAuthenticated);
      setLoading(true);

      // Only proceed with sign in if user is not already authenticated
      if (!isAuthenticated) {
        console.log(
          "🔐 [CustomSignIn] Not authenticated, initiating OAuth redirect with signInWithRedirect()...",
        );
        await signInWithRedirect();
        console.log(
          "🔐 [CustomSignIn] signInWithRedirect() returned (browser should redirect to Cognito)",
        );
      } else {
        console.log(
          "🔐 [CustomSignIn] Already authenticated, redirecting to app...",
        );
        window.location.href = "/";
      }
    } catch (error) {
      console.error("🔐 [CustomSignIn] ❌ Error during sign in:", error);
      console.error("🔐 [CustomSignIn] Error details:", {
        message: error.message,
        name: error.name,
        stack: error.stack,
      });
      setLoading(false);
    }
  };

  // Render content based on authentication mode
  const renderContent = () => {
    if (authMode === "direct") {
      // Direct authentication mode: show username/password forms
      if (view === "signup") {
        return <DirectAuthSignUp onSwitchToSignIn={() => setView("signin")} />;
      } else if (view === "forgot") {
        return <ForgotPassword onSwitchToSignIn={() => setView("signin")} />;
      } else {
        return (
          <DirectAuthSignIn
            onSwitchToSignUp={() => setView("signup")}
            onSwitchToForgotPassword={() => setView("forgot")}
          />
        );
      }
    } else {
      // OAuth mode: show Midway sign-in button
      return (
        <>
          {loading ? (
            <div
              style={{
                display: "flex",
                justifyContent: "center",
                padding: "2rem 0",
              }}
            >
              <Spinner size="large" />
            </div>
          ) : (
            <div style={{ marginTop: "2rem" }}>
              <div
                onClick={handleSignIn}
                style={{
                  backgroundColor: "#FF9900",
                  border: "1px solid #FF9900",
                  borderRadius: "8px",
                  padding: "12px 20px",
                  cursor: "pointer",
                  transition: "all 0.2s ease",
                  boxShadow: "0 2px 4px rgba(0, 0, 0, 0.1)",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.backgroundColor = "#ec7211";
                  e.currentTarget.style.borderColor = "#ec7211";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.backgroundColor = "#FF9900";
                  e.currentTarget.style.borderColor = "#FF9900";
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "12px",
                  }}
                >
                  <img
                    src="/amazicon.svg"
                    alt="AWS Logo"
                    style={{
                      width: "20px",
                      height: "20px",
                      filter: "brightness(0) invert(1)",
                    }}
                  />
                  <span
                    style={{
                      fontSize: "14px",
                      fontWeight: 700,
                      color: "#FFFFFF",
                    }}
                  >
                    Sign in with AWS Cognito
                  </span>
                </div>
              </div>

              <div
                style={{
                  marginTop: "1.5rem",
                  padding: "12px",
                  backgroundColor: "#f9fafb",
                  borderRadius: "4px",
                  border: "1px solid #e9ebed",
                  textAlign: "center",
                }}
              >
                <p
                  style={{
                    fontSize: "12px",
                    color: "#5f6b7a",
                    margin: 0,
                    lineHeight: "1.5",
                  }}
                >
                  Secure authentication powered by AWS Cognito
                </p>
              </div>
            </div>
          )}
        </>
      );
    }
  };

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: "#f2f3f3",
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
        padding: "2rem",
      }}
    >
      <div
        style={{
          backgroundColor: "#FFFFFF",
          borderRadius: "8px",
          boxShadow: "0 4px 12px rgba(0, 0, 0, 0.1)",
          padding: "3rem 2.5rem",
          maxWidth: "450px",
          width: "100%",
        }}
      >
        {/* Logo/Icon Section */}
        <div style={{ textAlign: "center", marginBottom: "2rem" }}>
          <div
            style={{
              width: "120px",
              height: "120px",
              margin: "0 auto 1.5rem",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <img
              src={customerLogo}
              alt="Logo"
              style={{
                maxWidth: "100%",
                maxHeight: "100%",
                objectFit: "contain",
              }}
              onError={(e) => {
                // Fallback to default SVG icon if image fails to load
                e.target.style.display = "none";
                const fallbackDiv = e.target.parentElement;
                fallbackDiv.style.backgroundColor = "#232f3e";
                fallbackDiv.style.borderRadius = "8px";
                fallbackDiv.style.width = "80px";
                fallbackDiv.style.height = "80px";
                fallbackDiv.innerHTML = `
                  <svg
                    width="48"
                    height="48"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="#FF9900"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                    <circle cx="12" cy="7" r="4" />
                  </svg>
                `;
              }}
            />
          </div>
          <h1
            style={{
              fontSize: "1.75rem",
              marginBottom: "0.5rem",
              fontWeight: 700,
              color: "#000716",
            }}
          >
            Welcome
          </h1>
          <h2
            style={{
              fontSize: "0.875rem",
              fontWeight: 400,
              color: "#5f6b7a",
              lineHeight: "1.5",
            }}
          >
            Sign in to continue to Ontology-driven Semantic Layer
          </h2>
        </div>

        {/* Render authentication content based on mode */}
        {renderContent()}
      </div>
    </div>
  );
};

// Direct Authentication Sign In Component
const DirectAuthSignIn = ({ onSwitchToSignUp, onSwitchToForgotPassword }) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmNewPassword, setConfirmNewPassword] = useState("");
  const [needsPasswordChange, setNeedsPasswordChange] = useState(false);

  const handleSignIn = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const signInOutput = await signIn({ username: email, password });

      // Check if additional steps are required
      if (signInOutput.isSignedIn) {
        window.location.href = "/";
      } else if (signInOutput.nextStep?.signInStep === "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED") {
        // User needs to change password
        setNeedsPasswordChange(true);
        setLoading(false);
      } else {
        // Handle other challenges if needed
        console.log("Sign in requires additional step:", signInOutput.nextStep);
        setError("Authentication requires additional steps. Please contact support.");
        setLoading(false);
      }
    } catch (err) {
      console.error("Sign in error:", err);
      setError(err.message || "Failed to sign in");
      setLoading(false);
    }
  };

  const handlePasswordChange = async (e) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmNewPassword) {
      setError("Passwords do not match");
      return;
    }

    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters long");
      return;
    }

    setLoading(true);

    try {
      const { confirmSignIn } = await import("aws-amplify/auth");
      await confirmSignIn({ challengeResponse: newPassword });
      window.location.href = "/";
    } catch (err) {
      console.error("Password change error:", err);
      setError(err.message || "Failed to change password");
    } finally {
      setLoading(false);
    }
  };

  if (needsPasswordChange) {
    return (
      <div style={{ width: "100%" }}>
        <form onSubmit={handlePasswordChange}>
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError("")}>
              {error}
            </Alert>
          )}

          <Alert type="info">
            You must change your password before continuing.
          </Alert>

          <FormField label="New Password">
            <Input
              value={newPassword}
              onChange={({ detail }) => setNewPassword(detail.value)}
              type="password"
              placeholder="Enter new password"
              disabled={loading}
            />
          </FormField>

          <FormField label="Confirm New Password">
            <Input
              value={confirmNewPassword}
              onChange={({ detail }) => setConfirmNewPassword(detail.value)}
              type="password"
              placeholder="Confirm new password"
              disabled={loading}
            />
          </FormField>

          <div style={{ marginTop: "0.5rem", fontSize: "12px", color: "#5f6b7a" }}>
            Password must be at least 8 characters with uppercase, lowercase, number, and special character.
          </div>

          <div style={{ marginTop: "1.5rem" }}>
            <Button
              variant="primary"
              formAction="submit"
              loading={loading}
              fullWidth
            >
              Change Password
            </Button>
          </div>
        </form>
      </div>
    );
  }

  return (
    <div style={{ width: "100%" }}>
      <form onSubmit={handleSignIn}>
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError("")}>
            {error}
          </Alert>
        )}

        <FormField label="Email" description="Use your email address to sign in">
          <Input
            value={email}
            onChange={({ detail }) => setEmail(detail.value)}
            type="email"
            placeholder="Enter your email"
            disabled={loading}
          />
        </FormField>

        <FormField label="Password">
          <Input
            value={password}
            onChange={({ detail }) => setPassword(detail.value)}
            type="password"
            placeholder="Enter your password"
            disabled={loading}
          />
        </FormField>

        <div style={{ marginTop: "1.5rem" }}>
          <Button
            variant="primary"
            formAction="submit"
            loading={loading}
            fullWidth
          >
            Sign In
          </Button>
        </div>
      </form>

      <div style={{ marginTop: "1.5rem", textAlign: "center" }}>
        <Button variant="link" onClick={onSwitchToForgotPassword}>
          Forgot password?
        </Button>
      </div>

      <div style={{ marginTop: "0.5rem", textAlign: "center" }}>
        <span style={{ fontSize: "14px", color: "#5f6b7a" }}>
          Don't have an account?{" "}
        </span>
        <Button variant="link" onClick={onSwitchToSignUp}>
          Sign up
        </Button>
      </div>
    </div>
  );
};

// Direct Authentication Sign Up Component
const DirectAuthSignUp = ({ onSwitchToSignIn }) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [needsVerification, setNeedsVerification] = useState(false);

  const handleSignUp = async (e) => {
    e.preventDefault();
    setError("");

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setLoading(true);

    try {
      await signUp({
        username: email,
        password,
        options: {
          userAttributes: { email },
        },
      });
      setNeedsVerification(true);
    } catch (err) {
      console.error("Sign up error:", err);
      setError(err.message || "Failed to sign up");
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyCode = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await confirmSignUp({
        username: email,
        confirmationCode: verificationCode,
      });
      window.location.href = "/";
    } catch (err) {
      console.error("Verification error:", err);
      setError(err.message || "Failed to verify code");
    } finally {
      setLoading(false);
    }
  };

  if (needsVerification) {
    return (
      <div style={{ width: "100%" }}>
        <form onSubmit={handleVerifyCode}>
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError("")}>
              {error}
            </Alert>
          )}

          <Alert type="info">
            A verification code has been sent to {email}
          </Alert>

          <FormField label="Verification Code">
            <Input
              value={verificationCode}
              onChange={({ detail }) => setVerificationCode(detail.value)}
              placeholder="Enter verification code"
              disabled={loading}
            />
          </FormField>

          <div style={{ marginTop: "1.5rem" }}>
            <Button
              variant="primary"
              formAction="submit"
              loading={loading}
              fullWidth
            >
              Verify Email
            </Button>
          </div>
        </form>

        <div style={{ marginTop: "1.5rem", textAlign: "center" }}>
          <Button variant="link" onClick={onSwitchToSignIn}>
            Back to Sign In
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ width: "100%" }}>
      <form onSubmit={handleSignUp}>
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError("")}>
            {error}
          </Alert>
        )}

        <FormField label="Email">
          <Input
            value={email}
            onChange={({ detail }) => setEmail(detail.value)}
            type="email"
            placeholder="Enter your email"
            disabled={loading}
          />
        </FormField>

        <FormField label="Password">
          <Input
            value={password}
            onChange={({ detail }) => setPassword(detail.value)}
            type="password"
            placeholder="Enter your password"
            disabled={loading}
          />
        </FormField>

        <FormField label="Confirm Password">
          <Input
            value={confirmPassword}
            onChange={({ detail }) => setConfirmPassword(detail.value)}
            type="password"
            placeholder="Confirm your password"
            disabled={loading}
          />
        </FormField>

        <div style={{ marginTop: "1.5rem" }}>
          <Button
            variant="primary"
            formAction="submit"
            loading={loading}
            fullWidth
          >
            Create Account
          </Button>
        </div>
      </form>

      <div style={{ marginTop: "1.5rem", textAlign: "center" }}>
        <span style={{ fontSize: "14px", color: "#5f6b7a" }}>
          Already have an account?{" "}
        </span>
        <Button variant="link" onClick={onSwitchToSignIn}>
          Sign in
        </Button>
      </div>
    </div>
  );
};

// Forgot Password Component
const ForgotPassword = ({ onSwitchToSignIn }) => {
  const [email, setEmail] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState("request"); // 'request' or 'reset'

  const handleRequestReset = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await resetPassword({ username: email });
      setStep("reset");
    } catch (err) {
      console.error("Reset request error:", err);
      setError(err.message || "Failed to send reset code");
    } finally {
      setLoading(false);
    }
  };

  const handleResetPassword = async (e) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setLoading(true);

    try {
      await confirmResetPassword({
        username: email,
        confirmationCode: verificationCode,
        newPassword,
      });
      onSwitchToSignIn();
    } catch (err) {
      console.error("Reset password error:", err);
      setError(err.message || "Failed to reset password");
    } finally {
      setLoading(false);
    }
  };

  if (step === "reset") {
    return (
      <div style={{ width: "100%" }}>
        <form onSubmit={handleResetPassword}>
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError("")}>
              {error}
            </Alert>
          )}

          <Alert type="info">
            A verification code has been sent to {email}
          </Alert>

          <FormField label="Verification Code">
            <Input
              value={verificationCode}
              onChange={({ detail }) => setVerificationCode(detail.value)}
              placeholder="Enter verification code"
              disabled={loading}
            />
          </FormField>

          <FormField label="New Password">
            <Input
              value={newPassword}
              onChange={({ detail }) => setNewPassword(detail.value)}
              type="password"
              placeholder="Enter new password"
              disabled={loading}
            />
          </FormField>

          <FormField label="Confirm New Password">
            <Input
              value={confirmPassword}
              onChange={({ detail }) => setConfirmPassword(detail.value)}
              type="password"
              placeholder="Confirm new password"
              disabled={loading}
            />
          </FormField>

          <div style={{ marginTop: "1.5rem" }}>
            <Button
              variant="primary"
              formAction="submit"
              loading={loading}
              fullWidth
            >
              Reset Password
            </Button>
          </div>
        </form>

        <div style={{ marginTop: "1.5rem", textAlign: "center" }}>
          <Button variant="link" onClick={onSwitchToSignIn}>
            Back to Sign In
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ width: "100%" }}>
      <form onSubmit={handleRequestReset}>
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError("")}>
            {error}
          </Alert>
        )}

        <FormField label="Email">
          <Input
            value={email}
            onChange={({ detail }) => setEmail(detail.value)}
            type="email"
            placeholder="Enter your email"
            disabled={loading}
          />
        </FormField>

        <div style={{ marginTop: "1.5rem" }}>
          <Button
            variant="primary"
            formAction="submit"
            loading={loading}
            fullWidth
          >
            Send Reset Code
          </Button>
        </div>
      </form>

      <div style={{ marginTop: "1.5rem", textAlign: "center" }}>
        <Button variant="link" onClick={onSwitchToSignIn}>
          Back to Sign In
        </Button>
      </div>
    </div>
  );
};

// Custom Header component (empty to override default)
export const CustomHeader = () => null;

// Custom Footer component (empty to override default)
export const CustomFooter = () => null;
