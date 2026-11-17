#!/usr/bin/env node
/**
 * Set `casing_mode=UPPER` on every deployed Snowflake connector Lambda.
 *
 * WHY THIS IS A SCRIPT AND NOT PART OF THE CDK STACK
 * --------------------------------------------------
 * Snowflake identifiers in this project are uppercase (`FHIR_DB.FHIR_R4.PATIENTS`)
 * while Athena lowercases identifiers before handing them to a federated connector.
 * Without `casing_mode=UPPER` the connector looks up `fhir_r4.patients`, Snowflake
 * reports no such table, and Athena surfaces it as a plain "table not found" — a
 * result that looks like a missing table rather than a configuration gap.
 *
 * The AthenaSnowflakeConnector SAR template exposes NO parameter for this, so the
 * only place to set it is the function's environment map, post-deploy. Worse: any
 * update to the SAR nested stack rewrites that map from the template and drops the
 * setting again. The fix therefore has to be re-applied after every connector
 * deploy, which is why `deploy:snowflake` chains it rather than leaving it to a
 * README step someone will forget.
 *
 * WHY IT MERGES INSTEAD OF SETTING
 * --------------------------------
 * `UpdateFunctionConfiguration --environment` REPLACES the whole variables map. The
 * SAR-provided entries (`default`, `spill_bucket`, `spill_prefix`,
 * `disable_spill_encryption`, `SNOWFLAKE_ENABLE_S3_EXPORT`) must be read back and
 * re-sent, or the connector loses its connection string and every query fails.
 *
 * WHY IT DISCOVERS FUNCTIONS VIA ATHENA
 * -------------------------------------
 * The connector name is derived in CDK from the project name and each Snowflake
 * database (`<project>-snowflake-<db-slug>-connector`). Re-deriving it here would
 * duplicate that rule and drift from it. Instead this reads the Athena data catalogs
 * (`snowflake_*`, type LAMBDA) and takes the function ARN each one actually routes
 * to — the same value Athena itself uses.
 *
 * ORDER: run AFTER `cdk deploy`.
 *
 * Usage (from the `cdk/` directory):
 *   npm run set:snowflake-casing
 *   npm run set:snowflake-casing -- --region us-east-1 --casing-mode UPPER
 *
 * Exit codes: 0 when every connector was updated (or already correct), 1 on any
 * failure, including "no Snowflake catalogs found" — a silent success there would
 * hide a connector that is deployed but unqueryable.
 */
import {
  AthenaClient,
  ListDataCatalogsCommand,
  GetDataCatalogCommand,
} from '@aws-sdk/client-athena';
import {
  LambdaClient,
  GetFunctionConfigurationCommand,
  UpdateFunctionConfigurationCommand,
} from '@aws-sdk/client-lambda';

/** Environment variable the connector reads for identifier casing. */
const CASING_ENV_KEY = 'casing_mode';

/** Parsed command-line options. */
export interface CasingCliOptions {
  /** AWS region; undefined lets the SDK resolve from the environment/profile. */
  region?: string;
  /** Casing mode to apply; defaults to `UPPER`. */
  casingMode?: string;
}

/**
 * Parse `--key value` and `--key=value` command-line arguments.
 *
 * @param argv - raw argument list (typically `process.argv.slice(2)`)
 * @returns the parsed options
 * @throws Error on an unknown flag or a flag missing its value — a typo must not be
 *   silently ignored and leave the casing unset.
 */
export function parseCasingArgs(argv: string[]): CasingCliOptions {
  const opts: CasingCliOptions = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const eq = arg.indexOf('=');
    const key = eq === -1 ? arg : arg.slice(0, eq);
    const inlineValue = eq === -1 ? undefined : arg.slice(eq + 1);

    const readValue = (): string => {
      if (inlineValue !== undefined) {
        return inlineValue;
      }
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        throw new Error(`Flag ${key} requires a value.`);
      }
      i += 1;
      return next;
    };

    switch (key) {
      // `npm run … -- --region x` can forward a bare `--`; ignoring it here matches
      // sync-snowflake-secret.ts so both scripts accept the same invocations.
      case '--':
        break;
      case '--region':
        opts.region = readValue();
        break;
      case '--casing-mode':
        opts.casingMode = readValue();
        break;
      default:
        throw new Error(`Unknown argument: ${arg}. Supported: --region, --casing-mode.`);
    }
  }
  return opts;
}

/**
 * Extract a Lambda function name from an Athena data catalog parameter value.
 *
 * The value is either a bare function name or a full ARN; Athena accepts both, so
 * both must be handled here.
 *
 * @param functionParameter - a catalog `function`/`metadata-function`/`record-function` value
 * @returns the function name
 * @throws Error when the value is empty
 */
export function functionNameFromCatalogParameter(functionParameter: string): string {
  const trimmed = functionParameter.trim();
  if (trimmed === '') {
    throw new Error('Athena catalog has an empty `function` parameter.');
  }
  // `arn:aws:lambda:<region>:<account>:function:<name>` → `<name>`; a bare name is
  // returned unchanged because it contains no `:`.
  const parts = trimmed.split(':');
  return parts[parts.length - 1];
}

/**
 * Collect every connector function a catalog routes to.
 *
 * CDK registers a single `function` parameter, but Athena stores it back as the pair
 * `metadata-function` + `record-function` (the metadata and record paths can address
 * different Lambdas). Reading only `function` therefore finds nothing on a deployed
 * catalog, so all three keys are accepted and the results de-duplicated.
 *
 * @param parameters - the catalog's `Parameters` map
 * @returns de-duplicated function names, in the order first seen
 * @throws Error when none of the recognised keys is present
 */
export function functionNamesFromCatalogParameters(
  parameters: Record<string, string> | undefined
): string[] {
  const names: string[] = [];
  for (const key of ['function', 'metadata-function', 'record-function']) {
    const value = parameters?.[key];
    if (value === undefined) {
      continue;
    }
    const name = functionNameFromCatalogParameter(value);
    if (!names.includes(name)) {
      names.push(name);
    }
  }
  if (names.length === 0) {
    throw new Error(
      'Athena catalog has no `function`, `metadata-function` or `record-function` parameter.'
    );
  }
  return names;
}

/**
 * Find the connector Lambda behind every `snowflake_*` Athena data catalog.
 *
 * @param athena - an Athena client
 * @returns catalog-name → function-name pairs, in catalog-name order
 */
export async function discoverSnowflakeConnectors(
  athena: AthenaClient
): Promise<Array<{ catalogName: string; functionName: string }>> {
  const catalogNames: string[] = [];
  let nextToken: string | undefined;
  // Paginate: an account with many catalogs would otherwise silently lose the tail.
  do {
    const page = await athena.send(new ListDataCatalogsCommand({ NextToken: nextToken }));
    for (const summary of page.DataCatalogsSummary ?? []) {
      if (summary.Type === 'LAMBDA' && summary.CatalogName?.startsWith('snowflake')) {
        catalogNames.push(summary.CatalogName);
      }
    }
    nextToken = page.NextToken;
  } while (nextToken !== undefined);

  const found: Array<{ catalogName: string; functionName: string }> = [];
  for (const catalogName of catalogNames.sort()) {
    const detail = await athena.send(new GetDataCatalogCommand({ Name: catalogName }));
    try {
      for (const functionName of functionNamesFromCatalogParameters(
        detail.DataCatalog?.Parameters
      )) {
        found.push({ catalogName, functionName });
      }
    } catch (err) {
      // Name the catalog: the helper cannot, and "no function parameter" is useless
      // without knowing which of several catalogs is broken.
      throw new Error(
        `Athena catalog ${catalogName}: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  }
  return found;
}

/**
 * Merge the casing setting into one function's existing environment map.
 *
 * @param lambda - a Lambda client
 * @param functionName - the connector function to update
 * @param casingMode - value for `casing_mode`
 * @returns `true` when an update was sent, `false` when the value was already correct
 */
export async function applyCasingMode(
  lambda: LambdaClient,
  functionName: string,
  casingMode: string
): Promise<boolean> {
  const current = await lambda.send(
    new GetFunctionConfigurationCommand({ FunctionName: functionName })
  );
  const variables = { ...(current.Environment?.Variables ?? {}) };
  if (variables[CASING_ENV_KEY] === casingMode) {
    return false;
  }

  // Read-modify-write: `--environment` replaces the entire map, so every SAR-provided
  // variable has to be re-sent alongside the new one.
  variables[CASING_ENV_KEY] = casingMode;
  await lambda.send(
    new UpdateFunctionConfigurationCommand({
      FunctionName: functionName,
      Environment: { Variables: variables },
    })
  );
  return true;
}

/**
 * Entry point: discover the connectors, then set `casing_mode` on each.
 *
 * @returns process exit code, 0 on success
 */
export async function main(): Promise<number> {
  const opts = parseCasingArgs(process.argv.slice(2));
  const casingMode = opts.casingMode ?? 'UPPER';
  const clientConfig = opts.region !== undefined ? { region: opts.region } : {};
  const athena = new AthenaClient(clientConfig);
  const lambda = new LambdaClient(clientConfig);

  const connectors = await discoverSnowflakeConnectors(athena);
  if (connectors.length === 0) {
    // Fail loudly: the caller asked to configure connectors and there were none, which
    // means either the deploy did not run or the connector flag was off.
    throw new Error(
      'No Snowflake Athena data catalogs found. Deploy with -c enableSnowflakeConnector=true first.'
    );
  }

  for (const { catalogName, functionName } of connectors) {
    const updated = await applyCasingMode(lambda, functionName, casingMode);
    console.log(
      updated
        ? `${catalogName}: set ${CASING_ENV_KEY}=${casingMode} on ${functionName}`
        : `${catalogName}: ${CASING_ENV_KEY} already ${casingMode} on ${functionName}`
    );
  }
  return 0;
}

// Only run when executed directly, so the helpers above stay unit-testable.
if (require.main === module) {
  main()
    .then((code) => process.exit(code))
    .catch((err: unknown) => {
      console.error(
        `set-snowflake-casing failed: ${err instanceof Error ? err.message : String(err)}`
      );
      process.exit(1);
    });
}
