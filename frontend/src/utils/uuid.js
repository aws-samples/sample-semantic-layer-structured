/**
 * uuid — generate an RFC-4122 v4 identifier for client-minted session IDs.
 *
 * Prefers the native crypto.randomUUID when available and falls back to
 * crypto.getRandomValues for browsers that predate randomUUID. Both paths use
 * a cryptographically secure generator; Math.random is never used because the
 * ID becomes a session identifier. Shared by useChatStream (persisting a
 * session) and LandingPage (minting one on first send).
 */
export function uuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC-4122 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return (
    `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-` +
    `${hex.slice(16, 20)}-${hex.slice(20)}`
  );
}
