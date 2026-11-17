import { parseArgs } from '../scripts/sync-snowflake-secret';

// Importing the script module must NOT execute the sync. The module guards its entry
// point with `require.main === module`; if that guard is ever removed, this suite
// starts making real PutSecretValue calls and this test is the tripwire.
describe('sync-snowflake-secret CLI parsing', () => {
  test('parses no arguments as all-defaults', () => {
    expect(parseArgs([])).toEqual({});
  });

  test('accepts the --flag=value form', () => {
    expect(parseArgs(['--env-path=/tmp/x.env', '--secret-name=a/b', '--region=us-east-1'])).toEqual(
      {
        envFile: '/tmp/x.env',
        secretName: 'a/b',
        region: 'us-east-1',
      }
    );
  });

  test('accepts the --flag value form', () => {
    expect(
      parseArgs(['--env-path', '/tmp/x.env', '--secret-name', 'a/b', '--region', 'us-east-1'])
    ).toEqual({ envFile: '/tmp/x.env', secretName: 'a/b', region: 'us-east-1' });
  });

  test('tolerates a bare `--` end-of-options separator', () => {
    expect(parseArgs(['--', '--region=us-east-1'])).toEqual({ region: 'us-east-1' });
  });

  test('rejects an unrecognized flag rather than ignoring it', () => {
    // Silently ignoring a typo would write the credential to the DEFAULT secret name
    // while the operator believed they had overridden it.
    expect(() => parseArgs(['--secrit-name', 'a/b'])).toThrow(/Unrecognized argument/);
  });

  test('rejects a flag whose value is missing', () => {
    expect(() => parseArgs(['--secret-name'])).toThrow(/requires a value/);
    expect(() => parseArgs(['--secret-name', '--region', 'us-east-1'])).toThrow(/requires a value/);
  });

  test('names the rename when given the old --env-file flag', () => {
    // Node 20 owns `--env-file` and ts-node forwards it to its node child, so in
    // practice node consumes it before argv reaches here. The explicit case exists so
    // any invocation path that DOES deliver it produces the actionable message.
    expect(() => parseArgs(['--env-file=/tmp/x.env'])).toThrow(/Use --env-path instead/);
  });
});
