/**
 * The publish gate's Athena grants on the REST API Lambda role.
 *
 * Found by live QA, not by any test: with the /rules router mounted and every
 * validation level green, `POST /rules/{id}:publish` came back 422 with
 *   "smoke execution of the compiled rule failed: An error occurred
 *    (AccessDeniedException) when calling the StartQueryExecution operation"
 * The gate behaved correctly — it fails closed and passes Athena's reason
 * through — but the effect is that NO evaluable rule can ever be published from
 * the admin UI. Every rule would fall to the advisory branch or be refused, and
 * the refusal reads like an Ontop/SQL problem rather than a missing permission.
 *
 * Two halves, both required:
 *   1. Query-EXECUTION actions. The role already had the Athena METADATA reads
 *      (the data-source picker needs them) and Glue + lakeformation:GetDataAccess
 *      + LF-admin registration, which is why validation passed and only the smoke
 *      step failed.
 *   2. `ATHENA_WORKGROUP`. `_execute_athena_sql` defaults to the literal
 *      'primary' workgroup, so grants scoped to the project workgroup would not
 *      have helped: the query would have been submitted somewhere else, with a
 *      results location nobody granted. The env var and the grant have to name
 *      the same workgroup.
 *
 * Source-text assertions, for the same reason as governed-rules-flag.test.ts and
 * lambda-api-connector-grants.test.ts: LambdaRestApiStack cannot be synthesized
 * hermetically (it Docker-bundles the FastAPI image and needs many fully-built
 * dependency stacks).
 */

import * as fs from 'fs';
import * as path from 'path';

describe('rule publish gate: Athena query execution', () => {
  const stackSrc = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/lambda-rest-api/index.ts'),
    'utf8'
  );
  const mainPy = fs.readFileSync(path.join(__dirname, '../../lambda/rest-api/main.py'), 'utf8');

  test('the role can run a query, not just read metadata', () => {
    for (const action of [
      'athena:StartQueryExecution',
      'athena:GetQueryExecution',
      'athena:StopQueryExecution',
    ]) {
      expect(stackSrc).toContain(action);
    }
  });

  test('the query-execution grant is scoped to the project workgroup', () => {
    // Not `workgroup/*`: the smoke query is the only query this Lambda runs, and
    // it runs in the project workgroup whose result location is granted below.
    expect(stackSrc).toContain('athena:${this.region}:${this.account}:workgroup/${workgroupName}');
  });

  test('ATHENA_WORKGROUP names the same workgroup the grant scopes to', () => {
    // main.py falls back to the literal 'primary' — a workgroup this deployment
    // does not own and did not grant. Without the env var the fix is inert.
    expect(mainPy).toContain("os.environ.get(\"ATHENA_WORKGROUP\", \"primary\")");
    expect(stackSrc).toContain('ATHENA_WORKGROUP: workgroupName,');
  });

  test('the Athena results bucket is writable', () => {
    // The workgroup sets enforceWorkGroupConfiguration, so results land in the
    // workgroup's own location regardless of what the client asks for. Athena
    // writes them as the CALLING principal, so a missing grant surfaces as a
    // FAILED query ("Access Denied writing to output location"), not as an
    // AccessDenied on StartQueryExecution — a different, later, less obvious
    // failure than the one live QA hit.
    expect(stackSrc).toContain('athenaResultsBucket?: s3.IBucket;');
    expect(stackSrc).toContain('props.athenaResultsBucket.grantReadWrite(lambdaRole)');
  });

  test('federated connector spill buckets are readable', () => {
    // A LIMIT 1 smoke query READS RECORDS, unlike the data-source picker, which
    // only reads metadata (see the connector-invoke comment nearby). Record reads
    // spill to S3 and the CALLER reads them back, so the picker's invoke-only
    // grant is not sufficient for the gate.
    expect(stackSrc).toContain('props.athenaStack.spillBucket.grantReadWrite(lambdaRole)');
    expect(stackSrc).toContain(
      'props.athenaStack.spillEncryptionKey.grantEncryptDecrypt(lambdaRole)'
    );
    expect(stackSrc).toContain('props.athenaStack.snowflakeSpillBucket.grantReadWrite(lambdaRole)');
  });

  test('app.ts wires both new inputs into the REST API stack', () => {
    const appSrc = fs.readFileSync(path.join(__dirname, '../bin/app.ts'), 'utf8');
    const start = appSrc.indexOf('new LambdaRestApiStack(');
    expect(start).toBeGreaterThan(-1);
    const end = appSrc.indexOf('\n});', start);
    expect(end).toBeGreaterThan(start);
    const propsBlock = appSrc.slice(start, end);
    expect(propsBlock).toContain('athenaStack');
    expect(propsBlock).toContain('athenaResultsBucket');
  });
});
