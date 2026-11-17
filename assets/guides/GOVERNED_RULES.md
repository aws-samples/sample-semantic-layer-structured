# Governed Rules — operator guide

Ontology-native **conditional logic**: a `Rule` is the conditional sibling of a governed metric.
A metric is an aggregate ("total incurred loss"); a rule is a statement extracted from a policy
document ("if the moisture reading exceeds 18 % at intake, treat that as a candidate contributor
to spoilage"), with the conditions, the threshold, the outcome, and **how strongly** the source
document actually claimed it.

Flag-gated on `enableGovernedRules` (default `true` as of 2026-08-16). **VKG and Hybrid layers** —
a rule is RDF and the evaluation path goes through Ontop, which needs a graph carrying the
`vkg:mapsToTable` / `vkg:mapsToColumn` mappings. A Hybrid layer's thin VKG emits exactly those, so
the tier applies there too (added 2026-08-18). A Semantic-RAG layer builds no graph at all, so it
has nothing for a rule to bind to and neither its tab nor its runtime flag is armed.

Note the asymmetry inside a Hybrid layer: its own Tier 2 generates SQL directly and never touches
Ontop. The rule tier is the **one** place the hybrid query agent reformulates through Ontop.

- Design: `docs/plans/2026-07-28-ontology-native-governed-rules-design.md`
- Implementation plan: `docs/plans/2026-07-28-ontology-native-governed-rules.md`
- Hybrid extension: `docs/plans/2026-08-17-governed-rules-hybrid-design.md`
- Gateway/Ontop tool contract: this repo has no `docs/GATEWAY.md`; the authoritative sources are
  `lambda/neptune-tools/index.py` (the `execute_sparql_query` / `translate_sparql_to_sql` tool
  handlers and the `/rules` graph exclusion) and `assets/guides/MCP_SERVER.md`.

---

## 1. Two flags, two deployments

| Env var                  | Set by CDK on                                                        | Gates                                               |
| ------------------------ | -------------------------------------------------------------------- | --------------------------------------------------- |
| `RULES_ENABLED`          | REST API Lambda                                                      | The `/rules` router — authoring, review, publish    |
| `GOVERNED_RULES_ENABLED` | ontology-query runtime **and** semantic-layer-query (Hybrid) runtime | `_rule_tier_answer` — rule evaluation at query time |

`GOVERNED_RULES_ENABLED` reaches exactly those two runtimes and no others; a
`cdk/__tests__/governed-rules-flag.test.ts` case counts the assignments in `agentcore-stack.ts` so
a copy-paste into a sibling env block cannot arm a runtime that has no graph to evaluate against.

Both derive from the single `enableGovernedRules` CDK context flag, but the names are deliberately
different. One shared name would let an API-only rollout silently **arm the query path**: an admin
could publish a rule and have it start answering user questions before the query runtime had been
redeployed and smoke-tested. Deploy the API first, publish and review, then redeploy the runtime.

The `/rules` router also requires `NEPTUNE_GATEWAY_URL`. Without the gateway every call would
500; a clean 404 is the better failure, so the router simply does not mount and
`lambda/rest-api/main.py` logs which of the two conditions was missing.

## 2. Vocabulary (`vkgr:`)

Namespace: `https://semantic-layer.aws/virtual-kg/rules#` — declared once in
`agents/shared/rule_vocab.py`. **Changing it orphans every stored rule.**

| Term                                       | Kind      | Meaning                                                                                                                                           |
| ------------------------------------------ | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vkgr:Rule`                                | class     | One conditional statement                                                                                                                         |
| `vkgr:scope` → `vkgr:Scope`                | node      | `subjectClass` (+ optional `processStage`) the rule applies to                                                                                    |
| `vkgr:hasCondition`                        | node      | `binding` (property IRI), `operator`, `value` or `threshold` ref                                                                                  |
| `vkgr:conditionIndex`                      | literal   | Explicit order — RDF is unordered, so order is stored, not implied                                                                                |
| `vkgr:threshold`                           | node      | `value`, `unit`, verbatim `sourceSpan`, `approvedBy`                                                                                              |
| `vkgr:conclusion`                          | node      | `outcome` class IRI + `epistemicStatus`                                                                                                           |
| `vkgr:missingInput`                        | literal   | A binding the rule needs but the data does not supply                                                                                             |
| `vkgr:derivedFrom`                         | node      | `vkgr:Provenance`: `documentId`, `page`, `sourceSentence`, `modelId`, `confidence`                                                                |
| `vkgr:attestedBy` / `vkgr:attestationNote` | literal   | The named human attestation `confirmedCauseOf` requires                                                                                           |
| `vkgr:lifecycle`                           | literal   | `DRAFT` → `APPROVED` → `PUBLISHED`                                                                                                                |
| `vkgr:compiledSparql`                      | literal   | Written **only** at the publish transition                                                                                                        |
| `vkgr:hasTestCase`                         | node      | One of the five generated cases (`vkgr:testCaseIndex` orders them)                                                                                |
| `vkg:mapsToColumn`                         | predicate | Ontology-side: datatype property → `table.column`. Not `vkgr:` — it is shared with `agents/ontology_agent/enum_shapes.py` and must stay identical |

**Epistemic statuses**, weakest → strongest (a closed set; index order is a contract because the
extraction parser clamps **down** it and never up):

`associatedWith` · `requiresFurtherEvidence` · `candidateContributorTo` · `supportsHypothesis` ·
`indicatesRiskOf` · `confirmedCauseOf`

`candidateContributorTo` is the default a hedged or unclear sentence collapses to.
`confirmedCauseOf` requires a named human attestation (see §4, epistemic).

## 3. Named-graph layout

```
<GRAPH_URI_PREFIX><ontologyId>            ← the ontology (classes, properties, vkg:mapsToColumn)
<GRAPH_URI_PREFIX><ontologyId>/rules      ← every Rule for that layer
```

Built by `rule_graph.rules_graph_uri(namespace=...)`; the `/rules` suffix is
`rule_vocab.RULES_GRAPH_SUFFIX`.

Why a sibling graph rather than triples in the ontology graph: the Tier 2 Phase 3 slice builder
CONSTRUCTs over the ontology graph. Rule triples living there would be swept into every slice and
quietly inflate the token budget of **every** VKG query — a cost regression with no error anywhere.
Two independent places exclude the suffix, and both must keep doing so:

- `lambda/neptune-tools/index.py` — `_resolve_graph_uri` skips `/rules` graphs
- `services/neptune_service.py:resolve_ontology_graph_uri` — `not g.endswith('/rules')`

Storage notes:

- **No DynamoDB projection.** Neptune is the single source of truth. The KNN index is built by
  embedding rules at hydration time (`knn_hydration.hydrate_rules_namespace`), so there is no row
  to drift out of sync with the graph.
- Writes are **DELETE-then-INSERT in one update** (`write_rule`). Without the DELETE, an edited
  rule's blank-node conditions and thresholds accumulate and the rule reads back with its
  conditions duplicated.
- `read_rules` returns parsed **`Rule` models**, not dicts. Callers that want rows (the KNN
  hydrator) must `model_dump()` — see `_fetch_published_rules` in the query agent.
- **There are two readers, and the difference is deliberate** (added 2026-08-27). Every field
  `rule_to_graph` writes unconditionally is read back through `_required`, which raises
  `RuleReadError` naming the rule and the field. Previously a missing triple was read as
  `str(None)` → the literal string `"None"`, stored as if it were data — a threshold could ship
  with `unit="None"` and `source_span="None"`, and `source_span` carries `min_length=1` precisely
  to make an invented bound impossible.
  - `read_rules` / `graph_to_rules` stay **strict**: one unreadable rule raises. The migration
    scripts and the agent runtime use these, because for them skipping a rule is worse than
    raising — a migration that skips a rule leaves it unmigrated while reporting success.
  - `read_rules_partitioned` / `partition_rules` return `(rules, failures)`, isolating each
    unreadable rule. Only the list endpoint uses these, so one corrupt rule cannot hide every
    healthy one. The failures are **returned, not logged** — a rule that vanished from the list
    without a word is the silent fallback this codebase forbids.

  `RuleReadError` deliberately does **not** subclass `ValueError`, because the routers map
  `ValueError` → 422 and a corrupt stored rule is a server-side data fault, not bad client input.
  Answering 422 would tell the caller to fix a payload they never sent.
  `test_rule_read_error_is_not_a_value_error` pins this, since it is invisible until it matters.

- **Child-node links go through `_required` too**, not just literals. rdflib treats
  `graph.value(None, p)` as a **wildcard over subjects**, so reading a field off a node that turned
  out to be absent silently picks up SOME OTHER rule's value — a wrong answer rather than an error.

## 4. The four validation levels

`agents/shared/rule_validator.py`. **No LLM anywhere in the approval path** — these are gates, so
they are deterministic and they **fail closed**: an error inside a level reports `passed=false`
rather than being swallowed. That is the inverse of the query-time helpers, which fail soft.

| Level           | What it proves                                                                                     | Runs via              |
| --------------- | -------------------------------------------------------------------------------------------------- | --------------------- |
| **structural**  | The RDF conforms to `agents/shared/rule_shapes.ttl`                                                | pyshacl               |
| **semantic**    | Each binding's `rdfs:domain` is compatible with the scope class, and the outcome class is declared | SPARQL `ASK`          |
| **operational** | Each binding resolves through `vkg:mapsToColumn` to a real column                                  | SPARQL `SELECT`       |
| **epistemic**   | `confirmedCauseOf` carries a named human attestation                                               | pure model inspection |

Example failure message per level:

```
structural:   MinCountConstraintComponent: Rule must have at least one vkgr:condition
semantic:     binding https://…/moistureReading is not a property of https://…/IntakeBatch
operational:  binding https://…/moistureReading has no vkg:mapsToColumn mapping —
              the rule can advise but not evaluate until it is mapped
epistemic:    confirmedCauseOf requires vkgr:attestedBy — a named reviewer must stand
              behind the strongest epistemic claim
```

**The operational level is the one to understand.** An unmapped binding is _not_ an error: the
probe succeeded, so we **know** the mapping state, and that is a publishable outcome. The report
comes back `passed=true` with the unmapped bindings listed as violations, and they are recorded on
the rule as `missing_inputs`. The rule then publishes as **advisory-only** (§6). `passed=false`
happens only when the probe itself could not run — e.g. the gateway is unreachable — in which case
**every** binding is reported missing, because an unknown mapping state must never read as "mapped".

## 5. The publish gate

`agents/shared/rule_publisher.py:publish_rule`. Refuses anything whose lifecycle is not
`APPROVED` (`PublishBlocked`). Then, in order:

1. Re-run all four validations. Any `passed=false` blocks.
2. Generate the five test cases (`rule_test_cases.generate_test_cases`) — all five must be present.
3. **Evaluable** (no `missing_inputs`, every applied threshold `approved_by` set):
   deterministically compile SPARQL (`rule_compiler`), translate it through Ontop, and **execute**
   the `LIMIT 1` result on Athena. Only then does the lifecycle advance to `PUBLISHED` with
   `compiled_sparql` stored.
4. **Advisory** (any missing input or unapproved threshold): publish with `compiled_sparql`
   deliberately **empty**. The rule can advise; it will never be executed.

Point 3 is the whole reason the gate exists: a rule Ontop cannot reformulate is refused at
**authoring** time, in front of the reviewer who can fix it, rather than failing at query time in
front of a user.

> **Contract trap when you inject `execute_sql` yourself.** `publish_rule` treats the smoke query
> as failed only if the callable **raises**. `_run_athena_sql` (the agent-side helper) reports a
> FAILED query by **returning** a dict with `state_change_reason` set. Bridging the two without
> re-raising lets a rule whose smoke query failed publish anyway. The REST Lambda's own
> `_execute_athena_sql` raises, and so does the adapter in the deploy-gated test — copy one of
> those, do not hand `_run_athena_sql` straight in.

> **The caller needs Athena _execution_ rights, and this is not obvious.** Whichever principal
> runs the gate has to be able to `StartQueryExecution`/`GetQueryExecution` in the workgroup it
> submits to, write that workgroup's results location, and read the federated **spill** buckets —
> a `LIMIT 1` reads records, and records spill to S3 for the caller to read back, unlike the
> metadata-only calls the admin data-source picker makes. Live QA of the default-on rollout hit
> exactly this: the REST Lambda role had the Athena metadata reads, Glue, `GetDataAccess` and
> LF-admin, so all four validation levels passed and only step 3 failed, with
> `422 smoke execution of the compiled rule failed: … (AccessDeniedException) … StartQueryExecution`.
> That reads like an Ontop or SQL problem; it is an IAM problem. Fixed in
> `cdk/lib/stacks/backend/lambda-rest-api/index.ts`, pinned by
> `cdk/__tests__/rules-publish-athena-grants.test.ts`.
>
> The workgroup half is just as easy to get wrong: `_execute_athena_sql` falls back to the literal
> `'primary'` workgroup, so a grant scoped to `${projectName}-workgroup` is inert unless
> `ATHENA_WORKGROUP` is also set on the function. The env var and the grant must name the same
> workgroup — the stack derives both from one `workgroupName` const for that reason, and
> `rules-publish-athena-grants.test.ts` asserts the two still agree.
>
> Same trap on the **results bucket**: the workgroup sets `enforceWorkGroupConfiguration`, so
> results land in the workgroup's own location whatever the client requests, written as the
> _calling_ principal. The grant therefore has to be on `dataLakeStack.athenaResultsBucket` — the
> bucket `AthenaStack` put on the workgroup — not on the artifacts bucket `main.py` falls back to
> when `ATHENA_OUTPUT_LOCATION` is unset (it is unset, deliberately). A grant on the wrong bucket
> fails the query with "Access Denied writing to output location", not the API call.

### The five test cases

Deterministic, from the rule's own structure (`rule_test_cases.py`) — no LLM, so they are stable
and a reviewer can reason about them: `positive`, `negative`, `boundary`, `missing_data`,
`out_of_scope`. **The boundary case matters most**: it pins the comparison semantics at the
threshold value itself (does `>` fire _at_ 18 %?), which is where numeric rules silently go wrong.
The out-of-scope case uses a sentinel class IRI that deliberately does not exist in any ontology,
so it can never accidentally match production data.

## 6. Query path: evaluate vs advise

`_rule_tier_answer` in `agents/ontology_query_agent/main.py`, between Tier 1 (metrics) and Tier 2.
Metrics keep priority — a governed metric is both more specific and cheaper. Matching mirrors
Tier 1 exactly: embed the question, KNN over the namespace's PUBLISHED rules, accept the top hit
only at cosine ≥ **0.85**, and fall through to Tier 2 on any miss or error.

| Rule state                                           | Outcome      | `provenance.tier` | Monitoring bucket | Answer contains                                                                                       |
| ---------------------------------------------------- | ------------ | ----------------- | ----------------- | ----------------------------------------------------------------------------------------------------- |
| No `missing_inputs`, all applied thresholds approved | **execute**  | `governed_rule`   | **rule**          | Row count, epistemic status in words, each applied threshold + its approver, source document (+ page) |
| Any `missing_inputs`                                 | advise       | `advisory`        | advisory          | `knowledge_gap` naming the unmapped bindings                                                          |
| Any applied threshold without `approved_by`          | advise       | `advisory`        | advisory          | `knowledge_gap` naming the unapproved threshold ids                                                   |
| Flag off / KNN miss / any error                      | fall through | (Tier 2 decides)  | semantic          | —                                                                                                     |

The advisory branch **never guesses the missing bound**. It names exactly what is missing and
provenances the answer `advisory` with `sources = ["rule:<id>"]`, so the Monitoring tab shows
_which_ rule stalled without counting it as an answered rule.

The evaluable/advise predicate is `unapproved_threshold_ids`, **imported from `rule_executor`**
rather than re-derived in the caller. A caller that disagreed with the executor would either advise
forever or be refused and fall to Tier 2 forever — both silently.

**Known gap:** neither the metric tier nor the rule tier calls `emit_answer_span`, so a
rule-answered turn exports no final-answer span and the AgentCore SESSION judges score it as
_missing_ rather than wrong. Only Tier 2's in-graph terminals emit. Affects evaluation numbers
only, not answers.

## 7. Extraction (`ruleminer`)

A Step Functions stage in the document pipeline: `chunk → NER → embed → link → **ruleminer** →
index` (`lambda/doc-pipeline/ruleminer/handler.py`).

Ordering is load-bearing in both directions. Before `link` there are no `linkedClass` IRIs to bind
conditions against, so the miner would emit rules whose conditions point at nothing; after `index`
the rules would never reach the Knowledge Base. Neither failure raises anything — both just
produce useless rules — which is why `cdk/__tests__/doc-pipeline-ruleminer.test.ts` asserts the
stage order against the synthesized state machine.

**Two invariants** (design §5), both enforced in the parser and covered by
`tests/unit/test_ruleminer.py`:

1. **A threshold the document does not state is emitted as a `missing_input`, never as a number.**
   An invented bound is the one failure mode that looks like a working rule.
2. **Hedged language clamps DOWN the epistemic ordering, never up.** "May be associated with"
   becomes `candidateContributorTo`; nothing the model returns can promote a statement to
   `confirmedCauseOf`.

Mined rules are written to **S3** and imported as unapproved **DRAFTs** via
`POST /rules/namespaces/{ns}/rules:import` (`services/rule_import.py`). The Lambda does not write
to Neptune directly: the doc-pipeline asset bundles standalone, so it has neither `rule_graph.py`
nor gateway access. It vendors only `rule_vocab.py`'s constants.

## 8. Authoring and review

`/rules` router (`lambda/rest-api/routers/rules.py`), mounted at `/rules`:

| Method | Path                                                        | Purpose                              |
| ------ | ----------------------------------------------------------- | ------------------------------------ |
| POST   | `/namespaces/{ns}/rules`                                    | Create a DRAFT                       |
| POST   | `/namespaces/{ns}/rules:import`                             | Import mined artefacts as DRAFTs     |
| GET    | `/namespaces/{ns}/rules`                                    | List — an **envelope**, see below    |
| GET    | `/namespaces/{ns}/rules/{rule_id}`                          | Read one                             |
| PUT    | `/namespaces/{ns}/rules/{rule_id}`                          | Edit a DRAFT                         |
| POST   | `/namespaces/{ns}/rules/{rule_id}/thresholds/{tid}:approve` | Approve **one** threshold            |
| POST   | `/namespaces/{ns}/rules/{rule_id}:approve`                  | DRAFT → APPROVED                     |
| POST   | `/namespaces/{ns}/rules/{rule_id}:validate`                 | Run the four levels, no state change |
| POST   | `/namespaces/{ns}/rules/{rule_id}:publish`                  | The publish gate (§5)                |
| DELETE | `/namespaces/{ns}/rules/{rule_id}`                          | Delete                               |

**The list is an envelope, not a bare array** (changed 2026-08-27):

```
GET /rules/namespaces/{ns}/rules
{"rules": [...], "unreadable": [{"rule_id": "RULE-X", "error": "rule RULE-X: threshold THR-1 is missing required unit (<…>)"}]}
```

`unreadable` non-empty means **the graph is corrupt, not that the request was bad** — the rules
listed there are stored in the layer and are **not executing**. It is a report, not a log line,
because a rule silently missing from the list is indistinguishable from a rule that was deleted;
that ambiguity is what made the threshold-IRI incident look like data loss. The tab renders the
entries as a warning banner above the table (§8.3).

Two consequences of the strict reader worth knowing before you read a 500:

- **A rules route can now answer 500 naming a rule.** `register_rule_error_handlers(app)` installs
  one app-level `RuleReadError` handler in `main.py` rather than a `try/except` in each of the
  seven routes that resolve a rule — a per-route catch would be forgotten by the eighth. The body
  carries `{"detail": …, "rule_id": …}`. This is **not** a client error; do not retry with a
  different payload.
- **`GET` of an unreadable rule is a 500, never a 404.** A 404 would claim the rule does not exist
  when it does. `RuleService.get` searches the readable rules, then raises if the requested id is
  among the failures. An unreadable _sibling_ no longer blocks reading a healthy rule.

Admin UI: the **Governed Rules** tab (`frontend/src/components/GovernedRulesTab.jsx`). It is
mounted on **two** pages, because `AdminDashboard.getActionButton` routes `type === "VKG"` to
`/admin/view-graph` and every other completed layer — Hybrid included — to
`/admin/view-semantic-metadata/:id`:

| Page                      | Layer types that land there | What the tab shows        |
| ------------------------- | --------------------------- | ------------------------- |
| `ViewKnowledgeGraph`      | VKG                         | The real tab              |
| `ViewSemanticRAGMetadata` | Hybrid                      | The real tab              |
| `ViewSemanticRAGMetadata` | Semantic RAG                | The not-applicable notice |

Earlier revisions of this document said `ViewKnowledgeGraph` served Hybrid too. It never did — a
Hybrid layer cannot reach that route from the dashboard, so between 2026-08-17 and 2026-08-18 the
tab was mounted on exactly one page and Hybrid reviewers had no rules surface at all. Mounting it
on `ViewSemanticRAGMetadata` (rather than re-routing Hybrid to `/admin/view-graph`) keeps Hybrid's
other tabs — Documents, Ground Truth, Monitoring, Evaluations — on the page that has them.

Which types are supported lives in **one** module,
`frontend/src/utils/layerCapabilities.js` — each tab used to carry its own `=== "VKG"` test, and
`ViewKnowledgeGraph` passed the literal string `"VKG"` regardless of the layer it had fetched, so
the guards could not fire and could not disagree visibly either. `rulesUnsupportedFor` is
deliberately **not** the negation of `hasOntologyGraph`: a layer whose type has not been read yet
(config fetch in flight or failed) must produce neither claim, because "rules do not apply to this
layer" is the one statement a reviewer cannot check from the screen they are on.

It renders the rule as a **form, not RDF** — a reviewer
approving a threshold should not have to read Turtle — with a distinct per-threshold Approve
control, the four validation reports, the five test cases, and Publish disabled until everything
passes. The disabled button is a convenience: **the gate is enforced server-side**, because a
disabled button is not a control.

Per-threshold approval is separate from rule approval on purpose. Approving a rule says "this
conditional is a faithful reading of the document"; approving a threshold says "this number is
correct and I stand behind it". They are different claims, often by different people, and the
answer text names the threshold's approver specifically.

### 8.1 Authoring a rule by hand

`rulesAPI.create` / `rulesAPI.update` existed from the first commit of the feature but nothing
called them: until 2026-08-18 the only way a rule reached a layer was the mined-artefact import, so
a layer with no ingested policy document could not have rules at all. **Add rule** / **Edit** in the
tab open a form that PUTs/POSTs the `Rule` model directly.

The API body **is** the pydantic model — snake_case, no camelCase aliases — and `namespace` in the
body must equal the `{ns}` in the path (`rule_id` too, on `PUT`), or the router 400s before the
service sees it. The form builds both from the layer id it was rendered with, so neither is typed.

Four fields are the **server's**, and the form sends them as the server will set them anyway rather
than offering a control the server ignores: `RuleService.create`/`update` both
`model_copy(update={"lifecycle": "DRAFT", "compiled_sparql": None})`, and a review is cleared on
every edit. Anything authored or edited is a DRAFT that must go through `:approve` and `:publish`.

Client-side validation covers exactly the input class the backend does **not** fail loudly on.
rdflib does not validate IRIs: `rule_graph.rule_to_graph` mints `URIRef(f"{VKGR}{rule.rule_id}")`
by string concatenation and wraps `binding`, `subject_class` and `outcome` in bare `URIRef(...)`. A
rule id containing a space, or a relative binding like `daysSinceLastPayment`, therefore persists
triples that parse cleanly, validate, publish — and match nothing. So the form rejects, before
sending:

| Check                                                        | Why it cannot be left to the server                                                                        |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `rule_id` matches `[A-Za-z0-9._:-]+`                         | Becomes an IRI by concatenation; a space yields an unaddressable rule                                      |
| `binding`/`subject_class`/`outcome` are absolute IRIs        | A relative IRI resolves against the graph base and silently matches nothing                                |
| Every `condition.threshold_id` names a threshold on the rule | An unresolved ref makes the rule permanently unevaluable, reported only as "advisory"                      |
| Threshold ids unique **within the rule**                     | Two same-id thresholds collapse to one subject in RDF, so one bound vanishes (also refused server-side)    |
| `source_span` non-blank on every threshold                   | The span is what a threshold approval attests to; a blank one makes the signature meaningless              |
| `confirmedCauseOf` requires an explicit attestation          | §6's ordering only means anything if the strongest status cannot be reached without someone signing for it |

**Threshold-id uniqueness is now also a server-side gate, and it reaches wider than the form's**
(added 2026-08-27). `RuleService._assert_thresholds_unowned` runs on `create` **and** `update` and
refuses with `ValueError` → **422** naming both the id and the rule that already owns it:

```
422 threshold_id THR-300 is already used by rule RULE-QA-large-set-300 in namespace 51e29707-…
```

The form can only see the rule on screen, so it catches repeats _within_ one rule; the server
checks the whole **namespace**. `update` excludes the rule being updated, or every edit would
collide with its own stored thresholds. Threshold ids name the bounds a human approves, so two
rules claiming one id makes an approval ambiguous — and that payload shape is what triggered the
shared-threshold-node corruption before threshold IRIs were rule-scoped.

The check runs against **readable rules only**. If an unreadable sibling happens to own the id the
collision is missed — accepted deliberately, because blocking all authoring in a namespace over one
unrelated corrupt rule would be worse, and that rule is already reported loudly by the list.

Two subtler behaviours worth knowing:

- **A numeric comparand is sent as a JSON number, not a string.** Ontop compares `?v > "90"`
  lexicographically and `?v > 90` numerically, so a bound stored as a string misfires quietly —
  `"9"` sorts after `"90"`.
- **Editing a threshold's value, unit or span withdraws its approval.** The form owns the whole PUT
  body, which makes it the one place a signature could be laundered onto a changed number. It
  carries `approved_by` forward only when all three are byte-identical to the stored rule, and says
  so in the row while editing.

A condition carries a `threshold_id` **or** a literal `value`, never both — represented as a select
rather than validated, so the invalid state cannot be typed.

### 8.2 The mined-rules artefact contract

The import dialog states this too (the tab is where someone hits it); it is repeated here because
nothing else in the repo documents the format outside `services/rule_import.py`.

The ruleminer stage writes one JSON object per document to the artifacts bucket the REST API knows
as `ARTIFACTS_BUCKET`, under the key `mined-rules/<documentId>.json`. `_read_artifact`
(`lambda/rest-api/main.py:429`) translates a `NoSuchKey` into a 404, which the tab renders as "no
mined rules for that document" — so a **misfiled artefact is indistinguishable from an empty one**.
Only the document id is entered in the UI; the key is derived from it.

The artefact's keys are **camelCase** while the `Rule` model's fields are snake_case. That is the
single most confusable thing about the format:

```json
{
  "documentId": "doc-7",
  "rules": [
    {
      "name": "Lapse risk after 90 days",
      "description": "A holding unpaid for more than 90 days is at risk of lapsing.",
      "ruleType": "risk",
      "scope": {
        "subjectClass": "https://example.org/vkg#Holding",
        "processStage": "renewal"
      },
      "conditions": [
        {
          "binding": "https://example.org/vkg#daysSinceLastPayment",
          "operator": ">",
          "thresholdId": "THR-090"
        }
      ],
      "thresholds": [
        {
          "thresholdId": "THR-090",
          "value": 90,
          "unit": "days",
          "sourceSpan": "lapses after 90 days of non-payment"
        }
      ],
      "conclusion": {
        "outcome": "https://example.org/vkg#LapseRisk",
        "epistemicStatus": "indicatesRiskOf"
      },
      "missingInputs": [],
      "sourceSentence": "A holding lapses after 90 days of non-payment.",
      "modelId": "claude-sonnet-4",
      "confidence": 0.0,
      "page": 4
    }
  ]
}
```

`mined_rules_to_drafts` then:

- **Derives the id**: `rule_id` is not read from the artefact at all, but computed as
  `RULE-<sha256(f"{documentId}|{index}|{name}")[:12]>`. Re-importing the same artefact therefore
  updates the same drafts instead of duplicating them — but **renaming a rule or reordering the
  list mints a new id** and orphans the old draft.
- **Forces `lifecycle` to DRAFT**, drops every threshold approval, imports no conclusion as
  attested, ignores `attestationNote`, and discards any `compiledSparql`. Nothing in a JSON file can
  grant itself governance.
- **Forces `level` to `contextualised`** — a mined rule always carries a scope, so it is
  context-bound by construction.
- **Downgrades unknown vocabulary rather than failing**: an unrecognised `ruleType` becomes
  `advisory` (`eligibility` → `compliance`, `classification` → `diagnostic`), and an unrecognised
  `epistemicStatus` is weakened to `candidateContributorTo`. A status nobody recognises must never
  become a causal claim by accident.

**Import is all-or-nothing.** A structural defect — a threshold with no `sourceSpan`, a rule with no
conditions — raises and the endpoint returns **422 naming the field** with nothing imported.
Importing the valid subset would hide a miner regression behind a set of plausible-looking drafts.

### 8.3 When the tab says a rule could not be read

Added 2026-08-27. Above the rules table, a **warning** banner — `N rule(s) could not be read` —
lists one `rule_id` and error per entry in the list response's `unreadable` array.

Read it as: those rules **are stored in this layer, are missing required fields, and are not
executing**. Every other rule on the page is unaffected, which is the point — before this, one
corrupt rule returned 500 for the whole namespace and the tab went **blank**, so the operator's
only signal was that all their rules had apparently vanished.

Three details that matter when you are looking at one:

- It is a **warning, not an error**, and it is held in its own state rather than in `error`: the
  request succeeded. What is damaged is the graph.
- It is **cleared on every refresh**, or a warning about a rule that has since been fixed would sit
  on screen for the rest of the session.
- The tab still accepts a **bare array** (`Array.isArray(res.data) ? res.data : res.data?.rules`),
  so a newer frontend against an older deployment degrades to "no banner" rather than an empty
  table.

There is no repair UI. Fix the rule by re-authoring it (the error names the missing field), or read
the graph directly — `docs/plans` and the `neptune-tools` direct-invoke path are the only verbatim
read/repair route.

## 9. Running the deploy-gated test

`tests/integration/test_governed_rules_e2e.py` covers the four things that cannot be faked: Ontop
reformulation at publish, the `/rules` graph round-trip, the `/rules` exclusion against real graph
listings, and KNN hydration from a live SPARQL SELECT.

It is skipped by default twice over — `pyproject.toml` sets `testpaths = ["tests/unit"]`, so a bare
`pytest` never collects it, and the gate below skips with a printed reason naming the missing
variables.

```bash
export RUN_GOVERNED_RULES_E2E=1
export GOVERNED_RULES_E2E_ONTOLOGY_ID=<layer-id>
export GOVERNED_RULES_E2E_SUBJECT_CLASS=<class IRI from that layer>
export GOVERNED_RULES_E2E_BINDING=<datatype-property IRI that IS vkg:mapsToColumn-mapped>
export GOVERNED_RULES_E2E_OUTCOME=<outcome class IRI from that layer>
export NEPTUNE_GATEWAY_URL=<the deployed REST Lambda's NEPTUNE_GATEWAY_URL>
export ATHENA_WORKGROUP=<projectName>-workgroup
python -m pytest tests/integration/test_governed_rules_e2e.py -v -s
```

`ATHENA_WORKGROUP` is not optional here even though the gate is armed by the `GOVERNED_RULES_E2E_*`
variables. The publish fixture executes the smoke query with **your** credentials through
`_run_athena_sql`, whose fallback is the literal `'primary'` — so without it the publish fails on
IAM and reports a `PublishBlocked` that is indistinguishable from the Ontop finding the next
paragraph tells you to take seriously. Your own principal needs the same Athena execution rights §5
lists for the Lambda role.

The IRIs must be **real IRIs from the layer under test**, and the binding must be OBDA-mapped. A
rule bound to invented IRIs fails semantic validation, and an unmapped binding routes the rule to
the advisory branch — either way the Ontop assertions never run and the test proves nothing. The
test asserts `missing == []` for exactly this reason.

The rule id is fixed (`RULE-E2E-1`), not random, so a crashed run leaves one known-named rule an
operator can delete by hand rather than accumulating litter. Teardown deletes it even on failure.

A `PublishBlocked` from the publish fixture is **a finding, not a flake**: it is Ontop telling you
it cannot reformulate that rule shape (accepted risk #1 in the design doc). Report the verbatim
reason; do not weaken the gate to make the test pass.

## 10. Operating notes

- **Rebuilding a layer survives the rules but not their validity.** The rules-graph URI is derived
  from the ontology **id**, which is stable, while a rebuild mints a new timestamped ontology graph.
  So the rules are still there — bound to class and property IRIs the new ontology may no longer
  declare, and to `vkg:mapsToColumn` mappings that may have moved. Nothing re-checks this
  automatically: re-run `:validate` on every rule after a rebuild that changes IRIs or mappings.
- **`compiled_sparql` is stale by design after an edit.** It is written only at publish, so an
  edited rule must be re-approved and re-published; nothing re-compiles in place.
- **Turning the flags off does not delete anything.** `GOVERNED_RULES_ENABLED=false` makes the
  query tier return before its first Neptune call; the rules stay in Neptune and the tab stops
  mounting. That is the rollback.
- **Rules written before the threshold-IRI fix need a one-time migration.** A threshold node used
  to be named `vkgr:<threshold_id>` — global, so two rules that happened to pick the same
  threshold id shared one node. Since `vkgr:threshold` is in the rule-delete child closure,
  deleting either rule deleted the other's threshold, and the survivor then failed to deserialize
  and returned 500 for the **whole namespace's** rule list. The serializer now scopes the IRI to
  its rule, but that only takes effect when a rule is next written, so pre-fix graphs keep the
  hazard until migrated:

  ```bash
  python3 scripts/migrate_threshold_iris.py --namespace <ontologyId>            # dry run
  python3 scripts/migrate_threshold_iris.py --namespace <ontologyId> --apply
  ```

  Two things to know before running it. **Deploy the query runtimes first** — a runtime still on
  the old code writes the old shape back, so migrating first just re-introduces what you removed.
  And the script reads every rule before it writes any, deliberately: two legacy rules sharing a
  threshold id share one node, so upserting the first would destroy the second's threshold before
  it had been captured. It re-persists each rule from its own in-memory model, which preserves
  `lifecycle` and `compiled_sparql` — the fields the REST API's own create/update paths reset —
  and verifies the round-trip afterwards, failing loudly rather than reporting a partial success.

- **Deploy the REST API and the agentcore stack together when `rule_graph.py` changes.**
  `agents/semantic_layer_query_agent/rule_graph.py` is a tracked **byte-identical duplicate** of
  `agents/shared/rule_graph.py` with **no sync script** — it is kept in step by hand (`cp`, then
  `diff`). A version skew between the two makes `Rule.is_evaluable()` return False, so a PUBLISHED
  rule **silently stops firing**. Deploy `semantic-layer-dev-lambda-api` **and**
  `semantic-layer-dev-agentcore` in one command, and remember `-c enableAuthz=true` — that flag is
  not in `cdk.json`, and a deploy without it disables enforcement.
- **An unreadable rule is a data fault you will see as a 500, not as a validation failure.** The
  stricter reader (§3) raises `RuleReadError` naming the rule and the field. No graph this codebase
  writes **intact** can cause it — only unconditionally-serialized fields are required — so the
  cause is a child node that went missing after the write, the shared-threshold-node case above
  being the one known example, not an old format the reader fails to understand. The list endpoint
  reports it without failing (§8.3); every other route answers 500 with the rule named.
