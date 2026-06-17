// Amplify configuration for AWS Cognito authentication
// This configuration will be populated by CDK outputs or environment variables

// Read authentication mode from environment variable
const authMode = process.env.REACT_APP_AUTH_MODE || "oauth";

// Conditionally configure login method based on authentication mode
const loginWithConfig =
  authMode === "oauth"
    ? {
        // OAuth configuration for AWS Cognito federation
        oauth: {
          domain: process.env.REACT_APP_OAUTH_DOMAIN || "",
          scopes: ["openid", "profile", "email"],
          redirectSignIn: [
            process.env.REACT_APP_OAUTH_REDIRECT_SIGN_IN ||
              window.location.origin,
          ],
          redirectSignOut: [
            process.env.REACT_APP_OAUTH_REDIRECT_SIGN_OUT ||
              window.location.origin,
          ],
          responseType: "code",
        },
      }
    : {
        // Direct authentication configuration for username/password
        email: true,
      };

const amplifyConfig = {
  Auth: {
    Cognito: {
      userPoolId: process.env.REACT_APP_USER_POOL_ID || "",
      userPoolClientId: process.env.REACT_APP_USER_POOL_CLIENT_ID || "",
      loginWith: loginWithConfig,
    },
  },
};

export default amplifyConfig;
