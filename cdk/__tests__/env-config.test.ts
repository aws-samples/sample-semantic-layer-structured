import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  parseEnv,
  readEnvFile,
  snowflakeContextFromEnv,
  redact,
  SECRET_ENV_KEYS,
} from '../bin/env-config';

describe('parseEnv', () => {
  test('parses plain KEY=value pairs and trims surrounding whitespace', () => {
    const env = parseEnv('SNOWFLAKE_ACCOUNT=myorg-myacct\n  SNOWFLAKE_WAREHOUSE = MY_WH  \n');
    expect(env.SNOWFLAKE_ACCOUNT).toBe('myorg-myacct');
    expect(env.SNOWFLAKE_WAREHOUSE).toBe('MY_WH');
  });

  test('skips blank lines and comments, and strips an `export ` prefix', () => {
    const env = parseEnv(['# a comment', '', '   # indented comment', 'export FOO=bar'].join('\n'));
    expect(env).toEqual({ FOO: 'bar' });
  });

  test('keeps `=` characters that appear inside a value', () => {
    // Base64 and JDBC fragments routinely contain '='; only the FIRST '=' splits.
    const env = parseEnv('KEY=abc=def==');
    expect(env.KEY).toBe('abc=def==');
  });

  test('decodes \\n escapes inside double quotes so a PEM key fits on one line', () => {
    const env = parseEnv(
      'SNOWFLAKE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nMIIEvg\\n-----END PRIVATE KEY-----"'
    );
    expect(env.SNOWFLAKE_PRIVATE_KEY).toBe(
      '-----BEGIN PRIVATE KEY-----\nMIIEvg\n-----END PRIVATE KEY-----'
    );
    // Three real lines, i.e. the escapes became actual newlines.
    expect(env.SNOWFLAKE_PRIVATE_KEY.split('\n')).toHaveLength(3);
  });

  test('treats single-quoted values as literal (no escape decoding)', () => {
    const env = parseEnv("KEY='a\\nb'");
    expect(env.KEY).toBe('a\\nb');
    expect(env.KEY).not.toContain('\n');
  });

  test('does not turn an escaped backslash-n into a newline', () => {
    // "\\\\n" in TS source is the two characters \ + \ followed by n, i.e. the .env
    // text `\\n` — an escaped backslash then a literal 'n'. A naive chain of
    // .replace() calls would wrongly yield a newline here.
    const env = parseEnv('KEY="a\\\\nb"');
    expect(env.KEY).toBe('a\\nb');
    expect(env.KEY).not.toContain('\n');
  });

  test('preserves an unknown escape verbatim, backslash included', () => {
    const env = parseEnv('KEY="a\\qb"');
    expect(env.KEY).toBe('a\\qb');
  });

  test('ignores malformed lines that contain no `=`', () => {
    const env = parseEnv('this is not an assignment\nKEY=value');
    expect(env).toEqual({ KEY: 'value' });
  });
});

describe('readEnvFile', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'env-config-test-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns an empty map when the file does not exist', () => {
    // A missing .env is not an error: everything can be supplied via `-c` context.
    expect(readEnvFile(path.join(tmpDir, 'does-not-exist'))).toEqual({});
  });

  test('reads and parses an existing file', () => {
    const file = path.join(tmpDir, '.env');
    fs.writeFileSync(file, 'SNOWFLAKE_ACCOUNT=acct\nSNOWFLAKE_WAREHOUSE=wh\n');
    expect(readEnvFile(file)).toEqual({
      SNOWFLAKE_ACCOUNT: 'acct',
      SNOWFLAKE_WAREHOUSE: 'wh',
    });
  });

  test('throws when the path exists but cannot be read as a file', () => {
    // A directory at the .env path is a misconfiguration, not an absent optional file,
    // so it must fail loudly rather than degrade to {}.
    expect(() => readEnvFile(tmpDir)).toThrow(/Failed to read env file/);
  });
});

describe('snowflakeContextFromEnv', () => {
  test('maps every non-secret key to its CDK context name', () => {
    const ctx = snowflakeContextFromEnv({
      SNOWFLAKE_ACCOUNT: 'acct',
      SNOWFLAKE_WAREHOUSE: 'wh',
      SNOWFLAKE_DATABASE: 'db',
      SNOWFLAKE_ROLE: 'role',
      SNOWFLAKE_SECRET_NAME: 'secret/name',
    });
    expect(ctx).toEqual({
      snowflakeAccount: 'acct',
      snowflakeWarehouse: 'wh',
      snowflakeDatabase: 'db',
      snowflakeRole: 'role',
      snowflakeSecretName: 'secret/name',
    });
  });

  test('NEVER exposes a credential key — this is what keeps secrets out of templates', () => {
    const ctx = snowflakeContextFromEnv({
      SNOWFLAKE_ACCOUNT: 'acct',
      SNOWFLAKE_USER: 'ATHENA_CONNECTOR',
      SNOWFLAKE_PRIVATE_KEY: '-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----',
      SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: 'pass',
      SNOWFLAKE_PASSWORD: 'hunter2',
    });
    // Only the account survives; nothing else leaks into synth-visible context.
    expect(ctx).toEqual({ snowflakeAccount: 'acct' });
    const serialized = JSON.stringify(ctx);
    for (const secretKey of [...SECRET_ENV_KEYS, 'SNOWFLAKE_USER']) {
      expect(serialized).not.toContain(secretKey);
    }
    expect(serialized).not.toContain('hunter2');
    expect(serialized).not.toContain('BEGIN PRIVATE KEY');
  });

  test('omits absent keys entirely so a caller default can apply', () => {
    const ctx = snowflakeContextFromEnv({ SNOWFLAKE_ACCOUNT: 'acct' });
    expect(ctx).toEqual({ snowflakeAccount: 'acct' });
    expect('snowflakeDatabase' in ctx).toBe(false);
  });

  test('treats an empty or whitespace-only value as unset', () => {
    // An emptied-out line must not shadow a default with ''.
    const ctx = snowflakeContextFromEnv({ SNOWFLAKE_ACCOUNT: 'acct', SNOWFLAKE_DATABASE: '   ' });
    expect('snowflakeDatabase' in ctx).toBe(false);
  });
});

describe('redact', () => {
  test('reports length only, never any part of the value', () => {
    expect(redact('hunter2')).toBe('<redacted, 7 chars>');
    expect(redact('hunter2')).not.toContain('hunter');
  });

  test('distinguishes unset from empty-string', () => {
    expect(redact(undefined)).toBe('<unset>');
    expect(redact('')).toBe('<unset>');
  });
});
