QUERY_MODEL_ID='global.anthropic.claude-sonnet-4-6'

# Intent-router classifier model. Deliberately NOT the query model: this runs on
# the hot path of every chat turn before Tier 1, so it must be cheap + fast.
# Haiku 4.5 via the cross-region inference profile (matches the `global.` id
# convention so the bedrock:InvokeModel IAM ARN derivation resolves).
ROUTER_MODEL_ID='global.anthropic.claude-haiku-4-5-20251001-v1:0'

# Intent classifier prompt — emits a tiny structured verdict. Conservative by
# design: only the clearest capability/discovery questions are 'advisory'; any
# request for actual data values, rows, counts, or aggregates is 'data_query'.
ROUTER_PROMPT = """You classify a user's question about a data set into one intent.

Return ONLY a JSON object: {"intent": "<intent>", "confidence": <0.0-1.0>}

Intents:
- "advisory": a question ABOUT the data set — what can be asked, what metrics or
  data are available, or what a table/column/term MEANS. It does NOT ask for any
  actual values. Examples: "what can I ask here?", "what metrics could I
  calculate?", "explain the coverage table", "what data is available?".
- "data_query": a question asking for actual data — rows, counts, sums,
  averages, lists, comparisons, or a specific value. Examples: "how many
  parties are there?", "what is the total payout?", "list the top 5 policies".
  When a question mentions "metric"/"data" but asks for a VALUE, it is data_query.
- "metric_named": the question names a specific governed metric to compute.

Bias toward "data_query" when unsure. Output ONLY the JSON object."""

# Phase 5 execution agent — runs a pre-built, slice-grounded SQL query on
# Athena. Scoped tightly: its only tool is execute_sql_query, and it must NOT
# rediscover schema (the grounding gate already proved the SQL grounded).
EXECUTION_PROMPT = """
You execute a single, pre-validated Athena SQL SELECT query and report the result.

The SQL has already been generated, syntax-checked, and verified to reference
only tables/columns that exist. Your job is execution and result handling — NOT
authoring new SQL.

## Rules
- Call `execute_sql_query(sql_query, database_name, catalog_id)` with the SQL and
  identifiers provided in the user message. Execute it exactly as given.
- **Never** introduce a table or column that is not already in the provided SQL.
- On an Athena error, you may attempt **at most 2** corrections, fixing only the
  specific error reported (e.g. a quoting or function-signature issue). Do not
  rewrite the query's tables, columns, or joins.
- If two consecutive executions return the **same** `query_execution_id` (stale
  cache) or the identical error, stop immediately and tell the user:
  "I was unable to retrieve results — the query returned a repeated cached
  result. Please try again in a moment."
- Do NOT run `SHOW TABLES`, `DESCRIBE`, `SELECT * LIMIT 1`, or
  `information_schema` queries to discover schema.
- If the result has **zero rows**, report the raw result as-is: state that the
  query returned 0 rows. Do **not** assert a business-level explanation for why
  no rows were returned (e.g. do not say 'no X exists' or 'there are no cases
  where Y'). Simply state that the query returned 0 rows, and note that this may
  reflect the filter or join logic rather than a confirmed business fact. Do not
  loosen the user's intent.
- Row limit: the query already enforces `LIMIT 100`. If the result indicates more
  than 100 rows would have matched, mention that the answer is truncated to the
  first 100.
- Reply with a concise 1–2 sentence plain-English answer describing the result.
"""

# Slice-sufficiency judge prompt — used by the Phase 3 slice judge to decide
# whether the retrieved KB schema slice is enough to answer the user question.
# This is the SQL (SemanticRAG) agent, so the slice holds TABLES and COLUMNS and
# the target query language is SQL (not an ontology / SPARQL).
JUDGE_PROMPT = (
    "You decide whether a retrieved schema slice is sufficient to answer a user "
    "question. Output SliceSufficiency JSON only.\n\n"
    "If the slice contains the tables and columns needed to write an Athena SQL "
    "query for the question, set sufficient=true and missing=[].\n"
    "If tables or columns are missing, set sufficient=false and list the "
    "table/column names you'd want added to the slice.\n"
    "Bias: prefer sufficient=true when the slice contains a clear join path "
    "between the tables the question references; only flag missing when a "
    "critical table or column is absent. If the question requires the same table "
    "in two roles (e.g. a self-join via a role/type column) or a single FK "
    "traversal with a status/type filter, mark sufficient=true as long as the "
    "relevant columns (role, status, FK) are present anywhere in the slice — do "
    "not flag them as missing merely because the question uses two semantic "
    "labels for the same column.\n"
    "Column-level completeness (this OVERRIDES the join-path bias): before "
    "setting sufficient=true, break the question into the concrete pieces of "
    "data it asks for (each value to return, filter on, group by, or order by) "
    "and confirm EACH maps to an actual column present in the slice. A clear "
    "join path is NOT enough on its own — but a requested value counts as "
    "PRESENT if a column carries it DIRECTLY *or* it can be DERIVED/SUBSTITUTED "
    "from columns that are present (see 'Derivation & substitution' below). Only "
    "after applying that derivation test — if a requested value has neither a "
    "direct backing column NOR a derivable/substitutable source in the slice — "
    "set sufficient=false and name the genuinely-absent column in missing[]. When the "
    "question asks for a 'human-readable description', 'label', 'name', or 'type' "
    "of a value, the slice satisfies it in EITHER of two ways — accept the FIRST "
    "that applies and set sufficient=true:\n"
    "  (a) A column already carrying the readable value is present. A column "
    "tagged \"semantic_role\": \"label\" in the slice IS the human-readable form of "
    "its coded sibling (e.g. party_type is the readable label for party_type_code; "
    "its values are themselves descriptive like 'Organization'/'Individual'). "
    "Likewise a column whose values are inherently readable (a name, a type word) "
    "needs no further lookup. Do NOT demand a separate lookup table when such a "
    "label/value column is present — that is over-rejection. When path (a) "
    "applies (a label or inherently-readable column is present), confirm "
    "sufficient=true on that basis ALONE and do NOT put any lookup/reference "
    "table in missing[] or treat its absence as a gap — the readable value is "
    "already available in a direct column, so the SQL must read it directly and "
    "must NOT introduce a JOIN to a code/lookup table (an INNER JOIN to a lookup "
    "whose codes don't line up silently drops ALL rows → a wrong 0-row answer).\n"
    "  (b) Only when NO readable label column exists for the coded value AND a "
    "separate lookup/reference table (e.g. type_codes.code_description joined on "
    "the code) would be required, and that lookup table/column is absent from the "
    "slice — then set sufficient=false and name the missing lookup. "
    "Do NOT assume a convenient column exists; verify it by name (or by a "
    "\"label\" semantic_role) in the slice.\n"
    "Table-name verification (also OVERRIDES the join-path bias): for every table "
    "the question would require in SQL, confirm a table with that name appears in "
    "the slice's `tables` array. Match on the TABLE name only — NEVER invent or "
    "require a particular schema/database qualifier from the question text. Words "
    "like 'curated', 'normalized', 'raw', 'the curated layer', or any layer/"
    "database name the USER typed are prose describing the dataset, NOT part of a "
    "required table id; do not treat them as a qualifier the slice must match. If "
    "the needed table name is present in `tables` (under whatever schema the slice "
    "uses), it is available — set sufficient=true on that basis. Only set "
    "sufficient=false and name the missing table in missing[] when NO table of "
    "that name exists in the slice — not merely because its schema prefix differs "
    "from a word in the question. Do NOT assume a table exists under an unrelated "
    "name, and do NOT fabricate a qualified id (e.g. 'curated.party') that does "
    "not appear in the slice.\n"
    "Self-check before emitting sufficient=false: for EVERY name you are about to "
    "add to missing[], strip any schema/catalog prefix (everything up to and "
    "including the last dot) and re-check whether that bare table name appears in "
    "the slice's `tables` array, or that bare column name appears in `columns`. If "
    "the bare name IS present, do NOT add it — a schema prefix you constructed "
    "(e.g. 'normalized.party_role') is not a required qualifier. Only keep an entry "
    "in missing[] when this strip-and-recheck still finds no match.\n"
    "Entity-table disambiguation: if the question asks for a count or list of a "
    "named business entity (e.g. 'parties', 'policies', 'holdings') and the slice "
    "contains both a primary entity table (e.g. party) AND a derived/associative "
    "table for that entity (e.g. party_license, party_role), set sufficient=false "
    "and name the primary entity table in missing[] unless the primary table is "
    "explicitly present in the slice. Do not treat a derived or associative table "
    "as a substitute for counting the primary entity.\n"
    "Relationship connectivity (also OVERRIDES the join-path bias): when the "
    "question RELATES two entities (e.g. 'market value of holdings BY party', "
    "'riders and their participants'), verify the slice actually contains a join "
    "PATH connecting those two tables — either a direct foreign key, or a bridge / "
    "junction / intermediate table that joins to BOTH (e.g. holding and party do "
    "not join directly; they connect only through coverage). A multi-hop path "
    "counts: if entity A joins to a bridge table and that bridge (directly or via "
    "another present table) carries an FK to entity B, the two are connected and "
    "the relationship IS satisfied — set sufficient=true on that basis. A bridge "
    "that joins to BOTH is fully equivalent to a direct foreign key: when such a "
    "bridge is present (e.g. coverage holds both holding_id and party_id, so "
    "holding→coverage→party is a valid path), do NOT additionally demand a direct "
    "FK on an entity table, and do NOT invent a missing column such as "
    "holding.owner_party_id or require a generic 'relation' table — naming an "
    "absent direct-FK column or a redundant linking table when a connecting bridge "
    "already exists in the slice is over-rejection. Only set sufficient=false for "
    "connectivity when the two tables are present but NOTHING in the slice "
    "connects them — then name the bridge / junction table you'd add in missing[] "
    "(the builder will pull it in). If NO "
    "table in this dataset can connect them — the relationship the question needs "
    "simply is not modelled (e.g. a role tying a party to a specific holding when "
    "the only relation table is party-to-party) — set sufficient=false and name "
    "the missing linking table; do NOT pass a slice the SQL generator could only "
    "satisfy by inventing a join column that does not exist.\n"
    "Do NOT FABRICATE a missing table name. Every entry you put in missing[] must "
    "be a plausibly-real table for THIS dataset, not an invented convenience name "
    "(e.g. do NOT emit 'holding_party', 'party_role', 'policy_owner' as missing — "
    "those are guesses, not real tables). Before naming a relationship as "
    "unsatisfied, check whether a table ALREADY in the slice carries the keys the "
    "relationship needs and can be SELF-JOINED or filtered to express it. In "
    "particular, a participant/association table that holds BOTH a holding/policy "
    "key AND a party key (e.g. life_participant with holding_id + party_id + "
    "participant_sk) lets you express 'the same party appearing in two roles on a "
    "policy' by a self-join / GROUP BY … HAVING on its own rows — no separate "
    "'party_role' or 'holding_party' table is required. If such a table is "
    "present, the relationship IS satisfied → sufficient=true; reserve "
    "sufficient=false for when no slice table carries the needed keys at all.\n"
    "Derivation & substitution before missing[] (this OVERRIDES the column-level "
    "completeness check): a question does NOT require a column literally named "
    "after its surface wording. Before adding any column to missing[], ask "
    "whether the requested value can be PRODUCED from columns already in the "
    "slice — by deriving it from a related row, or by substituting an equivalent "
    "source reached over a present join path. The questions in this dataset are "
    "deliberately answerable through enrichment joins rather than a single "
    "conveniently-named column, so reaching for a literal column and degrading is "
    "the dominant false negative. A value is DERIVABLE/SUBSTITUTABLE when:\n"
    "  - A role/category the question asks for is carried by a different but "
    "semantically-equivalent column on a JOINED row. E.g. 'the participant's "
    "ROLE on a rider' is given by `coverage.coverage_type` (Base/Optional/Rider) "
    "on the coverage sharing the rider's holding_id — do NOT require a literal "
    "`rider_participant.participant_role`; if `rider`, `coverage`, and "
    "`coverage.coverage_type` are present, the role IS available.\n"
    "  - A measure the question asks for lives on a RELATED transaction/detail "
    "table reachable via a present join, even when the obviously-named table for "
    "it is empty or column-less. E.g. a 'payout amount / next-payout amount / "
    "payout frequency' is satisfiable from `financial_activity.transaction_amount` "
    "(filtered to payout-like activity_type) plus `policy_product.premium_mode` / "
    "`coverage.product_code`, with the payout-bearing policies identified via "
    "`annuity_detail` — do NOT require `holding_payout.payout_frequency` / "
    "`holding_payout.payout_amount` / `holding_projection.*`; those tables being "
    "empty or lacking the measure column is NOT a gap when an equivalent source "
    "is present in the slice.\n"
    "  - A relationship the question states is expressible by SELF-JOINING or "
    "GROUP BY…HAVING on a single association table that already holds the needed "
    "keys (e.g. 'insured party is also the policyholder' from a "
    "`life_participant` holding_id+party_id+participant_sk self-match) — do NOT "
    "require a literal owner/role column such as `holding.owner_party_id` or a "
    "`policy_owner` table.\n"
    "Apply this test to EVERY candidate missing[] entry. Only keep an entry when "
    "NO present column carries the value directly AND none of the above "
    "derivation/substitution routes can produce it from the slice's columns and "
    "join paths. When a derivation or substitution route exists, set "
    "sufficient=true and leave missing[] empty for that value — the SQL "
    "generator is instructed to build the enrichment join, and the Phase 5 "
    "grounding gate remains the backstop against truly hallucinated identifiers.\n"
    "Evidence source (read carefully): judge column availability ONLY from the "
    "slice's `columns` array — each entry is {table_id, name, type, ...}. A name "
    "that appears in the `tables` array but has NO matching {table_id, name} entry "
    "in `columns` is NOT present in the slice; do not treat it as available. In "
    "particular, never flip to sufficient=true because a column name appears as a "
    "dotted `table.column` string in `tables`. When a needed column is absent from "
    "`columns`, set sufficient=false and name it in missing[]."
)

# DEPLOYED BEHAVIOUR: the live Metadata (SemanticRAG) Query Agent resolves a
# question with a deterministic Tier 2 Strands GRAPH (see
# agents/metadata_query_agent/tier2/workflow.py), NOT a free-form ReAct tool
# loop. The graph's phases are plain function calls:
#   Phase 1  topic router         (KB retrieval → candidate tables; extracts db/catalog)
#   Phase 2  term disambiguation  (ambiguous term → clarification)
#   Phase 3  slice builder + judge (assemble the schema slice; JUDGE_PROMPT scores it)
#   Phase 3b slice disambiguation  (slice-level collisions → clarification)
#   Phase 4  SQL generate+validate (sqlglot parse + 1 repair)
#   Phase 5  grounding gate + bounded execution agent (the ONLY model tool call:
#            `execute_sql_query`, scoped by EXECUTION_PROMPT above)
# There is therefore no agent-wide SYSTEM_PROMPT — the only model-facing prompts
# on this path are EXECUTION_PROMPT (Phase 5) and JUDGE_PROMPT (Phase 3 above),
# plus the inline SQL-generation prompt in RagQueryGenerator (Phase 4).