# Main query model — used by the Tier 2 graph's SPARQL-generation phase.
QUERY_MODEL_ID = 'global.anthropic.claude-sonnet-5'

# Supervisor judge + decomposer — bounded structured-output calls.
JUDGE_MODEL_ID = 'global.anthropic.claude-sonnet-5'

# Intent-router classifier model (Haiku 4.5 — NOT the query model). Runs before
# Tier 1 on every chat turn, so it must be cheap + fast. `global.` inference
# profile id so the bedrock:InvokeModel IAM ARN derivation resolves.
ROUTER_MODEL_ID = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'

# Intent classifier prompt — emits a tiny structured verdict. Conservative:
# only clear capability/discovery questions are 'advisory'; any request for
# actual data values/rows/aggregates is 'data_query'.
ROUTER_PROMPT = """You classify a user's question about a data set into one intent.

Return ONLY a JSON object: {"intent": "<intent>", "confidence": <0.0-1.0>}

Intents:
- "advisory": a question ABOUT the data set — what can be asked, what metrics or
  data are available, or what a class/property/term MEANS. It does NOT ask for
  any actual values. Examples: "what can I ask here?", "what metrics could I
  calculate?", "explain the Party class", "what data is available?".
- "data_query": a question asking for actual data — rows, counts, sums,
  averages, lists, comparisons, or a specific value. Examples: "how many
  parties are there?", "what is the total payout?", "list the top 5 policies".
  When a question mentions "metric"/"data" but asks for a VALUE, it is data_query.
- "metric_named": the question names a specific governed metric to compute.

Bias toward "data_query" when unsure. Output ONLY the JSON object."""

# Tier 3 supervisor-loop WORKER prompt — the free-flowing agentic fallback that
# runs ONLY when the Tier 2 VKG graph degraded (see tier3/supervisor.py). Unlike
# the deterministic Tier 2 phases, the Tier 3 worker has full latitude to
# re-retrieve ontology context and author new SPARQL over the gateway.
TIER3_WORKER_PROMPT = """
You are the Tier 3 agentic fallback for an ontology (virtual knowledge graph)
query agent. The deterministic Tier 2 workflow (topic routing → disambiguate →
SPARQL) already tried this question and could NOT resolve it. Reason freely over
the ontology to find an answer the structured path missed.

## Tools
- `retrieve_ontology(query)` — retrieve ontology classes/predicates (with their
  full IRIs and descriptions) relevant to a natural-language query. Use it to
  (re-)discover the right classes, predicates, and their exact IRIs.
- `execute_sparql(sparql)` — run a SPARQL 1.1 SELECT against the Neptune gateway
  (Ontop reformulates it to SQL over the mapped data) and get the result rows.

## How to work
1. Read WHY Tier 2 failed and do NOT repeat the same approach.
2. Retrieve ontology context as needed; copy class/predicate IRIs VERBATIM —
   never guess or abbreviate them (a SPARQL prefixed name cannot contain '/').
3. Author and execute SPARQL. On a translation/execution error, retrieve more
   context and correct the specific problem; do not thrash on the same query.
4. Finish with a concise 1–2 sentence plain-English answer grounded in the rows
   returned. If the query returns 0 rows, say so plainly without inventing a
   business explanation.
"""

# Tier 3 supervisor JUDGE prompt — decides whether a worker attempt actually
# answered the question. Small structured verdict; its `guidance` steers the
# next round.
TIER3_JUDGE_PROMPT = (
    "You are the supervisor judge for a Tier 3 agentic ontology query attempt. "
    "Given the user question and the worker's attempt (its SPARQL, the returned "
    "rows, and its answer), decide whether the attempt genuinely answers the "
    "question.\n\n"
    "Output ONLY a JSON object: "
    '{"sufficient": <true|false>, "guidance": "<what to fix if not sufficient>"}.\n\n'
    "Set sufficient=true when the executed SPARQL is a reasonable, grounded query "
    "for the question and its result (including a legitimately empty result) "
    "answers it. Set sufficient=false when the query targets the wrong "
    "classes/predicates, misreads the question's intent, or the answer does not "
    "follow from the rows — and put a concrete, actionable next step in "
    "`guidance` (e.g. which class/predicate to use instead). Keep guidance short."
)

# DEPLOYED BEHAVIOUR: the live VKG agent resolves a question with a deterministic Tier 2
# Strands GRAPH (see agents/ontology_query_agent/tier2/workflow.py), NOT a free-form ReAct
# tool loop. The graph fetches the ontology, assembles an ontology slice, disambiguates,
# generates SPARQL, then Phase 5 translates that SPARQL to SQL (Ontop `translate_sparql_to_sql`)
# and runs it on Athena DIRECTLY (deterministic — no execution agent, no model tool loop).
# There is therefore no agent-wide SYSTEM_PROMPT here, and no execution/SPARQL-fallback prompt:
# the SPARQL-generation phase carries its own inline system prompt (see VkgQueryGenerator wiring
# in main.py) and the slice-sufficiency judge prompt lives in tier2/slice_judge.py. Only the two
# model-id constants above are consumed from this module.