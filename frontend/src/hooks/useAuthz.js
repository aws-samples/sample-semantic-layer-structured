/**
 * The caller's authorization state, for deciding which surfaces to render.
 *
 * ⚠️ COSMETIC ONLY — see the header of `utils/authzRoles.js`. Enforcement is the
 * Cedar PDP in the REST API, which authorizes every request regardless of what
 * this hook returns. Hiding a nav link is decluttering, not a control.
 *
 * Resolution happens in two steps, deliberately:
 *
 *   1. SYNCHRONOUSLY from the ID token in localStorage. `cognito:groups` is right
 *      there, so an admin's nav renders correctly on the first paint with no
 *      network round-trip and no flash of a stripped-down menu.
 *   2. Then from `GET /namespaces/me`, which is the ONLY way to learn
 *      namespace-scoped roles. Those live in grant rows the browser cannot read,
 *      and group membership never confers one — so a namespace owner who is in no
 *      Cognito group looks role-less after step 1 and correct after step 2.
 *
 * Between the two, and if step 2 fails, the hook FAILS OPEN (`ready: false`, and
 * `can()` returns true). A transient fetch error must not hide the admin console
 * from an administrator; the request they then make is still authorized
 * server-side, so the worst case is a 403 they can see and report rather than a
 * surface that silently vanished.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { namespaceAPI } from "../services/api";
import {
  canPerform,
  decodeJwtPayload,
  groupsFromPayload,
  rolesFromCallerAuthz,
  rolesFromGroups,
} from "../utils/authzRoles";

/**
 * Read the roles derivable from the stored ID token, with no network call.
 *
 * @returns {string[]} Platform roles from `cognito:groups`, or [] when there is
 *   no readable token. [] here means "unknown", not "none" — the caller must
 *   treat it as fail-open until the fetch resolves.
 */
function rolesFromStoredToken() {
  const payload = decodeJwtPayload(localStorage.getItem("authToken"));
  return rolesFromGroups(groupsFromPayload(payload));
}

/**
 * Resolve the caller's roles and expose a capability predicate.
 *
 * @returns {{
 *   roles: string[],
 *   enforced: boolean|null,
 *   ready: boolean,
 *   isAdmin: boolean,
 *   can: (action: string) => boolean,
 * }} `enforced` is null until known. `ready` is false while roles are unknown,
 *   in which case `can()` returns true for everything (fail open).
 */
export function useAuthz() {
  // Seeded, not empty: the token is already in hand, so the first render is
  // usually already correct for platform admins.
  const [tokenRoles, setTokenRoles] = useState(rolesFromStoredToken);
  const [grantRoles, setGrantRoles] = useState(null);
  const [enforced, setEnforced] = useState(null);

  useEffect(() => {
    // `cancelled` rather than an AbortController: the axios wrapper resolves
    // errors into `{success: false}` instead of rejecting, so there is nothing
    // to abort — this only suppresses a setState after unmount.
    let cancelled = false;

    (async () => {
      const result = await namespaceAPI.getMyAuthz();
      if (cancelled) return;
      if (!result.success) {
        // Fail open. Common and benign: the endpoint 404s on any deployment
        // predating namespaces, where nothing is enforced anyway.
        setGrantRoles(null);
        setEnforced(null);
        return;
      }
      setGrantRoles(rolesFromCallerAuthz(result.data));
      setEnforced(result.data?.enforced === true);
      // The server's own view of the caller's groups supersedes the token's:
      // a group added since the token was minted is not in the token.
      setTokenRoles(rolesFromGroups(result.data?.groups));
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const roles = useMemo(() => {
    const merged = new Set(tokenRoles);
    for (const role of grantRoles || []) merged.add(role);
    return [...merged];
  }, [tokenRoles, grantRoles]);

  // Not ready until the fetch has landed, EVEN IF the token already yielded
  // roles: the token can only ever reveal platform roles, so a namespace
  // maintainer would otherwise be judged against an incomplete role set.
  //
  // With enforcement off, roles are never authoritative — the API resolves
  // nothing and denies nothing — so treat that as permanently not-ready and let
  // every surface render.
  const ready = grantRoles !== null && enforced === true;

  const can = useCallback(
    /**
     * Whether to render a surface gated on one Cedar action.
     *
     * @param {string} action A Cedar action name (a key of ACTION_ROLES).
     * @returns {boolean} True when permitted, or when roles are unknown.
     */
    (action) => {
      if (!ready) return true;
      return canPerform(action, roles);
    },
    [ready, roles],
  );

  return {
    roles,
    enforced,
    ready,
    // The specific question AppShell asks: is there any point showing the
    // semantic-layer admin console at all?
    isAdmin: can("manageOntology"),
    can,
  };
}

export default useAuthz;
