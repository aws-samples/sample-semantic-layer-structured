"""The publish gate's Athena coordinate is resolved from the LAYER config.

Regression cover for a defect measured on the live hybrid layer
``51e29707-4e76-4c72-8929-dba3d906eada``: the gate used the coordinate Ontop
derives from the ontology, and a hybrid layer's thin VKG stores ``mapsToTable``
BARE, so the derivation offered the alphabetically-first TABLE (``cards``) as the
schema. Athena answered ``SCHEMA_NOT_FOUND`` and no rule could ever reach
PUBLISHED on any hybrid layer.

`main.py` resolves at module scope, so the helper is only reachable through a
controlled re-import — the same technique as `test_main_rules_mount.py`.
"""
import importlib
import os
import sys

import pytest

# `main`, `routers.*` and `services.*` live under the REST-API package root,
# which is not on sys.path by default (only the repo root is, via pyproject's
# `pythonpath`).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)


@pytest.fixture()
def resolve(monkeypatch):
    """The `_athena_coordinate_for_layer` helper from a rules-enabled `main`.

    :param monkeypatch: pytest's env patcher; reverted after the test.
    :returns: The module-level helper function.
    """
    monkeypatch.setenv("RULES_ENABLED", "true")
    monkeypatch.setenv("NEPTUNE_GATEWAY_URL", "https://gw")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    return main._athena_coordinate_for_layer


@pytest.fixture(autouse=True)
def _restore_main():
    """Leave sys.modules clean so later tests get the default app."""
    yield
    sys.modules.pop("main", None)


def _live_hybrid_config() -> dict:
    """The shape actually stored for the live hybrid layer under test.

    Reduced to the fields the resolver reads, but faithful in the one detail
    that matters: the TOP-LEVEL ``catalogId``/``databaseName`` are null. A
    resolver that read those would return nothing for a real, working layer.

    :returns: A metadata config dict.
    """
    return {
        "id": "51e29707-4e76-4c72-8929-dba3d906eada",
        "name": "hybridsnowflake",
        "catalogId": None,
        "databaseName": None,
        "dataSources": [
            {
                "dataSource": "AwsDataCatalog",
                "catalogId": "s3tablescatalog/semantic-layer-dev-analytics-tables",
                "databaseName": "bird_card_games",
                "tableName": "cards",
                "tableId": (
                    "s3tablescatalog/semantic-layer-dev-analytics-tables::"
                    "bird_card_games.cards"
                ),
            }
        ],
    }


def test_the_live_hybrid_config_shape_resolves(resolve) -> None:
    coordinate = resolve(
        config=_live_hybrid_config(),
        namespace="51e29707-4e76-4c72-8929-dba3d906eada",
    )
    assert coordinate == {
        "database": "bird_card_games",
        "catalog": "s3tablescatalog/semantic-layer-dev-analytics-tables",
    }


def test_the_federated_catalog_is_not_flattened_to_the_glue_default(resolve) -> None:
    """The S3 Tables catalog must survive verbatim, slash and all. Replacing it
    with ``AwsDataCatalog`` is precisely how the query path fails
    SCHEMA_NOT_FOUND."""
    coordinate = resolve(config=_live_hybrid_config(), namespace="x")
    assert coordinate["catalog"].startswith("s3tablescatalog/")


def test_snake_case_keys_resolve(resolve) -> None:
    """Configs written by different producers spell these both ways; the query
    agent's own resolver accepts both, so this one must too."""
    config = {
        "dataSources": [
            {"catalog_id": "AwsDataCatalog", "database_name": "fhir_r4"}
        ]
    }
    assert resolve(config=config, namespace="x") == {
        "database": "fhir_r4",
        "catalog": "AwsDataCatalog",
    }


def test_a_config_with_no_data_sources_raises_naming_the_layer(resolve) -> None:
    with pytest.raises(ValueError, match="layer-42"):
        resolve(config={"dataSources": []}, namespace="layer-42")


def test_a_data_source_missing_the_catalog_raises(resolve) -> None:
    """Half a coordinate is not usable and must not be silently completed."""
    config = {"dataSources": [{"databaseName": "bird_card_games"}]}
    with pytest.raises(ValueError, match="catalog"):
        resolve(config=config, namespace="layer-42")


def test_a_data_source_missing_the_database_raises(resolve) -> None:
    config = {"dataSources": [{"catalogId": "AwsDataCatalog"}]}
    with pytest.raises(ValueError, match="database"):
        resolve(config=config, namespace="layer-42")


def test_a_missing_config_raises(resolve) -> None:
    """``get_metadata_config`` returns None for an unknown id — the publish
    gate must report that as "no such layer", not crash on a None subscript."""
    with pytest.raises(ValueError, match="layer-42"):
        resolve(config=None, namespace="layer-42")
