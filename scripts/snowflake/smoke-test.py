#!/usr/bin/env python3
"""Prove a live Snowflake connection works, through agents/shared/snowflake_bench.

This deliberately exercises the module the benchmark will use rather than the raw
driver, so a pass means `run_sql` itself works against the real account: key-pair
auth, session context, row drain, and the server-side stats lookup that R-VES
timing depends on.

Two credential sources, so this is runnable BEFORE anything is deployed:

    # Local PEM — no AWS, no CDK, no Secrets Manager.
    ./scripts/snowflake/smoke-test.py \
        --account <org>-<account> --warehouse HUTHMAC_WH \
        --user HUTHMAC --key tmp/rsa_key.p8 \
        --database FHIR_DB --schema FHIR_R4

    # Secrets Manager — the same secret the Athena connector reads, so this also
    # verifies what `npm run sync:snowflake-secret` wrote before blaming the Lambda.
    ./scripts/snowflake/smoke-test.py \
        --account <org>-<account> --warehouse HUTHMAC_WH \
        --secret-name semantic-layer/snowflake/connector \
        --database FHIR_DB --schema FHIR_R4

The passphrase is read from $SNOWFLAKE_PRIVATE_KEY_PASSPHRASE if set, otherwise
prompted for without echo. It is never accepted as a flag: argv is visible in `ps`
and lands in shell history. Account/warehouse/user default from
$SNOWFLAKE_ACCOUNT / $SNOWFLAKE_WAREHOUSE / $SNOWFLAKE_USER when set.

Requires snowflake-connector-python (scripts/benchmarks/requirements.txt), which is
NOT in the project venv by design. Throwaway install:

    uv venv tmp/sfvenv && uv pip install --python tmp/sfvenv/bin/python \
        snowflake-connector-python==4.7.2 cryptography boto3
    tmp/sfvenv/bin/python scripts/snowflake/smoke-test.py ...
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Import from the repo root so this runs from anywhere without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.shared.snowflake_bench import connect, load_credentials, run_sql  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test a live Snowflake connection via snowflake_bench.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--account",
        default=os.environ.get("SNOWFLAKE_ACCOUNT", ""),
        help="Snowflake account identifier, e.g. myorg-myaccount ($SNOWFLAKE_ACCOUNT).",
    )
    parser.add_argument(
        "--warehouse",
        default=os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
        help="Warehouse to run in ($SNOWFLAKE_WAREHOUSE). Snowflake fails every "
        "query without one in session context.",
    )
    parser.add_argument("--database", default="", help="Database to USE.")
    parser.add_argument("--schema", default="", help="Schema to USE.")
    parser.add_argument("--role", default="", help="Optional role to assume.")
    parser.add_argument(
        "--secret-name",
        default="",
        help="Secrets Manager secret holding the key-pair credential. Mutually "
        "exclusive with --key.",
    )
    parser.add_argument(
        "--key", default="", help="Path to a PKCS#8 private key PEM. Requires --user."
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("SNOWFLAKE_USER", ""),
        help="Snowflake user ($SNOWFLAKE_USER). Only needed with --key; the secret "
        "already carries it.",
    )
    parser.add_argument("--region", default="", help="AWS region for --secret-name.")
    parser.add_argument(
        "--sql",
        default="",
        help="Extra query to run last, e.g. a Spider 2.0 gold query. Runs with "
        "server-side stats so its timing is comparable to Athena's engine time.",
    )
    return parser.parse_args()


def _credentials_from_key(*, key_path: str, user: str) -> dict:
    """Build the same payload shape the CDK secret sync writes, straight from a local PEM.

    Args:
        key_path: Path to a PKCS#8 private key PEM.
        user: Snowflake user the key is registered on.

    Returns:
        A credential dict in the shape ``load_credentials`` returns, so the local-key
        and Secrets Manager paths exercise identical downstream code.
    """
    pem = Path(key_path).read_text(encoding="utf-8")
    if "PRIVATE KEY" not in pem:
        raise SystemExit(f"❌ {key_path} does not look like a PKCS#8 private key.")

    passphrase = ""
    if "ENCRYPTED PRIVATE KEY" in pem:
        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or getpass.getpass(
            f"Snowflake private-key passphrase for {user}: "
        )
        if not passphrase:
            raise SystemExit("❌ Empty passphrase, but the key is encrypted.")

    credentials = {"username": user, "sfUser": user, "pem_private_key": pem}
    if passphrase:
        credentials["pem_private_key_passphrase"] = passphrase
    return credentials


def _public_key_fingerprint(credentials: dict) -> str:
    """Compute Snowflake's RSA_PUBLIC_KEY_FP for the key in hand.

    Snowflake's fingerprint is SHA256 over the DER SubjectPublicKeyInfo, base64'd —
    the one value that can be compared against ``DESC USER`` without a working
    connection, which is exactly the situation when auth is failing.
    """
    import base64
    import hashlib

    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        credentials["pem_private_key"].encode("utf-8"),
        password=(
            credentials["pem_private_key_passphrase"].encode("utf-8")
            if credentials.get("pem_private_key_passphrase")
            else None
        ),
    )
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def main() -> int:
    args = _parse_args()

    if not args.account or not args.warehouse:
        raise SystemExit("❌ --account and --warehouse are required. See --help.")
    if bool(args.secret_name) == bool(args.key):
        raise SystemExit("❌ Pass exactly one of --secret-name or --key. See --help.")
    if args.key and not args.user:
        raise SystemExit("❌ --key requires --user (the secret would carry it).")

    if args.secret_name:
        print(f"🔐 Reading credential from secret {args.secret_name}...")
        credentials = load_credentials(secret_id=args.secret_name, region=args.region)
        print(f"   user={credentials.get('username') or credentials.get('sfUser')}")
    else:
        print(f"🔐 Reading credential from {args.key}...")
        credentials = _credentials_from_key(key_path=args.key, user=args.user)

    print(f"🔗 Connecting to {args.account} (warehouse {args.warehouse})...")
    try:
        conn = connect(
            account=args.account,
            warehouse=args.warehouse,
            credentials=credentials,
            database=args.database,
            schema=args.schema,
            role=args.role,
        )
    except ValueError as exc:
        # Wrong passphrase / unusable key material. A traceback here reads as a bug
        # in the module when it is almost always the credential.
        raise SystemExit(f"❌ {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — driver errors are diagnosed, then re-raised
        # "JWT token is invalid" (390144) means the key decrypted fine and the token
        # was well-formed, but Snowflake has no matching public key on the user. That
        # is an account-setup step, not a bug here, so print the fingerprint to
        # compare against DESC USER rather than leaving a bare driver traceback.
        if "JWT token is invalid" in str(exc):
            print(f"❌ Snowflake rejected the key-pair token: {exc}", file=sys.stderr)
            print(
                f"\n   The private key parsed and the JWT was built for user "
                f"{args.user or credentials.get('username')} with fingerprint:\n"
                f"     {_public_key_fingerprint(credentials)}\n\n"
                "   Snowflake has no public key matching it. Someone with ACCOUNTADMIN "
                "must register it:\n"
                "     DESC USER <user>;  -- compare the RSA_PUBLIC_KEY_FP property\n"
                "     ALTER USER <user> SET RSA_PUBLIC_KEY = '<contents of the .pub, "
                "one line, no PEM header>';",
                file=sys.stderr,
            )
            return 1
        raise

    try:
        # 1. Identity. Confirms key-pair auth resolved and which role is in effect —
        #    a missing GRANT shows up here as a role you did not expect, not as an
        #    auth failure three queries later.
        res = run_sql(
            conn=conn,
            sql="SELECT CURRENT_USER(), CURRENT_ACCOUNT(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE()",
        )
        print(f"✅ Authenticated: {res.rows[0]}  ({res.elapsed_ms} ms wall-clock)")

        # 2. Session context + a real warehouse-backed scan. The first query on a
        #    suspended warehouse pays a resume cost of several seconds; that is the
        #    warehouse waking, not the module being slow.
        if args.database and args.schema:
            res = run_sql(
                conn=conn,
                sql="SELECT table_name, row_count FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY row_count DESC NULLS LAST LIMIT 10",
                database_name=args.database,
                schema_name=args.schema,
                server_stats=True,
            )
            # Bind parameters work here; USE takes an identifier and cannot bind,
            # which is why _use_context validates instead.
            print(
                f"✅ {args.database}.{args.schema}: {len(res.rows)} tables listed "
                f"({res.elapsed_ms} ms, server_time={res.server_time}, "
                f"bytes_scanned={res.bytes_scanned})"
            )
            for name, rows in res.rows:
                print(f"     {name}  ({rows} rows)")
        else:
            print("⏭️  Skipping table listing: pass --database and --schema.")

        # 3. Prove the timeout path really cancels server-side rather than hanging
        #    until a socket timeout — the guard that stops a runaway benchmark query
        #    from burning warehouse credits.
        try:
            run_sql(
                conn=conn,
                sql="SELECT COUNT(*) FROM TABLE(GENERATOR(TIMELIMIT => 30))",
                max_wait_s=5,
            )
            print("⚠️  Timeout probe finished early; the cancel path went unexercised.")
        except TimeoutError as exc:
            print(f"✅ Timeout path cancels server-side: {str(exc)[:90]}")

        if args.sql:
            res = run_sql(
                conn=conn,
                sql=args.sql,
                database_name=args.database,
                schema_name=args.schema,
                server_stats=True,
            )
            print(
                f"✅ Custom query: {len(res.rows)} rows, columns={res.columns} "
                f"({res.elapsed_ms} ms, server_time={res.server_time}, "
                f"query_id={res.query_id})"
            )
            for row in res.rows[:5]:
                print(f"     {row}")
    finally:
        conn.close()

    print("\n🎉 snowflake_bench works against the live account.")
    print("   Next: the federated path is a separate test — deploy the Athena stack")
    print("   (-c snowflakeAccount/snowflakeWarehouse/snowflakeDatabases), run")
    print("   `npm run sync:snowflake-secret` and `npm run set:snowflake-casing`, then")
    print("   query snowflake_<database>.<schema>.<table>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
