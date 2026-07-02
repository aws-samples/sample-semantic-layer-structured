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

# DEPLOYED BEHAVIOUR: the live VKG agent resolves a question with a deterministic Tier 2
# Strands GRAPH (see agents/ontology_query_agent/tier2/workflow.py), NOT a free-form ReAct
# tool loop. The graph fetches the ontology, assembles an ontology slice, disambiguates,
# generates SPARQL, then Phase 5 translates that SPARQL to SQL (Ontop `translate_sparql_to_sql`)
# and runs it on Athena DIRECTLY (deterministic — no execution agent, no model tool loop).
# There is therefore no agent-wide SYSTEM_PROMPT here, and no execution/SPARQL-fallback prompt:
# the SPARQL-generation phase carries its own inline system prompt (see VkgQueryGenerator wiring
# in main.py) and the slice-sufficiency judge prompt lives in tier2/slice_judge.py. Only the two
# model-id constants above are consumed from this module.