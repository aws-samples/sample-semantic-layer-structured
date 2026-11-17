/**
 * Builds the Secrets Manager payload for the Athena Snowflake connector from a
 * parsed `.env` map.
 *
 * Kept separate from `sync-snowflake-secret.ts` so the auth-mode selection and JSON
 * shape are unit-testable with no AWS SDK client and no network. The sibling script
 * is the thin I/O wrapper around this pure function.
 *
 * The JSON shapes below are what the AWS `AthenaSnowflakeConnector` reads out of the
 * secret named by its `SecretNamePrefix` parameter — see
 * docs/plans/2026-07-23-snowflake-connector.md § Task 0 for the provenance of both
 * shapes and which one is the documented fallback.
 */
import { EnvMap } from '../bin/env-config';

/** Which Snowflake authentication mode the `.env` selected. */
export type SnowflakeAuthMode = 'key-pair' | 'password';

/** A built payload plus the mode it was built from (for redacted operator output). */
export interface SnowflakeSecretPayload {
  /** The auth mode inferred from which `.env` keys were populated. */
  mode: SnowflakeAuthMode;
  /** Parsed credential object, ready to be JSON-serialized into the secret. */
  secret: Record<string, string>;
}

/**
 * Build the connector credential payload from `.env` values.
 *
 * Auth mode is INFERRED from which keys are present, then validated:
 *   - `SNOWFLAKE_PRIVATE_KEY` set  → key-pair mode
 *     → `{sfUser, pem_private_key[, pem_private_key_passphrase]}`
 *   - `SNOWFLAKE_PASSWORD` set     → password mode
 *     → `{username, password}`
 *
 * Both set, or neither set, raises. Per this repo's fail-loudly convention we do
 * not guess: silently preferring one mode would deploy a credential the operator
 * did not intend and surface later as an opaque JWT/auth failure inside the
 * connector's CloudWatch logs.
 *
 * @param env - parsed `.env` map (from `readEnvFile`).
 * @returns the auth mode and the credential object to store.
 * @throws Error when `SNOWFLAKE_USER` is missing, when neither credential key is
 *   set, or when both are set. Messages name the offending KEYS only, never values.
 */
export function buildSnowflakeSecretPayload(env: EnvMap): SnowflakeSecretPayload {
  // Treat whitespace-only as unset so a leftover `SNOWFLAKE_PASSWORD=` line does not
  // register as "password mode selected".
  const value = (key: string): string | undefined => {
    const raw = env[key];
    if (raw === undefined || raw.trim() === '') {
      return undefined;
    }
    return raw;
  };

  const user = value('SNOWFLAKE_USER');
  const privateKey = value('SNOWFLAKE_PRIVATE_KEY');
  const passphrase = value('SNOWFLAKE_PRIVATE_KEY_PASSPHRASE');
  const password = value('SNOWFLAKE_PASSWORD');

  if (user === undefined) {
    throw new Error(
      'SNOWFLAKE_USER is not set in .env. It is required for both key-pair and password auth.'
    );
  }

  if (privateKey !== undefined && password !== undefined) {
    throw new Error(
      'Both SNOWFLAKE_PRIVATE_KEY and SNOWFLAKE_PASSWORD are set in .env. Set exactly one: ' +
        'key-pair auth (SNOWFLAKE_PRIVATE_KEY) or password auth (SNOWFLAKE_PASSWORD).'
    );
  }

  if (privateKey === undefined && password === undefined) {
    throw new Error(
      'Neither SNOWFLAKE_PRIVATE_KEY nor SNOWFLAKE_PASSWORD is set in .env. Set exactly one ' +
        'to choose an auth mode.'
    );
  }

  if (privateKey !== undefined) {
    // Guard the most likely paste errors. Every one of these otherwise surfaces as an
    // opaque JWT-signing failure in the connector's CloudWatch logs, far from the cause.
    if (!privateKey.includes('-----BEGIN')) {
      throw new Error(
        'SNOWFLAKE_PRIVATE_KEY does not contain a PEM header ("-----BEGIN"). Expected a full ' +
          'PEM private key; wrap it in double quotes and use \\n escapes for line breaks.'
      );
    }

    // A PUBLIC key is the classic mixup: the public half is what gets registered in
    // Snowflake (ALTER USER … SET RSA_PUBLIC_KEY), so it is the value already on the
    // clipboard. Signing needs the PRIVATE half.
    if (privateKey.includes('-----BEGIN PUBLIC KEY-----')) {
      throw new Error(
        'SNOWFLAKE_PRIVATE_KEY contains a PUBLIC key ("-----BEGIN PUBLIC KEY-----"). The public ' +
          'half belongs in Snowflake (ALTER USER … SET RSA_PUBLIC_KEY); this variable needs the ' +
          'PRIVATE key, whose header reads "-----BEGIN PRIVATE KEY-----" or ' +
          '"-----BEGIN ENCRYPTED PRIVATE KEY-----".'
      );
    }

    if (!privateKey.includes('-----END')) {
      throw new Error(
        'SNOWFLAKE_PRIVATE_KEY has no PEM footer ("-----END"), so the value is truncated. Copy ' +
          'the entire key file including the final -----END …----- line.'
      );
    }

    // Length check on the base64 body: an RSA-2048 PKCS#8 key carries ~1600-1700
    // base64 chars, so anything under 500 is a placeholder or a partial paste rather
    // than a usable key. Checked on the body, not the whole string, so PEM armor and
    // newline style do not affect the threshold.
    const base64Body = privateKey
      .replace(/-----[A-Z0-9 ]+-----/g, '')
      .replace(/\\n/g, '')
      .replace(/\s/g, '');
    const MIN_BASE64_BODY_CHARS = 500;
    if (base64Body.length < MIN_BASE64_BODY_CHARS) {
      throw new Error(
        `SNOWFLAKE_PRIVATE_KEY has only ${base64Body.length} base64 characters between its PEM ` +
          `markers; a real RSA key has roughly 1600. The value looks like a placeholder or a ` +
          `partial paste — copy the complete private key file.`
      );
    }

    // An encrypted key without its passphrase is unusable, and the failure lands far
    // from the cause: the sync succeeds, the secret looks well-formed, and the only
    // symptom is a decrypt error in the connector's CloudWatch logs at query time.
    // Catch the mismatch here instead, in both directions.
    const isEncrypted = privateKey.includes('-----BEGIN ENCRYPTED PRIVATE KEY-----');
    if (isEncrypted && passphrase === undefined) {
      throw new Error(
        'SNOWFLAKE_PRIVATE_KEY is an ENCRYPTED private key but SNOWFLAKE_PRIVATE_KEY_PASSPHRASE ' +
          'is not set, so the connector cannot decrypt it. Set the passphrase, or generate an ' +
          'unencrypted key (`openssl pkcs8 -topk8 -nocrypt`) and register its public half in ' +
          "Snowflake with ALTER USER … SET RSA_PUBLIC_KEY='…'."
      );
    }
    if (!isEncrypted && passphrase !== undefined) {
      throw new Error(
        'SNOWFLAKE_PRIVATE_KEY_PASSPHRASE is set but SNOWFLAKE_PRIVATE_KEY is not encrypted ' +
          '(its header reads "-----BEGIN PRIVATE KEY-----"). The connector would try to decrypt ' +
          'an unencrypted key and fail. Remove the passphrase, or supply the encrypted key.'
      );
    }

    const secret: Record<string, string> = {
      sfUser: user,
      pem_private_key: privateKey,
    };
    // Only include the passphrase key when the key is actually encrypted — the
    // connector treats an empty passphrase as a passphrase and fails to decrypt.
    if (passphrase !== undefined) {
      secret.pem_private_key_passphrase = passphrase;
    }
    return { mode: 'key-pair', secret };
  }

  // password is defined here: the both-unset and both-set cases already raised.
  return {
    mode: 'password',
    secret: {
      username: user,
      // Non-null assertion is safe: the two guards above prove password is set
      // whenever privateKey is not.
      password: password!,
    },
  };
}
