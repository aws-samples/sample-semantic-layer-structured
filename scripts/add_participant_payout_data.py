#!/usr/bin/env python3
"""Append participant / payout sub-entity rows to an ALREADY-LOADED dataset.

Why this exists
---------------
The original synthetic generator never emitted LIFEPARTICIPANT# / PARTICIPANT# /
PAYOUT# rows, so the normalized Iceberg tables ``life_participant``,
``rider_participant`` and ``holding_payout`` are EMPTY. That makes several
ground-truth questions unanswerable (insured-participant, payout-schedule).

A full regenerate would replace EVERY id/value in the dataset and invalidate the
already-built Semantic-RAG KB layer. Instead this script is **append-only**: it
reads the existing ``data/complete_synthetic_data/{coverages,riders,holdings}.json``,
generates participant/payout rows keyed to the EXISTING holdings/riders/parties
(via :func:`add_participant_payout_rows`), writes the augmented files back, and
loads ONLY the new rows into the live DynamoDB tables. Everything else is left
byte-for-byte intact.

It is idempotent: if the files already contain the sub-entity prefixes it makes
no changes (so it is safe to re-run).

Usage
-----
    # 1) augment local JSON only (no AWS writes):
    python3 scripts/add_participant_payout_data.py

    # 2) augment AND load the new rows into live DynamoDB:
    python3 scripts/add_participant_payout_data.py --load --region us-east-1

After loading, trigger the normalized-views Glue job so the new rows demux into
the Iceberg tables:
    aws glue start-job-run --job-name semantic-layer-dev-create-normalized-views
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

# Reuse the canonical generators + the shared sub-entity builder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_complete_synthetic_data as gen  # noqa: E402

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "complete_synthetic_data",
)

# Map each augmented JSON file to its live DynamoDB table name + the sk prefixes
# this script adds to it (used both to detect prior runs and to select the rows
# to load).
FILE_TABLE = {
    "coverages": ("semantic-layer-dev-coverages", ("LIFEPARTICIPANT#",)),
    "riders": ("semantic-layer-dev-riders", ("PARTICIPANT#",)),
    "holdings": ("semantic-layer-dev-holdings", ("PAYOUT#",)),
}


def _load(name: str) -> list:
    """Read a data file (list of item dicts) from the synthetic-data dir."""
    with open(os.path.join(DATA_DIR, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, records: list) -> None:
    """Write a data file back to the synthetic-data dir."""
    with open(os.path.join(DATA_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)


def _has_prefix(records: list, prefixes: tuple) -> bool:
    """True if any record's sk already starts with one of ``prefixes``."""
    return any(str(r.get("sk", "")).startswith(p)
               for r in records for p in prefixes)


def augment_files() -> dict:
    """Append sub-entity rows to the on-disk data files (idempotent).

    Returns:
        {file_name: [new_rows]} for the rows added this run (empty lists when the
        files already carried the prefixes).
    """
    all_data = {name: _load(name) for name in ("parties", "coverages",
                                               "riders", "holdings")}
    already = all(
        _has_prefix(all_data[name], prefixes)
        for name, (_, prefixes) in FILE_TABLE.items()
    )
    if already:
        print("• Participant/payout rows already present — nothing to do.")
        return {name: [] for name in FILE_TABLE}

    before = {name: len(all_data[name]) for name in FILE_TABLE}
    gen.add_participant_payout_rows(all_data)

    added: dict = {}
    for name, (_, prefixes) in FILE_TABLE.items():
        new_rows = [r for r in all_data[name]
                    if str(r.get("sk", "")).startswith(prefixes)]
        added[name] = new_rows
        _save(name, all_data[name])
        print(f"✓ {name}.json: {before[name]} → {len(all_data[name])} "
              f"(+{len(all_data[name]) - before[name]})")
    return added


def load_rows(added: dict, region: str) -> None:
    """Batch-write ONLY the new sub-entity rows into the live DynamoDB tables."""
    import boto3

    session = boto3.Session(region_name=region)
    ddb = session.resource("dynamodb")
    for name, (table_name, _) in FILE_TABLE.items():
        rows = added.get(name) or []
        if not rows:
            continue
        table = ddb.Table(table_name)
        # DynamoDB rejects native float — round-trip via Decimal (str) like the
        # main loader (load_to_dynamodb.py) does.
        clean = json.loads(json.dumps(rows), parse_float=Decimal)
        with table.batch_writer() as batch:
            for item in clean:
                batch.put_item(Item=item)
        print(f"✓ loaded {len(clean)} rows into {table_name}")


def main() -> None:
    """CLI entry: augment files, optionally load the new rows into DynamoDB."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--load", action="store_true",
                    help="also write the new rows into live DynamoDB")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    added = augment_files()
    total = sum(len(v) for v in added.values())
    if not total:
        return
    print(f"\nTotal new sub-entity rows: {total}")
    if args.load:
        load_rows(added, args.region)
        print("\nNext: trigger the normalized-views Glue job to demux these into "
              "Iceberg:\n  aws glue start-job-run --job-name "
              "semantic-layer-dev-create-normalized-views")
    else:
        print("\n(local files only — re-run with --load to write to DynamoDB)")


if __name__ == "__main__":
    main()
