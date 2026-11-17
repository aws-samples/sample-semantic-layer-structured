"""Bootstrap the Athena Snowflake connector's Snowflake-side identity.

Executes docs/setup_athena_connector.sql as an ACCOUNTADMIN-capable user, then proves the
result by re-connecting as the connector's own service user with key-pair auth.

Why this is a script and not a CDK construct: registering an RSA public key on a Snowflake
user (`ALTER USER … SET RSA_PUBLIC_KEY`) has no CloudFormation equivalent, so it can only be
driven through a Snowflake session. See cdk/README.md § Snowflake Athena connector.

The admin credential is read from cdk/.env, never from the command line, so it does not land
in shell history or process listings. Expected keys (both ignored by the CDK app, which only
reads the five non-secret SNOWFLAKE_* values):

    SNOWFLAKE_ADMIN_USER=<a user holding ACCOUNTADMIN>
    SNOWFLAKE_ADMIN_PASSWORD=<its password>

Usage:
    /tmp/sfvenv/bin/python scripts/snowflake/bootstrap_connector.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

REPO = Path(__file__).resolve().parents[2]
ENV_PATH = REPO / "cdk" / ".env"
SQL_PATH = REPO / "docs" / "setup_athena_connector.sql"
SERVICE_KEY_PATH = REPO / "docs" / "athena_connector.p8"


def parse_env(*, path: Path) -> dict[str, str]:
    """Parse a dotenv file into a dict, mirroring cdk/bin/env-config.ts.

    Line-based: `KEY=VALUE`, `#` comments and blank lines skipped, surrounding quotes
    stripped only when the same quote character opens and closes the value.

    :param path: filesystem path to the .env file
    :return: mapping of key to value
    """
    out: dict[str, str] = {}
    for raw in path.read_text().split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
            if raw.strip()[len(key) + 1] == '"':
                value = value.replace("\\n", "\n").replace("\\t", "\t")
        out[key.strip()] = value
    return out


def require(*, env: dict[str, str], key: str) -> str:
    """Return env[key], raising if absent or empty.

    Fails loudly rather than defaulting: a missing admin credential must stop the run, not
    silently fall through to an anonymous connection attempt.

    :param env: parsed environment mapping
    :param key: required key name
    :return: the non-empty value
    """
    value = env.get(key, "").strip()
    if not value:
        raise RuntimeError(
            f"{key} is not set in {ENV_PATH}. Add it (see this file's docstring) and re-run."
        )
    return value


def split_statements(*, sql: str) -> list[str]:
    """Split a SQL script into executable statements, dropping comments.

    The Snowflake driver executes one statement per call by default, so the script has to be
    split. Line comments are removed first; no statement here contains a semicolon inside a
    string literal, so a plain split is safe.

    :param sql: full text of the SQL script
    :return: list of statements with whitespace trimmed, empties removed
    """
    without_comments = "\n".join(
        line for line in sql.split("\n") if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in without_comments.split(";") if s.strip()]


def service_key_der(*, path: Path) -> bytes:
    """Load the connector's unencrypted PKCS#8 key and return it as DER.

    :param path: path to the PEM-encoded private key
    :return: DER-encoded PKCS#8 private key bytes
    """
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def label(*, statement: str) -> str:
    """Produce a short one-line label for progress output.

    :param statement: the SQL statement
    :return: first few words, with any long key literal elided
    """
    flat = re.sub(r"\s+", " ", statement)
    flat = re.sub(r"'[A-Za-z0-9+/=]{40,}'", "'<public key>'", flat)
    return flat[:88]


def run_bootstrap(*, env: dict[str, str], dry_run: bool) -> None:
    """Execute the setup script as the admin user.

    :param env: parsed environment mapping containing the admin credential
    :param dry_run: when True, print the statements and exit without connecting
    """
    statements = split_statements(sql=SQL_PATH.read_text())
    if dry_run:
        print(f"{len(statements)} statements from {SQL_PATH}:")
        for s in statements:
            print(f"  - {label(statement=s)}")
        return

    conn = snowflake.connector.connect(
        account=require(env=env, key="SNOWFLAKE_ACCOUNT"),
        user=require(env=env, key="SNOWFLAKE_ADMIN_USER"),
        password=require(env=env, key="SNOWFLAKE_ADMIN_PASSWORD"),
        login_timeout=60,
    )
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_USER(), CURRENT_ACCOUNT()")
    print(f"connected as {cur.fetchone()}")

    for statement in statements:
        cur.execute(statement)
        rows = cur.fetchall()
        print(f"  OK  {label(statement=statement)}")
        # DESC/SHOW/SELECT return rows worth seeing; DDL returns a single status string.
        if rows and not statement.upper().startswith(
            ("CREATE", "GRANT", "ALTER", "USE")
        ):
            for row in rows:
                print(f"        {row}")
    conn.close()


def verify_service_login(*, env: dict[str, str]) -> bool:
    """Connect as the connector's service user with key-pair auth.

    This is the real proof: it is the same auth path the Athena connector Lambda uses.

    :param env: parsed environment mapping
    :return: True when the login succeeds and a query runs
    """
    try:
        conn = snowflake.connector.connect(
            account=require(env=env, key="SNOWFLAKE_ACCOUNT"),
            user=require(env=env, key="SNOWFLAKE_USER"),
            role=env.get("SNOWFLAKE_ROLE") or None,
            warehouse=env.get("SNOWFLAKE_WAREHOUSE") or None,
            database=env.get("SNOWFLAKE_DATABASE") or None,
            private_key=service_key_der(path=SERVICE_KEY_PATH),
            login_timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - the Snowflake error text is the diagnosis
        print(f"service login FAILED: {str(exc).splitlines()[0]}")
        return False

    database = require(env=env, key="SNOWFLAKE_DATABASE")
    cur = conn.cursor()
    cur.execute(
        "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE()"
    )
    print(f"service login OK: {cur.fetchone()}")
    # Listing tables proves the GRANTs landed, not just that authentication succeeded:
    # a role can log in and still see nothing without USAGE on the schema.
    cur.execute(f"SHOW TABLES IN DATABASE {database}")
    tables = cur.fetchall()
    print(f"visible tables in {database}: {len(tables)}")
    for row in tables[:10]:
        print(f"        {row[1]}")
    conn.close()
    return True


def main() -> int:
    """Entry point: run the bootstrap, then verify the service login.

    :return: process exit code, 0 on success
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="list the statements without connecting"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="skip the bootstrap, only test service login",
    )
    args = parser.parse_args()

    env = parse_env(path=ENV_PATH)
    if not args.verify_only:
        run_bootstrap(env=env, dry_run=args.dry_run)
        if args.dry_run:
            return 0
    return 0 if verify_service_login(env=env) else 1


if __name__ == "__main__":
    sys.exit(main())
