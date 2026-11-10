/**
 * Snowflake federated connector configuration resolver.
 *
 * Extracted from `bin/app.ts` so the flag/config logic is typed and unit-testable
 * without synthesizing the whole CDK app. Consumed by AthenaStack in a later task.
 */

/** Fully-resolved Snowflake connection settings for the Athena federated connector. */
export interface SnowflakeConfig {
  /** Snowflake account identifier (e.g. `myorg-myacct`). No source-level default. */
  account: string;
  /** Snowflake virtual warehouse used to run federated queries. No source-level default. */
  warehouse: string;
  /**
   * Snowflake databases to expose. One connector Lambda and one Athena catalog is
   * deployed per entry, because a connector's `DefaultConnectionString` pins exactly
   * one `db=`. Always at least one element.
   */
  databases: string[];
  /** Snowflake role assumed by the connector. */
  role: string;
  /** Secrets Manager secret holding Snowflake credentials. */
  secretName: string;
}

/**
 * Resolve the list of Snowflake databases to expose.
 *
 * Two context keys, checked in this order:
 *   `snowflakeDatabases` — comma-separated list, e.g. `-c snowflakeDatabases=FHIR_DB,CLINICAL_DB`
 *   `snowflakeDatabase`  — a single database (the original single-database key)
 * Neither set falls back to `FHIR_DB`, the project's demo database.
 *
 * Setting BOTH is an error rather than a precedence rule: silently honouring one would
 * deploy a catalog set the deployer did not ask for, and the omission only surfaces much
 * later as "my table isn't in Athena".
 *
 * @param getContext - accessor over CDK context (typically `app.node.tryGetContext`)
 * @returns a non-empty list of trimmed, de-duplicated database names
 * @throws Error when both keys are set, or when the plural key parses to nothing usable
 */
function resolveDatabases(getContext: (key: string) => unknown): string[] {
  const plural = getContext('snowflakeDatabases');
  const singular = getContext('snowflakeDatabase');

  if (plural !== undefined && singular !== undefined) {
    throw new Error(
      'Both snowflakeDatabases and snowflakeDatabase are set. Use snowflakeDatabases ' +
        '(comma-separated) for one or many databases, and remove snowflakeDatabase.'
    );
  }

  if (plural === undefined) {
    return [singular !== undefined ? String(singular) : 'FHIR_DB'];
  }

  // Drop empties so a trailing comma or a stray space is not turned into a connector
  // named after the empty string.
  const parsed = String(plural)
    .split(',')
    .map((db) => db.trim())
    .filter((db) => db.length > 0);
  if (parsed.length === 0) {
    throw new Error(
      `snowflakeDatabases is set to '${String(plural)}', which contains no database names.`
    );
  }
  // De-duplicate: two identical entries would collide on every per-database resource name.
  return [...new Set(parsed)];
}

/**
 * Resolve the Snowflake connector configuration from CDK context.
 *
 * @param getContext - accessor over CDK context (typically `app.node.tryGetContext`),
 *   taking a context key and returning its raw value (or `undefined` when unset).
 * @returns a fully-typed `SnowflakeConfig` when `enableSnowflakeConnector` is truthy
 *   (`true` or the string `'true'`), otherwise `undefined`.
 * @throws Error when the connector is enabled but `snowflakeAccount` or
 *   `snowflakeWarehouse` context is not set — these have no source-level default
 *   because they are deployment-specific identifiers (fail loudly, no silent fallback).
 */
export function resolveSnowflakeConfig(
  getContext: (key: string) => unknown
): SnowflakeConfig | undefined {
  // --context values are always strings, so accept both the boolean and string form.
  const flag = getContext('enableSnowflakeConnector');
  const enabled = flag === true || flag === 'true';
  if (!enabled) {
    return undefined;
  }

  const account = getContext('snowflakeAccount');
  const warehouse = getContext('snowflakeWarehouse');
  // Fail loudly: no personal/dev identifiers baked into source. The deployer must
  // supply these at deploy time.
  if (account === undefined || warehouse === undefined) {
    throw new Error(
      'enableSnowflakeConnector is on but snowflakeAccount / snowflakeWarehouse ' +
        'context is not set. Pass -c snowflakeAccount=<acct> -c snowflakeWarehouse=<wh>.'
    );
  }

  const role = getContext('snowflakeRole');
  const secretName = getContext('snowflakeSecretName');

  // Coerce every context read to string so no `any` leaks into the typed config.
  return {
    account: String(account),
    warehouse: String(warehouse),
    databases: resolveDatabases(getContext),
    role: role !== undefined ? String(role) : 'ATHENA_CONNECTOR_ROLE',
    secretName:
      secretName !== undefined ? String(secretName) : 'semantic-layer/snowflake/connector',
  };
}
