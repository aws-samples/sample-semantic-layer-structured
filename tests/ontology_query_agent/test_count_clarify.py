from agents.ontology_query_agent.tier2.workflow import _collapse_shared_stem_siblings

B = "http://x/abc"


def test_collapses_party_siblings_when_base_absent():
    # 'parties' — base Party NOT in the match set; siblings share 'Party' stem.
    cm = [f"{B}/PartyBanking", f"{B}/PartyLicense"]
    out = _collapse_shared_stem_siblings("parties", cm, cm)
    assert out in cm  # collapses to one sibling (highest-ranked), does NOT clarify


def test_collapses_including_base_when_present():
    cm = [f"{B}/Party", f"{B}/PartyBanking"]
    out = _collapse_shared_stem_siblings("parties", cm, cm)
    assert out == f"{B}/Party"  # base is shortest/earliest → wins if ranked first


def test_does_not_collapse_party_and_participant():
    # CRITICAL: 'party' must NOT collapse Party + Participant — genuine distinct
    # entities that merely share the leading fragment 'part'. Must return None
    # (so the caller still clarifies the real tie).
    cm = [f"{B}/Party", f"{B}/Participant"]
    out = _collapse_shared_stem_siblings("party", cm, cm)
    assert out is None


def test_does_not_collapse_singular_email_distinct_classes():
    # 'email' (SINGULAR) names two genuinely distinct classes EmailMessage vs
    # EmailCampaign — a real entity choice. The plural-only gate keeps this a
    # clarification (returns None), matching the pre-existing genuine-tie guard
    # test_phase2_two_distinct_classes_still_clarifies in tests/unit.
    cm = [f"{B}/EmailMessage", f"{B}/EmailCampaign"]
    out = _collapse_shared_stem_siblings("email", cm, cm)
    assert out is None


def test_collapses_plural_holdings_siblings():
    # 'holdings' (PLURAL collection) → Holding subtypes: a non-choice, collapses.
    cm = [f"{B}/HoldingLoan", f"{B}/HoldingPayout"]
    out = _collapse_shared_stem_siblings("holdings", cm, cm)
    assert out in cm


def test_single_match_returns_none():
    assert _collapse_shared_stem_siblings("party", [f"{B}/Party"], [f"{B}/Party"]) is None


def test_short_term_returns_none():
    assert _collapse_shared_stem_siblings("ab", [f"{B}/Abc", f"{B}/Abd"], []) is None


def test_real_inflection_forms_drops_prefix_stems():
    """VKG round-1 root cause: inflection_variants('parties') over-generates the
    prefix stems 'parti'/'partie', which substring-match the UNRELATED entity
    'participant' (LifeParticipant), poisoning the candidate set and driving a
    spurious 'which interpretation of parties?' clarify. _real_inflection_forms
    must drop those prefix-of-token stems while keeping party/parties."""
    from agents.ontology_query_agent.tier2.workflow import _real_inflection_forms
    forms = _real_inflection_forms("parties")
    assert "party" in forms and "parties" in forms
    assert "parti" not in forms and "partie" not in forms
    # The dropped stems were the ones matching 'participant':
    assert not any(f in "participant" for f in forms if len(f) >= 4), forms
