// Default policy — loaded on EVERY authorization decision, in addition to the
// principal's resolved roles. It therefore carries the non-ACTIVE mutation
// guard below, which must participate regardless of which roles the caller
// holds.

// Unmapped-route guard.
//
// The action map returns the `denyAll` sentinel for any route it does not
// recognize. A plain "no policy permits it" would NOT be enough: platform-admin
// and namespace-owner both hold `permit (principal, action, resource)` with an
// unconstrained action, which matches `denyAll` too and would ALLOW an unmapped
// route for exactly the principals who can do the most damage. Only a `forbid`
// makes "unmapped denies" hold universally — which is the whole reason the
// sentinel exists.
forbid (
  principal,
  action == SL::Action::"denyAll",
  resource
);

// Any authenticated principal may enumerate namespaces. The router filters the
// result to the namespaces the caller can actually see, so `list` is a
// discovery permission, not a read of namespace contents.
permit (
  principal,
  action == SL::Action::"list",
  resource is SL::Namespace
);

// Non-ACTIVE-namespace mutation guard.
//
// A namespace that is not ACTIVE — ARCHIVED (frozen, retained for audit),
// DELETING (teardown running), DELETE_FAILED (teardown failed, awaiting retry)
// — must never accept a MUTATING action against it or any child resource
// (ontologies, metrics, rules, documents, grants). Because this is a `forbid`
// it overrides every `permit`, INCLUDING platform-admin and namespace-owner, so
// "no mutations unless ACTIVE" is a hard invariant rather than a
// role-overridable default.
//
// Deliberately NOT blocked in a non-ACTIVE status:
//   - every read action (viewNamespace, list, query, readMetric, readRule, …)
//   - setNamespaceStatus  → so ARCHIVED can be reactivated
//   - deleteNamespace     → so ARCHIVED / DELETE_FAILED can be torn down
// Blocking those would make a namespace unrecoverable the moment it left ACTIVE.
//
// The middleware attaches context.namespaceStatus only for namespace-scoped
// routes, so `context has namespaceStatus` leaves platform-scoped routes and
// namespace *creation* (no namespace yet) unaffected.
forbid (
  principal,
  action in [
    SL::Action::"manageNamespace",
    SL::Action::"manageOntology",
    SL::Action::"manageMetric",
    SL::Action::"manageRule",
    SL::Action::"manageDocuments",
    SL::Action::"manageGroundTruth",
    SL::Action::"manageEvaluation",
    SL::Action::"manageFeedback",
    SL::Action::"manageLessons",
    SL::Action::"manageDataSource",
    SL::Action::"approveRule",
    SL::Action::"publishRule",
    SL::Action::"publishMetric",
    SL::Action::"grantPermissions",
    SL::Action::"manageRoles"
  ],
  resource
)
when {
  context has namespaceStatus && context.namespaceStatus != "ACTIVE"
};
