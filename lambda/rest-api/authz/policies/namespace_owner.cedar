// Namespace owner: every action within their own namespace.
// Written by namespace creation as a grant on the creating principal.
permit (
  principal,
  action,
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "namespace-owner",
    "resourceUID": resource.id
  })
};
