// Platform admin: every action on every resource.
// The non-ACTIVE mutation guard in default.cedar still overrides this — a
// `forbid` beats every `permit`, by design.
permit (
  principal,
  action,
  resource
)
when { principal.globalRoles.contains("platform-admin") };
