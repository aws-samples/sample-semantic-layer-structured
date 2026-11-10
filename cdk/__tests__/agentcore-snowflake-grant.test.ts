/**
 * Task 5: cross-stack IAM grants — VKG agents → Snowflake connector.
 *
 * Two roles reach the connector: `queryAgentRole` (reads at query time) and
 * `ontologyAgentRole` (introspects and samples during a build). Both are asserted here.
 *
 * Why a source-level guard instead of a synthesized-template assertion?
 *  - The grants live on `AgentCoreStack`, and AgentCoreStack cannot
 *    be synthesized hermetically: its constructor requires six fully-built
 *    dependency stacks (vpc, bedrockKbStack, glueCatalogStack, dynamodbStack,
 *    athenaStack, dataLakeStack), it Docker-bundles the benchmark-eval + cloud-
 *    resource-id Lambda handlers, and BedrockKnowledgeBaseStack stages the
 *    git-ignored `data/ontology-docs` directory (the known `CannotFindAsset`
 *    failure). Standing up a faithful synth harness here would be heavy and
 *    fragile for what is a three-line policy addition.
 *  - The AthenaStack Task 3b harness builds AthenaStack in isolation but NOT the
 *    agentcore stack, so it cannot exercise this cross-stack grant either.
 *
 * So we assert on the source text of agentcore-stack.ts — the same cheap
 * regression guard app-flags.test.ts uses for the app.ts → AthenaStack wiring.
 * This catches anyone deleting or un-guarding the Snowflake connector grant.
 */

import * as fs from 'fs';
import * as path from 'path';

describe('AgentCoreStack → Snowflake connector grants (Task 5)', () => {
  const src = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/agentcore-stack.ts'),
    'utf8'
  );

  /** Source-text marker that opens a guarded Snowflake grant block. */
  const GUARD = 'props.athenaStack.snowflakeConnectorFunctionNames.length > 0';

  /**
   * Slice the guarded block belonging to one role.
   *
   * There is deliberately MORE than one guarded block: the query agent reads through
   * the connector, and the ontology agent introspects and samples through it during a
   * build. An earlier version of this test sliced from `src.indexOf(GUARD)` and so
   * silently asserted against whichever block happened to appear first in the file —
   * which broke the moment the ontology block was added ABOVE the query one, even
   * though both grants were correct. Anchor on the role instead of on file order.
   */
  const blockFor = (role: string): string => {
    const roleCall = `this.${role}.addToPolicy`;
    // Walk every guard occurrence and keep the one whose body drives `role`.
    for (let i = src.indexOf(GUARD); i !== -1; i = src.indexOf(GUARD, i + 1)) {
      const candidate = src.slice(i, i + 1800);
      if (candidate.includes(roleCall)) return candidate;
    }
    throw new Error(`No guarded Snowflake grant block found for ${role}`);
  };

  /** Assertions that must hold for EVERY role granted access to the connector. */
  const assertCommonGrants = (block: string, role: string): void => {
    // Both deployment predicates are AND-ed together in the guard.
    expect(block).toMatch(/props\.athenaStack\.snowflakeSpillBucket\n?\s*\)/);

    // The spill bucket uses SSE-S3 (bucket-managed), so there is no KMS key to
    // guard on or grant decrypt against — assert the old KMS wiring is gone.
    expect(block).not.toMatch(/snowflakeSpillEncryptionKey/);
    expect(block).not.toMatch(/grantEncryptDecrypt/);

    // Invoke permission derived from AthenaStack's function-name list, NOT a hardcoded
    // name: with one connector per Snowflake database, a hardcoded name silently omits
    // every database but the first.
    expect(block).toMatch(/actions: \['lambda:InvokeFunction'\]/);
    expect(block).toMatch(/resources: props\.athenaStack\.snowflakeConnectorFunctionNames\.map\(/);
    expect(block).not.toMatch(/-snowflake-connector`/);

    // Spill read/write on the connector's spill bucket, for THIS role.
    expect(block).toContain(
      `props.athenaStack.snowflakeSpillBucket.grantReadWrite(this.${role})`
    );
  };

  test('queryAgentRole: invoke + spill + catalog resolution, guarded on deployment', () => {
    const block = blockFor('queryAgentRole');
    assertCommonGrants(block, 'queryAgentRole');

    // Catalog resolution happens before any Lambda invoke; without it the first agent
    // query fails as CATALOG_NOT_FOUND. Granted inside the guard for this role.
    expect(block).toMatch(/'athena:GetDataCatalog'/);
  });

  test('ontologyAgentRole: invoke + spill, so a build can introspect through the connector', () => {
    // Phase 1 reads a federated table via `SELECT * ... LIMIT 0` and sample_table_data
    // runs as this role, so a build needs the same invoke/spill pair. Without it the
    // ontology is written from the LLM's domain knowledge and the build STILL reports
    // `completed` — a silent-wrong outcome, hence a guard here.
    const block = blockFor('ontologyAgentRole');
    assertCommonGrants(block, 'ontologyAgentRole');
  });

  test('ontologyAgentRole gets athena:GetDataCatalog unconditionally, not only when Snowflake is on', () => {
    // This role's catalog-metadata grant sits OUTSIDE the Snowflake guard because the
    // DynamoDB connector needs it too, so it must not regress into the guarded block.
    // Scoped to datacatalog/*, which covers the Snowflake catalog as well.
    expect(src).toMatch(
      /this\.ontologyAgentRole\.addToPolicy\([\s\S]{0,400}?'athena:GetDataCatalog'/
    );
  });

  test('the Snowflake grants never touch the metadata (RAG) role', () => {
    // VKG-first: this task must not touch metadataAgentRole.
    for (const role of ['queryAgentRole', 'ontologyAgentRole']) {
      expect(blockFor(role)).not.toMatch(/metadataAgentRole/);
    }
  });
});
