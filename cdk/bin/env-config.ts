/**
 * Minimal `.env` reader for CDK deploy-time configuration.
 *
 * Deliberately hand-rolled rather than pulling in `dotenv`: the CDK package has no
 * runtime dependency on it today, the parsing needed here is small and fully
 * specified below, and a build-time dependency that reads credentials is worth
 * avoiding. The subset supported is the subset this repo's `.env.example` uses.
 *
 * SECURITY: values returned by {@link readEnvFile} may include Snowflake
 * credentials. Only `scripts/sync-snowflake-secret.ts` reads the secret keys.
 * CDK synth (`bin/app.ts`) reads ONLY the non-secret keys via
 * {@link snowflakeContextFromEnv}, so no credential can reach a CloudFormation
 * template. Never log a raw value from this module — use {@link redact}.
 */
import * as fs from 'fs';
import * as path from 'path';

/** Parsed `.env` contents as key → value. */
export type EnvMap = Record<string, string>;

/**
 * The `.env` keys that hold Snowflake credentials. Listed so the sync script and
 * tests share one definition, and so a reviewer can see at a glance exactly which
 * keys are sensitive.
 */
export const SECRET_ENV_KEYS = [
  'SNOWFLAKE_PRIVATE_KEY',
  'SNOWFLAKE_PRIVATE_KEY_PASSPHRASE',
  'SNOWFLAKE_PASSWORD',
] as const;

/**
 * Parse `.env`-formatted text into a key/value map.
 *
 * Supported syntax:
 *   - `KEY=value` lines; the first `=` splits key from value
 *   - blank lines and lines whose first non-space character is `#` are skipped
 *   - an optional leading `export ` on the key is stripped
 *   - surrounding single or double quotes on the value are stripped
 *   - inside DOUBLE quotes only, the escapes `\n`, `\r`, `\t` and `\\` are decoded,
 *     so a PEM key can be written on one line as "-----BEGIN…\nMIIE…\n-----END…"
 *   - unquoted values are trimmed and may not contain escapes
 *
 * Malformed lines (no `=`) are ignored rather than raised: a stray word in a local
 * `.env` should not break a deploy, and every key this repo requires is validated
 * explicitly by its consumer, which fails loudly on absence.
 *
 * @param contents - raw text of a `.env` file.
 * @returns a map of every parsed key to its decoded value.
 */
export function parseEnv(contents: string): EnvMap {
  const out: EnvMap = {};

  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === '' || line.startsWith('#')) {
      continue;
    }

    const eq = line.indexOf('=');
    if (eq === -1) {
      continue;
    }

    const key = line
      .slice(0, eq)
      .replace(/^export\s+/, '')
      .trim();
    if (key === '') {
      continue;
    }

    let value = line.slice(eq + 1).trim();

    // Quoted value: strip the matching pair, and decode escapes for double quotes
    // only (mirroring shell semantics, where single quotes are literal).
    const isDoubleQuoted = value.length >= 2 && value.startsWith('"') && value.endsWith('"');
    const isSingleQuoted = value.length >= 2 && value.startsWith("'") && value.endsWith("'");

    if (isDoubleQuoted || isSingleQuoted) {
      value = value.slice(1, -1);
      if (isDoubleQuoted) {
        // Decode in one pass so a literal backslash-n ("\\n") is not turned into a
        // newline: a per-escape chain of .replace() calls would do exactly that.
        value = value.replace(/\\(.)/g, (_match, ch: string) => {
          switch (ch) {
            case 'n':
              return '\n';
            case 'r':
              return '\r';
            case 't':
              return '\t';
            case '\\':
              return '\\';
            default:
              // Unknown escape: keep it verbatim, backslash included.
              return `\\${ch}`;
          }
        });
      }
    }

    out[key] = value;
  }

  return out;
}

/**
 * Read and parse a `.env` file if it exists.
 *
 * A missing file is NOT an error — `.env` is one of several ways to supply
 * configuration (CDK context is the other), and a deploy that passes everything
 * via `-c` needs no `.env` at all. Callers that require a specific key must
 * validate it themselves and fail loudly.
 *
 * @param filePath - path to the `.env` file. Defaults to `.env` beside the `cdk/`
 *   package root (i.e. `cdk/.env`), resolved relative to this compiled file.
 * @returns the parsed map, or an empty map when the file does not exist.
 * @throws Error when the file exists but cannot be read (permissions, is a
 *   directory, etc.) — that is a real misconfiguration, not an absent optional file.
 */
export function readEnvFile(filePath?: string): EnvMap {
  // `__dirname` is `cdk/bin` at runtime (ts-node) and after `tsc` (compiled in
  // place), so `..` is the cdk package root in both cases.
  const target = filePath ?? path.resolve(__dirname, '..', '.env');

  if (!fs.existsSync(target)) {
    return {};
  }

  let contents: string;
  try {
    contents = fs.readFileSync(target, 'utf-8');
  } catch (err) {
    throw new Error(
      `Failed to read env file at ${target}: ${err instanceof Error ? err.message : String(err)}`
    );
  }

  return parseEnv(contents);
}

/**
 * Extract the NON-SECRET Snowflake settings from a parsed `.env` as a CDK-context
 * shaped map.
 *
 * The returned keys are the same names `resolveSnowflakeConfig` reads from CDK
 * context, so `bin/app.ts` can layer this underneath real context with a single
 * lookup chain. Credential keys are deliberately excluded — this is the function
 * CDK synth calls, and anything it returns can end up in a template.
 *
 * `enableSnowflakeConnector` is intentionally NOT sourced from `.env`: enabling a
 * whole stack of resources should be an explicit act on the command line, not a
 * side effect of a file being present on disk.
 *
 * @param env - a parsed `.env` map (from {@link readEnvFile}).
 * @returns context-keyed settings for every corresponding `.env` key that is set
 *   to a non-empty value; keys absent from `.env` are omitted entirely so a
 *   caller's `??` chain falls through to its own default.
 */
export function snowflakeContextFromEnv(env: EnvMap): Record<string, string> {
  // .env key → CDK context key. Non-secret identifiers only.
  const mapping: Record<string, string> = {
    SNOWFLAKE_ACCOUNT: 'snowflakeAccount',
    SNOWFLAKE_WAREHOUSE: 'snowflakeWarehouse',
    // Both database keys are mapped. `resolveSnowflakeConfig` rejects having both set
    // at once, so a `.env` carrying the old singular key keeps working while the plural
    // key is the way to expose more than one database.
    SNOWFLAKE_DATABASE: 'snowflakeDatabase',
    SNOWFLAKE_DATABASES: 'snowflakeDatabases',
    SNOWFLAKE_ROLE: 'snowflakeRole',
    SNOWFLAKE_SECRET_NAME: 'snowflakeSecretName',
  };

  const out: Record<string, string> = {};
  for (const [envKey, contextKey] of Object.entries(mapping)) {
    const value = env[envKey];
    // Treat an empty/whitespace value as unset: a commented-out-then-emptied line
    // should not shadow a default with ''.
    if (value !== undefined && value.trim() !== '') {
      out[contextKey] = value.trim();
    }
  }

  return out;
}

/**
 * Render a secret value as a safe-to-log placeholder.
 *
 * Shows only the length so an operator can distinguish "empty" from "set" and spot
 * a truncated paste, without any part of the credential reaching a terminal, log
 * file, or CI transcript.
 *
 * @param value - the sensitive value, or undefined when the key is unset.
 * @returns `<unset>` when absent or empty, otherwise `<redacted, N chars>`.
 */
export function redact(value: string | undefined): string {
  if (value === undefined || value === '') {
    return '<unset>';
  }
  return `<redacted, ${value.length} chars>`;
}
