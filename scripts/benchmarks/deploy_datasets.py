"""Resumable, idempotent deploy driver: download + ingest benchmark DBs into S3 Tables.

This driver ORCHESTRATES the already-reviewed ingest helpers
(``ingest_bird.py`` / ``ingest_common.py``); it does NOT reimplement ingestion.
It fills the gap those helpers leave open: ``ingest_database`` assumes the S3
Tables namespace already exists AND that Lake Formation ``CREATE_TABLE`` is
granted to the executing role — neither the ingest CLI nor its helpers create
the namespace or the LF grant. So, PER DATABASE, this driver:

  1. creates the S3 Tables namespace ``<db_prefix><db_id>`` if absent
     (``boto3 s3tables create_namespace``);
  2. grants Lake Formation ``CREATE_TABLE`` + ``DESCRIBE`` on that namespace
     (database) to the executing role so the CTAS can create tables, and
     ``SELECT`` + ``DESCRIBE`` to every AUTO-DISCOVERED agentcore role plus any
     extra ``--grant-roles`` role (for later agent access);
  3. ensures the Glue staging DB (``--stage-db``) exists;
  4. calls ``ingest_database(...)`` for that DB;
  5. records success in a RESUMABLE JSON state file so re-runs SKIP already
     ingested DBs.

It does NOT build any semantic layer — no DynamoDB config, no metadata-agent
invoke. Ingestion into S3 Tables ONLY.

The Athena SQL dialect quirks (CTAS ``is_external=false``, Hive backtick
external DDL, unquoted ``DROP``) are all inside the reused ingest helpers and
were confirmed live in the Task 0 spike (see ``scripts/benchmarks/README.md``,
"Write-path spike"). This driver only adds the namespace-create + LF-grant
prerequisites the runbook documents there.

Usage:
    python scripts/benchmarks/deploy_datasets.py \\
        --benchmark bird \\
        --bucket <stage-bucket> \\
        --catalog s3tablescatalog/<bench-bucket> \\
        [--workgroup primary] [--grant-roles extraRoleA,extraRoleB] \\
        [--limit N] [--only db1,db2] [--dry-run]

    # Re-apply Lake Formation grants to ALREADY-ingested namespaces (no ingest):
    python scripts/benchmarks/deploy_datasets.py \\
        --benchmark bird \\
        --catalog s3tablescatalog/<bench-bucket> \\
        --grants-only [--dry-run]

The ten roles that need read access (ontology build, metadata build, the hybrid
semantic-layer BUILD agent, the three query agents, the three eval Lambdas, and
the REST API Lambda whose governed-rules publish gate smoke-executes each rule's
SQL) are discovered from live IAM, so ``--grant-roles`` is only for EXTRA
principals. This is deliberate: the ontology build role was once omitted from a
hand-maintained grant list, and because ``get_single_table_schema`` returns
``{"error": …}`` on AccessDenied WITHOUT raising, the agent modelled tables from
the LLM's memory and still reported ``completed`` (2026-08-04 — see
``scripts/benchmarks/README.md``, "REDEPLOY TRAP").

WHY ``--grants-only`` EXISTS (2026-08-14): the grant step runs per PENDING
database, so a namespace already recorded in the resumable state file is skipped
whole — grants included. When a NEW agent role appears (the hybrid
semantic-layer creation agent did), there was no way to extend the grants to the
already-ingested namespaces short of re-ingesting them. The hybrid build then
failed with ``Insufficient Lake Formation permission(s): Required Describe on
<table>``, and the VKG phase logs that as a WARN ("composing without schema")
rather than raising — the same quiet class of failure as 2026-08-04.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import boto3

from scripts.benchmarks import ingest_bird as bird
from scripts.benchmarks import ingest_common as ic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Default namespace prefix per benchmark (matches each ingest CLI's --db-prefix).
_DEFAULT_DB_PREFIX = {"bird": "bird_", "cwd": "cwd_"}

# Root under which each benchmark's raw data + outputs live (mirrors the ingest
# CLIs' BENCH_DIR).
_BENCH_ROOT = Path("data/eval/benchmarks")

# Service principals that identify WHICH kind of role a name match is. See
# _AGENT_ROLE_NAME_PARTS for why the name alone is not enough.
_AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
_LAMBDA_PRINCIPAL = "lambda.amazonaws.com"

# (name substring, required trust-policy service principal) for EVERY agentcore
# role that must be able to read a benchmark database. Matched against live IAM
# roles because the CDK appends a random suffix to each
# (`…OntologyGenerationAgentRo-f0miAcVh87P9`), so the full names can neither be
# hardcoded nor survive a stack REPLACE.
#
# WHY THIS IS AUTO-DISCOVERED RATHER THAN LEFT TO --grant-roles (2026-08-04): the
# ontology BUILD role was missing from the documented grant list, and a from-scratch
# deploy therefore produced 10 BIRD layers that reported `completed` while being
# modelled from the LLM's memory of the public schema — Phase 1's
# `get_single_table_schema` returns `{"error": …}` on AccessDenied WITHOUT raising,
# so the build never fails. A grant list that a human must remember to pass is the
# wrong shape for a failure this quiet; the driver resolves it instead.
#
# WHY THE TRUST PRINCIPAL IS PART OF THE MATCH (2026-08-14): CloudFormation
# truncates the logical id to 25 chars in the physical role name, so the hybrid
# creation RUNTIME role and the six CodeBuild/custom-resource roles of its
# container build ALL collapse to `…-SemanticLayerCreationAgen-<suffix>` and are
# indistinguishable by name. Name-only matching would hand LF SELECT on every
# benchmark table to CodeBuild and Lambda service roles that have no business
# reading the data. The trust policy is the discriminator: only the runtime role
# is assumable by bedrock-agentcore.
_AGENT_ROLE_NAME_PARTS = (
    # ontology build — Phase 1 reads the Glue schema
    ("OntologyGenerationAgentRo", _AGENTCORE_PRINCIPAL),
    # metadata layer build — reads schemas
    ("MetadataAgentRole", _AGENTCORE_PRINCIPAL),
    # hybrid semantic-layer BUILD agent — VKG phase probes table schemas
    ("SemanticLayerCreationAgen", _AGENTCORE_PRINCIPAL),
    # VKG query agent — executes SQL
    ("SemanticQueryAgentRole", _AGENTCORE_PRINCIPAL),
    # hybrid query agent — executes SQL
    ("SemanticLayerQueryAgentRo", _AGENTCORE_PRINCIPAL),
    # SemanticRAG query agent — executes SQL
    ("MetadataQueryAgentRole", _AGENTCORE_PRINCIPAL),
    # 3 codeBased evaluator Lambdas — run gold SQL
    ("BenchmarkEvalHandler", _LAMBDA_PRINCIPAL),
    # REST API Lambda — the governed-rules PUBLISH gate smoke-executes each
    # rule's Ontop-generated SQL before a rule may reach PUBLISHED, so this role
    # runs real queries against the layer's tables. Without LF SELECT the query
    # fails "Principal does not have any privilege on specified resource" and the
    # publish is blocked with a reason that reads like broken SQL. IAM alone is
    # not enough — same trap as the BenchmarkEvalHandler Lambdas above.
    ("lambda-LambdaExecutionRole", _LAMBDA_PRINCIPAL),
)


# --------------------------------------------------------------------------- #
# PURE / testable core                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class DeployState:
    """Resumable record of which db_ids have been ingested for a benchmark.

    Persisted as JSON so a re-run skips completed DBs (idempotent deploy). The
    driver saves after EACH database so a crash mid-run is resumable.

    Args:
        benchmark: Benchmark slug (``"bird"`` / ``"cwd"``).
        completed: db_ids already ingested successfully (order preserved).
    """

    benchmark: str
    completed: List[str] = field(default_factory=list)

    def mark_done(self, *, db_id: str) -> None:
        """Record ``db_id`` as successfully ingested (idempotent).

        Args:
            db_id: The benchmark database id that just finished ingesting.

        Returns:
            None.
        """
        # Idempotent: a resumed run may re-mark a db already recorded; never
        # duplicate it in the completed list.
        if db_id not in self.completed:
            self.completed.append(db_id)

    def is_done(self, *, db_id: str) -> bool:
        """Return whether ``db_id`` has already been ingested.

        Args:
            db_id: The benchmark database id to check.

        Returns:
            True when ``db_id`` is in ``completed``.
        """
        return db_id in self.completed

    def save(self, *, path: Path) -> None:
        """Write the state to JSON, creating parent directories.

        Args:
            path: Destination path (parent dirs created).

        Returns:
            None.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(
                {"benchmark": self.benchmark, "completed": self.completed}, indent=2
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, *, path: Path) -> "DeployState":
        """Load state from JSON.

        Args:
            path: Source JSON path.

        Returns:
            The parsed ``DeployState``.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(benchmark=raw["benchmark"], completed=list(raw.get("completed", [])))


def pending_dbs(*, all_db_ids: List[str], state: DeployState) -> List[str]:
    """Return db_ids not yet marked complete in state (order preserved).

    Args:
        all_db_ids: All candidate db_ids (already filtered by ``--only`` /
            ``--limit`` by the caller).
        state: The resumable ``DeployState`` recording completed db_ids.

    Returns:
        The db_ids from ``all_db_ids`` that are NOT already done, in the same
        order they appear in ``all_db_ids``.
    """
    return [db_id for db_id in all_db_ids if not state.is_done(db_id=db_id)]


def trusted_services(*, assume_role_policy: Any) -> List[str]:
    """Extract the service principals an IAM trust policy allows sts:AssumeRole for.

    Pure (no AWS) so the parsing is unit-testable. ``iam list_roles`` returns
    ``AssumeRolePolicyDocument`` already URL-decoded into a dict via boto3, but
    the raw API/CLI returns a URL-encoded JSON string, so both are accepted.
    ``Principal.Service`` may be a single string or a list.

    Args:
        assume_role_policy: The role's ``AssumeRolePolicyDocument`` — a dict, a
            JSON string, or None/absent.

    Returns:
        Sorted, de-duplicated service principals (empty when the policy names
        none, e.g. a role trusted only by an IAM principal or federated identity).
    """
    if not assume_role_policy:
        return []
    if isinstance(assume_role_policy, str):
        # A raw API/CLI response is URL-encoded JSON; boto3 pre-decodes it.
        assume_role_policy = json.loads(unquote(assume_role_policy))

    services: set = set()
    for statement in assume_role_policy.get("Statement", []):
        principal = statement.get("Principal", {})
        service = principal.get("Service", [])
        # A single-service trust policy uses a bare string, not a list.
        if isinstance(service, str):
            services.add(service)
        else:
            services.update(service)
    return sorted(services)


def match_agent_roles(
    *, roles: Dict[str, List[str]], name_parts: tuple = _AGENT_ROLE_NAME_PARTS
) -> Dict[str, List[str]]:
    """Group IAM roles by which agentcore role they are (name substring + trust).

    Pure (no AWS) so the matching rules are unit-testable. Grouping by name part
    rather than returning a flat list lets the caller detect a part that matched
    NOTHING — the silent-failure case this function exists to prevent. A role
    must match BOTH the name substring and the expected trust service principal;
    see ``_AGENT_ROLE_NAME_PARTS`` for why the name alone is ambiguous.

    Args:
        roles: All IAM roles in the account as ``{role name: trusted service
            principals}`` (see ``trusted_services``).
        name_parts: ``(substring, trust service principal)`` pairs to look for
            (defaults to ``_AGENT_ROLE_NAME_PARTS``).

    Returns:
        A dict mapping each name part to the sorted role names that match it. A
        part with no matches maps to an empty list (the caller decides whether
        that is fatal).
    """
    return {
        part: sorted(
            name
            for name, services in roles.items()
            if part in name and trust_service in services
        )
        for part, trust_service in name_parts
    }


# --------------------------------------------------------------------------- #
# Thin, idempotent AWS helpers (boto3 calls + already-exists handling)        #
# --------------------------------------------------------------------------- #
def discover_agent_role_arns(*, iam: Any, account_id: str) -> List[str]:
    """Resolve the ARNs of every agentcore role that must read benchmark data.

    Args:
        iam: boto3 ``iam`` client.
        account_id: The AWS account id (for building ARNs).

    Returns:
        Sorted, de-duplicated role ARNs for all matched roles.

    Raises:
        RuntimeError: A name part in ``_AGENT_ROLE_NAME_PARTS`` matched no role.
            Fail loudly: a part matching nothing means either the agentcore stack
            is not deployed yet or a role was renamed, and proceeding would grant
            an INCOMPLETE set — reproducing the exact 2026-08-04 failure where a
            missing ontology-role grant yielded hallucinated ontologies that still
            reported `completed`.
    """
    roles: Dict[str, List[str]] = {}
    for page in iam.get_paginator("list_roles").paginate():
        for role in page["Roles"]:
            roles[role["RoleName"]] = trusted_services(
                assume_role_policy=role.get("AssumeRolePolicyDocument")
            )

    matched = match_agent_roles(roles=roles)
    unmatched = [part for part, hits in matched.items() if not hits]
    if unmatched:
        raise RuntimeError(
            f"No IAM role found for agentcore role part(s) {unmatched}. Every part "
            f"in _AGENT_ROLE_NAME_PARTS must resolve, or the Lake Formation grant "
            f"set would be incomplete and ontology builds would silently fall back "
            f"to modelling tables from the LLM's memory. Deploy the agentcore stack "
            f"first, or update _AGENT_ROLE_NAME_PARTS if a role was renamed or its "
            f"trust policy no longer names the expected service principal."
        )

    arns = sorted(
        {
            f"arn:aws:iam::{account_id}:role/{name}"
            for hits in matched.values()
            for name in hits
        }
    )
    for part, hits in sorted(matched.items()):
        logger.info("Resolved agentcore role %s -> %s", part, hits)
    return arns
def _table_bucket_arn(*, catalog: str, account_id: str, region: str) -> str:
    """Derive the S3 Tables table-bucket ARN from the federated catalog id.

    The Athena catalog is ``s3tablescatalog/<table-bucket-name>``; the S3 Tables
    API (``create_namespace``) needs the bucket's ARN
    (``arn:aws:s3tables:<region>:<account>:bucket/<table-bucket-name>``). The
    account id is derived from STS (never hardcoded — this repo has a public
    remote).

    Args:
        catalog: Athena catalog id, e.g. ``s3tablescatalog/<bucket>``.
        account_id: AWS account id from ``sts.get_caller_identity()``.
        region: AWS region the table bucket lives in.

    Returns:
        The ``arn:aws:s3tables:...:bucket/<bucket>`` ARN.

    Raises:
        ValueError: ``catalog`` is not of the form ``s3tablescatalog/<bucket>``.
    """
    parts = catalog.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(
            f"--catalog {catalog!r} is not of the form "
            "'s3tablescatalog/<table-bucket-name>'; cannot derive the S3 Tables "
            "bucket ARN for namespace creation."
        )
    bucket_name = parts[1]
    return f"arn:aws:s3tables:{region}:{account_id}:bucket/{bucket_name}"


def create_namespace_if_absent(
    *, s3tables: Any, table_bucket_arn: str, namespace: str, dry_run: bool
) -> None:
    """Create the S3 Tables namespace if it does not already exist (idempotent).

    S3 Tables namespaces are created via ``s3tables create_namespace`` (NOT
    Athena ``CREATE SCHEMA`` — confirmed live, README "Namespace creation" row).
    A re-run must be a no-op, so a ``ConflictException`` / already-exists error
    is caught and logged rather than raised.

    Args:
        s3tables: boto3 ``s3tables`` client (``None`` under ``dry_run``).
        table_bucket_arn: The S3 Tables table-bucket ARN.
        namespace: The namespace name (already ``safe_ident``-validated).
        dry_run: When True, only log the planned create and return.

    Returns:
        None.

    Raises:
        botocore.exceptions.ClientError: Any error other than the namespace
            already existing (fail loudly).
    """
    if dry_run:
        logger.info(
            "[DRY RUN] would create s3tables namespace %r in %s (if absent)",
            namespace,
            table_bucket_arn,
        )
        return

    try:
        # create_namespace takes a LIST of namespace name parts.
        s3tables.create_namespace(
            tableBucketARN=table_bucket_arn, namespace=[namespace]
        )
        logger.info("Created s3tables namespace %r", namespace)
    except s3tables.exceptions.ConflictException:
        # Already exists — idempotent no-op (the whole point of "if absent").
        logger.info("s3tables namespace %r already exists (skipping create)", namespace)


def list_namespaces(*, s3tables: Any, table_bucket_arn: str, prefix: str) -> List[str]:
    """List every namespace in the table bucket whose name starts with ``prefix``.

    Used by ``--grants-only``, which must re-grant on namespaces that are ALREADY
    ingested. The resumable state file is the wrong source there: it records what
    THIS checkout ingested, while the grants must cover what is actually deployed
    (a different machine, or a cleaned ``data/`` dir, would silently under-grant).

    Args:
        s3tables: boto3 ``s3tables`` client (read-only use here).
        table_bucket_arn: The S3 Tables table-bucket ARN.
        prefix: Namespace prefix to filter on (e.g. ``bird_``).

    Returns:
        Sorted namespace names carrying the prefix (empty list if none).
    """
    found: List[str] = []
    for page in s3tables.get_paginator("list_namespaces").paginate(
        tableBucketARN=table_bucket_arn
    ):
        for entry in page["namespaces"]:
            # The API models a namespace as a LIST of name parts; S3 Tables only
            # supports one level, so element 0 is the whole name.
            name = entry["namespace"][0]
            if name.startswith(prefix):
                found.append(name)
    return sorted(found)


def grant_lf(
    *,
    lakeformation: Any,
    catalog_id: str,
    namespace: str,
    executing_role_arn: str,
    grant_role_arns: List[str],
    dry_run: bool,
) -> None:
    """Grant Lake Formation permissions on a namespace (database) for ingest + agents.

    A fresh S3 Tables namespace returns ``Insufficient Lake Formation
    permission(s): Required Create Table`` until the executing role is granted
    ``CREATE_TABLE`` (README "CREATE privilege" row). This grants:

      * executing role: ``CREATE_TABLE`` + ``DESCRIBE`` on the namespace database
        (so the ingest CTAS can create tables);
      * each ``--grant-roles`` role: ``DESCRIBE`` on the database AND
        ``SELECT`` + ``DESCRIBE`` on ALL tables in it (table wildcard) — this is
        what a later query/metadata agent role needs to read the ingested data.

    Grants are idempotent in Lake Formation (re-granting an existing permission
    is a no-op), so this is safe to re-run.

    Args:
        lakeformation: boto3 ``lakeformation`` client (``None`` under ``dry_run``).
        catalog_id: The LF CatalogId — ``<account>:<catalog>`` (account derived
            from STS, never hardcoded).
        namespace: The namespace/database name (already ``safe_ident``-validated).
        executing_role_arn: ARN of the role running this driver (gets CREATE_TABLE).
        grant_role_arns: ARNs of additional roles to grant SELECT/DESCRIBE.
        dry_run: When True, only log the planned grants and return.

    Returns:
        None.
    """
    if dry_run:
        logger.info(
            "[DRY RUN] would LF-grant CREATE_TABLE+DESCRIBE on %s to %s; "
            "SELECT+DESCRIBE to %s",
            namespace,
            executing_role_arn,
            grant_role_arns or "(none)",
        )
        return

    # Executing role: CREATE_TABLE + DESCRIBE on the DATABASE (namespace) so the
    # CTAS can create tables inside it.
    lakeformation.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": executing_role_arn},
        Resource={"Database": {"CatalogId": catalog_id, "Name": namespace}},
        Permissions=["CREATE_TABLE", "DESCRIBE"],
    )
    logger.info(
        "LF-granted CREATE_TABLE+DESCRIBE on %s to executing role %s",
        namespace,
        executing_role_arn,
    )

    for role_arn in grant_role_arns:
        # Agent roles need to DESCRIBE the database and SELECT/DESCRIBE its
        # tables. Table permissions use a wildcard (TableWildcard) so grants
        # apply to every table the ingest CTAS creates in this namespace.
        lakeformation.grant_permissions(
            Principal={"DataLakePrincipalIdentifier": role_arn},
            Resource={"Database": {"CatalogId": catalog_id, "Name": namespace}},
            Permissions=["DESCRIBE"],
        )
        lakeformation.grant_permissions(
            Principal={"DataLakePrincipalIdentifier": role_arn},
            Resource={
                "Table": {
                    "CatalogId": catalog_id,
                    "DatabaseName": namespace,
                    "TableWildcard": {},
                }
            },
            Permissions=["SELECT", "DESCRIBE"],
        )
        logger.info(
            "LF-granted DESCRIBE(db)+SELECT/DESCRIBE(table*) on %s to %s",
            namespace,
            role_arn,
        )


def ensure_stage_db(*, glue: Any, stage_db: str, dry_run: bool) -> None:
    """Create the Glue staging database if absent (idempotent).

    ``ingest_database`` creates transient EXTERNAL tables in ``stage_db`` but
    never creates the database itself. A re-run must be a no-op, so an
    ``AlreadyExistsException`` is caught and logged rather than raised.

    Args:
        glue: boto3 ``glue`` client (``None`` under ``dry_run``).
        stage_db: The Glue staging database name (already validated).
        dry_run: When True, only log the planned create and return.

    Returns:
        None.
    """
    if dry_run:
        logger.info("[DRY RUN] would ensure Glue staging db %r exists", stage_db)
        return

    try:
        glue.create_database(DatabaseInput={"Name": stage_db})
        logger.info("Created Glue staging database %r", stage_db)
    except glue.exceptions.AlreadyExistsException:
        logger.info(
            "Glue staging database %r already exists (skipping create)", stage_db
        )


def _role_arn_from_caller_identity(*, arn: str) -> str:
    """Convert an STS caller-identity ARN to the executing role's IAM role ARN.

    ``get_caller_identity`` returns an ASSUMED-ROLE ARN
    (``arn:aws:sts::<acct>:assumed-role/<RoleName>/<session>``). Lake Formation
    principals must be the IAM ROLE ARN
    (``arn:aws:iam::<acct>:role/<RoleName>``), so this converts the former to the
    latter. The account id is parsed FROM the ARN (never hardcoded). Kept as a
    pure string function (takes the ARN, not the STS client) so it is unit-tested
    without AWS.

    KNOWN LIMITATION: an IAM role with a PATH (``role/team/Name``) is reported by
    the assumed-role ARN without its path (just ``assumed-role/Name/...``), so
    the reconstructed role ARN drops the path. This is an accepted edge case
    (deploy roles here are pathless) — documented, not fixed.

    Args:
        arn: The caller-identity ARN from ``sts.get_caller_identity()["Arn"]``.

    Returns:
        The IAM role ARN of the executing principal.

    Raises:
        ValueError: The ARN is neither an assumed-role nor a plain role ARN this
            driver can map to an IAM role (fail loudly rather than guess a
            principal).
    """
    # ARN layout: arn:aws:<service>:<region>:<account>:<resource> — the account
    # id is field index 4, parsed from the ARN itself (never hardcoded).
    account_id = arn.split(":")[4]
    # arn:aws:sts::<acct>:assumed-role/<RoleName>/<session-name>
    if ":assumed-role/" in arn:
        role_name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
        return f"arn:aws:iam::{account_id}:role/{role_name}"
    # A plain IAM role ARN is already usable as an LF principal.
    if ":role/" in arn:
        return arn
    raise ValueError(
        f"Cannot derive an IAM role ARN for Lake Formation from caller identity "
        f"{arn!r}. Run this driver under an assumed IAM role."
    )


# --------------------------------------------------------------------------- #
# CLI orchestration                                                           #
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse the deploy-driver CLI arguments.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``); passing it
            explicitly keeps the parser testable.

    Returns:
        The parsed ``argparse.Namespace``.
    """
    parser = argparse.ArgumentParser(
        description="Resumable, idempotent deploy driver: download + ingest a "
        "benchmark's databases into S3 Tables (creates each namespace + Lake "
        "Formation grants, then calls the existing ingest helpers). Does NOT "
        "build any semantic layer.",
    )
    parser.add_argument(
        "--benchmark",
        required=True,
        choices=["bird", "cwd"],
        help="Which dataset to deploy.",
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="S3 staging bucket for Parquet exports. Required unless "
        "--grants-only (which never stages or ingests anything).",
    )
    parser.add_argument(
        "--grants-only",
        action="store_true",
        help="Re-apply the Lake Formation grants to every ALREADY-DEPLOYED "
        "namespace carrying this benchmark's prefix, then exit — no namespace "
        "create, no staging, no ingest. Use after a new agent role appears: the "
        "normal per-database path skips namespaces the state file marks done, so "
        "their grants would otherwise never be extended.",
    )
    parser.add_argument(
        "--catalog",
        required=True,
        help="Athena catalog id (federated S3 Tables catalog, "
        "e.g. s3tablescatalog/semantic-layer-dev-analytics-tables).",
    )
    parser.add_argument(
        "--workgroup",
        default="primary",
        help="Athena workgroup name (default: primary).",
    )
    parser.add_argument(
        "--stage-db",
        default="bench_stage",
        help="Glue database for transient external tables (default: bench_stage).",
    )
    parser.add_argument(
        "--output-loc",
        default="",
        help="Athena result s3:// location (default: workgroup-enforced).",
    )
    parser.add_argument(
        "--db-prefix",
        default="",
        help="Namespace prefix for db_id (default: derived from --benchmark, "
        "i.e. bird_ / cwd_).",
    )
    parser.add_argument(
        "--grant-roles",
        default="",
        help="EXTRA IAM role NAMES to also grant SELECT/DESCRIBE on each ingested "
        "namespace, beyond the auto-discovered agentcore roles. The executing role "
        "always gets CREATE_TABLE. Default: empty (auto-discovery is enough).",
    )
    parser.add_argument(
        "--no-auto-grant-agent-roles",
        action="store_true",
        help="Do NOT auto-discover and grant the agentcore roles (ontology build, "
        "metadata build, the 3 query agents, the 3 eval Lambdas). Only use this if "
        "the agentcore stack is not deployed yet; the ontology build will otherwise "
        "silently produce ontologies modelled from the LLM's memory instead of the "
        "real Glue schema.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Deploy only the first N databases (0 = all).",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated db_ids to restrict the deploy to (optional).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: no AWS writes, no state changes; log planned actions.",
    )
    parser.add_argument(
        "--state-file",
        default="",
        help="Resumable state JSON path (default: "
        "data/eval/benchmarks/<benchmark>/deploy_state.json).",
    )
    return parser.parse_args(argv)


def _resolve_bird_db_ids(*, raw_dir: Path, db_prefix: str) -> Dict[str, Path]:
    """Enumerate BIRD databases as ``{namespace: sqlite_path}`` (order preserved).

    Reuses ``ingest_bird.find_sqlite_files``; the namespace is
    ``<db_prefix><db_id>`` (matching ``ingest_database``'s own naming), which is
    what the state file and manifest key on.

    Args:
        raw_dir: The BIRD ``raw/`` directory holding the extracted mini-dev tree.
        db_prefix: Namespace prefix (e.g. ``bird_``).

    Returns:
        Insertion-ordered ``{namespace: <db_id>.sqlite path}``.
    """
    result: Dict[str, Path] = {}
    for db_path in bird.find_sqlite_files(raw_dir=raw_dir):
        namespace = f"{db_prefix}{db_path.stem}"
        result[namespace] = db_path
    return result


@dataclass
class GrantContext:
    """Everything the LF grant + namespace calls need, resolved once from AWS.

    Shared by the normal per-database deploy and ``--grants-only`` so the two
    paths cannot drift on WHO gets granted or on WHICH catalog.

    Args:
        session: The boto3 session (region already validated).
        region: Resolved region (``<region>`` placeholder under --dry-run only).
        account_id: Account id from STS (``<account>`` placeholder under --dry-run).
        executing_role_arn: Role ARN running this driver (gets CREATE_TABLE).
        lf_catalog_id: LF CatalogId ``<account>:<catalog>``.
        grant_role_arns: Every principal to grant SELECT/DESCRIBE to.
        table_bucket_arn: The S3 Tables table-bucket ARN.
    """

    session: Any
    region: str
    account_id: str
    executing_role_arn: str
    lf_catalog_id: str
    grant_role_arns: List[str]
    table_bucket_arn: str


def resolve_grant_context(*, args: argparse.Namespace) -> GrantContext:
    """Resolve region, caller identity, catalog id and every grant principal.

    Args:
        args: Parsed CLI args (reads ``catalog``, ``grant_roles``,
            ``no_auto_grant_agent_roles``, ``dry_run``).

    Returns:
        A populated ``GrantContext``.

    Raises:
        RuntimeError: No region configured on a real run.
        Exception: Any STS failure on a real run (the executing role and account
            are mandatory for the LF grant and the catalog id).
    """
    session = boto3.session.Session()
    # Fail loud on an unresolved region for a REAL run — the table-bucket ARN and
    # every AWS client depend on it, and silently defaulting could target the
    # wrong region. Under --dry-run a placeholder is fine (logging only).
    region = session.region_name
    if not region:
        if not args.dry_run:
            raise RuntimeError(
                "AWS region is not configured. Set AWS_REGION / AWS_DEFAULT_REGION "
                "(or configure a region in your AWS profile) before a real deploy."
            )
        region = "<region>"

    executing_role_arn = ""
    account_id = ""
    grant_role_names = [s.strip() for s in args.grant_roles.split(",") if s.strip()]
    try:
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        account_id = ident["Account"]
        executing_role_arn = _role_arn_from_caller_identity(arn=ident["Arn"])
    except Exception as exc:  # noqa: BLE001 - dry-run may run without credentials
        if not args.dry_run:
            # A real run MUST know the executing role (for the LF grant) and the
            # account (for the LF CatalogId / bucket ARN). Fail loud.
            raise
        logger.warning(
            "Could not resolve caller identity (%s); continuing --dry-run with "
            "placeholder role/account for logging only.",
            exc,
        )
        account_id = "<account>"
        executing_role_arn = "<executing-role-arn>"

    # LF CatalogId is "<account>:<catalog>" — account derived from STS, NEVER
    # hardcoded (this repo has a public GitHub remote).
    lf_catalog_id = f"{account_id}:{args.catalog}"
    grant_role_arns = [
        f"arn:aws:iam::{account_id}:role/{name}" for name in grant_role_names
    ]
    # Auto-discover the agentcore roles ON TOP OF any --grant-roles. Default ON
    # because every one of these roles is REQUIRED for the benchmark to produce
    # valid numbers, and the most damaging omission (the ontology build role) fails
    # silently rather than loudly — see _AGENT_ROLE_NAME_PARTS. Discovery is a
    # read-only IAM list, so it also runs under --dry-run whenever credentials
    # resolved: the whole point of the preview is seeing the real principal set.
    if not args.no_auto_grant_agent_roles and account_id != "<account>":
        discovered = discover_agent_role_arns(
            iam=session.client("iam"), account_id=account_id
        )
        # De-dup while preserving the explicit --grant-roles first.
        for arn in discovered:
            if arn not in grant_role_arns:
                grant_role_arns.append(arn)
    elif args.no_auto_grant_agent_roles:
        logger.warning(
            "--no-auto-grant-agent-roles set: granting ONLY %s. Ontology builds "
            "against these databases may silently model tables from the LLM's "
            "memory instead of the real Glue schema.",
            grant_role_arns or "(none)",
        )
    return GrantContext(
        session=session,
        region=region,
        account_id=account_id,
        executing_role_arn=executing_role_arn,
        lf_catalog_id=lf_catalog_id,
        grant_role_arns=grant_role_arns,
        table_bucket_arn=_table_bucket_arn(
            catalog=args.catalog, account_id=account_id, region=region
        ),
    )


def run_grants_only(*, args: argparse.Namespace, db_prefix: str) -> int:
    """Re-apply the LF grants to every already-deployed namespace of a benchmark.

    Grants ONLY — no namespace create, no download, no staging, no ingest. Reads
    the live namespace list from S3 Tables rather than the resumable state file so
    the grant set covers what is actually deployed (see ``list_namespaces``).

    Args:
        args: Parsed CLI args (reads ``catalog``, ``only``, ``limit``, ``dry_run``).
        db_prefix: Namespace prefix for this benchmark (e.g. ``bird_``).

    Returns:
        Process exit code: 0 on success, 1 if any namespace failed to grant.

    Raises:
        RuntimeError: Credentials did not resolve — the live namespace list is
            mandatory here, so a placeholder account cannot be tolerated even
            under --dry-run.
    """
    ctx = resolve_grant_context(args=args)
    if ctx.account_id == "<account>":
        raise RuntimeError(
            "--grants-only needs working AWS credentials: it enumerates the live "
            "S3 Tables namespaces to decide what to grant on, which cannot be "
            "faked under --dry-run."
        )

    # list_namespaces is read-only, so it runs even under --dry-run.
    namespaces = list_namespaces(
        s3tables=ctx.session.client("s3tables"),
        table_bucket_arn=ctx.table_bucket_arn,
        prefix=db_prefix,
    )
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else []
    if only:
        # --only accepts bare (superhero) or namespaced (bird_superhero) values.
        wanted = set(only) | {f"{db_prefix}{s}" for s in only}
        namespaces = [ns for ns in namespaces if ns in wanted]
    if args.limit > 0:
        namespaces = namespaces[: args.limit]

    if not namespaces:
        # Fail loud: an operator ran a grant fix and nothing was granted. Silence
        # here would read as success.
        logger.error(
            "No deployed namespace matches prefix %r in %s — nothing to grant. "
            "Check --catalog / --db-prefix / --only.",
            db_prefix,
            ctx.table_bucket_arn,
        )
        return 1

    logger.info(
        "Re-granting on %d namespace(s) %s for %d principal(s)",
        len(namespaces),
        namespaces,
        len(ctx.grant_role_arns),
    )
    lakeformation = None if args.dry_run else ctx.session.client("lakeformation")
    failures: List[str] = []
    for namespace in namespaces:
        ic.safe_ident(name=namespace)
        try:
            grant_lf(
                lakeformation=lakeformation,
                catalog_id=ctx.lf_catalog_id,
                namespace=namespace,
                executing_role_arn=ctx.executing_role_arn,
                grant_role_arns=ctx.grant_role_arns,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - fail loud per-ns, continue
            logger.exception("FAILED to grant on %r: %s", namespace, exc)
            failures.append(namespace)
    logger.info(
        "Grants-only summary: %d granted, %d failed",
        len(namespaces) - len(failures),
        len(failures),
    )
    if failures:
        logger.error("Failed namespace(s): %s", ", ".join(failures))
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Run the resumable deploy driver.

    Composes: download (reuse the ingest CLI's downloader) -> enumerate DBs ->
    load resumable state -> ensure stage db -> per pending DB {create namespace,
    LF grant, ingest_database, mark done + save state + save manifest}.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``); passed through
            to ``_parse_args`` for testability.

    Returns:
        Process exit code: 0 on full success, 2 for an unsupported/blocked
        benchmark (CWD not wired), 1 if any per-db ingest failed.
    """
    args = _parse_args(argv)
    benchmark = args.benchmark
    db_prefix = args.db_prefix or _DEFAULT_DB_PREFIX[benchmark]
    bench_dir = _BENCH_ROOT / benchmark
    raw_dir = bench_dir / "raw"
    state_file = (
        Path(args.state_file) if args.state_file else bench_dir / "deploy_state.json"
    )

    # --grants-only short-circuits BEFORE the download / CWD gate: it touches no
    # local data at all, so it works for CWD too (whose ingest is not wired here).
    if args.grants_only:
        return run_grants_only(args=args, db_prefix=db_prefix)

    if not args.bucket:
        # Only --grants-only can omit it; every other path stages Parquet.
        raise RuntimeError("--bucket is required unless --grants-only is set.")

    # CWD download/ingest is NOT cleanly reusable as a multi-DB deploy: ingest_cwd
    # requires a pinned commit SHA (download_cwd fails loud on the <PINNED_SHA>
    # placeholder) AND its ingest is single-namespace + DDL/CSV-driven (no
    # find_sqlite_files equivalent). Rather than half-wire it, fail loud with a
    # clear operator message and a non-zero exit; the BIRD path is fully wired.
    if benchmark == "cwd":
        logger.error(
            "CWD deploy is not yet wired into this driver: ingest_cwd requires a "
            "pinned commit SHA (CWD_COMMIT_SHA is still a placeholder) and loads a "
            "single DDL/CSV-defined namespace rather than a set of SQLite DBs. "
            "Populate %s manually per the runbook and run "
            "scripts/benchmarks/ingest_cwd.py directly, or wire CWD into this "
            "driver once its SHA is pinned.",
            raw_dir,
        )
        return 2

    # 1. Download the dataset (reuse the ingest CLI's downloader). Skip if raw/
    #    is already populated (idempotent).
    if raw_dir.exists() and any(raw_dir.iterdir()):
        logger.info("%s raw data present at %s (skipping download)", benchmark, raw_dir)
    else:
        # download_bird fails loud on a SHA mismatch; under --dry-run it only logs
        # the planned download and does NOT touch the network/filesystem.
        bird.download_bird(dest=raw_dir, dry_run=args.dry_run)
        if not (raw_dir.exists() and any(raw_dir.iterdir())):
            # raw/ still absent — under --dry-run the download was only logged, so
            # there is nothing to enumerate. Report cleanly (no traceback).
            logger.info(
                "raw/ absent at %s — nothing to enumerate. Under --dry-run the "
                "download is only logged; populate raw/ (or run without --dry-run "
                "with a pinned dataset) to preview/execute per-db actions.",
                raw_dir,
            )
            return 0

    # 2. Enumerate DBs and apply --only / --limit (order preserved).
    db_map = _resolve_bird_db_ids(raw_dir=raw_dir, db_prefix=db_prefix)
    all_namespaces = list(db_map)

    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else []
    if only:
        # --only values may be given bare (superhero) or namespaced (bird_superhero);
        # accept both by matching either form against the resolved namespaces.
        wanted = set(only) | {f"{db_prefix}{s}" for s in only}
        all_namespaces = [ns for ns in all_namespaces if ns in wanted]
        logger.info("Restricted to --only db(s): %s", ", ".join(all_namespaces))
    if args.limit > 0:
        all_namespaces = all_namespaces[: args.limit]

    logger.info("Candidate database(s): %d", len(all_namespaces))

    # 3. Load resumable state (or start fresh) and compute what's still pending.
    if state_file.exists():
        state = DeployState.load(path=state_file)
        logger.info(
            "Loaded deploy state from %s (%d completed)",
            state_file,
            len(state.completed),
        )
    else:
        state = DeployState(benchmark=benchmark)
        logger.info("No prior deploy state at %s — starting fresh", state_file)

    pending = pending_dbs(all_db_ids=all_namespaces, state=state)
    skipped = len(all_namespaces) - len(pending)
    logger.info("%d pending, %d already done (skipped)", len(pending), skipped)

    # Validate each namespace as a safe identifier BEFORE any interpolation into
    # AWS calls / SQL (reuse ic.safe_ident; fail loud on a bad name).
    for namespace in pending:
        ic.safe_ident(name=namespace)

    # 4. Resolve region, identity, catalog + grant principals, then clients. Under
    #    --dry-run do NOT create mutating clients or call mutating APIs; plan-only.
    ctx = resolve_grant_context(args=args)
    session = ctx.session
    lf_catalog_id = ctx.lf_catalog_id
    executing_role_arn = ctx.executing_role_arn
    grant_role_arns = ctx.grant_role_arns
    table_bucket_arn = ctx.table_bucket_arn

    s3 = None if args.dry_run else session.client("s3")
    athena = None if args.dry_run else session.client("athena")
    s3tables = None if args.dry_run else session.client("s3tables")
    glue = None if args.dry_run else session.client("glue")
    lakeformation = None if args.dry_run else session.client("lakeformation")

    # 5. Ensure the Glue staging DB exists (once, before the per-db loop).
    ic.safe_ident(name=args.stage_db)
    ensure_stage_db(glue=glue, stage_db=args.stage_db, dry_run=args.dry_run)

    # 6. Per pending DB: create namespace -> LF grant -> ingest -> record.
    # Driver-owned resumable manifest (deploy_manifest.json). Its entries are
    # transpile-blind (transpile_ok always True); step 7 derives the real flags
    # when it writes manifest.json alongside bird.jsonl.
    manifest_path = bench_dir / "deploy_manifest.json"
    manifest = (
        ic.Manifest.load(path=manifest_path)
        if manifest_path.exists()
        else ic.Manifest(benchmark=benchmark)
    )
    ingested = 0
    failures: List[str] = []
    for namespace in pending:
        db_path = db_map[namespace]
        try:
            # a. Create the S3 Tables namespace if absent.
            create_namespace_if_absent(
                s3tables=s3tables,
                table_bucket_arn=table_bucket_arn,
                namespace=namespace,
                dry_run=args.dry_run,
            )
            # b. Lake Formation grants (executing role CREATE_TABLE; agent roles
            #    SELECT/DESCRIBE).
            grant_lf(
                lakeformation=lakeformation,
                catalog_id=lf_catalog_id,
                namespace=namespace,
                executing_role_arn=executing_role_arn,
                grant_role_arns=grant_role_arns,
                dry_run=args.dry_run,
            )
            # c. Ingest the DB via the EXISTING helper (no reimplementation).
            #    stage_prefix=benchmark keeps this benchmark's staging Parquet
            #    under its own S3 key prefix.
            entry = bird.ingest_database(
                db_path=db_path,
                db_prefix=db_prefix,
                bucket=args.bucket,
                catalog=args.catalog,
                workgroup=args.workgroup,
                stage_db=args.stage_db,
                output_loc=args.output_loc,
                s3=s3,
                athena=athena,
                dry_run=args.dry_run,
                stage_prefix=benchmark,
            )
        except Exception as exc:  # noqa: BLE001 - fail loud per-db, continue
            # Fail loudly for THIS db but continue to the next so one broken DB
            # doesn't abort the whole deploy; record it and exit non-zero later.
            logger.exception("FAILED to deploy db %r: %s", namespace, exc)
            failures.append(namespace)
            continue

        # d/e. On success: persist after EACH db so a crash mid-run is resumable.
        # ORDER MATTERS — the manifest is written FIRST and the resumable state
        # LAST (state is the commit point). If state were saved first, a crash
        # before the manifest write would mark the db done while its manifest
        # entry is missing forever -> the re-run skips it -> KeyError at scoring.
        # Manifest-first is safe because Manifest.add dedupes by db_id: a crash
        # before state.save re-ingests + re-adds on the next run, yielding one
        # entry. Skip all writes under dry-run.
        manifest.add(entry=entry)
        ingested += 1
        if not args.dry_run:
            manifest.save(path=manifest_path)  # (1) durable output first
            state.mark_done(db_id=namespace)
            state.save(path=state_file)  # (2) commit point LAST
            logger.info("Recorded %r done (manifest saved, then state)", namespace)
        else:
            logger.info(
                "[DRY RUN] would record %r done (no state/manifest write)", namespace
            )

    # 7. Emit bird.jsonl + manifest.json for every db deployed so far (this run or
    #    earlier ones). The BIRD eval (notebook 12 / run_bird_per_db_eval.py) loads
    #    its questions and transpiled gold SQL from bird.jsonl, so a driver-only
    #    ingest that skipped this left every per-DB eval failing FileNotFoundError.
    #    emit_dataset re-derives transpile_ok, so manifest.json is accurate.
    if not failures:
        records = bird.load_question_records(raw_dir=raw_dir)
        rows = bird.bird_records_to_rows(records=records, db_prefix=db_prefix)
        bird.emit_dataset(rows=rows, manifest=manifest, dry_run=args.dry_run)

    # 8. Summary + exit code.
    logger.info(
        "Deploy summary [%s]: %d ingested, %d skipped (already done), %d failed",
        benchmark,
        ingested,
        skipped,
        len(failures),
    )
    if failures:
        logger.error("Failed db(s): %s", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
