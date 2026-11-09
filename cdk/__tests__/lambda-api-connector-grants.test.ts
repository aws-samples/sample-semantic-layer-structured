/**
 * LambdaRestApiStack → Athena connector-catalog grants.
 *
 * The admin data-source picker enumerates LAMBDA-type connector catalogs (Snowflake) through
 * the Athena metadata APIs, because such a catalog is not a Glue object and glue:GetCatalogs
 * can never see it. Two distinct grants make that work, and they fail in different ways:
 *
 *  - The Athena metadata actions, WITHOUT which the picker 403s on discovery.
 *  - lambda:InvokeFunction on the connector, WITHOUT which the metadata call fails and the
 *    picker silently renders a Glue-only list — no error reaches the user, the Snowflake
 *    databases simply never appear. That silent-wrong mode is why this is pinned.
 *
 * Asserted on source text rather than a synthesized template for the same reason as
 * agentcore-snowflake-grant.test.ts: LambdaRestApiStack cannot be synthesized hermetically
 * (it Docker-bundles the FastAPI image and requires many fully-built dependency stacks).
 */

import * as fs from 'fs';
import * as path from 'path';

describe('LambdaRestApiStack → Athena connector-catalog grants', () => {
  const src = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/lambda-rest-api/index.ts'),
    'utf8'
  );
  const appSrc = fs.readFileSync(path.join(__dirname, '../bin/app.ts'), 'utf8');

  /** The guarded block that grants invoke on the connector Lambdas. */
  const GUARD = 'props.athenaStack.snowflakeConnectorFunctionNames.length > 0';

  test('grants the six Athena metadata actions the picker calls', () => {
    for (const action of [
      'athena:ListDataCatalogs', // discovery — which connectors exist
      'athena:GetDataCatalog',
      'athena:ListDatabases', // schemas within a connector
      'athena:GetDatabase',
      'athena:ListTableMetadata', // tables + columns in one call
      'athena:GetTableMetadata',
    ]) {
      expect(src).toContain(`'${action}'`);
    }
  });

  test('ListDataCatalogs is granted on `*`, not scoped to datacatalog/*', () => {
    // Regression guard for a silent-failure mode observed live: ListDataCatalogs enumerates
    // the account and names no catalog, so a `datacatalog/*` resource never matches it.
    // Athena then reports "no identity-based policy allows the athena:ListDataCatalogs
    // action" despite the action appearing in the policy — and the picker degrades to a
    // Glue-only list with nothing but a CloudWatch warning to show for it.
    expect(src).toMatch(/actions: \['athena:ListDataCatalogs'\],\s*\n\s*resources: \['\*'\],/);
  });

  test('the per-catalog metadata reads stay scoped to catalogs in this account', () => {
    const scopedIdx = src.indexOf("'athena:GetTableMetadata'");
    expect(scopedIdx).toBeGreaterThan(-1);
    const block = src.slice(scopedIdx, scopedIdx + 300);
    expect(block).toMatch(
      /resources: \[`arn:aws:athena:\$\{this\.region\}:\$\{this\.account\}:datacatalog\/\*`\]/
    );
  });

  test('the Athena metadata grant is unconditional, not gated on the Snowflake flag', () => {
    // ListDataCatalogs is how the picker discovers whether any connector exists at all.
    // With none deployed it returns just the built-ins; withholding the grant would turn a
    // flag-off deployment into a permission error on every page load. So the statement must
    // sit in the role's inline policy, not inside the guarded block.
    const guardIdx = src.indexOf(GUARD);
    const athenaIdx = src.indexOf("'athena:ListDataCatalogs'");
    expect(athenaIdx).toBeGreaterThan(-1);
    expect(guardIdx).toBeGreaterThan(-1);
    expect(athenaIdx).toBeLessThan(guardIdx);
  });

  test('connector invoke is derived from AthenaStack, never a hardcoded function name', () => {
    const block = src.slice(src.indexOf(GUARD), src.indexOf(GUARD) + 900);
    expect(block).toMatch(/actions: \['lambda:InvokeFunction'\]/);
    // A hardcoded name would silently omit every Snowflake database but the first.
    expect(block).toMatch(/resources: props\.athenaStack\.snowflakeConnectorFunctionNames\.map\(/);
    expect(block).not.toMatch(/-snowflake-connector`/);
  });

  test('the connector invoke grant is guarded so a flag-off deploy adds no policy', () => {
    expect(src).toMatch(
      /if \(props\.athenaStack && props\.athenaStack\.snowflakeConnectorFunctionNames\.length > 0\)/
    );
  });

  test('the spill grant exists for the publish gate, and is guarded', () => {
    // This assertion used to be `not.toMatch(/snowflakeSpillBucket/)`: when the picker
    // was the only caller, metadata came back inline and spill access would have been
    // unnecessary privilege. The governed-rule publish gate then added a `LIMIT 1` smoke
    // query, which READS RECORDS — and records spill to S3 for the caller to read back.
    // So the grant is now required, but it must stay behind the connector guard so a
    // flag-off deploy still adds no policy (snowflakeSpillBucket is undefined then).
    expect(src).toMatch(/props\.athenaStack\.snowflakeSpillBucket\.grantReadWrite\(lambdaRole\)/);
    expect(src).toMatch(/if \(props\.athenaStack\.snowflakeSpillBucket\)/);
  });

  test('app.ts passes athenaStack into LambdaRestApiStack', () => {
    // Without the prop the guard is permanently false and the grant silently never renders.
    const call = appSrc.slice(appSrc.indexOf('new LambdaRestApiStack('));
    expect(call.slice(0, 2000)).toMatch(/^\s*athenaStack,$/m);
    expect(appSrc).toMatch(/lambdaApiStack\.addDependency\(athenaStack\)/);
  });
});
