"""Recall-phase disambiguation (Hybrid): term -> candidate mapping + clarification.

Copy-and-own of the RAG agent's Phase 2 / Phase 3b disambiguation
(``metadata_query_agent/tier2/disambiguation.py``) together with the
mode-agnostic helpers it used to import from ``agents.shared`` (``_query_terms``,
``build_clarification``, ``inflection_variants``, ``local_name``). Under the
Hybrid copy-and-own isolation rule none of those may be imported, so they are
inlined here verbatim.

The module maps significant question terms to the recalled candidate tables and
surfaces a clarification when a term is genuinely ambiguous (spans >1 federated
definition), the concept set spans >1 domain, or the top retrieval score is too
low to trust. It is importable so later Hybrid tasks (the graph / clarification
resolution phases) can drive it; standard library ONLY.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Top retrieval score below this floor → treat the whole retrieval as
# low-confidence and ask the user to clarify rather than guessing a table.
DISAMBIG_SCORE_FLOOR = 0.4

# Question-word / filler stop-words removed before matching terms to tables, so
# a question's structural words never masquerade as table references.
_STOP_WORDS = {
    'how', 'many', 'what', 'which', 'who', 'where', 'when', 'why',
    'show', 'me', 'get', 'find', 'list', 'give', 'tell', 'count',
    'are', 'is', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
    'do', 'does', 'did', 'can', 'could', 'would', 'should', 'will',
    'all', 'the', 'a', 'an', 'any', 'some', 'each', 'every',
    'this', 'that', 'these', 'those',
    'from', 'with', 'their', 'for', 'in', 'on', 'at', 'to', 'of',
    'and', 'or', 'not', 'no', 'by', 'as', 'per',
    'there', 'total', 'number', 'records', 'entries', 'items',
    'please', 'just', 'only', 'top', 'first', 'last', 'latest',
}


def inflection_variants(word: str) -> set:
    """Return ``word`` plus its simple English singular/plural inflections.

    Matches a question term against a table name across number (``parties`` ↔
    ``party``, ``addresses`` ↔ ``address``). A naive ``rstrip('s')`` mishandles
    the irregular endings (``parties`` → ``partie``, ``boxes`` → ``boxe``), so
    variants are generated in BOTH directions:

      * ``-y`` → ``-ies`` and ``-ies`` → ``-y`` (party ↔ parties)
      * sibilant + ``-es`` and ``-es`` → bare (address ↔ addresses, box ↔ boxes)
      * ``-s`` → bare and bare → ``-s`` (table ↔ tables)

    Args:
        word: A single query term or name token.

    Returns:
        The lower-cased set of candidate forms to test for an exact name match
        (always including the original word).
    """
    w = (word or "").strip().lower()
    if not w:
        return set()
    variants = {w}
    # Pluralizing direction — term is singular, the name might be plural.
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        variants.add(w[:-1] + "ies")  # party -> parties
    if w.endswith(("s", "x", "z", "ch", "sh")):
        variants.add(w + "es")        # address -> addresses, box -> boxes
    variants.add(w + "s")             # table -> tables
    # Singularizing direction — term is plural, the name might be singular.
    if w.endswith("ies") and len(w) > 3:
        variants.add(w[:-3] + "y")    # parties -> party
    if w.endswith("es") and len(w) > 2:
        variants.add(w[:-2])          # addresses -> address, boxes -> box
    if w.endswith("s") and len(w) > 1:
        variants.add(w[:-1])          # tables -> table
    return variants


def _query_terms(question: str) -> List[str]:
    """Return the significant lower-cased terms in ``question``.

    Drops stop-words and short tokens so only words that could name a table
    survive.

    Args:
        question: The natural-language user question.

    Returns:
        The list of significant terms, lower-cased, in question order.
    """
    words = re.findall(r'\b\w+\b', question.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


def local_name(identifier: str) -> str:
    """Return the lower-cased last segment of a table id / IRI / option id.

    Normalises ``normalized.party_banking`` → ``party_banking`` so candidate ids
    and clarification option ids compare on equal footing.

    Args:
        identifier: A candidate id, IRI, or clarification option id/label.

    Returns:
        The lower-cased trailing segment.
    """
    s = (identifier or "").strip()
    # Split on the rightmost structural separator (IRI fragment / path / db.table).
    for sep in ("#", "/", "."):
        if sep in s:
            s = s.rsplit(sep, 1)[1]
    return s.lower()


def build_clarification(*, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the ``needs_clarification`` frontend payload from ambiguity ``items``.

    Shape::

        {"needs_clarification": true,
         "clarification_question": "...",
         "options": [{"id": "...", "label": "..."}],
         "terms": ["..."]}

    Args:
        items: Each item has a ``term`` and a ``matches`` list of
            ``{table, database}`` (optionally ``column`` / ``label``) dicts.

    Returns:
        The clarification payload the frontend renders as selectable chips.
    """
    options: List[Dict[str, str]] = []
    seen: set = set()
    terms: List[str] = []
    for item in items:
        term = item.get('term', '')
        if term:
            terms.append(term)
        for match in item.get('matches', []):
            table = match.get('table', '')
            database = match.get('database', '')
            column = match.get('column', '')
            opt_id = table if not column else f"{table}.{column}"
            if opt_id in seen:
                continue
            seen.add(opt_id)
            label = match.get('label') or (
                f"{table}" + (f".{column}" if column else ""))
            if database:
                label = f"{label} (database: {database})"
            options.append({'id': opt_id, 'label': label})

    if terms:
        joined = "', '".join(terms)
        question = (
            f"Which interpretation of '{joined}' do you mean?"
            if len(terms) == 1
            else f"Could you clarify which interpretation you mean for: '{joined}'?"
        )
    else:
        question = "Could you clarify your request?"

    return {
        'needs_clarification': True,
        'clarification_question': question,
        'options': options,
        'terms': terms,
    }


def analyze_terms(*, question: str, structured: Dict[str, Any],
                  recall_resolver=None,
                  resolved_names: Optional[set] = None) -> Dict[str, Any]:
    """Map query terms to candidate tables using the recall structured payload.

    Args:
        question: The natural-language user question.
        structured: The recall payload — ``{candidates: [{table_id, score,
            column_id?}], ...}``.
        recall_resolver: Optional callable ``(term, candidate_table_ids) ->
            Optional[table_id]`` that consults prior lessons to silently resolve an
            otherwise-ambiguous term from memory. ``None`` disables recall.
        resolved_names: Lower-cased local names of candidate tables the user
            already CHOSE on a prior clarification. A confirmed pick is a
            confident binding that suppresses the low-confidence clarification so
            the same question cannot re-clarify a table the user confirmed.
            ``None`` on a normal turn.

    Returns:
        ``{status, mappings, ambiguities, unknown_terms, can_proceed,
        low_confidence}`` where ``status`` is CLEAR / AMBIGUOUS / UNKNOWN /
        LOW_CONFIDENCE.
    """
    candidates = structured.get('candidates', []) or []
    terms = _query_terms(question)
    resolved_names = {n.lower() for n in (resolved_names or set())}

    # --- low-confidence retrieval heuristic --------------------------------
    # The top candidate score gates the whole retrieval: if even the best match
    # is weak, the slice we'd build is untrustworthy.
    top_score = max((c.get('score', 0.0) for c in candidates), default=0.0)
    low_confidence = bool(candidates) and top_score < DISAMBIG_SCORE_FLOOR

    # Build table_name -> [{table, database}] from candidate ids ("database.table").
    # A bare table name under >1 database is the core federated-collision signal.
    name_to_info: Dict[str, List[Dict[str, str]]] = {}
    databases: set = set()
    # Recall cosine score per "database.table" — breaks a purely-fuzzy multi-match
    # by the router's own ranking instead of clarifying.
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

    # Terms that matched a table by EXACT name (direct or inflected) — stronger
    # evidence than a modest cosine score, so they suppress the low-confidence
    # clarification. Fuzzy token/substring matches do NOT count: they are guesses.
    exact_match_terms: set = set()

    # --- multi-token phrase pre-pass (suppresses spurious clarifications) -----
    # A single term like "products" substring-matches every "*_product" table and
    # would fire a spurious clarification, but an adjacent word usually
    # disambiguates ("coverage products" → coverage_product). Join each adjacent
    # term pair (and inflections) with an underscore; a UNIQUE candidate-name match
    # binds both terms and marks them resolved so the per-term loop can't re-raise
    # them. A phrase hitting >1 table is left to the normal ambiguity path.
    phrase_resolved: set = set()
    for i in range(len(terms) - 1):
        a, b = terms[i], terms[i + 1]
        phrase_forms: set = set()
        for fa in inflection_variants(a):
            for fb in inflection_variants(b):
                phrase_forms.add(f"{fa}_{fb}")
        phrase_matches: List[Any] = []
        for table_l, infos in name_to_info.items():
            if table_l in phrase_forms or any(
                    f in table_l for f in phrase_forms if len(f) >= 6):
                phrase_matches.append((table_l, infos))
        unique_tables = {tl for tl, _ in phrase_matches}
        if len(unique_tables) == 1:
            _, infos = phrase_matches[0]
            picked = infos[0]
            for t in (a, b):
                mappings[t] = {'status': 'CLEAR', 'table': picked['table'],
                               'database': picked['database'], 'confidence': 0.95,
                               'source': 'phrase'}
                exact_match_terms.add(t)
                phrase_resolved.add(t)

    for term in terms:
        # A term already bound by the phrase pre-pass is settled — skip it.
        if term in phrase_resolved:
            continue
        # All number-inflections of the term, for both exact-name match and the
        # token fallback, so an irregular plural like "parties" maps to "party".
        term_forms = inflection_variants(term)
        matches = name_to_info.get(term, [])
        if not matches:
            for form in term_forms:
                matches = name_to_info.get(form, [])
                if matches:
                    break
        if matches:
            exact_match_terms.add(term)
        if not matches:
            # Token/substring fallback — a question term rarely equals a
            # multi-token table name. Match when an inflected form is an underscore
            # token of the table name, or (for forms >=4 chars) a substring.
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
            # Genuinely ambiguous on this turn — but the user may have resolved
            # this exact term in a PRIOR session. Consult long-term lessons first.
            resolved_tid = None
            if recall_resolver is not None:
                cand_tids = [
                    (f"{m['database']}.{m['table']}" if m['database'] else m['table'])
                    for m in unique
                ]
                try:
                    resolved_tid = recall_resolver(term, cand_tids)
                except Exception:  # noqa: BLE001 — recall must never break analysis
                    resolved_tid = None
            if resolved_tid:
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
            # Purely-FUZZY multi-match (the term named no table exactly, it only
            # token/substring-matched several): a guess, not a real collision, so
            # defer to the recall ranking — bind to the highest-scored candidate at
            # reduced confidence. An EXACT-name multi-match is a real ambiguity and
            # still clarifies below.
            if term not in exact_match_terms:
                def _tid(m: Dict[str, str]) -> str:
                    return (f"{m['database']}.{m['table']}"
                            if m['database'] else m['table'])
                best = max(unique, key=lambda m: score_by_tid.get(_tid(m), 0.0))
                mappings[term] = {
                    'status': 'CLEAR', 'table': best['table'],
                    'database': best['database'], 'confidence': 0.6,
                    'source': 'fuzzy_top_score',
                }
                continue
            ambiguities.append({'term': term, 'matches': unique})

    # A prior-turn pick is a POSITIVE confident binding. Record a CLEAR mapping for
    # every confirmed candidate and resolve any ambiguity purely between confirmed
    # candidates — this is what lets a LOW_CONFIDENCE clarification converge
    # (picking a table never lifts its cosine score above the floor by itself).
    confirmed_picks: List[Dict[str, str]] = []
    if resolved_names:
        for cand in candidates:
            tid = cand.get('table_id', '')
            database, table = (tid.split('.', 1) if '.' in tid else ('', tid))
            if local_name(tid) in resolved_names:
                confirmed_picks.append({'table': table, 'database': database})
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

    # An exact table-name match (or a confirmed pick) is stronger than a cosine
    # score: if present, trust the bindings and do NOT fire the low-confidence
    # clarification. Without any exact match there is nothing lexical to trust, so
    # a weak top score still gates.
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
