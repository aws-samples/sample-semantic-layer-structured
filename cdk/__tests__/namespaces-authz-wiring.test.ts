/**
 * Deploy-time wiring for namespaces, Cedar authorization and the SQL firewall.
 *
 * Every assertion here corresponds to a failure that unit tests of the Python
 * code cannot see, because the code is correct and the *infrastructure* is what
 * disagrees with it. Two of them are lockouts:
 *
 *   - `AUTHZ_GROUP_ROLE_MAP` emitted as the literal `'{}'` instead of `''`.
 *     role_resolver falls back to its built-in map (`Admin` → `platform-admin`)
 *     only when the value is blank; `'{}'` parses to "no group grants anything",
 *     so a freshly-seeded deployment has no principal who can write the first
 *     grant. This was a real bug in an earlier revision of the stack.
 *   - `enableAuthz` defaulting on. Unlike every other feature flag in this
 *     project, this one can deny the operator access to the API that would fix
 *     it, so it must stay off until the seeded grants have been verified live.
 *
 * The rest are silent-mismatch traps: a prefix that disagrees between the env var
 * and the IAM scope, a namespace id that disagrees between the function and the
 * bootstrap that seeds it, and a hand-rolled DynamoDB statement that omits the
 * GSI every authorized request queries.
 *
 * DynamoDBStack is synthesized because it is hermetic. LambdaRestApiStack and
 * AgentCoreStack are asserted at source level for the same reason as
 * rules-publish-athena-grants.test.ts: they Docker-bundle images and require
 * many fully-built dependency stacks.
 */

import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';
import { DynamoDBStack } from '../lib/stacks/backend/dynamodb-stack';

const restApiSrc = fs.readFileSync(
  path.join(__dirname, '../lib/stacks/backend/lambda-rest-api/index.ts'),
  'utf8'
);
const agentcoreSrc = fs.readFileSync(
  path.join(__dirname, '../lib/stacks/backend/agentcore-stack.ts'),
  'utf8'
);
const appSrc = fs.readFileSync(path.join(__dirname, '../bin/app.ts'), 'utf8');
const cdkJson = JSON.parse(fs.readFileSync(path.join(__dirname, '../cdk.json'), 'utf8'));

describe('namespaces + grants tables', () => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const stack = new DynamoDBStack(app, 'AuthzTablesTestStack', {
    projectName: 'semantic-layer',
    loadSyntheticData: false,
  });
  const template = Template.fromStack(stack);

  test('the grants table carries the PrincipalIndex GSI', () => {
    // "What can this principal do" is the query every authorized request runs.
    // The base table only answers "who has access to this namespace".
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'semantic-layer-grants',
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'PrincipalIndex',
          KeySchema: [
            { AttributeName: 'principalKey', KeyType: 'HASH' },
            { AttributeName: 'sk', KeyType: 'RANGE' },
          ],
          // ALL, not KEYS_ONLY: that read also needs the firewall's restriction
          // attributes, so a projection miss turns one Query into a Query plus N
          // BatchGetItems on the hot path of every request.
          Projection: { ProjectionType: 'ALL' },
        }),
      ]),
    });
  });

  test('the namespaces table has a TTL attribute for name reservations', () => {
    // The reservation TTL is what frees a name when a create dies between
    // claiming it and writing the metadata row. Without it an abandoned create
    // burns the name permanently.
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'semantic-layer-namespaces',
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  test('neither authz table is RETAIN', () => {
    // RETAIN is what made the metrics table survive `cdk destroy` and then
    // collide with the next create, stranding the whole deploy. The bootstrap
    // custom resource re-seeds both tables, so a rebuild recovers on its own.
    for (const tableName of ['semantic-layer-namespaces', 'semantic-layer-grants']) {
      template.hasResource('AWS::DynamoDB::Table', {
        Properties: Match.objectLike({ TableName: tableName }),
        DeletionPolicy: 'Delete',
      });
    }
  });
});

describe('REST API authorization environment', () => {
  test('AUTHZ_GROUP_ROLE_MAP is blank, not "{}", when no override is supplied', () => {
    // The lockout. `JSON.stringify(props.authzGroupRoleMap ?? {})` yields '{}',
    // which role_resolver reads as "every group grants nothing".
    expect(restApiSrc).toContain(`AUTHZ_GROUP_ROLE_MAP: props.authzGroupRoleMap
          ? JSON.stringify(props.authzGroupRoleMap)
          : '',`);
    expect(restApiSrc).not.toContain('JSON.stringify(props.authzGroupRoleMap ?? {})');
  });

  test('the resolver still falls back to a built-in map on a blank value', () => {
    // The stack side above is only safe because of this fallback; if the
    // resolver stopped defaulting, a blank value would become the lockout.
    const resolverSrc = fs.readFileSync(
      path.join(__dirname, '../../lambda/rest-api/authz/role_resolver.py'),
      'utf8'
    );
    expect(resolverSrc).toContain('return dict(_DEFAULT_GROUP_ROLE_MAP)');
    expect(resolverSrc).toContain("'Admin': Role.PLATFORM_ADMIN.value");
  });

  test('RESOURCE_PREFIX names the same prefix the workgroup grant scopes to', () => {
    // namespace_service builds `<RESOURCE_PREFIX>-ns-<name>`. A grant scoped to a
    // different prefix is an AccessDenied on every namespace create, which reads
    // like the grant was never added.
    expect(restApiSrc).toContain('RESOURCE_PREFIX: projectName,');
    expect(restApiSrc).toContain(
      'athena:${this.region}:${this.account}:workgroup/${projectName}-ns-*'
    );
  });

  test('the workgroup grant covers create, read and tag', () => {
    for (const action of ['athena:CreateWorkGroup', 'athena:GetWorkGroup', 'athena:TagResource']) {
      expect(restApiSrc).toContain(action);
    }
  });

  test('installing the middleware and enforcing it are separate switches', () => {
    // NAMESPACES_TABLE mounts /namespaces and installs the PDP; ENABLE_AUTHZ is
    // what makes a decision deny. Collapsing them would arm enforcement in the
    // same deploy that creates an empty grants table.
    expect(restApiSrc).toContain("NAMESPACES_TABLE: props.namespacesTable?.tableName ?? ''");
    expect(restApiSrc).toContain('ENABLE_AUTHZ: String(props.enableAuthz === true)');
  });

  test('both authz tables are granted through grantReadWriteData', () => {
    // Not a hand-rolled PolicyStatement: CDK's grant includes
    // `<tableArn>/index/*`, and role resolution queries the PrincipalIndex GSI,
    // not the base table. A statement naming only the table ARN passes every
    // unit test and then fails on every authorized request.
    expect(restApiSrc).toContain('props.namespacesTable.grantReadWriteData(lambdaRole)');
    expect(restApiSrc).toContain('props.grantsTable.grantReadWriteData(lambdaRole)');
  });
});

describe('authorization bootstrap custom resource', () => {
  const handlerSrc = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/authz-bootstrap/index.py'),
    'utf8'
  );

  test('the function env and the bootstrap are seeded with the same namespace id', () => {
    // DEFAULT_NAMESPACE_ID is baked into the function; if the bootstrap seeded a
    // different id, every pre-namespace ontology would authorize against a
    // namespace that does not exist.
    expect(restApiSrc).toContain("const DEFAULT_NAMESPACE_ID = 'default'");
    expect(restApiSrc).toContain('DefaultNamespaceId: DEFAULT_NAMESPACE_ID,');
    expect(restApiSrc).toContain(
      "DEFAULT_NAMESPACE_ID: props.namespacesTable ? DEFAULT_NAMESPACE_ID : '',"
    );
  });

  test('the API does not start serving before the default namespace exists', () => {
    expect(restApiSrc).toContain('this.restApiFunction.node.addDependency(bootstrap)');
  });

  test('the bootstrap can write all three tables it seeds', () => {
    for (const grant of [
      'props.namespacesTable.grantReadWriteData(bootstrapHandler)',
      'props.grantsTable.grantReadWriteData(bootstrapHandler)',
      'props.ontologyMetadataTable.grantReadWriteData(bootstrapHandler)',
    ]) {
      expect(restApiSrc).toContain(grant);
    }
  });

  test('the bootstrap provisions the workgroup under the same prefix scope', () => {
    // It runs the identical create-or-adopt path as the API, so a narrower scope
    // here fails the deploy rather than a request.
    const bootstrapBlock = restApiSrc.slice(restApiSrc.indexOf('AuthzBootstrapHandler'));
    expect(bootstrapBlock).toContain(
      'athena:${this.region}:${this.account}:workgroup/${projectName}-ns-*'
    );
  });

  test('the handler needs no bundled dependencies', () => {
    // `Code.fromAsset` without bundling only works because the handler imports
    // boto3 alone; a pydantic import would make synth silently ship a broken zip.
    expect(handlerSrc).not.toMatch(/^import pydantic/m);
    expect(handlerSrc).not.toMatch(/^from pydantic/m);
    expect(restApiSrc).toContain(
      "lambda.Code.fromAsset(path.join(__dirname, '../authz-bootstrap'))"
    );
  });

  test('delete does not remove grants', () => {
    // A rollback arrives as a Delete for the just-created resource — the worst
    // possible moment to strip the administrator access needed to diagnose it.
    expect(handlerSrc).toContain('authz_bootstrap_delete_noop');
  });
});

describe('agent runtimes read grants', () => {
  test('the three query runtimes are given GRANTS_TABLE', () => {
    // The firewall's table/column restrictions come from the grants table. A
    // runtime without it silently applies no restrictions — the firewall still
    // blocks non-read-only SQL, so the downgrade is invisible in testing.
    const occurrences = agentcoreSrc.match(/\.\.\.grantsEnv,/g) ?? [];
    expect(occurrences).toHaveLength(3);
    expect(agentcoreSrc).toContain('GRANTS_TABLE: props.dynamodbStack.grantsTable.tableName');
  });

  test('each of those runtime roles can query the grants GSI', () => {
    const occurrences = agentcoreSrc.match(/grantsTable\.grantReadData\(/g) ?? [];
    expect(occurrences).toHaveLength(3);
  });
});

describe('enforcement flag', () => {
  test('enableAuthz defaults off in source', () => {
    expect(appSrc).toContain("const _authzCtx = app.node.tryGetContext('enableAuthz')");
    expect(appSrc).toContain("const enableAuthz = _authzCtx === true || _authzCtx === 'true'");
  });

  test('enableAuthz is not turned on in cdk.json', () => {
    // Deliberately unlike enableGovernedRules / enableSnowflakeConnector: a bare
    // `cdk deploy --all` must land the tables and the seeding WITHOUT arming
    // enforcement, so the operator can verify their grant resolves first.
    expect(cdkJson.context?.enableAuthz).not.toBe(true);
    expect(cdkJson.context?.enableAuthz).not.toBe('true');
  });

  test('the tables are wired regardless of the flag', () => {
    // The registry, /namespaces and the bootstrap must deploy on the first pass;
    // only the deny decision waits for the flag.
    expect(appSrc).toContain('namespacesTable: dynamodbStack.namespacesTable,');
    expect(appSrc).toContain('grantsTable: dynamodbStack.grantsTable,');
  });
});
