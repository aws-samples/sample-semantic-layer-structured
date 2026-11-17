/**
 * Tests for hooks/useAuthz.js.
 *
 * The behaviour under test is mostly about the FAILURE modes, because those are
 * the ones that matter: this hook hides UI, so every uncertain state has to
 * resolve to "show it". A test suite that only covered the happy path would pass
 * against a hook that hid the admin console whenever the network hiccupped.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { useAuthz } from "../hooks/useAuthz";
import { namespaceAPI } from "../services/api";
import {
  NAMESPACE_DATA_ANALYST,
  NAMESPACE_MAINTAINER,
  PLATFORM_ADMIN,
  PLATFORM_VIEWER,
} from "../utils/authzRoles";

jest.mock("../services/api", () => ({
  namespaceAPI: { getMyAuthz: jest.fn() },
}));

/**
 * Build a JWT whose payload carries the given Cognito groups.
 *
 * @param {string[]} groups Group names for the `cognito:groups` claim.
 * @returns {string} An unsigned token in the shape the hook reads.
 */
function tokenWithGroups(groups) {
  const payload = Buffer.from(
    JSON.stringify({ sub: "u-1", email: "a@b.c", "cognito:groups": groups }),
  ).toString("base64url");
  return `header.${payload}.signature`;
}

/** Store an ID token where the hook (and the axios interceptor) look for it. */
function setToken(groups) {
  localStorage.setItem("authToken", tokenWithGroups(groups));
}

/** @returns {Object} A successful /namespaces/me response envelope. */
function meResponse({
  enforced = true,
  groups = [],
  global = [],
  resource = [],
}) {
  return {
    success: true,
    data: {
      enforced,
      userId: "u-1",
      email: "a@b.c",
      groups,
      globalRoles: global,
      resourceRoles: resource,
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  jest.clearAllMocks();
});

describe("token seeding", () => {
  test("an admin's roles are known on the very first render", async () => {
    // No `await` before the assertion: the point is that the nav renders
    // correctly without waiting for the network, so an admin never sees a
    // stripped-down menu flash.
    setToken(["Admin"]);
    namespaceAPI.getMyAuthz.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useAuthz());

    expect(result.current.roles).toEqual([PLATFORM_ADMIN]);
    expect(result.current.isAdmin).toBe(true);
  });

  test("a viewer sees admin surfaces until the fetch lands", async () => {
    // Fail open while in flight. The token says platform-viewer, which alone
    // would deny manageOntology — but the token cannot see a namespace grant, so
    // acting on it now would hide the console from a maintainer.
    setToken(["Users"]);
    namespaceAPI.getMyAuthz.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useAuthz());

    expect(result.current.roles).toEqual([PLATFORM_VIEWER]);
    expect(result.current.ready).toBe(false);
    expect(result.current.isAdmin).toBe(true);
  });
});

describe("refinement from /namespaces/me", () => {
  test("a grant-only maintainer is admitted", async () => {
    // The case that motivates the endpoint: no Cognito group at all, and a
    // namespace-maintainer grant the browser could never have seen.
    setToken([]);
    namespaceAPI.getMyAuthz.mockResolvedValue(
      meResponse({
        resource: [{ role: NAMESPACE_MAINTAINER, resourceUID: "ns-1" }],
      }),
    );

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.roles).toEqual([NAMESPACE_MAINTAINER]);
    expect(result.current.isAdmin).toBe(true);
  });

  test("a read-only caller is denied once roles are known", async () => {
    setToken(["Users"]);
    namespaceAPI.getMyAuthz.mockResolvedValue(
      meResponse({
        groups: ["Users"],
        global: [PLATFORM_VIEWER],
        resource: [{ role: NAMESPACE_DATA_ANALYST, resourceUID: "ns-1" }],
      }),
    );

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.isAdmin).toBe(false);
    expect(result.current.can("query")).toBe(true);
    expect(result.current.can("publishRule")).toBe(false);
  });

  test("the server's group list supersedes the token's", async () => {
    // A group added after the token was minted is not in the token. The server
    // resolved the caller fresh, so its view wins.
    setToken(["Users"]);
    namespaceAPI.getMyAuthz.mockResolvedValue(
      meResponse({ groups: ["Admin"], global: [PLATFORM_ADMIN] }),
    );

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.roles).toEqual([PLATFORM_ADMIN]);
  });

  test("a group removed since the token was minted stops conferring a role", async () => {
    // The inverse, and the reason tokenRoles is REPLACED rather than merged with
    // the server's: merging would let a revoked group keep its role until the
    // token expired.
    setToken(["Admin"]);
    namespaceAPI.getMyAuthz.mockResolvedValue(
      meResponse({ groups: ["Users"], global: [PLATFORM_VIEWER] }),
    );

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.roles).toEqual([PLATFORM_VIEWER]);
    expect(result.current.isAdmin).toBe(false);
  });
});

describe("fail-open states", () => {
  test("enforcement off shows everything", async () => {
    // With ENABLE_AUTHZ off the API denies nothing, so empty roles must not read
    // as "may do nothing" — that would hide the admin console on every
    // deployment that has not armed enforcement yet, i.e. all of them at first.
    setToken([]);
    namespaceAPI.getMyAuthz.mockResolvedValue(meResponse({ enforced: false }));

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(result.current.enforced).toBe(false));
    expect(result.current.ready).toBe(false);
    expect(result.current.isAdmin).toBe(true);
    expect(result.current.can("publishRule")).toBe(true);
  });

  test("a failed fetch shows everything", async () => {
    // A transient 5xx, or a 404 on a deployment predating namespaces. Hiding the
    // console here would be a support ticket; showing it costs nothing, because
    // the API still authorizes every request behind it.
    setToken(["Users"]);
    namespaceAPI.getMyAuthz.mockResolvedValue({
      success: false,
      error: "Unable to connect to server.",
    });

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(namespaceAPI.getMyAuthz).toHaveBeenCalled());
    expect(result.current.ready).toBe(false);
    expect(result.current.isAdmin).toBe(true);
  });

  test("no token at all shows everything", async () => {
    namespaceAPI.getMyAuthz.mockResolvedValue({ success: false, error: "401" });

    const { result } = renderHook(() => useAuthz());

    await waitFor(() => expect(namespaceAPI.getMyAuthz).toHaveBeenCalled());
    expect(result.current.roles).toEqual([]);
    expect(result.current.isAdmin).toBe(true);
  });
});

describe("lifecycle", () => {
  test("resolves once, not on every render", async () => {
    setToken(["Admin"]);
    namespaceAPI.getMyAuthz.mockResolvedValue(
      meResponse({ groups: ["Admin"], global: [PLATFORM_ADMIN] }),
    );

    const { result, rerender } = renderHook(() => useAuthz());
    await waitFor(() => expect(result.current.ready).toBe(true));
    rerender();
    rerender();

    expect(namespaceAPI.getMyAuthz).toHaveBeenCalledTimes(1);
  });

  test("a response arriving after unmount does not warn", async () => {
    // Guards the `cancelled` flag. A setState after unmount is a console error
    // in React 18 test output, which is how this would surface.
    setToken(["Admin"]);
    let resolve;
    namespaceAPI.getMyAuthz.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useAuthz());
    unmount();
    resolve(meResponse({ global: [PLATFORM_ADMIN] }));
    await Promise.resolve();

    expect(errorSpy).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});
