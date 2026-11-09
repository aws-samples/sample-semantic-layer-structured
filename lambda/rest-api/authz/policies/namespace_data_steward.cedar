// Namespace data steward: author and read data assets within their namespace.
//
// Deliberately does NOT grant approveRule / publishRule / publishMetric — a
// steward may author a governed rule but not approve or publish their own. That
// separation of duties is the point of the governed-rules workflow; collapsing
// it would reopen the gate §3.2 flags.
//
// Also withheld: manageNamespace, setNamespaceStatus, deleteNamespace,
// grantPermissions, manageRoles.
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
    SL::Action::"viewMonitoring",
    SL::Action::"manageOntology",
    SL::Action::"manageMetric",
    SL::Action::"manageRule",
    SL::Action::"manageDocuments",
    SL::Action::"manageGroundTruth",
    SL::Action::"manageEvaluation",
    SL::Action::"manageFeedback",
    SL::Action::"manageLessons",
    SL::Action::"manageDataSource"
  ],
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "namespace-data-steward",
    "resourceUID": resource.id
  })
};
