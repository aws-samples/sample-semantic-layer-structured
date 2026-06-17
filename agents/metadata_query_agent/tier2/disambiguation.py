"""Phase 2 (RAG): term-level disambiguation over the Phase 1 structured payload.

Adapted from the ``disambiguate_query_terms`` ``@tool`` in
``agents/metadata_query_agent/main.py``, with two differences:

1. It runs **before** the slice is built, driven by the Phase 1 structured
   retrieval payload (``candidates`` + ``chunks_by_table``) rather than a
   cached ``kb_context`` JSON blob.
2. It adds Phase 2 trigger heuristics: a term hitting >1
   federated definition, a concept set spanning >1 domain, or a low-confidence
   top retrieval score all surface clarification.

The ``_query_terms`` and ``build_clarification`` helpers now live in
``agents.shared.disambiguation_common`` (shared with the VKG agent's Phase 2 /
Phase 3b guards). They are re-imported here so existing call sites — and the
Phase 3b slice-disambiguation guard — keep working unchanged and emit
byte-identical clarification payloads to the frontend.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Shared, mode-agnostic helpers (moved out of this module). Re-exported so
# ``from .disambiguation import _query_terms, build_clarification`` still works.
# Dual-import: repo root uses ``agents.shared``; the container has ``shared`` on
# PYTHONPATH directly (no top-level ``agents`` package).
try:
    from agents.shared.disambiguation_common import (  # noqa: F401
        _STOP_WORDS,
        _query_terms,
        build_clarification,
        inflection_variants,
    )
    from agents.shared.clarification import local_name
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.disambiguation_common import (  # type: ignore  # noqa: F401
        _STOP_WORDS,
        _query_terms,
        build_clarification,
        inflection_variants,
    )
    from shared.clarification import local_name  # type: ignore

# Top retrieval score below this floor → treat the whole retrieval as
# low-confidence and ask the user to clarify rather than guessing a table.
DISAMBIG_SCORE_FLOOR = 0.4


def analyze_terms(*, question: str, structured: Dict[str, Any],
                  recall_resolver=None,
                  resolved_names: Optional[set] = None) -> Dict[str, Any]:
    """Map query terms to tables using the Phase 1 structured payload.

    Args:
        question: The natural-language user question.
        structured: The Phase 1 retrieval payload — ``{candidates:
            [{table_id, score, column_id?}], chunks_by_table: {table_id:
            markdown}}`` from ``retrieve_kb_context_structured``.
        recall_resolver: Optional callable
            ``(term: str, candidate_table_ids: List[str]) -> Optional[str]``
            that consults the user's long-term lessons and returns the single
            ``table_id`` a prior session tied ``term`` to (or ``None``). Used to
            silently resolve an otherwise-ambiguous term from memory before
            escalating to a user clarification. ``None`` disables recall.
        resolved_names: Lower-cased local names of candidate tables the user
            already CHOSE on a prior turn (from a resolved clarification — see
            ``ClarificationResolution.chosen_names``). A pick is a positive,
            confident binding: any candidate whose local name is in this set is
            recorded as a CLEAR mapping and suppresses the low-confidence
            clarification, so the same question cannot re-clarify a table the
            user already confirmed. ``None``/empty on a normal turn. Without it,
            a low-confidence clarification is structurally unresolvable —
            picking a table never raises its cosine score above the floor.

    Returns:
        ``{status, mappings, ambiguities, unknown_terms, can_proceed,
        low_confidence}`` where ``status`` is CLEAR / AMBIGUOUS / UNKNOWN /
        LOW_CONFIDENCE.
    """
    candidates = structured.get('candidates', []) or []
    terms = _query_terms(question)
    resolved_names = {n.lower() for n in (resolved_names or set())}

    # --- low-confidence retrieval heuristic --------------------------------
    # The top candidate score gates the whole retrieval: if even the best
    # match is weak, the slice we'd build is untrustworthy.
    top_score = max((c.get('score', 0.0) for c in candidates), default=0.0)
    low_confidence = bool(candidates) and top_score < DISAMBIG_SCORE_FLOOR

    # Build table_name -> [{table, database}] from candidate table_ids
    # ("database.table"). A bare table name appearing under >1 database is the
    # core "term spans >1 federated definition" ambiguity signal.
    name_to_info: Dict[str, List[Dict[str, str]]] = {}
    databases: set = set()
    # Phase-1 cosine score per "database.table" — used to break a purely-fuzzy
    # multi-match by the router's own ranking instead of clarifying.
    score_by_tid: Dict[str, float] = {}
    for cand in candidates:
        tid = cand.get('table_id', '')
        if '.' in tid:
            database, table = tid.split('.', 1)
        else:
            database, table = '', tid
        if database:
            databases.add(database)
        table_l = table.lower().strip()
        if not table_l:
            continue
        score_by_tid[tid] = float(cand.get('score', 0.0) or 0.0)
        entry = {'table': table, 'database': database}
        name_to_info.setdefault(table_l, [])
        if entry not in name_to_info[table_l]:
            name_to_info[table_l].append(entry)

    mappings: Dict[str, Any] = {}
    ambiguities: List[Dict[str, Any]] = []
    unknown_terms: List[Dict[str, Any]] = []

    # Terms that matched a table by EXACT name (direct or plural/singular) — as
    # opposed to the fuzzy token/substring fallback below. An exact lexical match
    # is strong evidence the user named that table, stronger than a modest cosine
    # score, so it suppresses the low-confidence clarification (see the status
    # decision below). Fuzzy matches do NOT count: they are guesses.
    exact_match_terms: set = set()

    # --- multi-token phrase pre-pass (suppresses spurious clarifications) -----
    # A single term like "products" substring-matches every "*_product" table
    # (coverage_product, policy_product, invest_product) and would fire a 3-way
    # clarification. But the question usually disambiguates itself with an
    # ADJACENT word: "coverage products" names coverage_product uniquely. Before
    # the per-term loop, join each adjacent term pair (and its inflections) with
    # an underscore and look for an exact, UNIQUE candidate-table name match. When
    # found, bind both terms to that table and mark them resolved so the per-term
    # loop cannot re-raise them as ambiguous. Only a UNIQUE phrase match resolves
    # — a phrase hitting >1 table is left to the normal ambiguity path.
    phrase_resolved: set = set()
    for i in range(len(terms) - 1):
        a, b = terms[i], terms[i + 1]
        phrase_forms: set = set()
        for fa in inflection_variants(a):
            for fb in inflection_variants(b):
                phrase_forms.add(f"{fa}_{fb}")  # coverage_products, coverage_product
        phrase_matches: List[Dict[str, str]] = []
        for table_l, infos in name_to_info.items():
            if table_l in phrase_forms or any(
                    f in table_l for f in phrase_forms if len(f) >= 6):
                phrase_matches.append((table_l, infos))
        # Unique table name → confident phrase binding.
        unique_tables = {tl for tl, _ in phrase_matches}
        if len(unique_tables) == 1:
            _, infos = phrase_matches[0]
            # Bind to the single (table, database); prefer the first info entry.
            picked = infos[0]
            for t in (a, b):
                mappings[t] = {'status': 'CLEAR', 'table': picked['table'],
                               'database': picked['database'], 'confidence': 0.95,
                               'source': 'phrase'}
                exact_match_terms.add(t)
                phrase_resolved.add(t)

    for term in terms:
        # A term already bound by the phrase pre-pass is settled — skip it so it
        # cannot be re-raised as a single-token ambiguity.
        if term in phrase_resolved:
            continue
        # All number-inflections of the term (parties↔party, addresses↔address)
        # — used for both the exact-name match and the token fallback so an
        # irregular plural like "parties" still maps to the "party" table. A
        # naive rstrip('s') turned "parties" into "partie" and silently missed.
        term_forms = inflection_variants(term)
        matches = name_to_info.get(term, [])
        if not matches:
            # plural/singular fallback (still an exact NAME match, just inflected)
            for form in term_forms:
                matches = name_to_info.get(form, [])
                if matches:
                    break
        if matches:
            exact_match_terms.add(term)
        if not matches:
            # Token/substring fallback — a question term rarely equals a
            # multi-token table name (e.g. "codes" vs "admin_codes", "policy"
            # vs "policy_product"). Match a term against a table when one of the
            # term's inflected forms is an underscore token, or (for forms
            # >=4 chars) a substring of the table name (or vice versa). This is
            # what lets Phase 2 record a binding for these so the trace shows
            # the mapping instead of a bare "clear".
            for table_l, infos in name_to_info.items():
                tokens = set(table_l.split('_'))
                tok_forms = set()
                for tok in tokens:
                    tok_forms |= inflection_variants(tok)
                hit = bool(term_forms & tokens) or bool(term_forms & tok_forms)
                if not hit:
                    hit = any(len(f) >= 4 and (f in table_l or table_l in f)
                              for f in term_forms)
                if hit:
                    matches.extend(infos)
        if not matches:
            continue  # term doesn't name a table — not our concern at this phase
        # dedup by (table, database)
        unique = []
        seen_pairs: set = set()
        for m in matches:
            pair = (m['table'], m['database'])
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                unique.append(m)
        if len(unique) == 1:
            only = unique[0]
            mappings[term] = {'status': 'CLEAR', 'table': only['table'],
                              'database': only['database'], 'confidence': 0.9}
        else:
            # Genuinely ambiguous on this turn's retrieval — but the user may have
            # resolved this exact term in a PRIOR session. Consult long-term
            # lessons before surfacing a clarification.
            resolved_tid = None
            if recall_resolver is not None:
                cand_tids = [
                    (f"{m['database']}.{m['table']}" if m['database'] else m['table'])
                    for m in unique
                ]
                try:
                    resolved_tid = recall_resolver(term, cand_tids)
                except Exception:  # noqa: BLE001 — recall must never break Phase 2
                    resolved_tid = None
            if resolved_tid:
                # Map the recalled table_id back to its {table, database} entry.
                picked = next(
                    (m for m in unique if (
                        (f"{m['database']}.{m['table']}" if m['database']
                         else m['table']) == resolved_tid)),
                    None,
                )
                if picked is not None:
                    mappings[term] = {
                        'status': 'CLEAR', 'table': picked['table'],
                        'database': picked['database'], 'confidence': 0.85,
                        'source': 'memory',
                    }
                    continue
            # Purely-FUZZY multi-match: the term named no table exactly — it only
            # token/substring-matched several (e.g. "policies" → policy_product /
            # policy_loan_summary; "participants" → rider_participant /
            # life_participant). These are guesses, not a genuine federated-name
            # collision, and the head noun of a normal question shouldn't force a
            # clarification on every common word. Defer to Phase 1's ranking: bind
            # to the highest-scored candidate (the router already preferred it) at
            # reduced confidence and let the Phase 3 slice judge / bridge expansion
            # widen if needed. An EXACT-name multi-match (e.g. the same table name
            # under two databases) is a real ambiguity and still clarifies below.
            if term not in exact_match_terms:
                def _tid(m: Dict[str, str]) -> str:
                    return f"{m['database']}.{m['table']}" if m['database'] else m['table']
                best = max(unique, key=lambda m: score_by_tid.get(_tid(m), 0.0))
                mappings[term] = {
                    'status': 'CLEAR', 'table': best['table'],
                    'database': best['database'], 'confidence': 0.6,
                    'source': 'fuzzy_top_score',
                }
                continue
            ambiguities.append({'term': term, 'matches': unique})

    # A pick the user already made on a prior turn is a POSITIVE, confident
    # binding — not just a negative prune of the rivals (which Phase 1 already
    # applied). Record a CLEAR mapping for every surviving candidate the user
    # confirmed, and resolve any term that is still ambiguous purely between
    # confirmed candidates. This is what lets a LOW_CONFIDENCE clarification
    # converge: picking a table can never lift its cosine score above the floor,
    # so without treating the pick as confident the same question would
    # re-clarify forever (the "How many parties" loop).
    confirmed_picks: List[Dict[str, str]] = []
    if resolved_names:
        for cand in candidates:
            tid = cand.get('table_id', '')
            database, table = (tid.split('.', 1) if '.' in tid else ('', tid))
            if local_name(tid) in resolved_names:
                confirmed_picks.append({'table': table, 'database': database})
        # Drop ambiguities that are wholly between confirmed candidates — the
        # user already chose among them — and bind them to the confirmed pick.
        if confirmed_picks:
            still_ambiguous: List[Dict[str, Any]] = []
            picked = confirmed_picks[0]
            for amb in ambiguities:
                amb_names = {local_name(m.get('table', '')) for m in amb['matches']}
                if amb_names & resolved_names:
                    mappings[amb['term']] = {
                        'status': 'CLEAR', 'table': picked['table'],
                        'database': picked['database'], 'confidence': 1.0,
                        'source': 'clarification',
                    }
                else:
                    still_ambiguous.append(amb)
            ambiguities = still_ambiguous

    # concept set spans >1 domain → ambiguous (which domain did they mean?)
    multi_domain = len(databases) > 1 and not mappings and not ambiguities

    # An exact table-name match is stronger evidence than a cosine score: if at
    # least one term named a table exactly, trust those bindings and do NOT fire
    # the low-confidence clarification (which would otherwise force the user to
    # pick among the top-K candidates even though the head noun is unambiguous).
    # Without any exact match there is nothing lexical to trust, so a weak top
    # score still gates. A genuine multi-table term ambiguity is handled by the
    # ``ambiguities`` branch above and is unaffected by this. A user's confirmed
    # pick is treated the same way — picking a table is the strongest possible
    # evidence, so it likewise suppresses the low-confidence gate.
    has_exact_mapping = bool(exact_match_terms) or bool(confirmed_picks)

    if ambiguities:
        status, can_proceed = 'AMBIGUOUS', False
    elif low_confidence and not has_exact_mapping:
        status, can_proceed = 'LOW_CONFIDENCE', False
    elif multi_domain:
        status, can_proceed = 'AMBIGUOUS', False
    else:
        status, can_proceed = 'CLEAR', True

    return {
        'status': status,
        'mappings': mappings,
        'ambiguities': ambiguities,
        'unknown_terms': unknown_terms,
        'can_proceed': can_proceed,
        'low_confidence': low_confidence,
    }
