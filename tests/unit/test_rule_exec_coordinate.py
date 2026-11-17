"""rule_executor — WHICH Athena catalog/database a governed rule runs against.

The regression under test, observed live on 2026-08-18 against the hybrid layer
``hybridsnowflake``: a PUBLISHED rule matched (``rule_hit``) and then died with
``SCHEMA_NOT_FOUND: Schema 'cards' does not exist``, falling through to Tier 2.
``cards`` is a TABLE in that layer, not a schema.

The cause is Ontop's ``deriveDatabase``, which returns ``splitDottedTable(...)[0]``
— the schema half of a DOTTED ``vkg:mapsToTable``. A VKG layer stores it dotted
(``FHIR_R4.FHIR_RESOURCES``) so that is right; a hybrid layer's thin VKG stores
the BARE table name on purpose (``kb_table_index`` matches on it), so ``[0]`` is
the alphabetically-first table. The same defect was fixed in the publish gate
first — see ``test_rule_publish_coordinate.py``.

So the coordinate cannot be derived inside the executor: it depends on the layer
type, which only the caller knows. ``coordinate`` is therefore a REQUIRED keyword
that a caller may set to None to mean "the translation is authoritative here",
and the executor reports back the pair it actually ran with rather than the
translation's — a returned coordinate that was never used would be a log lie.
"""
from typing import Any, Dict, List, Optional, Tuple

import pytest

from agents.shared.rule_executor import RuleExecutionError, execute_rule

# The live hybrid layer's real coordinate, and the wrong pair Ontop derives for
# it. Kept concrete so the test names the actual failure rather than a fiction.
LAYER_CATALOG = "s3tablescatalog/semantic-layer-dev-analytics-tables"
LAYER_DATABASE = "bird_card_games"
ONTOP_WRONG_DATABASE = "cards"


def _threshold() -> Any:
    """Build an approved duck-typed threshold a condition can reference.

    :returns: An object with ``Threshold``'s executor-visible surface.
    """
    return type("T", (), {
        "threshold_id": "THR-1", "value": 300.0, "unit": "cards",
        "approved_by": "alice", "source_span": "more than 300 cards",
        "is_approved": lambda self: True})()


class _Rule:
    """Duck-typed PUBLISHED, evaluable rule — keeps this independent of models."""

    def __init__(self, **over: Any) -> None:
        """Build an evaluable rule; ``over`` replaces any attribute."""
        self.rule_id = "RULE-QA-large-set-300"
        self.name = "large set by base set size"
        self.lifecycle = "PUBLISHED"
        self.compiled_sparql = "SELECT ?subject WHERE { ?subject a <x> }"
        self.missing_inputs: List[str] = []
        self.conditions = [type("C", (), {
            "binding": "https://ex.org/vkg#baseSetSize",
            "operator": ">", "threshold_id": "THR-1", "value": None})()]
        self.conclusion = type("C", (), {
            "outcome": "https://ex.org/vkg#LargeSet",
            "epistemic_status": "associatedWith"})()
        self.thresholds = [_threshold()]
        self.provenance = None
        for key, value in over.items():
            setattr(self, key, value)


def _wrong_translate(**_kw: Any) -> Dict[str, Any]:
    """Stand in for the gateway returning Ontop's DEFECTIVE hybrid coordinate."""
    return {
        "sql": "SELECT id\nFROM cards",
        "database": ONTOP_WRONG_DATABASE,
        "catalog": "AwsDataCatalog",
    }


def _capturing_execute() -> Tuple[List[Dict[str, Any]], Any]:
    """Return a (calls, execute_sql) pair recording every execute_sql kwarg.

    :returns: The list the seam appends to, and the seam itself.
    """
    calls: List[Dict[str, Any]] = []

    def execute_sql(**kw: Any) -> Dict[str, Any]:
        """Record the call and return one successful row."""
        calls.append(kw)
        return {"columns": ["n"], "rows": [[52]], "state_change_reason": None}

    return calls, execute_sql


def _run(coordinate: Optional[Tuple[str, str]]) -> Tuple[
    List[Dict[str, Any]], Dict[str, Any]
]:
    """Execute the rule with a defective translation and the given coordinate.

    :param coordinate: The ``(catalog_id, database_name)`` the caller supplies, or
        None to defer to the translation.
    :returns: ``(execute_sql calls, execute_rule result)``.
    """
    calls, execute_sql = _capturing_execute()
    out = execute_rule(
        rule=_Rule(), ontology_json={"classes": {}}, ontology_id="ont-1",
        translate=_wrong_translate, execute_sql=execute_sql,
        coordinate=coordinate,
    )
    return calls, out


def test_a_layer_coordinate_overrides_the_translations() -> None:
    """THE regression: the hybrid layer's own pair must reach Athena.

    Ontop hands back ``cards`` as the database. If that is what executes, the
    query dies SCHEMA_NOT_FOUND and the rule tier silently degrades to Tier 2 —
    which answered with a different number, so nothing surfaced to the user.
    """
    calls, _out = _run(coordinate=(LAYER_CATALOG, LAYER_DATABASE))
    assert len(calls) == 1
    assert calls[0]["database_name"] == LAYER_DATABASE
    assert calls[0]["catalog_id"] == LAYER_CATALOG
    assert calls[0]["database_name"] != ONTOP_WRONG_DATABASE


def test_the_returned_coordinate_is_the_one_actually_used() -> None:
    """Reporting the translation's pair would misname where the rows came from.

    The result dict feeds the response metadata and the log line, so a coordinate
    that was never used would send a future reader of this same bug back to the
    wrong place.
    """
    _calls, out = _run(coordinate=(LAYER_CATALOG, LAYER_DATABASE))
    assert out["database"] == LAYER_DATABASE
    assert out["catalog"] == LAYER_CATALOG
    assert out["rows"] == [[52]]


def test_coordinate_none_keeps_the_translations_pair() -> None:
    """The VKG contract: a dotted ``mapsToTable`` makes Ontop's derivation right.

    The VKG agent has no layer-config coordinate anywhere — its Phase 5 and its
    ``execute_sparql`` tool both use the translation. None must therefore stay a
    supported, explicit choice rather than becoming a second defect.
    """
    calls, out = _run(coordinate=None)
    assert calls[0]["database_name"] == ONTOP_WRONG_DATABASE
    assert calls[0]["catalog_id"] == "AwsDataCatalog"
    assert out["database"] == ONTOP_WRONG_DATABASE


@pytest.mark.parametrize(
    "coordinate",
    [("", LAYER_DATABASE), (LAYER_CATALOG, ""), ("", "")],
    ids=["no-catalog", "no-database", "neither"],
)
def test_a_half_or_fully_empty_coordinate_is_refused(
    coordinate: Tuple[str, str],
) -> None:
    """A supplied-but-incomplete pair is a broken config, so fail loudly.

    ``_catalog_database_for_layer`` returns ``("", "")`` when a config carries no
    ``dataSources``. Silently falling back to the translation there is precisely
    the behaviour under repair: it would restore the SCHEMA_NOT_FOUND with no
    trace of why. None means "defer"; an empty string means "broken".
    """
    _calls, execute_sql = _capturing_execute()
    with pytest.raises(RuleExecutionError, match="coordinate"):
        execute_rule(
            rule=_Rule(), ontology_json={}, ontology_id="ont-1",
            translate=_wrong_translate, execute_sql=execute_sql,
            coordinate=coordinate,
        )


def test_an_incomplete_coordinate_is_refused_before_athena_is_touched() -> None:
    """The refusal must precede execution, not abort a query already running."""
    calls, execute_sql = _capturing_execute()
    with pytest.raises(RuleExecutionError):
        execute_rule(
            rule=_Rule(), ontology_json={}, ontology_id="ont-1",
            translate=_wrong_translate, execute_sql=execute_sql,
            coordinate=("", ""),
        )
    assert calls == []


def test_the_coordinate_keyword_is_required() -> None:
    """Required so no future caller can inherit Ontop's derivation by omission.

    This is the whole prevention mechanism: the defect existed because the
    coordinate had a single implicit source. Every caller must now say which one
    applies to its layer type.
    """
    _calls, execute_sql = _capturing_execute()
    with pytest.raises(TypeError, match="coordinate"):
        execute_rule(  # type: ignore[call-arg]
            rule=_Rule(), ontology_json={}, ontology_id="ont-1",
            translate=_wrong_translate, execute_sql=execute_sql,
        )
