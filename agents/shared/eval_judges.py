"""Shared definitions for the custom AgentCore LLM-as-Judge evaluators.

Why this module exists
----------------------
Notebooks 2 (Semantic-RAG / metadata-query agent), 6 (VKG / ontology-query
agent), and 11 (VKG OntologyRAG on/off) all create the SAME three custom
SESSION-level evaluators — ``GoalSuccess``, ``FinalAnswerFaithfulness``, and
``SqlGrounded`` — and score arms with them. Re-declaring the judge prompts
inline in every notebook let them **drift**: nb11 had carried trimmed copies of
the VKG prompts, so its WITHOUT-OntologyRAG arm was graded by a *different*
rubric than its WITH arm (reused from nb6), making that A/B not apples-to-apples.

Centralizing the prompts + the ``create_*`` factory here guarantees every
notebook scores with byte-identical judges. There are exactly **two legitimate
prompt families**, because the two agents emit different telemetry:

- **RAG** (``metadata_query`` agent) — Tier 2 graph retrieves KB chunks /
  assembles a schema slice, then runs the real ``execute_sql_query`` tool. The
  judges reason about that tool call.
- **VKG** (``ontology_query`` agent) — Tier 2 graph assembles an ontology slice,
  generates SPARQL, then Phase 5 translates SPARQL→SQL (Ontop) and runs it on
  Athena DIRECTLY (no ``execute_sql_query`` tool call). The judges reason about
  the ontology slice + the Ontop-translated executed SQL.

Each family is binary (0/1), SESSION-level, judged by ``JUDGE_MODEL_ID``. The
RAG and VKG ``FinalAnswerFaithfulness`` prompts ALSO differ for a real reason:
the VKG path emits several intermediate ``chat`` spans (SPARQL-generation,
grounding record) that are NOT the user-facing answer, so its FAF prompt carries
an explicit "which span is the final answer" block. Do not collapse the two
families into one prompt.

Placeholder constraint (AWS ``CreateEvaluator``)
------------------------------------------------
A SESSION evaluator may reference ONLY ``{context}``, ``{available_tools}``,
``{assertions}``, ``{expected_tool_trajectory}``, ``{actual_tool_trajectory}``.
``{expected_response}`` is TRACE-only — so the expected final answer is threaded
in as a SESSION assertion (``eval_multiturn.build_trajectory_assertions``,
prefixed ``FINAL_ANSWER_ASSERTION_PREFIX``) and the faithfulness judge reads it
from ``{assertions}``.

Why SESSION (not TRACE)
-----------------------
Both agents answer in ONE turn OR ask a clarifying question first and answer on a
LATER turn. SESSION judges read the whole conversation via ``{context}`` and
score the FINAL answer once. A clarify turn must still emit an evaluable span —
``shared/answer_span.emit_answer_span`` writes a deterministic span on both the
clarification and final-answer paths, so every turn has one (without it a TRACE
judge would raise "no spans to evaluate" and fail the session). SESSION is the
right granularity because answer quality is a per-conversation question and
``{expected_response}`` is TRACE-only.
"""
from __future__ import annotations

import uuid
from typing import Dict, List

# The judge model every custom evaluator runs on. Sonnet 4.6 (matches the
# deployed CDK eval stack's JUDGE_MODEL_ID).
JUDGE_MODEL_ID = "global.anthropic.claude-sonnet-5"

# Binary 0/1 rating scale shared by all custom judges.
BINARY_SCALE = {
    "numerical": [
        {"value": 0.0, "label": "fail", "definition": "Does not satisfy the criterion."},
        {"value": 1.0, "label": "pass", "definition": "Fully satisfies the criterion."},
    ]
}


# ─────────────────────────────────────────────────────────────────────────────
# Judge prompt text — RAG family (metadata_query agent)
# ─────────────────────────────────────────────────────────────────────────────
RAG_FINAL_ANSWER_FAITHFULNESS = (
    "You are a strict binary evaluator for a multi-turn text-to-SQL data agent.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Assertions about this session:\n{assertions}\n\n"
    "Exactly one assertion begins with 'The conversation's final answer matches:' — the "
    "text after that prefix is the EXPECTED final answer. The agent may take several "
    "turns (e.g. ask a clarifying question, get a reply, then answer); judge ONLY the "
    "FINAL substantive answer the agent gives at the end of the conversation.\n\n"
    "Score 1 (pass) iff that final answer is factually consistent with the expected "
    "answer — same key numbers, entities, and conclusion. Score 0 (fail) if it "
    "contradicts the expected answer, invents figures, omits the requested result, or if "
    "the conversation never reaches a substantive answer (e.g. ends still asking for "
    "clarification). If no assertion carries the expected-answer prefix, score 0 and note "
    "that the ground truth is missing. Briefly justify your score."
)

RAG_SQL_GROUNDED = (
    "You are a strict binary grounding evaluator for a text-to-SQL data agent.\n\n"
    "Session context (full conversation, including tool calls and tool results):\n"
    "{context}\n\n"
    "Available tools: {available_tools}\n\n"
    "This agent runs a deterministic resolution graph: it retrieves Knowledge Base "
    "schema for the question, assembles a SCHEMA SLICE (the allowed tables/columns/joins), "
    "generates SQL against that slice, then executes it with the `execute_sql_query` tool. "
    "The retrieval/slice steps are graph phases, not model tool calls, so do NOT expect a "
    "`retrieve_kb_context` tool call. In the context, locate:\n"
    "  (a) the RETRIEVED SCHEMA CONTEXT — the KB chunks / schema slice describing the "
    "allowed tables, columns, and joins (the ONLY schema the agent may use); and\n"
    "  (b) the ARGUMENTS of the `execute_sql_query` tool — the SQL the agent actually ran.\n\n"
    "IMPORTANT — degraded runs: this agent has a grounding gate that REFUSES to "
    "execute SQL it cannot ground in the slice. If there is NO execute_sql_query tool "
    "call in the context (the agent degraded rather than running ungrounded "
    "SQL), the grounding invariant was UPHELD — score 1 (pass) and note 'no SQL executed "
    "(degraded)'. Do NOT treat generated-but-not-executed SQL appearing in "
    "reasoning/answer text as executed SQL; only the execute_sql_query tool-call "
    "arguments count as executed.\n"
    "Otherwise, score 1 (pass) iff EVERY table, column, and join referenced in the "
    "executed SQL appears in the retrieved schema context (case-insensitive; tolerate "
    "aliases, quoted vs unquoted identifiers, and SQL builtin functions such as "
    "COUNT/SUM/DATE_TRUNC — those are not schema). Score 0 (fail) if the executed SQL "
    "references any table or column that is absent from the retrieved schema context "
    "(hallucinated schema), or if SQL was executed but no retrieved schema context is "
    "present at all (grounding cannot be verified). Briefly name the first offending "
    "identifier when you fail it."
)

RAG_GOAL_SUCCESS = (
    "You are a strict binary evaluator for a multi-turn text-to-SQL data agent. You "
    "decide whether the agent ACHIEVED THE USER'S GOAL across the whole conversation, "
    "judged against a set of assertions.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Assertions that define success for this session:\n{assertions}\n\n"
    "CRITICAL — which span is the agent's user-facing answer: this agent runs a "
    "DETERMINISTIC RESOLUTION GRAPH, not a free-form chat. Each turn therefore emits "
    "SEVERAL intermediate spans that are NOT what the user saw — in particular an "
    "intent-classification JSON span (a small JSON object with an 'intent' field such as "
    "'data_query' and a numeric 'confidence'), a 'SliceSufficiency' tool result (a JSON "
    "object with a boolean 'sufficient' field and a 'missing' list), and a follow-up "
    "question-rewrite span. DO NOT treat any of those intermediate artifacts as the "
    "agent's answer, and "
    "in particular do NOT conclude the agent 'returned JSON instead of asking a "
    "clarifying question' — that JSON is an internal graph phase. The real user-facing "
    "answer for each turn is the span whose INPUT begins with 'Final-answer record for a "
    "deterministic graph turn'; its OUTPUT is the natural-language text the user actually "
    "received — a substantive answer, a CLARIFICATION question (often tagged "
    "'[CLARIFICATION]' with the offered options), or a degraded explanation. Read the "
    "trajectory from these final-answer records, in order; for the conversation's final "
    "answer use the LAST one.\n\n"
    "CRITICAL — reconstructing the multi-turn trajectory: the session context may "
    "surface the spans of only the LAST turn (each turn is a separate trace), so it can "
    "look like a single turn even when the conversation had several. DO NOT conclude "
    "'there is only one turn' from the visible spans alone. The final-answer record's "
    "INPUT contains a '[conversation_so_far]' block listing every PRIOR turn of this same "
    "session (lines like 'user: ...' / 'assistant: ...', oldest first). You MUST read "
    "that block and treat those prior turns as real turns of the conversation, then append "
    "this record's own user_question + OUTPUT as the latest turn. Score the trajectory "
    "assertions against the FULL reconstructed conversation (the [conversation_so_far] "
    "lines + the final answer), NOT just the spans you can see directly.\n\n"
    "Scoring rules:\n"
    "  - A turn whose final-answer OUTPUT is a clarification question SATISFIES an "
    "assertion that the agent should ask for / keep seeking clarification — it is NOT a "
    "failure to answer.\n"
    "  - One assertion begins with 'The conversation's final answer matches:' — the text "
    "after that prefix is the EXPECTED final answer; the conversation's last substantive "
    "answer must be factually consistent with it (same key numbers, entities, conclusion).\n"
    "  - If the agent asks a clarifying question when an assertion says it should NOT (or "
    "vice versa), fabricates an answer from an unresolved question, contradicts/omits the "
    "expected result, or never reaches a substantive answer when one was required, the "
    "goal is NOT met.\n\n"
    "Score 1 (pass) iff EVERY assertion is satisfied by the trajectory of final-answer "
    "records. Score 0 (fail) if any assertion is violated. Briefly justify your score, "
    "citing the specific final-answer record(s) — never an intermediate JSON/tool span."
)

RAG_TOOL_CALL_ORDERING = (
    "You are a strict binary evaluator checking whether a text-to-SQL agent grounded its "
    "SQL in retrieved schema BEFORE executing it.\n\n"
    "This agent runs a deterministic resolution graph, not a free-form tool loop. The "
    "prescribed flow for a NEW question is, in this order:\n"
    "  1. retrieve Knowledge Base schema for the question (graph phase — appears as KB "
    "chunks in the context, not a model tool call);\n"
    "  2. assemble + disambiguate a schema SLICE of the allowed tables/columns (graph phase);\n"
    "  3. generate SQL against that slice, then call the `execute_sql_query` tool to run it.\n\n"
    "Available tools: {available_tools}\n"
    "Session context (phase outputs + the execute_sql_query call, chronological): {context}\n\n"
    "Determine whether the retrieved schema context (KB chunks / schema slice) and the SQL "
    "generated from it appear BEFORE the `execute_sql_query` call. Ignore tool RESULTS and "
    "any other tools; judge only this ordering invariant.\n\n"
    "Score 1 (pass) iff a retrieved schema context is present and precedes the first "
    "`execute_sql_query` call (a follow-up that reuses already-in-scope schema and skips "
    "fresh retrieval is acceptable). ALSO score 1 if the conversation never executes SQL "
    "(e.g. it ends in a clarifying question) — there is no ordering violation to find. "
    "Score 0 (fail) if `execute_sql_query` is invoked with no retrieved schema context "
    "anywhere before it, or if SQL is executed against schema that was never "
    "retrieved/assembled. Briefly explain the offending ordering when you fail it."
)


# ─────────────────────────────────────────────────────────────────────────────
# Judge prompt text — VKG family (ontology_query agent)
# ─────────────────────────────────────────────────────────────────────────────
VKG_FINAL_ANSWER_FAITHFULNESS = (
    "You are a strict binary evaluator for a multi-turn virtual-knowledge-graph (VKG) "
    "text-to-SQL data agent.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Assertions about this session:\n{assertions}\n\n"
    "Exactly one assertion begins with 'The conversation's final answer matches:' — the "
    "text after that prefix is the EXPECTED final answer. The agent may take several "
    "turns (e.g. ask a clarifying question, get a reply, then answer); judge ONLY the "
    "FINAL substantive answer the agent gives at the end of the conversation.\n\n"
    "CRITICAL — which span is the agent's final answer: this agent runs a "
    "deterministic resolution graph, so each turn emits SEVERAL assistant 'chat' "
    "spans that are NOT the user-facing answer — in particular an intermediate "
    "SPARQL-GENERATION span (its content is a raw SPARQL query, e.g. 'SELECT "
    "(COUNT(*) AS ?n) ...') and a grounding record (its content begins "
    "'[executed_sql]'). DO NOT treat a SPARQL query, a SQL query, or a "
    "grounding/executed_sql record as the final answer — those are intermediate "
    "query artifacts, never the answer the user received. The real user-facing "
    "answer is carried in the span whose content is explicitly labelled as the "
    "final-answer record: its INPUT begins with 'Final-answer record for a "
    "deterministic graph turn' and its OUTPUT is the natural-language answer the "
    "user actually saw (e.g. 'The result is 15.', a clarification question, or a "
    "degraded-explanation sentence). Score the OUTPUT of THAT final-answer span "
    "against the expected answer; if multiple final-answer records exist "
    "(multi-turn), use the LAST one. Only if NO final-answer record span exists "
    "should you fall back to the last natural-language assistant message — and a "
    "bare SPARQL/SQL query is NOT a natural-language answer.\n\n"
    "Score 1 (pass) iff that final answer is factually consistent with the expected "
    "answer — same key numbers, entities, and conclusion. Score 0 (fail) if it "
    "contradicts the expected answer, invents figures, omits the requested result, or if "
    "the conversation never reaches a substantive answer (e.g. ends still asking for "
    "clarification). If no assertion carries the expected-answer prefix, score 0 and note "
    "that the ground truth is missing. Briefly justify your score."
)

VKG_GOAL_SUCCESS = (
    "You are a strict binary evaluator for a multi-turn virtual-knowledge-graph (VKG) "
    "text-to-SQL data agent. You decide whether the agent ACHIEVED THE USER'S GOAL "
    "across the whole conversation, judged against a set of assertions.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Assertions that define success for this session:\n{assertions}\n\n"
    "CRITICAL — which span is the agent's user-facing answer: this agent runs a "
    "DETERMINISTIC RESOLUTION GRAPH, so each turn emits SEVERAL intermediate 'chat' "
    "spans that are NOT what the user saw — in particular an intent-classification JSON "
    "span, an intermediate SPARQL-GENERATION span (its content is a raw SPARQL query, "
    "e.g. 'SELECT (COUNT(*) AS ?n) ...'), and a grounding record (its content begins "
    "'[executed_sql]'). DO NOT treat a SPARQL query, a SQL query, a grounding/"
    "executed_sql record, or an intent-classification JSON as the agent's answer, and do "
    "NOT conclude the agent 'returned JSON/SPARQL instead of asking a clarifying "
    "question' — those are internal graph phases. The real user-facing answer for each "
    "turn is the span whose INPUT begins with 'Final-answer record for a deterministic "
    "graph turn'; its OUTPUT is the natural-language text the user actually received — a "
    "substantive answer (e.g. 'The result is 15.'), a CLARIFICATION question (often "
    "tagged '[CLARIFICATION]' with the offered options), or a degraded explanation. Read "
    "the trajectory from these final-answer records, in order; for the conversation's "
    "final answer use the LAST one. Only if NO final-answer record exists should you fall "
    "back to the last natural-language assistant message — a bare SPARQL/SQL query is NOT "
    "a natural-language answer.\n\n"
    "CRITICAL — reconstructing the multi-turn trajectory: the session context may "
    "surface the spans of only the LAST turn (each turn is a separate trace), so it can "
    "look like a single turn even when the conversation had several. DO NOT conclude "
    "'there is only one turn' from the visible spans alone. The final-answer record's "
    "INPUT contains a '[conversation_so_far]' block listing every PRIOR turn of this same "
    "session (lines like 'user: ...' / 'assistant: ...', oldest first). You MUST read "
    "that block and treat those prior turns as real turns of the conversation, then append "
    "this record's own user_question + OUTPUT as the latest turn. Score the trajectory "
    "assertions against the FULL reconstructed conversation (the [conversation_so_far] "
    "lines + the final answer), NOT just the spans you can see directly.\n\n"
    "Scoring rules:\n"
    "  - A turn whose final-answer OUTPUT is a clarification question SATISFIES an "
    "assertion that the agent should ask for / keep seeking clarification — it is NOT a "
    "failure to answer.\n"
    "  - One assertion begins with 'The conversation's final answer matches:' — the text "
    "after that prefix is the EXPECTED final answer; the conversation's last substantive "
    "answer must be factually consistent with it (same key numbers, entities, conclusion).\n"
    "  - If the agent asks a clarifying question when an assertion says it should NOT (or "
    "vice versa), fabricates an answer from an unresolved question, contradicts/omits the "
    "expected result, or never reaches a substantive answer when one was required, the "
    "goal is NOT met.\n\n"
    "Score 1 (pass) iff EVERY assertion is satisfied by the trajectory of final-answer "
    "records. Score 0 (fail) if any assertion is violated. Briefly justify your score, "
    "citing the specific final-answer record(s) — never an intermediate SPARQL/SQL/JSON span."
)

VKG_SQL_GROUNDED = (
    "You are a strict binary grounding evaluator for a VKG text-to-SQL data agent.\n\n"
    "Session context (full conversation across ALL turns, including tool calls/results):\n"
    "{context}\n\n"
    "Available tools: {available_tools}\n\n"
    "This VKG agent runs a deterministic resolution graph: it fetches the ontology, "
    "assembles an ontology SLICE, generates SPARQL, then Phase 5 translates that SPARQL to "
    "SQL (Ontop) and runs it on Athena DIRECTLY. The fetch/slice steps are graph phases, "
    "not model tool calls, so do NOT expect get_ontology_from_neptune / "
    "disambiguate_query_terms / execute_sql_query tool CALLS. In the context, locate:\n"
    "  (a) the RETRIEVED ONTOLOGY CONTEXT — the ontology slice (classes/properties with "
    "mapsToTable/mapsToColumn) that is the ONLY schema the agent may use; and\n"
    "  (b) the EXECUTED SQL — surfaced in the Phase 5 output / reasoning.sqlQuery / "
    "executed_sql in the context (the SQL the agent actually ran on Athena).\n\n"
    "IMPORTANT — degraded runs: this agent REFUSES to execute SQL it cannot ground. If "
    "there is NO executed SQL in the context (the agent degraded rather than running "
    "ungrounded SQL), the grounding invariant was UPHELD — score 1 "
    "(pass) and note 'no SQL executed (degraded)'.\n"
    "Otherwise, score 1 (pass) iff EVERY table, column, and join in the executed SQL maps "
    "to a class/property/mapping in the retrieved ontology context (case-insensitive; "
    "tolerate aliases, quoted vs unquoted identifiers, and SQL builtins such as "
    "COUNT/SUM/DATE_TRUNC — those are not schema). Score 0 (fail) if the executed SQL "
    "references a table/column absent from the ontology context (hallucinated schema), or "
    "if SQL was executed but no ontology context is present. Briefly name the first "
    "offending identifier when you fail it."
)

VKG_TOOL_CALL_ORDERING = (
    "You are a strict binary evaluator checking whether a VKG text-to-SQL agent grounded "
    "its query in the retrieved ontology BEFORE executing SQL.\n\n"
    "This agent runs a deterministic resolution graph, not a tool loop. The prescribed "
    "flow for a NEW question is, in order: (1) fetch the ontology (graph phase — appears "
    "as the ontology slice in the context); (2) assemble + disambiguate an ontology SLICE; "
    "(3) generate SPARQL against the slice, then Phase 5 translates it to SQL (Ontop) and "
    "runs it on Athena.\n\n"
    "Available tools: {available_tools}\n"
    "Session context (phase outputs + SQL execution across ALL turns, chronological): "
    "{context}\n\n"
    "Score 1 (pass) iff a retrieved ontology context (the slice) is present and precedes "
    "the SQL execution (a follow-up reusing in-scope ontology and skipping a fresh fetch "
    "is acceptable). ALSO score 1 if the conversation never executes SQL (e.g. ends in a "
    "clarifying question) — there is no ordering violation. Score 0 (fail) if SQL is "
    "executed with no retrieved ontology context before it, or against schema never in "
    "the ontology. Briefly explain the offending ordering when you fail it."
)


# ─────────────────────────────────────────────────────────────────────────────
# ONLINE (reference-FREE) judge prompts — deployed by the CDK eval pipeline
# ─────────────────────────────────────────────────────────────────────────────
# The judges above (GoalSuccess / FinalAnswerFaithfulness) read ``{assertions}``
# — the expected answer threaded in as a SESSION assertion — so they are
# REFERENCE-BASED and run ON-DEMAND ONLY (batch eval in the notebooks). An ONLINE
# evaluation config samples LIVE traffic, which has NO ground truth, so AgentCore
# rejects any online evaluator that references a reference placeholder
# (``{assertions}`` / ``{expected_response}`` / ``{expected_tool_trajectory}`` /
# ``{actual_tool_trajectory}``). Online evaluators may use ONLY ``{context}`` and
# ``{available_tools}``.
#
# The CDK stack (``agentcore-eval-stack.ts``) deploys the four prompts below as
# its online judges. They are kept here, beside the on-demand judges, as the
# single documented source of truth so the two copies cannot drift again (the
# TS file carries a byte-identical copy because TypeScript cannot import this
# module). ``SqlGrounded`` (RAG) and ``ToolCallOrdering`` (RAG) are already
# reference-free, so the online copies ARE the on-demand copies — the CDK file
# must mirror ``RAG_SQL_GROUNDED`` / ``RAG_TOOL_CALL_ORDERING`` (and the VKG
# pair) verbatim. ``GoalSuccess`` is reference-based on-demand, so the online
# variants below are a distinct, reference-free reformulation: they judge
# whether the agent reached a coherent, grounded, user-facing answer (or an
# appropriate clarification) WITHOUT a known-correct answer to compare against.
# They replace the un-editable ``Builtin.GoalSuccessRate``, which mis-grades this
# deterministic-graph agent by mistaking an intermediate intent-classification
# JSON span for the assistant's turn.
ONLINE_RAG_GOAL_SUCCESS = (
    "You are a strict binary evaluator for a multi-turn text-to-SQL data agent "
    "running on LIVE traffic (no ground-truth answer is available). You decide "
    "whether the agent delivered a COHERENT, GROUNDED, user-facing resolution to "
    "the user's request across the whole conversation.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Available tools: {available_tools}\n\n"
    "CRITICAL — which span is the agent's user-facing answer: this agent runs a "
    "DETERMINISTIC RESOLUTION GRAPH, not a free-form chat. Each turn therefore "
    "emits SEVERAL intermediate spans that are NOT what the user saw — in "
    "particular an intent-classification JSON span (a small JSON object with an "
    "'intent' field such as 'data_query' and a numeric 'confidence'), a "
    "'SliceSufficiency' tool result (a JSON object with a boolean 'sufficient' "
    "field and a 'missing' list), and a follow-up question-rewrite span. DO NOT "
    "treat any of those intermediate artifacts as the agent's answer, and in "
    "particular do NOT conclude the agent 'returned JSON instead of asking a "
    "clarifying question' — that JSON is an internal graph phase. The real "
    "user-facing answer for each turn is the span whose INPUT begins with "
    "'Final-answer record for a deterministic graph turn'; its OUTPUT is the "
    "natural-language text the user actually received — a substantive answer, a "
    "CLARIFICATION question (often tagged '[CLARIFICATION]' with offered options), "
    "or a degraded explanation. Read the trajectory from these final-answer "
    "records, in order; for the conversation's outcome use the LAST one.\n\n"
    "Score 1 (pass) iff the conversation's final user-facing answer is a "
    "RESPONSIVE, GROUNDED resolution of the user's request: either (a) a "
    "substantive answer whose figures/entities are drawn from the executed SQL / "
    "tool results in the context (not invented), or (b) an APPROPRIATE "
    "clarification question when the request was genuinely ambiguous or "
    "underspecified, or (c) an honest degraded explanation when the agent could "
    "not ground an answer. Score 0 (fail) if the final answer fabricates "
    "figures/entities not supported by any tool result, ignores or contradicts the "
    "user's actual request, asks for clarification on an already-unambiguous "
    "request (needless stalling), or never reaches any user-facing answer at all. "
    "Briefly justify your score, citing the specific final-answer record — never "
    "an intermediate JSON/tool span."
)

ONLINE_VKG_GOAL_SUCCESS = (
    "You are a strict binary evaluator for a multi-turn virtual-knowledge-graph "
    "(VKG) text-to-SQL data agent running on LIVE traffic (no ground-truth answer "
    "is available). You decide whether the agent delivered a COHERENT, GROUNDED, "
    "user-facing resolution to the user's request across the whole conversation.\n\n"
    "Session context (full conversation across ALL turns — user prompts, assistant "
    "responses, tool calls, and tool results):\n{context}\n\n"
    "Available tools: {available_tools}\n\n"
    "CRITICAL — which span is the agent's user-facing answer: this agent runs a "
    "DETERMINISTIC RESOLUTION GRAPH, so each turn emits SEVERAL intermediate 'chat' "
    "spans that are NOT what the user saw — in particular an intent-classification "
    "JSON span, an intermediate SPARQL-GENERATION span (its content is a raw SPARQL "
    "query, e.g. 'SELECT (COUNT(*) AS ?n) ...'), and a grounding record (its "
    "content begins '[executed_sql]'). DO NOT treat a SPARQL query, a SQL query, a "
    "grounding/executed_sql record, or an intent-classification JSON as the agent's "
    "answer, and do NOT conclude the agent 'returned JSON/SPARQL instead of asking "
    "a clarifying question' — those are internal graph phases. The real user-facing "
    "answer for each turn is the span whose INPUT begins with 'Final-answer record "
    "for a deterministic graph turn'; its OUTPUT is the natural-language text the "
    "user actually received — a substantive answer (e.g. 'The result is 15.'), a "
    "CLARIFICATION question (often tagged '[CLARIFICATION]' with offered options), "
    "or a degraded explanation. Read the trajectory from these final-answer "
    "records, in order; for the conversation's outcome use the LAST one. Only if NO "
    "final-answer record exists should you fall back to the last natural-language "
    "assistant message — a bare SPARQL/SQL query is NOT a natural-language answer.\n\n"
    "Score 1 (pass) iff the conversation's final user-facing answer is a "
    "RESPONSIVE, GROUNDED resolution of the user's request: either (a) a "
    "substantive answer whose figures/entities are drawn from the executed SQL / "
    "tool results in the context (not invented), or (b) an APPROPRIATE "
    "clarification question when the request was genuinely ambiguous or "
    "underspecified, or (c) an honest degraded explanation when the agent could "
    "not ground an answer. Score 0 (fail) if the final answer fabricates "
    "figures/entities not supported by any tool result, ignores or contradicts the "
    "user's actual request, asks for clarification on an already-unambiguous "
    "request (needless stalling), or never reaches any user-facing answer at all. "
    "Briefly justify your score, citing the specific final-answer record — never an "
    "intermediate SPARQL/SQL/JSON span."
)


# Order matters: GoalSuccess is the headline metric, so it is created/returned
# FIRST. The notebooks unpack ``create_custom_judges`` positionally as
# ``GOAL_SUCCESS_ID, ANSWER_FAITHFUL_ID, SQL_GROUNDED_ID`` — keep this order in sync.
RAG_JUDGE_PROMPTS: Dict[str, str] = {
    "GoalSuccess": RAG_GOAL_SUCCESS,
    "FinalAnswerFaithfulness": RAG_FINAL_ANSWER_FAITHFULNESS,
    "SqlGrounded": RAG_SQL_GROUNDED,
}

VKG_JUDGE_PROMPTS: Dict[str, str] = {
    "GoalSuccess": VKG_GOAL_SUCCESS,
    "FinalAnswerFaithfulness": VKG_FINAL_ANSWER_FAITHFULNESS,
    "SqlGrounded": VKG_SQL_GROUNDED,
}

# Reference-free judges the CDK pipeline deploys onto the two query-agent online
# eval configs. ``agentcore-eval-stack.ts`` MUST carry byte-identical copies of
# these strings (TypeScript cannot import this module); the parity test in
# ``tests/`` asserts they match so the two copies can never silently drift.
ONLINE_RAG_JUDGE_PROMPTS: Dict[str, str] = {
    "GoalSuccess": ONLINE_RAG_GOAL_SUCCESS,
    "SqlGrounded": RAG_SQL_GROUNDED,
    "ToolCallOrdering": RAG_TOOL_CALL_ORDERING,
}

ONLINE_VKG_JUDGE_PROMPTS: Dict[str, str] = {
    "GoalSuccess": ONLINE_VKG_GOAL_SUCCESS,
    "SqlGrounded": VKG_SQL_GROUNDED,
    "ToolCallOrdering": VKG_TOOL_CALL_ORDERING,
}


def create_custom_judges(
    *,
    control_client,
    family: str,
    name_suffix: str = "",
) -> List[str]:
    """Create the three custom SESSION judges for one agent family and return their ids.

    All are binary (0/1) LLM-as-Judge evaluators on ``JUDGE_MODEL_ID``,
    created via ``CreateEvaluator`` on the ``bedrock-agentcore-control`` plane.
    The returned id list is ORDERED ``[GoalSuccess, FinalAnswerFaithfulness,
    SqlGrounded]`` — the order every notebook's ``CUSTOM_EVALUATOR_IDS`` expects.

    ``GoalSuccess`` is a custom replacement for the AWS-managed
    ``Builtin.GoalSuccessRate``. The builtin's instructions are not editable, so
    on this DETERMINISTIC-GRAPH agent it mistook an intermediate intent-
    classification JSON span (``{"intent":"data_query",...}``) for the assistant's
    turn and scored correctly-handled clarification conversations 0.0 ("returned
    JSON, not a clarifying question"). The custom judge carries the same "which
    span is the final answer / ignore intermediate graph phases" guidance the
    FAF judges use (keyed off ``answer_span.emit_answer_span``'s "Final-answer
    record" marker), so it grades the answer the user actually saw.

    ``ToolCallOrdering`` was removed: the deterministic graph guarantees schema
    retrieval always precedes ``execute_sql_query`` (or no SQL runs at all), so
    it scored 1.0 on every scenario and produced no diagnostic signal.

    Args:
        control_client: A boto3 ``bedrock-agentcore-control`` client.
        family: ``"rag"`` (metadata_query agent) or ``"vkg"`` (ontology_query
            agent) — selects which prompt set to register. Raises on any other
            value (fail loudly — a typo must not silently pick the wrong rubric).
        name_suffix: Suffix appended to each evaluator name to keep redeploys
            unique (``f"{name}_{suffix}"``). Defaults to a fresh 8-char uuid hex
            when empty, matching the notebooks' ``_SUFFIX`` behaviour.

    Returns:
        ``[goal_success_id, final_answer_faithfulness_id, sql_grounded_id]``.
    """
    prompts = _prompts_for_family(family=family)
    suffix = name_suffix or uuid.uuid4().hex[:8]
    return [
        _create_one_judge(
            control_client=control_client,
            name=name,
            instructions=prompts[name],
            name_suffix=suffix,
        )
        for name in ("GoalSuccess", "FinalAnswerFaithfulness", "SqlGrounded")
    ]


def _prompts_for_family(*, family: str) -> Dict[str, str]:
    """Return the judge-prompt dict for the named agent family.

    Args:
        family: ``"rag"`` or ``"vkg"`` (case-insensitive).

    Returns:
        The ordered prompt dict for that family.

    Raises:
        ValueError: if ``family`` is neither ``"rag"`` nor ``"vkg"``.
    """
    key = family.strip().lower()
    if key == "rag":
        return RAG_JUDGE_PROMPTS
    if key == "vkg":
        return VKG_JUDGE_PROMPTS
    raise ValueError(f"unknown judge family {family!r}; expected 'rag' or 'vkg'")


def _create_one_judge(
    *,
    control_client,
    name: str,
    instructions: str,
    name_suffix: str,
) -> str:
    """Create one binary SESSION LLM-as-Judge evaluator and return its evaluatorId.

    Args:
        control_client: A boto3 ``bedrock-agentcore-control`` client.
        name: Human-readable evaluator name (a ``_{suffix}`` is appended).
        instructions: The judge prompt text (may reference SESSION placeholders).
        name_suffix: Uniqueness suffix appended to ``name``.

    Returns:
        The created evaluator's ``evaluatorId``.
    """
    resp = control_client.create_evaluator(
        evaluatorName=f"{name}_{name_suffix}",
        level="SESSION",
        evaluatorConfig={
            "llmAsAJudge": {
                "instructions": instructions,
                "ratingScale": BINARY_SCALE,
                "modelConfig": {
                    "bedrockEvaluatorModelConfig": {
                        "modelId": JUDGE_MODEL_ID,
                        # maxTokens raised from 1024: JUDGE_MODEL_ID (Sonnet 5) has
                        # adaptive thinking on by default, and thinking tokens share
                        # the OUTPUT budget — a 1024 cap risks truncating the binary
                        # verdict before it emits. No temperature is set here
                        # (thinking is incompatible with temperature/top_p/top_k).
                        "inferenceConfig": {"maxTokens": 4096},
                    }
                },
            }
        },
    )
    return resp["evaluatorId"]
