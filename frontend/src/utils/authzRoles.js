/**
 * What the caller's roles imply about which admin surfaces are worth showing.
 *
 * ⚠️ THIS IS NOT ENFORCEMENT. The policy enforcement point is the Cedar
 * middleware in `lambda/rest-api/authz/middleware.py`, which authorizes every
 * request server-side. Anything here is decluttering: it stops a read-only user
 * from being shown buttons that will 403. A user who bypasses it gains nothing,
 * because the API refuses the request regardless.
 *
 * That is also why every predicate here FAILS OPEN when the caller's roles are
 * not known (no token, an undecodable token, enforcement switched off, the
 * `/namespaces/me` fetch still in flight or failed). Failing closed would be the
 * wrong trade in both directions: it would hide the admin UI from a legitimate
 * admin over a transient fetch error, and it would buy no security, since the
 * hiding was never the control. Note the deliberate contrast with the SQL
 * firewall, which fails CLOSED — there, the check *is* the control.
 *
 * The role names, the group→role map and the action→roles map below all mirror
 * the backend. `frontend/src/__tests__/authzRoles.test.js` parses
 * `lambda/rest-api/authz/cedar_schema.py`, `authz/role_resolver.py` and the
 * `authz/policies/*.cedar` files and fails if any of the three drift, because a
 * silent disagreement here shows a user affordances the API will reject (or
 * hides ones it would allow) with no error anywhere.
 */

export const PLATFORM_ADMIN = "platform-admin";
export const PLATFORM_VIEWER = "platform-viewer";
export const NAMESPACE_OWNER = "namespace-owner";
export const NAMESPACE_MAINTAINER = "namespace-maintainer";
export const NAMESPACE_DATA_STEWARD = "namespace-data-steward";
export const NAMESPACE_DATA_ANALYST = "namespace-data-analyst";

export const ALL_ROLES = [
  PLATFORM_ADMIN,
  PLATFORM_VIEWER,
  NAMESPACE_OWNER,
  NAMESPACE_MAINTAINER,
  NAMESPACE_DATA_STEWARD,
  NAMESPACE_DATA_ANALYST,
];

/**
 * Cognito group → platform role, mirroring `_DEFAULT_GROUP_ROLE_MAP` in
 * `authz/role_resolver.py`.
 *
 * Group membership only ever confers a PLATFORM role — there is no namespace in
 * a group name — so this map alone can never reveal a namespace-scoped role.
 * That is the whole reason `useAuthz` also calls `GET /namespaces/me`.
 */
export const GROUP_ROLE_MAP = {
  Admin: PLATFORM_ADMIN,
  Admins: PLATFORM_ADMIN,
  Users: PLATFORM_VIEWER,
  QueryUsers: PLATFORM_VIEWER,
};

/**
 * Which roles permit each Cedar action this UI branches on.
 *
 * Keyed by the backend's own action names (`authz/cedar_schema.py`) rather than
 * invented UI vocabulary, so the drift test can check each row against the
 * `.cedar` policy that grants it.
 *
 * Only the actions the UI actually gates on are listed; this is not a complete
 * transcription of the policy set, and it must not become one — the policies are
 * the authority.
 */
export const ACTION_ROLES = {
  // Build/edit a semantic layer — the whole /admin wizard.
  manageOntology: [
    PLATFORM_ADMIN,
    NAMESPACE_OWNER,
    NAMESPACE_MAINTAINER,
    NAMESPACE_DATA_STEWARD,
  ],
  // Governed-rule authoring.
  manageRule: [
    PLATFORM_ADMIN,
    NAMESPACE_OWNER,
    NAMESPACE_MAINTAINER,
    NAMESPACE_DATA_STEWARD,
  ],
  // The approval/publish gate — deliberately narrower than authoring.
  publishRule: [PLATFORM_ADMIN, NAMESPACE_OWNER, NAMESPACE_MAINTAINER],
  approveRule: [PLATFORM_ADMIN, NAMESPACE_OWNER, NAMESPACE_MAINTAINER],
  // Namespace administration and grant management.
  manageNamespace: [PLATFORM_ADMIN, NAMESPACE_OWNER, NAMESPACE_MAINTAINER],
  grantPermissions: [PLATFORM_ADMIN, NAMESPACE_OWNER, NAMESPACE_MAINTAINER],
  // Asking questions — every role can, which is why the chat UI is never hidden.
  query: ALL_ROLES,
};

/**
 * Decode a JWT's payload without verifying its signature.
 *
 * Verification is neither possible nor needed here: the token was minted for
 * this browser by Amplify and is re-verified by the API on every request. This
 * only reads a claim in order to decide what to render.
 *
 * @param {string|null|undefined} token A JWT.
 * @returns {Object|null} The decoded payload, or null if it cannot be read.
 */
export function decodeJwtPayload(token) {
  if (!token || typeof token !== "string") return null;
  const segments = token.split(".");
  if (segments.length < 2) return null;
  try {
    // base64url → base64 before atob, which rejects the URL-safe alphabet.
    const base64 = segments[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(base64));
  } catch {
    return null;
  }
}

/**
 * Read the `cognito:groups` claim in any of the shapes it arrives in.
 *
 * In the browser the ID-token payload is real JSON, so the claim is an array.
 * The other two shapes are handled because the API Gateway HTTP API JWT
 * authorizer STRINGIFIES multi-valued claims — `cognito:groups` reaches the
 * backend as `"[Admin Users]"` — and that shape has a way of being passed
 * through into places that then hand it back.
 *
 * @param {Object|null|undefined} payload A decoded JWT payload.
 * @returns {string[]} The group names, or [] when the claim is absent.
 */
export function groupsFromPayload(payload) {
  const raw = payload?.["cognito:groups"];
  if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
  if (typeof raw !== "string") return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
    } catch {
      // Not JSON — the authorizer's space-separated `[Admin Users]` form.
    }
    return trimmed
      .slice(1, -1)
      .split(/[\s,]+/)
      .filter(Boolean);
  }
  return [trimmed];
}

/**
 * Map Cognito groups to the platform roles they confer.
 *
 * @param {string[]|null|undefined} groups Group names.
 * @returns {string[]} The distinct roles, in a stable order.
 */
export function rolesFromGroups(groups) {
  const roles = new Set();
  for (const group of groups || []) {
    const role = GROUP_ROLE_MAP[group];
    if (role) roles.add(role);
  }
  return ALL_ROLES.filter((role) => roles.has(role));
}

/**
 * Flatten the `/namespaces/me` response into a single set of role names.
 *
 * Namespace scoping is intentionally dropped. This UI hides whole surfaces
 * (a nav entry, a wizard route), and a user who maintains any one namespace has
 * a reason to see the surface — the per-namespace decision is then made
 * server-side, per request, where the namespace is actually known.
 *
 * @param {Object|null|undefined} me The `/namespaces/me` body.
 * @returns {string[]} The distinct roles, in a stable order.
 */
export function rolesFromCallerAuthz(me) {
  const roles = new Set(me?.globalRoles || []);
  for (const entry of me?.resourceRoles || []) {
    if (entry?.role) roles.add(entry.role);
  }
  return ALL_ROLES.filter((role) => roles.has(role));
}

/**
 * Whether any of the caller's roles permits an action.
 *
 * @param {string} action A Cedar action name; must be a key of ACTION_ROLES.
 * @param {string[]|null|undefined} roles The caller's roles.
 * @returns {boolean} True when at least one role permits the action.
 * @throws {Error} If the action is not one this module maps. A typo would
 *   otherwise silently read as "nobody may do this" and hide the surface for
 *   everyone — including platform admins.
 */
export function canPerform(action, roles) {
  const permitted = ACTION_ROLES[action];
  if (!permitted) {
    throw new Error(
      `unknown action ${action}; expected one of ${Object.keys(ACTION_ROLES).join(", ")}`,
    );
  }
  return (roles || []).some((role) => permitted.includes(role));
}
