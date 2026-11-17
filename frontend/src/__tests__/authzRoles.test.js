/**
 * Unit + drift tests for utils/authzRoles.js.
 *
 * The drift block is the important half. The role names, the group→role map and
 * the action→roles map in authzRoles.js are hand-mirrored from Python and Cedar,
 * and a disagreement produces no error anywhere: a stale action name means a
 * surface is hidden from someone the API would allow, and a stale role name means
 * a role is silently unrecognized. So the tests below parse the backend files and
 * derive the truth rather than restating it.
 */

import fs from "fs";
import path from "path";
import {
  ACTION_ROLES,
  ALL_ROLES,
  GROUP_ROLE_MAP,
  NAMESPACE_DATA_ANALYST,
  NAMESPACE_DATA_STEWARD,
  NAMESPACE_MAINTAINER,
  NAMESPACE_OWNER,
  PLATFORM_ADMIN,
  PLATFORM_VIEWER,
  canPerform,
  decodeJwtPayload,
  groupsFromPayload,
  rolesFromCallerAuthz,
  rolesFromGroups,
} from "../utils/authzRoles";

const AUTHZ_DIR = path.join(__dirname, "../../../lambda/rest-api/authz");

/** @returns {string} The contents of one file under lambda/rest-api/authz. */
function readAuthzFile(relative) {
  return fs.readFileSync(path.join(AUTHZ_DIR, relative), "utf8");
}

/**
 * Build role → permitted actions from the Cedar policy files themselves.
 *
 * A policy is either unconstrained (`action,` with no list — platform-admin,
 * namespace-owner, namespace-maintainer) or an explicit `action in [...]` list.
 * The role each policy applies to comes from its `when` clause.
 *
 * @returns {Object<string, {unconstrained: boolean, actions: Set<string>}>}
 */
function rolesFromPolicies() {
  const out = {};
  const dir = path.join(AUTHZ_DIR, "policies");
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith(".cedar") || file === "default.cedar") continue;
    const src = fs.readFileSync(path.join(dir, file), "utf8");
    // The role literal in `globalRoles.contains("x")` or `"role": "x"`.
    const roleMatch =
      src.match(/globalRoles\.contains\("([^"]+)"\)/) ||
      src.match(/"role":\s*"([^"]+)"/);
    expect(roleMatch).toBeTruthy();
    const role = roleMatch[1];
    const listMatch = src.match(/action in \[([^\]]+)\]/);
    if (!listMatch) {
      // No action list at all — a blanket `permit (principal, action, resource)`.
      expect(src).toMatch(/permit \(\s*principal,\s*action,\s*resource\s*\)/);
      out[role] = { unconstrained: true, actions: new Set() };
      continue;
    }
    const actions = new Set(
      [...listMatch[1].matchAll(/SL::Action::"([^"]+)"/g)].map((m) => m[1]),
    );
    out[role] = { unconstrained: false, actions };
  }
  return out;
}

describe("drift against the backend", () => {
  test("the role literals match cedar_schema.Role", () => {
    const schema = readAuthzFile("cedar_schema.py");
    // Every role the backend defines, from the `Role` enum body.
    const body = schema.slice(schema.indexOf("class Role"));
    const backendRoles = [...body.matchAll(/^ {4}[A-Z_]+ = '([a-z-]+)'/gm)].map(
      (m) => m[1],
    );
    expect(backendRoles.length).toBeGreaterThan(0);
    expect([...ALL_ROLES].sort()).toEqual([...backendRoles].sort());
  });

  test("GROUP_ROLE_MAP matches _DEFAULT_GROUP_ROLE_MAP", () => {
    // If the backend adds a group mapping and this map does not, a user in that
    // group renders a stripped-down UI and then finds every action permitted.
    const resolver = readAuthzFile("role_resolver.py");
    const block = resolver.slice(
      resolver.indexOf("_DEFAULT_GROUP_ROLE_MAP"),
      resolver.indexOf("}", resolver.indexOf("_DEFAULT_GROUP_ROLE_MAP")),
    );
    const pairs = [...block.matchAll(/'([^']+)':\s*Role\.([A-Z_]+)\.value/g)];
    expect(pairs.length).toBeGreaterThan(0);
    const backendMap = {};
    for (const [, group, roleConst] of pairs) {
      // PLATFORM_ADMIN → platform-admin: the enum name is the value uppercased
      // with dashes replaced, so the mapping is derivable without importing.
      backendMap[group] = roleConst.toLowerCase().replace(/_/g, "-");
    }
    expect(GROUP_ROLE_MAP).toEqual(backendMap);
  });

  test("every action in ACTION_ROLES is a real Cedar action", () => {
    const schema = readAuthzFile("cedar_schema.py");
    const known = new Set(
      [...schema.matchAll(/^ {4}[A-Z_]+ = '([a-zA-Z]+)'/gm)].map((m) => m[1]),
    );
    for (const action of Object.keys(ACTION_ROLES)) {
      expect(known).toContain(action);
    }
  });

  test("each action maps to exactly the roles the policies permit", () => {
    // The assertion that actually catches drift: for every action the UI gates
    // on, recompute the permitted role set from the .cedar files and compare.
    const policies = rolesFromPolicies();
    expect(Object.keys(policies).sort()).toEqual([...ALL_ROLES].sort());

    for (const [action, expected] of Object.entries(ACTION_ROLES)) {
      const derived = Object.entries(policies)
        .filter(([, p]) => p.unconstrained || p.actions.has(action))
        .map(([role]) => role);
      expect([...derived].sort()).toEqual([...expected].sort());
    }
  });

  test("the steward's separation of duties still holds", () => {
    // Spelled out rather than left to the derivation above, because collapsing
    // author and approve is the specific regression §3.2 of the comparison doc
    // flags: a steward may author a governed rule but not publish their own.
    expect(ACTION_ROLES.manageRule).toContain(NAMESPACE_DATA_STEWARD);
    expect(ACTION_ROLES.publishRule).not.toContain(NAMESPACE_DATA_STEWARD);
    expect(ACTION_ROLES.approveRule).not.toContain(NAMESPACE_DATA_STEWARD);
  });
});

describe("decodeJwtPayload", () => {
  /** @returns {string} A JWT whose payload is the given object. */
  const jwt = (payload) =>
    `h.${Buffer.from(JSON.stringify(payload)).toString("base64url")}.s`;

  test("decodes a base64url payload", () => {
    // base64url, not base64: a `sub` containing `-` or `_` in the encoded form
    // is exactly what atob rejects without the replace.
    expect(decodeJwtPayload(jwt({ sub: "abc~?>", email: "a@b.c" }))).toEqual({
      sub: "abc~?>",
      email: "a@b.c",
    });
  });

  test.each([
    ["null", null],
    ["undefined", undefined],
    ["empty", ""],
    ["not a jwt", "just-a-string"],
    ["undecodable payload", "h.!!!!.s"],
    ["non-object payload", "h.bm90LWpzb24.s"],
  ])("returns null for %s", (_label, token) => {
    expect(decodeJwtPayload(token)).toBeNull();
  });
});

describe("groupsFromPayload", () => {
  test("reads the array form the browser actually sees", () => {
    expect(groupsFromPayload({ "cognito:groups": ["Admin", "Users"] })).toEqual(
      ["Admin", "Users"],
    );
  });

  test("reads the JSON-string form", () => {
    expect(
      groupsFromPayload({ "cognito:groups": '["Admin","Users"]' }),
    ).toEqual(["Admin", "Users"]);
  });

  test("reads the API Gateway bracket form", () => {
    // `[Admin Users]` is not JSON — the HTTP API JWT authorizer stringifies
    // multi-valued claims this way, so a naive JSON.parse would yield [].
    expect(groupsFromPayload({ "cognito:groups": "[Admin Users]" })).toEqual([
      "Admin",
      "Users",
    ]);
  });

  test("reads a bare single group", () => {
    expect(groupsFromPayload({ "cognito:groups": "Admin" })).toEqual(["Admin"]);
  });

  test.each([
    ["a missing claim", {}],
    ["a null payload", null],
    ["an empty string", { "cognito:groups": "  " }],
    ["a non-string, non-array claim", { "cognito:groups": 7 }],
  ])("returns [] for %s", (_label, payload) => {
    expect(groupsFromPayload(payload)).toEqual([]);
  });
});

describe("rolesFromGroups", () => {
  test("maps known groups", () => {
    expect(rolesFromGroups(["Admin"])).toEqual([PLATFORM_ADMIN]);
    expect(rolesFromGroups(["QueryUsers"])).toEqual([PLATFORM_VIEWER]);
  });

  test("ignores unknown groups rather than failing", () => {
    // An unrecognized group is not an error: Cognito groups exist for reasons
    // unrelated to this app, and the backend resolver ignores them identically.
    expect(rolesFromGroups(["SomeOtherGroup"])).toEqual([]);
  });

  test("dedupes groups that confer the same role", () => {
    expect(rolesFromGroups(["Admin", "Admins"])).toEqual([PLATFORM_ADMIN]);
  });

  test("returns [] for no groups", () => {
    expect(rolesFromGroups(undefined)).toEqual([]);
    expect(rolesFromGroups([])).toEqual([]);
  });
});

describe("rolesFromCallerAuthz", () => {
  test("merges global and per-namespace roles", () => {
    expect(
      rolesFromCallerAuthz({
        globalRoles: [PLATFORM_VIEWER],
        resourceRoles: [
          { role: NAMESPACE_OWNER, resourceUID: "ns-1" },
          { role: NAMESPACE_DATA_ANALYST, resourceUID: "ns-2" },
        ],
      }),
    ).toEqual([PLATFORM_VIEWER, NAMESPACE_OWNER, NAMESPACE_DATA_ANALYST]);
  });

  test("surfaces a grant-only role", () => {
    // The case the ID token cannot see, and the whole reason /namespaces/me
    // exists: no groups, no global roles, one namespace grant.
    expect(
      rolesFromCallerAuthz({
        globalRoles: [],
        resourceRoles: [{ role: NAMESPACE_MAINTAINER, resourceUID: "ns-1" }],
      }),
    ).toEqual([NAMESPACE_MAINTAINER]);
  });

  test("returns [] for an empty or absent body", () => {
    expect(rolesFromCallerAuthz(null)).toEqual([]);
    expect(rolesFromCallerAuthz({})).toEqual([]);
  });

  test("skips a malformed resourceRoles entry", () => {
    expect(
      rolesFromCallerAuthz({ resourceRoles: [{ resourceUID: "ns-1" }, null] }),
    ).toEqual([]);
  });
});

describe("canPerform", () => {
  test("a platform admin may do everything mapped", () => {
    for (const action of Object.keys(ACTION_ROLES)) {
      expect(canPerform(action, [PLATFORM_ADMIN])).toBe(true);
    }
  });

  test("a platform viewer may query but not manage", () => {
    expect(canPerform("query", [PLATFORM_VIEWER])).toBe(true);
    expect(canPerform("manageOntology", [PLATFORM_VIEWER])).toBe(false);
    expect(canPerform("publishRule", [PLATFORM_VIEWER])).toBe(false);
  });

  test("a grant-only maintainer may manage", () => {
    expect(canPerform("manageOntology", [NAMESPACE_MAINTAINER])).toBe(true);
  });

  test("no roles permits nothing", () => {
    expect(canPerform("manageOntology", [])).toBe(false);
    expect(canPerform("manageOntology", null)).toBe(false);
  });

  test("an unmapped action throws rather than returning false", () => {
    // Fail loudly. Returning false would hide the surface from everyone,
    // including platform admins, and look like a working authorization rule.
    expect(() => canPerform("manageOntolgy", [PLATFORM_ADMIN])).toThrow(
      /unknown action manageOntolgy/,
    );
  });
});
