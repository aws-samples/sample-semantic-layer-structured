"""Regression: enum_shape_builder must probe the PHYSICAL table name.

The Phase-2 enum-shape builder receives the real Glue/Athena ``table_name`` but
its candidates carry a ``table`` field split from the model-authored
``vkg:mapsToColumn "table.column"`` literal. That token routinely differs from
the physical table name, so probing it made Athena fail and the fail-soft path
silently dropped every enum shape for the table. This test pins that the probe
uses ``table_name``, not ``candidate["table"]``.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents"))


@patch("ontology_agent.main.append_enum_shape_nquads")
@patch("ontology_agent.main.select_distinct_values")
@patch("ontology_agent.main.extract_enum_candidates")
@patch("ontology_agent.main._read_phase1_nquads")
def test_probe_uses_physical_table_name(
    mock_read, mock_extract, mock_probe, mock_append
):
    import ontology_agent.main as m

    mock_read.return_value = "<some> <nquads> <here> ."
    # Candidate carries the MODEL's friendly token in `table`, which differs
    # from the physical table name passed to enum_shape_builder.
    mock_extract.return_value = [
        {
            "table": "party",  # friendly mapsToColumn token
            "column": "status_code",
            "class_iri": "http://ex/Party",
            "prop_iri": "http://ex/Party/statusCode",
            "graph": "http://ex/ns",
        }
    ]
    mock_probe.return_value = {
        "values": ["ACTIVE", "INACTIVE"],
        "truncated": False,
        "distinct_count": 2,
        "sample_rows": 100,
    }

    m.enum_shape_builder(
        ontology_id="ont-1",
        table_name="semantic_layer_parties",  # physical Glue table
        database="db1",
        catalog="AWSDataCatalog",
    )

    assert mock_probe.called
    args, kwargs = mock_probe.call_args
    # select_distinct_values(database, table, column, catalog) — positional.
    probed_table = args[1] if len(args) > 1 else kwargs.get("table")
    assert probed_table == "semantic_layer_parties", (
        f"probe used {probed_table!r}, expected the physical table name"
    )
