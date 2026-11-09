/**
 * uuid — generate an RFC-4122 v4 identifier for client-minted session IDs.
 *
 * Prefers the native crypto.randomUUID when available and falls back to a
 * Math.random-based generator for older browsers. Shared by useChatStream
 * (persisting a session) and LandingPage (minting one on first send).
 */
export function uuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
