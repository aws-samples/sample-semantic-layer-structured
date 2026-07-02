# Normalized layer enrichment brief

Authoritative join / derivation / label knowledge for the curated `normalized.*`
star schema. Each `normalized.<table>` or `normalized.<table>.<column>` heading below
carries ONE recipe the enrichment agent must fold into that table's or column's
description (RAG: Glue/KB `## Columns`; VKG: `rdfs:comment` on the class/property IRI).
Names are authoritative — they come from the curated schema, not raw ACORD.

> **Authoring rule — KEEP DESCRIPTIONS TERSE.** Fold each recipe into the table's or
> column's existing description as ONE or TWO short sentences. Do NOT expand a recipe
> into a multi-paragraph description, and do NOT repeat the same join clause across the
> table description, the column description, AND a query-patterns block — state the
> join once, on the most specific target (the column for a column recipe, the table for
> a table recipe). Verbose descriptions bloat the downstream query slice and cause the
> query agent to drop or mis-judge columns. Concise + correct beats long.

## Domain & sources

**Domain context.** Life-insurance / annuity policy analytics over a curated
`normalized.*` star schema (policies/holdings, parties and their roles, coverages,
financial activity/payouts, sub-accounts). Answers GROUP BY human-readable labels (not
surrogate codes), join through the documented bridge tables, and reproduce declared key
transforms verbatim.

**Data-sources context.** Curated, query-ready `normalized` Iceberg tables (not raw
ACORD). Some relationships bridge through junction tables and some FKs are stored in a
different surface form than the referenced PK (e.g. an unprefixed id); each such
join/transform is stated on the relevant column below and must be honored exactly.

---

## holding ↔ coverage ↔ party bridge + the `PARTY#` key transform

### normalized.coverage
Bridge/junction between `holding` and `party`; carries `holding_id` and `party_id`.

### normalized.coverage.party_id
FK to `party` stored UNPREFIXED (e.g. `PARTY000042`); join `CONCAT('PARTY#', coverage.party_id) = party.party_id` (bare equality matches no rows) and GROUP BY `party.party_id`, not this column.

### normalized.party.party_id
Canonical party PK, PREFIXED (e.g. `PARTY#PARTY000042`); bridge/child FKs hold the same id unprefixed, so join via `CONCAT('PARTY#', child.party_id) = party.party_id`. GROUP BY this to return the party.

### normalized.holding
Does not join `party` directly; relate through `coverage` (`holding.holding_id = coverage.holding_id`, then `CONCAT('PARTY#', coverage.party_id) = party.party_id`).

---

## Code vs label — `party_type` is the readable label

### normalized.party.party_type
Human-readable party category (Individual / Organization / Trust); SELECT/GROUP BY this directly — it is the label.

### normalized.party.party_type_code
Surrogate code, unique per row; use for joins/filters, never GROUP BY. The readable form is `party_type` on this same row — for any "party type(s)/types/description/label" question SELECT and GROUP BY `party.party_type` DIRECTLY and do NOT join `type_codes` (the readable value is already on `party`; a code-lookup join is unnecessary and can drop rows).

---

## Entity measure vs event-table substitution — payout derivation

### normalized.holding.market_value
The holding's own numeric value; SUM for "total market value" — do not substitute an amount column from an activity/transaction table.

### normalized.holding_payout
Thin marker table with no payout amount/frequency column; get amount from `financial_activity.transaction_amount` (payout-like `activity_type`), frequency from `COALESCE(policy_product.premium_mode, coverage.product_code)`, and "has a payout schedule" from `annuity_detail`.

### normalized.financial_activity.transaction_amount
Per-transaction amount (VARCHAR — `CAST(... AS DOUBLE)` before SUM); for a policy's payout amount filter `activity_type IN ('Withdrawal','Dividend','Claim')` and join on `holding_id`.

### normalized.annuity_detail
One row per holding that HAS a payout schedule; start payout questions here and JOIN `coverage` on `holding_id`.

---

## Optional/sparse child tables → LEFT JOIN + COALESCE

### normalized.holding_subaccount.fundname
Investment/fund name when present; for "investment product names" COALESCE with `policy_product.product_name` (via `coverage.product_code`), else 'N/A'. LEFT JOIN parent on `holding_id`.

### normalized.policy_product.product_name
Product name reached via `coverage.product_code = policy_product.product_code`; secondary investment/product-name source after `holding_subaccount.fundname`.

---

## Same-policy multi-role via self-join — insured == policyholder

### normalized.life_participant
Associates a party to a policy holding in a role (no separate role table; role is per-row via `participant_sk`). To find a party in two roles on the same policy (e.g. insured AND policyholder), SELF-JOIN on `holding_id + party_id` keeping `COUNT(DISTINCT participant_sk) > 1`, filter `is_deleted = false`. Roles: Owner (synonym Policyholder), Insured, Beneficiary.

### normalized.life_participant.participant_sk
Per-role surrogate key; a party with >1 distinct `participant_sk` on the same `holding_id` holds more than one role on that policy.

---

## Readable coverage-product name

### normalized.coverage_product.product_name
Human-readable coverage-product name; SELECT/ORDER BY directly for "coverage products by name" (it is the label, not a code).
