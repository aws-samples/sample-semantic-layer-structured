// Platform viewer: read-only across every namespace.
//
// This is the role the `Users` / `QueryUsers` Cognito groups map to, so
// enabling authorization does not revoke read or query access from existing
// users — it revokes every WRITE, including the rule approval/publish gate that
// docs/comparison-coa.md §3.2 flags as open to any authenticated user.
permit (
  principal,
  action in [
    SL::Action::"viewNamespace",
    SL::Action::"list",
    SL::Action::"query",
    SL::Action::"readMetric",
    SL::Action::"readRule",
    SL::Action::"viewOntology",
    SL::Action::"traverseGraph",
    SL::Action::"searchDocuments",
    SL::Action::"viewEvaluation",
    SL::Action::"viewMonitoring"
  ],
  resource
)
when { principal.globalRoles.contains("platform-viewer") };
