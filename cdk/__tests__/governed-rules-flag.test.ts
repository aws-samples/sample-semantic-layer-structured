/**
 * enableGovernedRules flag plumbing.
 *
 * The flag has to survive four hops to reach the Lambda: a default in cdk.json,
 * a context parse in app.ts, a prop on the REST API stack, and a String()-ed env
 * var on the function. A break in any hop is silent — the /rules router simply
 * never mounts and the admin UI shows an empty rule list, which is
 * indistinguishable from "this layer has no rules yet".
 *
 * Asserted on source text rather than a synthesized template, for the same
 * reason as lambda-api-connector-grants.test.ts: LambdaRestApiStack cannot be
 * synthesized hermetically (it Docker-bundles the FastAPI image and requires
 * many fully-built dependency stacks).
 */

import * as fs from 'fs';
import * as path from 'path';

describe('enableGovernedRules flag plumbing', () => {
  const cdkJson = JSON.parse(fs.readFileSync(path.join(__dirname, '../cdk.json'), 'utf8'));
  const appSrc = fs.readFileSync(path.join(__dirname, '../bin/app.ts'), 'utf8');
  const stackSrc = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/lambda-rest-api/index.ts'),
    'utf8'
  );

  test('defaults to true in cdk.json', () => {
    // Was opt-in until the authoring loop had been exercised on a real layer;
    // it now is, so the feature ships on by default and a bare `cdk deploy
    // --all` keeps both halves armed. Turning the flag off is the rollback (it
    // deletes nothing — see assets/guides/GOVERNED_RULES.md §10).
    expect(cdkJson.context.enableGovernedRules).toBe(true);
  });

  test('app.ts parses the context flag, accepting the CLI string form', () => {
    // `-c enableGovernedRules=true` arrives as the STRING "true"; comparing to
    // the boolean alone would silently ignore the CLI form.
    expect(appSrc).toContain("app.node.tryGetContext('enableGovernedRules')");
    expect(appSrc).toMatch(/const enableGovernedRules = \w+ === true \|\| \w+ === 'true';/);
  });

  test('app.ts passes the flag to the REST API stack', () => {
    const propsIdx = appSrc.indexOf('enableOboPassthrough,');
    expect(propsIdx).toBeGreaterThan(-1);
    // Both flags are REST-API-stack props; assert ours sits in that same block.
    expect(appSrc.slice(propsIdx, propsIdx + 600)).toContain('enableGovernedRules,');
  });

  test('the stack declares the prop', () => {
    expect(stackSrc).toContain('enableGovernedRules?: boolean;');
  });

  test('the stack renders RULES_ENABLED on the Lambda', () => {
    // `=== true` rather than a truthy check: an undefined prop must render
    // "false", never "undefined", which main.py would read as enabled-ish.
    expect(stackSrc).toContain('RULES_ENABLED: String(props.enableGovernedRules === true),');
  });
});

/**
 * The agent-runtime half of the same flag.
 *
 * One context flag, TWO deployments, and deliberately two different env-var
 * names: `RULES_ENABLED` gates the REST Lambda's /rules router (authoring), and
 * `GOVERNED_RULES_ENABLED` gates each query runtime's rule tier (execution). A
 * single shared name would let an API-only rollout silently arm the query path —
 * an admin could publish a rule and have it start answering user questions
 * before the query runtimes had been redeployed and smoke-tested.
 *
 * `GOVERNED_RULES_ENABLED` goes to the two runtimes whose layers build an
 * ontology graph — ontology-query (VKG) and semantic-layer-query (Hybrid) — and
 * to no others.
 *
 * Source-text assertions again, for the reason spelled out at length in
 * agentcore-snowflake-grant.test.ts: AgentCoreStack cannot be synthesized
 * hermetically (six fully-built dependency stacks, two Docker-bundled Lambda
 * handlers, and BedrockKnowledgeBaseStack staging the git-ignored
 * `data/ontology-docs` directory). A `Template.fromStack` assertion here would
 * be a harness, not a test.
 */
describe('enableGovernedRules reaches both graph-backed query runtimes', () => {
  const appSrc = fs.readFileSync(path.join(__dirname, '../bin/app.ts'), 'utf8');
  const agentcoreSrc = fs.readFileSync(
    path.join(__dirname, '../lib/stacks/backend/agentcore-stack.ts'),
    'utf8'
  );
  const requirements = fs.readFileSync(
    path.join(__dirname, '../../agents/requirements.txt'),
    'utf8'
  );

  /** The `ontologyQueryRuntimeEnv = { ... }` slice, bounded by the next env block. */
  const ontologyQueryEnvBlock = (): string => {
    const start = agentcoreSrc.indexOf('ontologyQueryRuntimeEnv = {');
    expect(start).toBeGreaterThan(-1);
    // The metadata-query block is assigned after it; anything past that boundary
    // belongs to a different runtime.
    const end = agentcoreSrc.indexOf('metadataQueryRuntimeEnv = {', start);
    expect(end).toBeGreaterThan(start);
    return agentcoreSrc.slice(start, end);
  };

  /** The `semanticLayerQueryRuntimeEnv = { ... }` slice (the Hybrid chat path). */
  const semanticLayerQueryEnvBlock = (): string => {
    const start = agentcoreSrc.indexOf('semanticLayerQueryRuntimeEnv = {');
    expect(start).toBeGreaterThan(-1);
    // The creation runtime's env block is assigned after it.
    const end = agentcoreSrc.indexOf('semanticLayerCreationRuntimeEnv = {', start);
    expect(end).toBeGreaterThan(start);
    return agentcoreSrc.slice(start, end);
  };

  test('the AgentCore stack declares the prop', () => {
    expect(agentcoreSrc).toContain('enableGovernedRules?: boolean;');
  });

  test('the ontology-query runtime env renders GOVERNED_RULES_ENABLED', () => {
    // `=== true`, matching the REST side: an undefined prop must render the
    // literal 'false'. `_rule_tier_answer` compares the value against
    // ('true', '1'), so 'undefined' would read as off — but only by luck, and
    // AgentCore rejects a non-string env value outright.
    expect(ontologyQueryEnvBlock()).toContain(
      'GOVERNED_RULES_ENABLED: String(props.enableGovernedRules === true),'
    );
  });

  test('the hybrid semantic-layer-query runtime env renders it too', () => {
    // Hybrid layers build a thin VKG carrying the same `vkg:mapsToTable` /
    // `vkg:mapsToColumn` mappings Ontop reformulates through, so the tier is
    // genuinely applicable there. Without this the runtime's rule tier would
    // read an unset flag and stay dark forever — and the symptom is a rule that
    // simply never matches, which looks exactly like a poor KNN score.
    expect(semanticLayerQueryEnvBlock()).toContain(
      'GOVERNED_RULES_ENABLED: String(props.enableGovernedRules === true),'
    );
  });

  test('no other runtime sees the flag', () => {
    // Only the two graph-backed query runtimes: a rule binds to an ontology, and
    // Semantic RAG builds none, so its runtimes have nothing to evaluate.
    // Exactly two env-var ASSIGNMENTS in the whole file proves it was not
    // copy-pasted into a sibling env block. Counted with the trailing colon so
    // the name appearing in prose (the prop's doc comment explains the two-name
    // split) is not miscounted as another runtime being armed.
    const assignments = agentcoreSrc.split('GOVERNED_RULES_ENABLED:').length - 1;
    expect(assignments).toBe(2);
  });

  test('app.ts passes the flag to the AgentCore stack', () => {
    const start = appSrc.indexOf('new AgentCoreStack(');
    expect(start).toBeGreaterThan(-1);
    // The props object ends at the stack's closing `});`.
    const end = appSrc.indexOf('\n});', start);
    expect(end).toBeGreaterThan(start);
    expect(appSrc.slice(start, end)).toContain('enableGovernedRules,');
  });

  test('both deployments derive from a single context parse', () => {
    // Two env-var names are intentional; two context parses are not. A second
    // parse could drift (a different default, a missed string form) and the two
    // halves would disagree with no error anywhere.
    const parses = appSrc.split("tryGetContext('enableGovernedRules')").length - 1;
    expect(parses).toBe(1);
  });

  test('the agent image pins pyshacl', () => {
    // rule_validator lives in agents/shared/ and the runtime container imports
    // it; a missing pin surfaces as an ImportError on the publish path only,
    // long after the deploy looked healthy.
    expect(requirements).toMatch(/^pyshacl==\d+\.\d+\.\d+/m);
  });
});
