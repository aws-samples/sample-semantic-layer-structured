"""Tests for retrieve_kb_context_structured (Task 23)."""
from unittest.mock import MagicMock

from agents.metadata_query_agent import main


def test_retrieve_kb_context_returns_structured_candidates(monkeypatch):
    fake_bedrock = MagicMock()
    fake_bedrock.retrieve.return_value = {
        "retrievalResults": [
            {
                "score": 0.91,
                "content": {"text": "Table customers: customer master"},
                "metadata": {"table_id": "db.customers", "kind": "table"},
            },
            {
                "score": 0.84,
                "content": {"text": "Column first_name on customers"},
                "metadata": {
                    "table_id": "db.customers",
                    "column_id": "db.customers.first_name",
                    "kind": "column",
                },
            },
        ]
    }
    monkeypatch.setattr(main, "_bedrock_agent_runtime", lambda: fake_bedrock)
    main._layer_id_var.set("layer-1")
    main._layer_version_var.set("1")

    out = main.retrieve_kb_context_structured(
        user_query="who has a first name?",
        kb_id="kb-1",
    )

    call_kwargs = fake_bedrock.retrieve.call_args.kwargs
    vc = call_kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vc["filter"] == {
        "andAll": [
            {"equals": {"key": "semantic_layer_id", "value": "layer-1"}},
            {"equals": {"key": "semantic_layer_version", "value": "1"}},
        ]
    }

    # candidates carry database_name (derived from the table_id prefix when not
    # in metadata) so Phase 5 can execute against the right Athena database;
    # catalog_id is only added when present in the chunk metadata (absent here).
    assert out["candidates"] == [
        {"table_id": "db.customers", "score": 0.91, "database_name": "db"},
        {
            "table_id": "db.customers",
            "score": 0.84,
            "column_id": "db.customers.first_name",
            "database_name": "db",
        },
    ]
    assert len(out["chunks"]) == 2
