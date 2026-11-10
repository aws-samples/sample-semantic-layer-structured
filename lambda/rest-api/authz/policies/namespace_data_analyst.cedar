// Namespace data analyst: read and query within their namespace. No writes.
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
when {
  principal.resourceRoles.contains({
    "role": "namespace-data-analyst",
    "resourceUID": resource.id
  })
};
