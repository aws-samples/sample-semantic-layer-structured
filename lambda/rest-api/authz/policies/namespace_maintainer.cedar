// Namespace maintainer: every action within their namespace (same reach as the
// owner; the distinction is administrative — an owner is who the namespace
// belongs to, a maintainer is who else may operate it).
permit (
  principal,
  action,
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "namespace-maintainer",
    "resourceUID": resource.id
  })
};
