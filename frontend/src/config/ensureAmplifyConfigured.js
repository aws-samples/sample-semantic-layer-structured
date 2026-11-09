// Deferred, idempotent Amplify configuration.
//
// aws-amplify is ~400 KB of the boot bundle but is only needed on the auth
// path (checking the session, sign-in, sign-out). Importing it at module load
// forced it into main.js and gated every page's load event on parsing it.
//
// This helper dynamic-imports aws-amplify (so webpack splits it into its own
// async chunk that is NOT downloaded for first paint) and calls
// Amplify.configure exactly once. Every auth call site awaits it first, so
// configuration is guaranteed to have run before any Amplify API is used —
// preserving the original top-level-configure ordering guarantee.

let configuredPromise = null;

/**
 * Ensure Amplify has been configured. Returns a cached promise so concurrent
 * callers share a single configure() call.
 * @returns {Promise<void>} resolves once Amplify.configure has run.
 */
export default function ensureAmplifyConfigured() {
  if (!configuredPromise) {
    configuredPromise = (async () => {
      const { Amplify } = await import("aws-amplify");
      const { default: amplifyConfig } = await import("./amplify-config");
      Amplify.configure(amplifyConfig);
    })();
  }
  return configuredPromise;
}
