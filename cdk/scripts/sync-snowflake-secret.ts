#!/usr/bin/env node
/**
 * Push Snowflake credentials from the local `.env` into the Secrets Manager secret
 * that the Athena Snowflake connector reads.
 *
 * WHY THIS IS A SCRIPT AND NOT PART OF THE CDK STACK
 * --------------------------------------------------
 * Putting the credential into the stack requires
 * `cdk.SecretValue.unsafePlainText(...)`, which writes the value verbatim into the
 * synthesized CloudFormation template — landing the private key in `cdk.out/` on
 * disk AND in the stack template stored in CloudFormation, where it is readable by
 * anyone holding `cloudformation:GetTemplate`. That is a strictly wider audience
 * than `secretsmanager:GetSecretValue` on a single secret ARN.
 *
 * So CDK owns the secret RESOURCE (name, IAM policy, connector wiring) and this
 * script owns the secret VALUE. The plaintext path is:
 *   .env  →  this process  →  PutSecretValue  →  Secrets Manager (encrypted)
 * It never touches a template, `cdk.out/`, or git.
 *
 * ORDER: run AFTER `cdk deploy` — the secret resource must exist first. The
 * connector only reads the secret when it serves its first query, so populating the
 * value post-deploy is in time.
 *
 * Usage (from the `cdk/` directory):
 *   npm run sync:snowflake-secret
 *   npm run sync:snowflake-secret -- --secret-name semantic-layer/snowflake/connector
 *   npm run sync:snowflake-secret -- --env-path /path/to/.env --region us-east-1
 *
 * NOTE the flag is `--env-path`, not `--env-file`: Node 20 added its own `--env-file`
 * CLI option, and ts-node forwards it to the node child process, so `--env-file`
 * never reaches this script's argv (node tries to LOAD the path as an env file and
 * exits). `--env-path` has no such collision.
 *
 * Exit codes: 0 on success, 1 on any validation or API failure (never silently
 * partial — a failed sync leaves the previous secret version intact).
 */
import {
  SecretsManagerClient,
  PutSecretValueCommand,
  DescribeSecretCommand,
} from '@aws-sdk/client-secrets-manager';
import { readEnvFile, snowflakeContextFromEnv, redact, SECRET_ENV_KEYS } from '../bin/env-config';
import { buildSnowflakeSecretPayload } from './snowflake-secret-payload';

/** Default secret name — must match `resolveSnowflakeConfig`'s `snowflakeSecretName` default. */
const DEFAULT_SECRET_NAME = 'semantic-layer/snowflake/connector';

/** Parsed command-line options. */
export interface CliOptions {
  /** Explicit `.env` path (`--env-path`); undefined uses `cdk/.env`. */
  envFile?: string;
  /** Explicit secret name override; undefined resolves from `.env` then the default. */
  secretName?: string;
  /** AWS region; undefined lets the SDK resolve from the environment/profile. */
  region?: string;
}

/**
 * Parse `--key value` and `--key=value` command-line arguments.
 *
 * @param argv - raw argument list, typically `process.argv.slice(2)`.
 * @returns the recognized options.
 * @throws Error on an unrecognized flag or a flag missing its value — a typo'd flag
 *   must not be silently ignored when the operation writes a credential.
 */
export function parseArgs(argv: string[]): CliOptions {
  const opts: CliOptions = {};

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    // A bare `--` is the conventional end-of-options separator and shows up when a
    // caller writes `ts-node script.ts -- --flag`. Skip it rather than reporting it as
    // an unrecognized flag.
    if (arg === '--') {
      continue;
    }
    // Support both `--flag=value` and `--flag value`.
    const eq = arg.indexOf('=');
    const flag = eq === -1 ? arg : arg.slice(0, eq);
    const inlineValue = eq === -1 ? undefined : arg.slice(eq + 1);

    const takeValue = (): string => {
      if (inlineValue !== undefined) {
        return inlineValue;
      }
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        throw new Error(`Flag ${flag} requires a value.`);
      }
      i += 1;
      return next;
    };

    switch (flag) {
      case '--env-path':
        opts.envFile = takeValue();
        break;
      case '--secret-name':
        opts.secretName = takeValue();
        break;
      case '--region':
        opts.region = takeValue();
        break;
      case '--env-file':
        // Node 20 owns `--env-file` and ts-node forwards it to its node child, so this
        // case is normally unreachable — node consumes the flag first and exits. Kept
        // as a named case so that if it ever DOES arrive (a different runner, a
        // compiled `node dist/...` invocation), the operator gets the rename hint
        // instead of a generic "unrecognized argument".
        throw new Error(
          "--env-file collides with Node 20's built-in --env-file option. Use --env-path instead."
        );
      default:
        throw new Error(
          `Unrecognized argument: ${flag}. Supported: --env-path, --secret-name, --region.`
        );
    }
  }

  return opts;
}

/**
 * Read `.env`, build the credential payload, and write it as a new secret version.
 *
 * @param opts - parsed CLI options.
 * @returns nothing; resolves once `PutSecretValue` has succeeded.
 * @throws Error when `.env` is missing required keys, when the target secret does
 *   not exist (deploy has not run), or when the API call fails.
 */
async function main(opts: CliOptions): Promise<void> {
  const env = readEnvFile(opts.envFile);

  if (Object.keys(env).length === 0) {
    throw new Error(
      `No .env values found${opts.envFile ? ` at ${opts.envFile}` : ' at cdk/.env'}. ` +
        'Copy cdk/.env.example to cdk/.env and fill in the Snowflake settings.'
    );
  }

  // Secret name precedence mirrors bin/app.ts: explicit flag > .env > shared default.
  const secretName =
    opts.secretName ?? snowflakeContextFromEnv(env).snowflakeSecretName ?? DEFAULT_SECRET_NAME;

  // Build (and validate) the payload BEFORE any AWS call, so a malformed .env fails
  // without touching the secret.
  const { mode, secret } = buildSnowflakeSecretPayload(env);

  console.log('Snowflake secret sync');
  console.log(`  secret name : ${secretName}`);
  console.log(`  auth mode   : ${mode}`);
  // Redacted inventory so the operator can confirm what was picked up without the
  // values appearing in a terminal scrollback or CI log.
  for (const key of SECRET_ENV_KEYS) {
    console.log(`  ${key.padEnd(32)}: ${redact(env[key])}`);
  }

  const client = new SecretsManagerClient(opts.region ? { region: opts.region } : {});

  // Confirm the secret exists first. PutSecretValue on a missing secret returns
  // ResourceNotFoundException, but naming the likely cause is more useful than
  // relaying the raw API error.
  try {
    await client.send(new DescribeSecretCommand({ SecretId: secretName }));
  } catch (err) {
    const name = err instanceof Error ? err.name : '';
    if (name === 'ResourceNotFoundException') {
      throw new Error(
        `Secret "${secretName}" does not exist. It is created by the Athena stack — run ` +
          '`cdk deploy -c enableSnowflakeConnector=true ...` first, then re-run this sync. ' +
          '(This script deliberately does not create the secret, so a typo in --secret-name ' +
          'cannot silently create an orphan.)'
      );
    }
    throw err;
  }

  await client.send(
    new PutSecretValueCommand({
      SecretId: secretName,
      SecretString: JSON.stringify(secret),
    })
  );

  console.log(`\nWrote a new version of "${secretName}" (${mode} auth).`);
  console.log('The connector reads it on its next query — no redeploy needed.');
}

// Entry point. Errors print a single-line message (never a stack trace that could
// include a credential in a frame argument) and exit non-zero.
//
// parseArgs is called INSIDE the async wrapper rather than in `main(parseArgs(...))`
// argument position: there it would throw synchronously, before main() returned a
// promise, so `.catch` never saw it and a typo'd flag dumped a raw stack trace.
//
// Guarded by `require.main === module` so a unit test can import parseArgs without
// the import triggering a real PutSecretValue against whatever profile is active.
if (require.main === module) {
  (async (): Promise<void> => {
    await main(parseArgs(process.argv.slice(2)));
  })().catch((err: unknown) => {
    console.error(`\nSnowflake secret sync failed: ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
