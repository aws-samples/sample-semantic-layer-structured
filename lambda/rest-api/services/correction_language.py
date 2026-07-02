"""Correction-language detection for the admin "Monitoring" tab.

A production signal we want to surface per semantic layer is the share of
user turns that CORRECT the agent — e.g. "that's the wrong table", "you're
missing the fraud filter", "I meant policies, not products". A rising
correction rate is an early warning that the layer's schema/metadata or the
agent's resolution is drifting from what users expect, and it should track the
lessons the agent extracts into AgentCore Memory (each correction is a
candidate mapping-lesson).

This is a READ-SIDE heuristic: it runs over already-persisted user-turn text
at monitoring-aggregation time, so it needs no agent-runtime change and applies
retroactively to historical turns. It is deliberately precise (anchored
correction phrasings) over greedy — a false "correction" inflates the signal
and erodes trust in the dashboard, so when in doubt we DON'T flag.
"""
from __future__ import annotations

import re
from typing import List

# Schema/data nouns a correction tends to name. Shared by several patterns so
# the noun set stays in one place. "one"/"thing" are included because users say
# "you picked the wrong one" — but only ever after an explicit correction anchor
# (a "wrong"/"not the" lead-in), never on their own.
_SCHEMA_NOUN = (r"(table|column|field|filter|join|metric|query|schema|"
                r"data(set)?|number|value|result|answer|one|thing)s?")

# Each pattern targets an UNAMBIGUOUS correction phrasing. The hard problem is
# that "X, not Y" is syntactically identical whether it CORRECTS the agent
# ("policies, not products") or merely DESCRIBES a filter ("active, not
# cancelled, policies"). A bare ",\s*not" comma pattern can't tell them apart and
# over-fires on ordinary analytical requests, inflating the headline correction
# rate. So every negated/contrastive form here is anchored to a CORRECTING
# SUBJECT — a demonstrative ("that/this/it"), a 2nd-person reference to the agent
# ("you ..."), or a back-referential restatement verb ("I meant/asked for/said")
# — which a filter description lacks. ``\b`` word boundaries keep "wrongful" /
# "missingno" from matching. All matched case-insensitively.
_CORRECTION_PATTERNS: List[re.Pattern] = [
    # "wrong <schema-noun>" — the canonical "that's the wrong table/column/join".
    re.compile(r"\bwrong\s+" + _SCHEMA_NOUN + r"\b", re.IGNORECASE),
    # "you <verb> the wrong ..." — "you picked the wrong one", "you used the
    # wrong join". The "you ... wrong" frame is unambiguously a correction.
    re.compile(r"\byou\s+\w+(\s+\w+)?\s+the\s+wrong\b", re.IGNORECASE),
    # clause-initial verdict "wrong, ..." / "wrong. ..." — a bare verdict on the
    # agent ("wrong, I wanted premiums"). Anchored to start or after sentence
    # punctuation so it never matches "the wrong table" or "is this wrong".
    re.compile(r"(^|[.!?]\s+)wrong[,.!]", re.IGNORECASE),
    # "<schema-noun> is wrong" — the inverted form.
    re.compile(r"\b(table|column|field|filter|join|metric|query|schema|"
               r"result|answer|number|value)s?\s+(is|are|was|were)\s+wrong\b",
               re.IGNORECASE),
    # "you're/you are missing the <something>" and "missing the fraud filter".
    re.compile(r"\b(you('?re| are)\s+)?missing\s+(the|a|an|that|my)\b",
               re.IGNORECASE),
    re.compile(r"\bmissing\s+(the\s+)?(filter|column|field|join|table|"
               r"condition|where\s+clause)\b", re.IGNORECASE),
    # "you forgot / you left out / you missed ..."
    re.compile(r"\byou\s+(forgot|left\s+out|missed|skipped|ignored)\b",
               re.IGNORECASE),
    # "that's not right/correct/what", "this isn't correct", "this isn't right".
    re.compile(r"\b(that('?s| is)|this\s+is|it('?s| is))\s+"
               r"(not\s+(right|correct|what)|incorrect|wrong)\b", re.IGNORECASE),
    # "isn't/aren't/wasn't right|correct" — the contracted-negation form.
    re.compile(r"\b(is|are|was|were)n'?t\s+(right|correct|what)\b", re.IGNORECASE),
    # "that's incorrect" / "incorrect <noun>".
    re.compile(r"\bincorrect\b", re.IGNORECASE),
    # DEMONSTRATIVE-anchored negation of a data object: "that is not the data I
    # need", "that's not the right table". The "that/this/it ... not" subject is
    # what distinguishes a correction from the interrogative "which is not the
    # table?" (subject "which" is excluded) or a filter "active, not cancelled"
    # (no demonstrative subject).
    re.compile(r"\b(that|this|it)(('?s)|\s+(is|was))?\s+not\s+"
               r"(the\s+)?(\w+\s+){0,2}" + _SCHEMA_NOUN + r"\b", re.IGNORECASE),
    # RESTATEMENT + negation: a back-referential verb ("I meant/asked for/said",
    # "you meant/said") in the SAME clause as a "not" — "I meant policies, not
    # products", "I asked for active policies not all of them". Requiring the
    # "not" keeps it from firing on ordinary present-tense requests ("I want
    # totals" is excluded — only past/back-referential verbs, and only with a
    # negation). [^.?!]* stays within one sentence; it is linear (no nested
    # quantifier) so there is no ReDoS exposure.
    re.compile(r"\b(i\s+(meant|asked\s+for|wanted|said)|"
               r"you\s+(meant|said))\b[^.?!]*\bnot\b", re.IGNORECASE),
    # "I meant <noun>" with NO "to" — "I meant policies" (a correction), but NOT
    # "I meant to ask about premiums" (a soft intent statement). The negative
    # lookahead on "to" is the discriminator.
    re.compile(r"\bi\s+meant\b(?!\s+to\b)", re.IGNORECASE),
    # "not the <schema-noun>" when it OPENS a clause (start / after comma-dash) —
    # the contrastive "not the products table — the policy_product one". Anchored
    # to clause start so it won't match mid-sentence filter phrasing.
    #
    # KNOWN RECALL LIMIT: a contrastive that NAMES the replacement only AFTER the
    # comma ("should be the party table, not holding") is NOT matched here,
    # because a "<schema-noun> ..., not <word>" pattern is indistinguishable from
    # an ordinary filter description ("group the table by month, not by year")
    # and would reopen the filter-false-positive class. We accept the miss: the
    # correction rate is a directional signal, and a false LOW is safer than the
    # false HIGH that pattern caused.
    re.compile(r"(^|[,—–-]\s*)not\s+(the\s+)?(\w+\s+)?"
               r"(table|column|field|filter|join|metric|query|schema)s?\b",
               re.IGNORECASE),
    # "instead of the <schema-noun>" — a correction only when it swaps a
    # concrete schema object ("use coverage instead of the holding table").
    # "revenue instead of cost" has no schema noun and is NOT counted.
    re.compile(r"\binstead\s+of\s+(the\s+)?(\w+\s+)?"
               r"(table|column|field|filter|join|metric|query|schema)s?\b",
               re.IGNORECASE),
]


def is_correction(text: str) -> bool:
    """Return True if a user turn reads as a CORRECTION of the agent.

    Args:
        text: The user-turn plain text (already PII-redacted on the write
            path; this only inspects phrasing, never stores it).

    Returns:
        True when any anchored correction pattern matches; False otherwise
        (including for empty/non-string input — fail closed, do not over-flag).
    """
    if not text or not isinstance(text, str):
        return False
    return any(p.search(text) for p in _CORRECTION_PATTERNS)


def matched_phrases(text: str, *, max_len: int = 120) -> List[str]:
    """Return the distinct correction substrings matched in ``text``.

    Used to surface a few concrete (already-redacted) examples in the
    Monitoring tab so an admin can see WHY a turn was counted as a correction
    — not just the aggregate percentage.

    Args:
        text: The user-turn plain text.
        max_len: Truncate each returned snippet to this many characters.

    Returns:
        Distinct matched snippets (order-preserving), each <= ``max_len``.
    """
    if not text or not isinstance(text, str):
        return []
    seen = set()
    out: List[str] = []
    for pattern in _CORRECTION_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0).strip()[:max_len]
            key = snippet.lower()
            if snippet and key not in seen:
                seen.add(key)
                out.append(snippet)
    return out
