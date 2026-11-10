import { buildSnowflakeSecretPayload } from '../scripts/snowflake-secret-payload';

// A real RSA-2048 PKCS#8 key carries ~1600 base64 characters, and the builder enforces
// a 500-char floor, so the fixture body has to be long enough to clear it. Repeating a
// fixed 12-char block keeps it deterministic and obviously non-secret.
const PEM_BODY = 'MIIEvgIBADAN'.repeat(50);
const PEM = `-----BEGIN PRIVATE KEY-----\n${PEM_BODY}\n-----END PRIVATE KEY-----`;
// The encrypted variant. A passphrase is only valid alongside THIS header, so any test
// that supplies one has to use it.
const PEM_ENCRYPTED = `-----BEGIN ENCRYPTED PRIVATE KEY-----\n${PEM_BODY}\n-----END ENCRYPTED PRIVATE KEY-----`;

describe('buildSnowflakeSecretPayload — key-pair mode', () => {
  test('produces the connector`s key-pair JSON shape', () => {
    const { mode, secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'ATHENA_CONNECTOR',
      SNOWFLAKE_PRIVATE_KEY: PEM,
    });
    expect(mode).toBe('key-pair');
    // Exact key set matters: the connector looks up these names verbatim.
    expect(secret).toEqual({ sfUser: 'ATHENA_CONNECTOR', pem_private_key: PEM });
  });

  test('includes the passphrase only when one is actually set', () => {
    const { secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'ATHENA_CONNECTOR',
      SNOWFLAKE_PRIVATE_KEY: PEM_ENCRYPTED,
      SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: 's3cret',
    });
    expect(secret.pem_private_key_passphrase).toBe('s3cret');
  });

  test('rejects an ENCRYPTED key with no passphrase', () => {
    // The combination that actually occurred: `openssl genrsa`-derived keys are encrypted
    // by default, so the passphrase is easy to forget. Without this guard the sync
    // succeeds and the only symptom is a decrypt error at query time, in the connector's
    // logs rather than at the point of the mistake.
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: PEM_ENCRYPTED,
      })
    ).toThrow(/ENCRYPTED private key but SNOWFLAKE_PRIVATE_KEY_PASSPHRASE is not set/);
  });

  test('rejects a passphrase supplied for an unencrypted key', () => {
    // The mirror-image mistake: a leftover passphrase from a previous key. The connector
    // would try to decrypt a plaintext key and fail just as opaquely.
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: PEM,
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: 'left-over',
      })
    ).toThrow(/PASSPHRASE is set but SNOWFLAKE_PRIVATE_KEY is not encrypted/);
  });

  test('omits the passphrase key entirely when the value is empty or whitespace', () => {
    // The connector reads a blank passphrase as a real passphrase and then fails to
    // decrypt an unencrypted key, so the key must be absent — not present-and-empty.
    for (const blank of ['', '   ']) {
      const { secret } = buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'ATHENA_CONNECTOR',
        SNOWFLAKE_PRIVATE_KEY: PEM,
        SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: blank,
      });
      expect('pem_private_key_passphrase' in secret).toBe(false);
    }
  });

  test('preserves the PEM newlines byte-for-byte', () => {
    const { secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'u',
      SNOWFLAKE_PRIVATE_KEY: PEM,
    });
    expect(secret.pem_private_key).toBe(PEM);
    expect(secret.pem_private_key.split('\n')).toHaveLength(3);
  });

  test('rejects a private key with no PEM header', () => {
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: 'MIIEvgIBADAN',
      })
    ).toThrow(/PEM header/);
  });

  test('rejects a PUBLIC key pasted into SNOWFLAKE_PRIVATE_KEY', () => {
    // The public half is what gets registered in Snowflake, so it is the value most
    // likely to be on the operator's clipboard. It satisfies a naive `includes('-----BEGIN')`
    // check, which is exactly why this case is asserted separately.
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: `-----BEGIN PUBLIC KEY-----\n${PEM_BODY}\n-----END PUBLIC KEY-----`,
      })
    ).toThrow(/PUBLIC key/);
  });

  test('rejects a truncated key that is missing its PEM footer', () => {
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\n${PEM_BODY}`,
      })
    ).toThrow(/no PEM footer/);
  });

  test('rejects a well-formed but implausibly short key body', () => {
    // Correct markers, correct order, but only a handful of base64 characters — the
    // shape of a placeholder in a checked-in .env.example.
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: '-----BEGIN PRIVATE KEY-----\nAB\n-----END PRIVATE KEY-----',
      })
    ).toThrow(/base64 characters/);
  });

  test('accepts an ENCRYPTED PRIVATE KEY header', () => {
    // Key-pair auth with a passphrase produces this header; it must not be mistaken for
    // an invalid key by the PUBLIC-key guard.
    const { mode } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'u',
      SNOWFLAKE_PRIVATE_KEY: `-----BEGIN ENCRYPTED PRIVATE KEY-----\n${PEM_BODY}\n-----END ENCRYPTED PRIVATE KEY-----`,
      SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: 's3cret',
    });
    expect(mode).toBe('key-pair');
  });

  test('accepts a key whose newlines are still literal \\n escapes', () => {
    // `readEnvFile` decodes \\n inside double quotes, but a single-quoted or unquoted
    // value reaches here with literal backslash-n. The body-length check strips those so
    // the threshold measures base64 only.
    const { secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'u',
      SNOWFLAKE_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\\n${PEM_BODY}\\n-----END PRIVATE KEY-----`,
    });
    expect(secret.pem_private_key).toContain('\\n');
  });
});

describe('buildSnowflakeSecretPayload — password mode', () => {
  test('produces the connector`s password JSON shape', () => {
    const { mode, secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'ATHENA_CONNECTOR',
      SNOWFLAKE_PASSWORD: 'hunter2',
    });
    expect(mode).toBe('password');
    // Password mode uses `username`, NOT `sfUser` — different key name than key-pair.
    expect(secret).toEqual({ username: 'ATHENA_CONNECTOR', password: 'hunter2' });
  });

  test('a set passphrase does not leak into a password-mode payload', () => {
    const { secret } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'u',
      SNOWFLAKE_PASSWORD: 'hunter2',
      SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: 'stale-leftover',
    });
    expect(Object.keys(secret).sort()).toEqual(['password', 'username']);
  });
});

describe('buildSnowflakeSecretPayload — validation', () => {
  test('raises when SNOWFLAKE_USER is missing', () => {
    expect(() => buildSnowflakeSecretPayload({ SNOWFLAKE_PASSWORD: 'hunter2' })).toThrow(
      /SNOWFLAKE_USER is not set/
    );
  });

  test('raises when SNOWFLAKE_USER is whitespace-only', () => {
    expect(() =>
      buildSnowflakeSecretPayload({ SNOWFLAKE_USER: '  ', SNOWFLAKE_PASSWORD: 'hunter2' })
    ).toThrow(/SNOWFLAKE_USER is not set/);
  });

  test('raises when both credential keys are set — never guesses a mode', () => {
    expect(() =>
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: PEM,
        SNOWFLAKE_PASSWORD: 'hunter2',
      })
    ).toThrow(/Both SNOWFLAKE_PRIVATE_KEY and SNOWFLAKE_PASSWORD are set/);
  });

  test('raises when neither credential key is set', () => {
    expect(() => buildSnowflakeSecretPayload({ SNOWFLAKE_USER: 'u' })).toThrow(
      /Neither SNOWFLAKE_PRIVATE_KEY nor SNOWFLAKE_PASSWORD is set/
    );
  });

  test('treats blanked-out credential values as unset rather than as a mode choice', () => {
    // A leftover `SNOWFLAKE_PASSWORD=` line alongside a real key must not read as
    // "both modes set".
    const { mode } = buildSnowflakeSecretPayload({
      SNOWFLAKE_USER: 'u',
      SNOWFLAKE_PRIVATE_KEY: PEM,
      SNOWFLAKE_PASSWORD: '',
    });
    expect(mode).toBe('key-pair');
  });

  test('error messages name keys, never credential values', () => {
    let message = '';
    try {
      buildSnowflakeSecretPayload({
        SNOWFLAKE_USER: 'u',
        SNOWFLAKE_PRIVATE_KEY: PEM,
        SNOWFLAKE_PASSWORD: 'hunter2',
      });
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).not.toContain('hunter2');
    expect(message).not.toContain('MIIEvgIBADAN');
  });
});
