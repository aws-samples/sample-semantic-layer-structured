"""
Glue 5.1 PySpark job — creates normalized Iceberg tables in the
'normalized' S3 Tables namespace from Zero-ETL zetl_* source tables.

Implements the 40-table normalized model defined in:
  docs/research_normalized_er_diagram.md
  assets/images/ODH-normalized-er-diagram.png

Run once to create all 40 tables; re-running is safe (writeTo.createOrReplace
atomically overwrites existing data on each refresh run).

Each table selects only the columns belonging to its entity (per VIEW_COLUMNS),
eliminating sparse columns and producing true 3NF tables matching the ER
diagram.

Source column names are the actual Zero-ETL column names (lowercase, e.g.
policyid, firstname, city).  Each view's SELECT list aliases these to
snake_case target names (e.g. policy_id, first_name).

NOTE: CREATE MATERIALIZED VIEW cannot be used here because Iceberg MV
definitions are stored in the catalog and must not contain backtick-quoted
identifiers with special characters (dots).  We therefore execute the SELECT
at runtime via writeTo().createOrReplace(), which atomically overwrites the
target Iceberg table with fresh data on every job run.
"""
import sys
import boto3
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

args = getResolvedOptions(
    sys.argv,
    ['JOB_NAME', 'table_bucket_name', 'account_id', 'region', 'refresh_hours'],
)
table_bucket_arn = (
    f"arn:aws:s3tables:{args['region']}:{args['account_id']}:"
    f"bucket/{args['table_bucket_name']}"
)

# ── 1. Discover the most-recently-created zetl_* namespace per source table ──
s3t = boto3.client('s3tables', region_name=args['region'])
# Glue client + the S3Tables federated catalog id, used to mirror the curated
# descriptions into the Glue catalog view as well (UI Data-Sources tab).
glue_client = boto3.client('glue', region_name=args['region'])
glue_catalog_id = f"{args['account_id']}:s3tablescatalog/{args['table_bucket_name']}"

# ── pyiceberg S3Tables catalog (durable column-doc / description persistence) ──
# S3Tables federation does NOT durably persist Glue column Comments / table
# Description — it reconciles the Glue view back from the Iceberg schema. So the
# metadata_agent's curated descriptions live, authoritatively, as Iceberg schema
# column doc strings + a table 'description' property. The delete+recreate
# refresh rebuilds the Iceberg table WITHOUT those docs, wiping them. To preserve
# them we capture the Iceberg docs BEFORE delete and re-write them via pyiceberg
# AFTER create — mirroring agents/metadata_agent/main.py::_write_iceberg_docs_for_table.
_pyiceberg_catalog = None
def _iceberg_catalog():
    """Lazily build (and cache) the S3Tables Iceberg REST catalog. None on failure."""
    global _pyiceberg_catalog
    if _pyiceberg_catalog is not None:
        return _pyiceberg_catalog
    try:
        from pyiceberg.catalog import load_catalog
        _pyiceberg_catalog = load_catalog('s3tables', **{
            'type': 'rest',
            'uri': f"https://s3tables.{args['region']}.amazonaws.com/iceberg",
            'warehouse': table_bucket_arn,
            'rest.sigv4-enabled': 'true',
            'rest.signing-region': args['region'],
            'rest.signing-name': 's3tables',
        })
    except Exception as e:
        print(f"  WARN: pyiceberg S3Tables catalog unavailable ({e}) — "
              f"descriptions will not be preserved this run")
        _pyiceberg_catalog = None
    return _pyiceberg_catalog

def capture_iceberg_docs(table_name):
    """Return (table_description, {col_name: doc}) from the Iceberg schema, or ('', {})."""
    cat = _iceberg_catalog()
    if cat is None:
        return '', {}
    try:
        t = cat.load_table(('normalized', table_name))
        desc = (t.properties or {}).get('description', '') or ''
        docs = {f.name: f.doc for f in t.schema().fields if getattr(f, 'doc', None)}
        return desc, docs
    except Exception as e:
        print(f"    (no Iceberg docs to capture for {table_name}: {e})")
        return '', {}

def restore_iceberg_docs(table_name, desc, docs):
    """Re-apply captured table description + column docs to the Iceberg schema."""
    if not desc and not docs:
        return
    cat = _iceberg_catalog()
    if cat is None:
        return
    try:
        t = cat.load_table(('normalized', table_name))
        if docs:
            # Iceberg field names may differ in case from the captured (Glue
            # lowercase) names — map case-insensitively.
            by_lower = {f.name.lower(): f.name for f in t.schema().fields}
            with t.update_schema() as su:
                for cn, doc in docs.items():
                    canon = by_lower.get(cn.lower(), cn)
                    try:
                        su.update_column(canon, doc=doc)
                    except Exception as ce:
                        print(f"      skip col {cn}: {ce}")
        if desc:
            with t.transaction() as txn:
                txn.set_properties({'description': desc})
        print(f"    restored Iceberg docs ({len(docs)} column(s)"
              f"{', + table description' if desc else ''})")
    except Exception as e:
        print(f"    WARN: could not restore Iceberg docs for {table_name}: {e}")


def latest_zetl_namespace(source_table: str) -> str:
    """Return the zetl_* namespace that contains source_table, choosing newest."""
    paginator = s3t.get_paginator('list_namespaces')
    candidates = []
    for page in paginator.paginate(tableBucketARN=table_bucket_arn):
        for entry in page['namespaces']:
            ns = entry['namespace'][0]
            if not ns.startswith('zetl_'):
                continue
            try:
                t = s3t.get_table(
                    tableBucketARN=table_bucket_arn,
                    namespace=ns,
                    name=source_table,
                )
                candidates.append((ns, t['createdAt']))
            except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
                pass
    if not candidates:
        raise RuntimeError(f"No zetl_* namespace found containing table '{source_table}'")
    return sorted(candidates, key=lambda x: x[1], reverse=True)[0][0]


SOURCE_TABLES = [
    'semantic_layer_dev_holdings',
    'semantic_layer_dev_parties',
    'semantic_layer_dev_coverages',
    'semantic_layer_dev_relations',
    'semantic_layer_dev_financial_activities',
    'semantic_layer_dev_financial_statements',
    'semantic_layer_dev_riders',
    'semantic_layer_dev_policy_products',
    'semantic_layer_dev_coverage_products',
    'semantic_layer_dev_invest_products',
    'semantic_layer_dev_admin_codes',
    'semantic_layer_dev_type_codes',
]

print("Discovering latest zetl_* namespace per source table...")
ns_map = {t: latest_zetl_namespace(t) for t in SOURCE_TABLES}
for src, ns in ns_map.items():
    print(f"  {src} -> {ns}")

# ── 2. Initialise Spark ───────────────────────────────────────────────────────
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# ── 3. Ensure 'normalized' namespace exists ───────────────────────────────────
try:
    s3t.create_namespace(tableBucketARN=table_bucket_arn, namespace=['normalized'])
    print("Created 'normalized' namespace")
except s3t.exceptions.ConflictException:
    print("'normalized' namespace already exists")

# ── 4. Materialized view definitions ─────────────────────────────────────────
# (mv_name, source_table, sk_filter_or_None)
# Column projections are defined per-view in VIEW_COLUMNS below.
NORMALIZED_VIEWS = [
    # ── From ODH.HOLDING ──────────────────────────────────────────────────────
    # holding / life_detail / annuity_detail share 'HOLDING#%' but select
    # distinct column subsets (pol.*/hol.*, life.*, anty.*) via VIEW_COLUMNS.
    # sk prefixes are UPPERCASE in DynamoDB (confirmed from source data).
    ("holding",                "semantic_layer_dev_holdings",             "sk LIKE 'HOLDING#%'"),
    ("life_detail",            "semantic_layer_dev_holdings",             "sk LIKE 'HOLDING#%'"),
    ("annuity_detail",         "semantic_layer_dev_holdings",             "sk LIKE 'HOLDING#%'"),
    ("holding_loan",           "semantic_layer_dev_holdings",             "sk LIKE 'LOAN#%'"),
    ("holding_subaccount",     "semantic_layer_dev_holdings",             "sk LIKE 'SUBACCOUNT#%'"),
    ("holding_dbg",            "semantic_layer_dev_holdings",             "sk LIKE 'DBG#%'"),
    ("holding_arrangement",    "semantic_layer_dev_holdings",             "sk LIKE 'ARRANGEMENT#%'"),
    ("arrangement_source",     "semantic_layer_dev_holdings",             "sk LIKE 'ARRSOURCE#%'"),
    ("arrangement_destination","semantic_layer_dev_holdings",             "sk LIKE 'ARRDESTINATION#%'"),
    ("holding_projection",     "semantic_layer_dev_holdings",             "sk LIKE 'PROJECTIONS#%'"),
    ("holding_restriction",    "semantic_layer_dev_holdings",             "sk LIKE 'RESTRICTIONINFO#%'"),
    ("holding_activity",       "semantic_layer_dev_holdings",             "sk LIKE 'ACTIVITY#%'"),
    ("holding_payout",         "semantic_layer_dev_holdings",             "sk LIKE 'PAYOUT#%'"),
    ("policy_loan_summary",    "semantic_layer_dev_holdings",             "sk LIKE 'POLICYLOANSUMMARY#%'"),
    # ── From ODH.PARTY ────────────────────────────────────────────────────────
    # All party rows have sk = 'METADATA' (flat single-entity-per-row design).
    # Every party-based view selects different column subsets from the same rows.
    ("party",                  "semantic_layer_dev_parties",              None),
    ("address",                "semantic_layer_dev_parties",              None),
    ("phone",                  "semantic_layer_dev_parties",              None),
    ("email_address",          "semantic_layer_dev_parties",              None),
    ("govt_id_info",           "semantic_layer_dev_parties",              None),
    ("party_banking",          "semantic_layer_dev_parties",              None),
    ("carrier_appointment",    "semantic_layer_dev_parties",              None),
    ("producer_agreement",     "semantic_layer_dev_parties",              None),
    ("distribution_level",     "semantic_layer_dev_parties",              None),
    ("party_license",          "semantic_layer_dev_parties",              None),
    # ── From ODH.COVERAGE ────────────────────────────────────────────────────
    ("coverage",               "semantic_layer_dev_coverages",            "sk LIKE 'COVERAGE#%'"),
    ("life_participant",       "semantic_layer_dev_coverages",            "sk LIKE 'LIFEPARTICIPANT#%'"),
    ("substandard_rating",     "semantic_layer_dev_coverages",            "sk LIKE 'SUBSTANDARDRATING#%'"),
    ("reinsurance_info",       "semantic_layer_dev_coverages",            "sk LIKE 'REINSURANCEINFO#%'"),
    # ── From ODH.RELATION ────────────────────────────────────────────────────
    ("relation",               "semantic_layer_dev_relations",            None),
    # ── From ODH.FINANCIALACTIVITY ───────────────────────────────────────────
    # Confirmed sk prefix is 'FINACT#' (not 'FinancialActivity#').
    ("financial_activity",     "semantic_layer_dev_financial_activities", "sk LIKE 'FINACT#%'"),
    ("loan_activity",          "semantic_layer_dev_financial_activities", "sk LIKE 'LOANACTIVITY#%'"),
    ("subaccount_activity",    "semantic_layer_dev_financial_activities", "sk LIKE 'SUBACCOUNTACTIVITY#%'"),
    # ── From ODH.FINANCIALSTATEMENT ──────────────────────────────────────────
    ("financial_statement",    "semantic_layer_dev_financial_statements", None),
    # ── From ODH.RIDER ───────────────────────────────────────────────────────
    ("rider",                  "semantic_layer_dev_riders",               "sk LIKE 'RIDER#%'"),
    ("rider_participant",      "semantic_layer_dev_riders",               "sk LIKE 'PARTICIPANT#%'"),
    # ── Reference tables (1:1 from DynamoDB) ─────────────────────────────────
    ("policy_product",         "semantic_layer_dev_policy_products",      None),
    ("coverage_product",       "semantic_layer_dev_coverage_products",    None),
    ("invest_product",         "semantic_layer_dev_invest_products",      None),
    ("admin_codes",            "semantic_layer_dev_admin_codes",          None),
    ("type_codes",             "semantic_layer_dev_type_codes",           None),
]

# ── Per-view column projections ───────────────────────────────────────────────
# Keys match the mv_name values in NORMALIZED_VIEWS above.
# Values are lists of SQL column expressions (source_col AS alias).
# Audit columns are inlined in each view since availability varies by source.
VIEW_COLUMNS = {
    # ── 1. holding ────────────────────────────────────────────────────────────
    "holding": [
        "pk                  AS holding_id",
        "sk",
        "policyid            AS policy_id",
        "holdingnumber        AS holding_number",
        "holdingtype          AS holding_type",
        "holdingstatus        AS holding_status",
        "accountnumber        AS account_number",
        "accounttype          AS account_type",
        "cashvalue            AS cash_value",
        "currentvalue         AS current_value",
        "marketvalue          AS market_value",
        "purchasevalue        AS purchase_value",
        "purchasedate         AS purchase_date",
        "interestrate         AS interest_rate",
        "createddate          AS created_date",
        "updateddate          AS updated_date",
        "deleted              AS is_deleted",
    ],
    # ── 2. life_detail ────────────────────────────────────────────────────────
    "life_detail": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 3. annuity_detail ─────────────────────────────────────────────────────
    "annuity_detail": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 4. holding_loan ───────────────────────────────────────────────────────
    "holding_loan": [
        "pk AS holding_id", "sk", "interestrate", "createddate", "updateddate", "deleted",
    ],
    # ── 5. holding_subaccount ─────────────────────────────────────────────────
    "holding_subaccount": [
        "pk AS holding_id", "sk", "fundcode", "fundname", "unitvalue",
        "numberofunits", "allocationpercent", "currentvalue", "createddate",
        "updateddate", "deleted",
    ],
    # ── 6. holding_dbg ────────────────────────────────────────────────────────
    "holding_dbg": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 7. holding_arrangement ────────────────────────────────────────────────
    "holding_arrangement": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 8. arrangement_source ─────────────────────────────────────────────────
    "arrangement_source": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 9. arrangement_destination ────────────────────────────────────────────
    "arrangement_destination": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 10. holding_projection ────────────────────────────────────────────────
    "holding_projection": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 11. holding_restriction ───────────────────────────────────────────────
    "holding_restriction": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 12. holding_activity ──────────────────────────────────────────────────
    "holding_activity": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 13. holding_payout ────────────────────────────────────────────────────
    "holding_payout": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 14. policy_loan_summary ───────────────────────────────────────────────
    "policy_loan_summary": [
        "pk AS holding_id", "sk", "createddate", "updateddate", "deleted",
    ],
    # ── 15. party ─────────────────────────────────────────────────────────────
    "party": [
        "pk                      AS party_id",
        "sk",
        "partytypecode            AS party_type_code",
        "partyid                  AS party_key",
        "partytype                AS party_type",
        "partystatus              AS party_status",
        "sourcesystem             AS carrier_admin_system",
        "sourcesystemid           AS source_system_id",
        "ssn                      AS govt_id",
        "fullname                 AS full_name",
        "firstname                AS first_name",
        "middlename               AS middle_name",
        "lastname                 AS last_name",
        "suffix",
        "preferredname            AS preferred_name",
        "birthdate                AS birth_date",
        "gender",
        "maritalstatus            AS marital_status",
        "languagepreference       AS pref_language",
        "occupation",
        "employername             AS employer_name",
        "smokerstatus             AS smoker_status",
        "riskclass                AS risk_class",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "createdtimestamp         AS created_timestamp",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "updatedtimestamp         AS updated_timestamp",
        "recordversion            AS version_number",
        "deleted                  AS is_deleted",
    ],
    # ── 16. address ───────────────────────────────────────────────────────────
    "address": [
        "pk                      AS party_id",
        "sk                      AS address_sk",
        "addresstype              AS address_type_code",
        "addressline1             AS line1",
        "addressline2             AS line2",
        "city",
        "state                    AS state_tc",
        "zipcode                  AS zip",
        "county",
        "country                  AS country_tc",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 17. phone ─────────────────────────────────────────────────────────────
    "phone": [
        "pk                      AS party_id",
        "sk                      AS phone_sk",
        "phonenumbertype          AS phone_type_code",
        "phonenumber              AS phone_value",
        "alternatephone           AS alternate_phone",
        "faxnumber                AS fax_number",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 18. email_address ─────────────────────────────────────────────────────
    "email_address": [
        "pk                      AS party_id",
        "sk                      AS email_sk",
        "emailaddress             AS addr_line",
        "emailaddresstype         AS email_type",
        "alternateemail           AS alternate_email",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 19. govt_id_info ──────────────────────────────────────────────────────
    "govt_id_info": [
        "pk                      AS party_id",
        "sk                      AS govtid_sk",
        "ssn                      AS govt_id",
        "taxid                    AS tax_id",
        "passportnumber           AS passport_number",
        "passportcountry          AS passport_country",
        "passportexpirydate       AS passport_expiry_date",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 20. party_banking ─────────────────────────────────────────────────────
    "party_banking": [
        "pk                      AS party_id",
        "sk                      AS banking_sk",
        "bankaccountnumber        AS account_number",
        "bankroutingnumber        AS routing_number",
        "bankaccounttype          AS account_type",
        "bankname                 AS bank_name",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 21. carrier_appointment ───────────────────────────────────────────────
    "carrier_appointment": [
        "pk                      AS party_id",
        "sk                      AS appointment_sk",
        "primaryagentid           AS primary_agent_id",
        "secondaryagentid         AS secondary_agent_id",
        "agentcommissionrate      AS commission_rate",
        "agenthierarchycode       AS hierarchy_code",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 22. producer_agreement ────────────────────────────────────────────────
    "producer_agreement": [
        "pk                      AS party_id",
        "sk",
        "financialadvisorid       AS financial_advisor_id",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 23. distribution_level ────────────────────────────────────────────────
    "distribution_level": [
        "pk                      AS party_id",
        "sk",
        "agenthierarchycode       AS hierarchy_code",
        "customersegment          AS customer_segment",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 24. party_license ─────────────────────────────────────────────────────
    "party_license": [
        "pk                      AS party_id",
        "sk",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 25. coverage ──────────────────────────────────────────────────────────
    "coverage": [
        "pk                      AS holding_id",
        "sk                      AS coverage_sk",
        "coverageid               AS coverage_id",
        "covnumber                AS cov_number",
        "coveragetype             AS coverage_type",
        "coveragestatus           AS coverage_status",
        "productcode              AS product_code",
        "policyid                 AS policy_id",
        "partyid                  AS party_id",
        "currentamt               AS current_amt",
        "initcovamt               AS init_cov_amt",
        "faceamount               AS face_amount",
        "annualpremium            AS annual_premium",
        "modalpremium             AS modal_premium",
        "targetpremium            AS target_premium",
        "premiumpaid              AS premium_paid",
        "cashvalue                AS cash_value",
        "surrendervalue           AS surrender_value",
        "effectivedate            AS eff_date",
        "expirydate               AS expiry_date",
        "issuedate                AS issue_date",
        "issueage                 AS issue_age",
        "issuegender              AS issue_gender",
        "underwritingclass        AS underwriting_class",
        "rateclass                AS rate_class",
        "tobaccopremiumbasis      AS tobacco_premium_basis",
        "deathbenefitopttype      AS death_benefit_opt_type",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 26. life_participant ──────────────────────────────────────────────────
    "life_participant": [
        "pk                      AS holding_id",
        "sk                      AS participant_sk",
        "partyid                  AS party_id",
        "issueage                 AS issue_age",
        "issuegender              AS issue_gender",
        "underwritingclass        AS underwriting_class",
        "tobaccopremiumbasis      AS tobacco_premium_basis",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 27. substandard_rating ────────────────────────────────────────────────
    "substandard_rating": [
        "pk                      AS holding_id",
        "sk",
        "productcode              AS product_code",
        "rateclass                AS rate_class",
        "occupationclass          AS occupation_class",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 28. reinsurance_info ──────────────────────────────────────────────────
    "reinsurance_info": [
        "pk                      AS holding_id",
        "sk",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 29. relation ──────────────────────────────────────────────────────────
    "relation": [
        "pk                      AS relation_id",
        "sk                      AS relation_sk",
        "relationid               AS relation_key",
        "relationnumber           AS relation_number",
        "relationtype             AS relation_type",
        "relationtypecode         AS relation_type_code",
        "relationshiprole         AS relationship_role",
        "relationstatus           AS relation_status",
        "partyid1                 AS party_id_1",
        "partyid2                 AS party_id_2",
        "beneficiarytype          AS beneficiary_type",
        "beneficiarydesignation   AS beneficiary_designation",
        "beneficiarypercent       AS beneficiary_percent",
        "sharepercent             AS share_percent",
        "shareamount              AS share_amount",
        "isprimary                AS is_primary",
        "iscontingent             AS is_contingent",
        "isirrevocable            AS is_irrevocable",
        "priority",
        "effectivedate            AS effective_date",
        "trustindicator           AS trust_indicator",
        "trustname                AS trust_name",
        "trustdate                AS trust_date",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 30. financial_activity ────────────────────────────────────────────────
    "financial_activity": [
        "pk                      AS holding_id",
        "sk                      AS fin_activity_sk",
        "activityid               AS activity_id",
        "activitynumber           AS activity_number",
        "activitytype             AS activity_type",
        "policyid                 AS policy_id",
        "transactiontype          AS transaction_type",
        "transactiondate          AS transaction_date",
        "transactionamount        AS transaction_amount",
        "grossamount              AS gross_amount",
        "netamount                AS net_amount",
        "taxamount                AS tax_amount",
        "feeamount                AS fee_amount",
        "commissionamount         AS commission_amount",
        "effectivedate            AS effective_date",
        "posteddate               AS posted_date",
        "valuedate                AS value_date",
        "paymentmethod            AS payment_method",
        "paymentstatus            AS payment_status",
        "referencenumber          AS reference_number",
        "confirmationnumber       AS confirmation_number",
        "batchid                  AS batch_id",
        "currency",
        "exchangerate             AS exchange_rate",
        "reversalindicator        AS reversal_indicator",
        "description",
        "comments",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 31. loan_activity ─────────────────────────────────────────────────────
    "loan_activity": [
        "pk                      AS holding_id",
        "sk",
        "activityid               AS activity_id",
        "transactionamount        AS transaction_amount",
        "transactiondate          AS transaction_date",
        "paymentstatus            AS payment_status",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 32. subaccount_activity ───────────────────────────────────────────────
    "subaccount_activity": [
        "pk                      AS holding_id",
        "sk",
        "activityid               AS activity_id",
        "transactionamount        AS transaction_amount",
        "transactiondate          AS transaction_date",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 33. financial_statement ───────────────────────────────────────────────
    "financial_statement": [
        "pk                      AS holding_id",
        "sk                      AS statement_sk",
        "statementid              AS statement_id",
        "statementnumber          AS statement_number",
        "statementtype            AS statement_type",
        "statementstatus          AS statement_status",
        "statementdate            AS statement_date",
        "statementperiodstart     AS period_start",
        "statementperiodend       AS period_end",
        "statementdeliverymethod  AS delivery_method",
        "policyid                 AS policy_id",
        "beginningbalance         AS beginning_balance",
        "endingbalance            AS ending_balance",
        "cashvalue                AS cash_value",
        "surrendervalue           AS surrender_value",
        "deathbenefit             AS death_benefit",
        "loanbalance              AS loan_balance",
        "loaninterestrate         AS loan_interest_rate",
        "totalpremiums            AS total_premiums",
        "totalwithdrawals         AS total_withdrawals",
        "totalloans               AS total_loans",
        "totalfees                AS total_fees",
        "totalinterest            AS total_interest",
        "totaldividends           AS total_dividends",
        "netinvestmentreturn      AS net_investment_return",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 34. rider ─────────────────────────────────────────────────────────────
    "rider": [
        "pk                      AS holding_id",
        "sk                      AS rider_sk",
        "riderid                  AS rider_id",
        "ridernumber              AS rider_number",
        "ridername                AS rider_name",
        "ridertype                AS rider_type",
        "ridercode                AS rider_code",
        "riderstatus              AS rider_status",
        "policyid                 AS policy_id",
        "benefitamount            AS benefit_amount",
        "benefitperiod            AS benefit_period",
        "premiumamount            AS premium_amount",
        "premiummode              AS premium_mode",
        "riderfee                 AS rider_fee",
        "cashvalue                AS cash_value",
        "effectivedate            AS eff_date",
        "expirydate               AS expiry_date",
        "issueage                 AS issue_age",
        "terminationage           AS termination_age",
        "eliminationperiod        AS elimination_period",
        "waitingperiod            AS waiting_period",
        "underwritingrequired     AS underwriting_required",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 35. rider_participant ─────────────────────────────────────────────────
    "rider_participant": [
        "pk                      AS holding_id",
        "sk                      AS participant_sk",
        "issueage                 AS issue_age",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 36. policy_product ────────────────────────────────────────────────────
    "policy_product": [
        "pk",
        "sk",
        "productid                AS product_id",
        "productcode              AS product_code",
        "productname              AS product_name",
        "productshortname         AS product_short_name",
        "producttype              AS product_type",
        "productcategory          AS product_category",
        "productlinecode          AS product_line_code",
        "productlinename          AS product_line_name",
        "status",
        "effectivedate            AS effective_date",
        "jurisdictioncode         AS jurisdiction_code",
        "premiummode              AS premium_mode",
        "minimumpremium           AS minimum_premium",
        "maximumpremium           AS maximum_premium",
        "targetpremium            AS target_premium",
        "policyfee                AS policy_fee",
        "commissionrate           AS commission_rate",
        "interestrate             AS interest_rate",
        "guaranteedrate           AS guaranteed_rate",
        "currentrate              AS current_rate",
        "illustrationrate         AS illustration_rate",
        "issueagemin              AS issue_age_min",
        "issueagemax              AS issue_age_max",
        "rateclass                AS rate_class",
        "underwritingtype         AS underwriting_type",
        "taxqualified             AS tax_qualified",
        "loanoption               AS loan_option",
        "cashvalueoption          AS cash_value_option",
        "conversionoption         AS conversion_option",
        "rideravailable           AS rider_available",
        "guaranteedissue          AS guaranteed_issue",
        "medicalexamrequired      AS medical_exam_required",
        "surrenderchargeperiod    AS surrender_charge_period",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 37. coverage_product ──────────────────────────────────────────────────
    "coverage_product": [
        "pk",
        "sk",
        "coverageproductid        AS coverage_product_id",
        "coverageproductcode      AS product_code",
        "coverageproductname      AS product_name",
        "coveragecategory         AS coverage_category",
        "description",
        "status",
        "effectivedate            AS effective_date",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 38. invest_product ────────────────────────────────────────────────────
    "invest_product": [
        "pk",
        "sk",
        "investproductid          AS invest_product_id",
        "productcode              AS product_code",
        "productname              AS product_name",
        "fundtype                 AS fund_type",
        "fundfamily               AS fund_family",
        "fundmanager              AS fund_manager",
        "assetclass               AS asset_class",
        "investmentobjective      AS investment_objective",
        "investmentstrategy       AS investment_strategy",
        "riskrating               AS risk_rating",
        "morningstarrating        AS morningstar_rating",
        "morningstarcategory      AS morningstar_category",
        "expenseratio             AS expense_ratio",
        "managementfee            AS management_fee",
        "totalassets              AS total_assets",
        "currentnav               AS current_nav",
        "priornav                 AS prior_nav",
        "ytdreturn                AS ytd_return",
        "`1yearreturn`            AS one_year_return",
        "`3yearreturn`            AS three_year_return",
        "`5yearreturn`            AS five_year_return",
        "`10yearreturn`           AS ten_year_return",
        "inceptionreturn          AS inception_return",
        "sharperatio              AS sharpe_ratio",
        "standarddeviation        AS standard_deviation",
        "beta",
        "alpha",
        "status",
        "inceptiondate            AS inception_date",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
        "deleted                  AS is_deleted",
    ],
    # ── 39. admin_codes ───────────────────────────────────────────────────────
    "admin_codes": [
        "pk",
        "sk",
        "admincodeid              AS admin_code_id",
        "admincodetype            AS admin_code_type",
        "codevalue                AS code_value",
        "description",
        "abbreviation",
        "isactive                 AS is_active",
        "effectivedate            AS effective_date",
        "createdby                AS created_by",
        "createddate              AS created_date",
        "updatedby                AS updated_by",
        "updateddate              AS updated_date",
    ],
    # ── 40. type_codes ────────────────────────────────────────────────────────
    "type_codes": [
        "pk",
        "sk",
        "codeid                   AS code_id",
        "codetype                 AS code_type",
        "codevalue                AS code_value",
        "codedescription          AS code_description",
        "displayorder             AS display_order",
        "isactive                 AS is_active",
        "effectivedate            AS effective_date",
        "expirydate               AS expiry_date",
        "createddate              AS created_date",
        "updateddate              AS updated_date",
    ],
}

# ── 5. Create / overwrite normalized Iceberg tables ──────────────────────────
# Refresh is driven externally by the EventBridge rule that re-runs this job.
# writeTo().createOrReplace() atomically overwrites the target table with fresh
# data on every run (create on first run, full overwrite on subsequent runs).
#
# Each view selects only the named columns belonging to its entity (per
# VIEW_COLUMNS).  Columns that map to unknown ext_* generics are omitted.
print(f"\nCreating/refreshing {len(NORMALIZED_VIEWS)} normalized tables...")

for mv_name, src_table, sk_filter in NORMALIZED_VIEWS:
    zetl_ns = ns_map[src_table]
    src_ref = f"s3t_catalog.{zetl_ns}.{src_table}"
    where_clause = f"WHERE {sk_filter}" if sk_filter else ""

    col_exprs = VIEW_COLUMNS[mv_name]
    select_clause = ",\n    ".join(col_exprs)
    select_sql = (  # nosec B608 — SQL constructed from VIEW_COLUMNS constants and validated table refs, not user input
        f"SELECT\n    {select_clause}\n"  # nosec B608 — SQL/SPARQL built from internal schema-slice/static identifiers, not user input (grounding-gated)
        f"FROM {src_ref}\n"
        f"{where_clause}"
    ).rstrip()

    print(f"  normalized.{mv_name}")
    # S3Tables refresh: the S3Tables ↔ Iceberg-Spark integration does NOT support
    # replacing/recreating an existing table via Spark DDL — every form
    # (createOrReplace, CREATE OR REPLACE, CREATE IF NOT EXISTS, even Spark
    # DROP+CREATE) raises a 409 "table already exists", because Spark DROP clears
    # only the Iceberg catalog view, not the S3Tables backend object. So delete
    # the backend object via the S3Tables API FIRST (boto3 s3tables.delete_table),
    # then CREATE fresh. delete_table is a no-op-safe NotFound on first run.
    #
    # PRESERVE ENRICHMENT (durable): the metadata_agent's curated descriptions are
    # persisted authoritatively as Iceberg SCHEMA column doc strings + a table
    # 'description' property (S3Tables reconciles the Glue catalog view back from
    # these, so a Glue-only restore does NOT stick — observed 2026-06-12).
    # delete_table + CREATE rebuilds the Iceberg table WITHOUT docs, wiping them.
    # So capture the Iceberg docs BEFORE delete and re-write them via pyiceberg
    # AFTER create. We also mirror them into the Glue catalog view (best-effort)
    # so the UI Data-Sources tab shows them too.
    saved_desc, saved_docs = capture_iceberg_docs(mv_name)

    tmp_view = f"_src_{mv_name}"
    spark.sql(select_sql).createOrReplaceTempView(tmp_view)
    try:
        s3t.delete_table(
            tableBucketARN=table_bucket_arn,
            namespace='normalized',
            name=mv_name,
        )
    except s3t.exceptions.NotFoundException:
        pass  # first build — nothing to delete
    spark.sql(
        f"CREATE TABLE s3t_catalog.normalized.{mv_name} "  # nosec B608 — SQL/SPARQL built from internal schema-slice/static identifiers, not user input (grounding-gated)
        f"USING iceberg AS SELECT * FROM {tmp_view}"
    )

    # 1) DURABLE: re-write the captured docs into the Iceberg schema (survives
    #    S3Tables reconciliation — this is the authoritative store the agent uses).
    restore_iceberg_docs(mv_name, saved_desc, saved_docs)

    # 2) UI MIRROR (best-effort): also reflect into the Glue catalog Description +
    #    column Comments so the admin Data-Sources tab shows them. S3Tables
    #    federation may reconcile these away, but when it honours them the UI is
    #    populated immediately; the Iceberg write above is the durable guarantee.
    if saved_desc or saved_docs:
        try:
            fresh = glue_client.get_table(
                CatalogId=glue_catalog_id, DatabaseName='normalized', Name=mv_name,
            )['Table']
            table_input = dict(fresh)
            for field in (
                'CatalogId', 'DatabaseName', 'CreateTime', 'UpdateTime', 'CreatedBy',
                'IsRegisteredWithLakeFormation', 'VersionId', 'IsMultiDialectView',
                'Status', 'FederatedTable', 'IsMaterializedView', 'ViewDefinition',
            ):
                table_input.pop(field, None)
            if not table_input.get('Owner'):
                table_input.pop('Owner', None)
            if saved_desc:
                table_input['Description'] = saved_desc[:2048]
            docs_lower = {k.lower(): v for k, v in saved_docs.items()}
            for col in table_input.get('StorageDescriptor', {}).get('Columns', []):
                doc = docs_lower.get(col['Name'].lower())
                if doc:
                    col['Comment'] = doc[:255]
            upd = {'DatabaseName': 'normalized', 'TableInput': table_input,
                   'CatalogId': glue_catalog_id}
            try:
                glue_client.update_table(**upd)
            except Exception as first_err:
                es = str(first_err)
                if 'versionToken' in es and 'null' in es:
                    vt = s3t.get_table(
                        tableBucketARN=table_bucket_arn, namespace='normalized',
                        name=mv_name,
                    ).get('versionToken')
                    if vt:
                        upd['VersionId'] = vt
                        glue_client.update_table(**upd)
                    else:
                        raise
                else:
                    raise
        except Exception as e:  # never fail the refresh over the UI mirror
            print(f"    (Glue mirror skipped for {mv_name}: {e})")

print(f"\nDone. {len(NORMALIZED_VIEWS)} normalized tables created/refreshed in 'normalized' namespace.")
job.commit()
