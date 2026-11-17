import {
  parseCasingArgs,
  functionNameFromCatalogParameter,
  functionNamesFromCatalogParameters,
  applyCasingMode,
} from '../scripts/set-snowflake-casing';

// Importing the script module must NOT execute anything: the module guards its entry
// point with `require.main === module`. If that guard is removed, this suite starts
// making real UpdateFunctionConfiguration calls and this file is the tripwire.
describe('set-snowflake-casing CLI parsing', () => {
  test('parses no arguments as all-defaults', () => {
    expect(parseCasingArgs([])).toEqual({});
  });

  test('accepts both the --flag=value and --flag value forms', () => {
    expect(parseCasingArgs(['--region=us-east-1', '--casing-mode=LOWER'])).toEqual({
      region: 'us-east-1',
      casingMode: 'LOWER',
    });
    expect(parseCasingArgs(['--region', 'us-east-1'])).toEqual({ region: 'us-east-1' });
  });

  test('tolerates a bare `--` end-of-options separator', () => {
    expect(parseCasingArgs(['--', '--region=us-east-1'])).toEqual({ region: 'us-east-1' });
  });

  test('rejects an unknown flag rather than ignoring it', () => {
    // A silently-ignored typo would report success while leaving casing_mode unset,
    // and the connector would keep answering "table not found".
    expect(() => parseCasingArgs(['--casingmode', 'UPPER'])).toThrow(/Unknown argument/);
  });

  test('rejects a flag whose value is missing', () => {
    expect(() => parseCasingArgs(['--region'])).toThrow(/requires a value/);
    expect(() => parseCasingArgs(['--region', '--casing-mode', 'UPPER'])).toThrow(
      /requires a value/
    );
  });
});

describe('functionNameFromCatalogParameter', () => {
  test('extracts the name from a full Lambda ARN', () => {
    expect(
      functionNameFromCatalogParameter(
        'arn:aws:lambda:us-east-1:123456789012:function:semantic-layer-snowflake-fhir-db-connector'
      )
    ).toBe('semantic-layer-snowflake-fhir-db-connector');
  });

  test('passes a bare function name through', () => {
    // Athena accepts either form in the catalog's `function` parameter.
    expect(functionNameFromCatalogParameter('my-connector')).toBe('my-connector');
  });

  test('rejects an empty parameter', () => {
    expect(() => functionNameFromCatalogParameter('   ')).toThrow(/empty `function` parameter/);
  });
});

describe('functionNamesFromCatalogParameters', () => {
  const arn = (name: string): string => `arn:aws:lambda:us-east-1:123456789012:function:${name}`;

  test('reads the metadata/record pair Athena actually stores', () => {
    // CDK registers ONE `function` parameter; Athena stores it back split in two. A
    // reader that only looks at `function` finds nothing on a deployed catalog — which
    // is exactly how this first failed live.
    expect(
      functionNamesFromCatalogParameters({
        catalog: 'snowflake_fhir_db',
        'metadata-function': arn('sf-connector'),
        'record-function': arn('sf-connector'),
      })
    ).toEqual(['sf-connector']);
  });

  test('returns both functions when the metadata and record paths differ', () => {
    expect(
      functionNamesFromCatalogParameters({
        'metadata-function': arn('meta-fn'),
        'record-function': arn('record-fn'),
      })
    ).toEqual(['meta-fn', 'record-fn']);
  });

  test('accepts the single `function` form used at registration time', () => {
    expect(functionNamesFromCatalogParameters({ function: arn('sf-connector') })).toEqual([
      'sf-connector',
    ]);
  });

  test('throws when no recognised key is present', () => {
    expect(() => functionNamesFromCatalogParameters({ catalog: 'x' })).toThrow(
      /no `function`, `metadata-function` or `record-function`/
    );
    expect(() => functionNamesFromCatalogParameters(undefined)).toThrow(/no `function`/);
  });
});

describe('applyCasingMode', () => {
  /** Minimal Lambda-client stub recording the commands it is sent. */
  function stubLambda(existing: Record<string, string> | undefined): {
    client: { send: (cmd: unknown) => Promise<unknown> };
    sent: unknown[];
  } {
    const sent: unknown[] = [];
    const client = {
      send: async (cmd: unknown): Promise<unknown> => {
        sent.push(cmd);
        // GetFunctionConfigurationCommand carries no Environment in its input.
        const input = (cmd as { input: Record<string, unknown> }).input;
        if (!('Environment' in input)) {
          return { Environment: existing === undefined ? undefined : { Variables: existing } };
        }
        return {};
      },
    };
    return { client, sent };
  }

  test('preserves the SAR-provided variables when adding casing_mode', async () => {
    // The real bug this guards: UpdateFunctionConfiguration REPLACES the variables map,
    // so a naive `{ casing_mode: 'UPPER' }` wipes the connection string and every query
    // fails with a missing-default error.
    const { client, sent } = stubLambda({
      default: 'snowflake://jdbc:snowflake://acct.snowflakecomputing.com/?db=FHIR_DB',
      spill_bucket: 'semantic-layer-snowflake-connector-spill',
    });

    const updated = await applyCasingMode(
      client as never,
      'semantic-layer-snowflake-fhir-db-connector',
      'UPPER'
    );

    expect(updated).toBe(true);
    const update = sent[1] as { input: { Environment: { Variables: Record<string, string> } } };
    expect(update.input.Environment.Variables).toEqual({
      default: 'snowflake://jdbc:snowflake://acct.snowflakecomputing.com/?db=FHIR_DB',
      spill_bucket: 'semantic-layer-snowflake-connector-spill',
      casing_mode: 'UPPER',
    });
  });

  test('sends no update when casing_mode is already correct', async () => {
    const { client, sent } = stubLambda({ casing_mode: 'UPPER', spill_bucket: 'b' });

    const updated = await applyCasingMode(client as never, 'fn', 'UPPER');

    expect(updated).toBe(false);
    // Only the read happened — re-sending an identical config would churn the function
    // version for nothing.
    expect(sent).toHaveLength(1);
  });

  test('handles a function with no environment variables at all', async () => {
    const { client, sent } = stubLambda(undefined);

    const updated = await applyCasingMode(client as never, 'fn', 'UPPER');

    expect(updated).toBe(true);
    const update = sent[1] as { input: { Environment: { Variables: Record<string, string> } } };
    expect(update.input.Environment.Variables).toEqual({ casing_mode: 'UPPER' });
  });
});
