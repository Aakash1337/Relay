"""Migration runner: schema, constraints, triggers, RLS, rule seeding.

Idempotent by construction — safe to run repeatedly:

1. ensure the ``relay_app`` role exists (login, no superpowers);
2. ``metadata.create_all`` for tables/constraints/indexes;
3. apply the SQL files in src/relay/db/sql/ in order (functions,
   triggers, RLS — all written idempotently);
4. seed ``lead_transition_rules`` from the Python state machine, so the
   database enforces exactly the transitions relay.domain.states defines.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

from relay.config import get_settings
from relay.db.engine import admin_engine, reset_engines
from relay.db.models import Base
from relay.domain.states import transition_rule_rows
from relay.logs import get_logger, setup_logging

log = get_logger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"


def _ensure_app_role() -> None:
    # CREATE/ALTER ROLE are utility statements: no bind parameters allowed.
    # Use psycopg's SQL composition for safe literal quoting instead.
    from psycopg import sql as pgsql

    password = get_settings().app_db_password.get_secret_value()
    with admin_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'relay_app'")
        ).scalar()
        verb = "ALTER" if exists else "CREATE"
        statement = pgsql.SQL("{} ROLE relay_app WITH LOGIN PASSWORD {}").format(
            pgsql.SQL(verb), pgsql.Literal(password)
        )
        raw = conn.connection.dbapi_connection
        statement_str = statement.as_string(raw)
        conn.exec_driver_sql(statement_str)
        if not exists:
            log.info("created role", role="relay_app")


def _apply_sql_files() -> None:
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        statements = sql_file.read_text(encoding="utf-8")
        with admin_engine().begin() as conn:
            # Raw cursor with params=None: psycopg must not interpret the
            # '%' characters inside plpgsql RAISE format strings.
            raw = conn.connection.dbapi_connection
            with raw.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(statements)
        log.info("applied sql", file=sql_file.name)


def _seed_transition_rules() -> None:
    rows = transition_rule_rows()
    with admin_engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM lead_transition_rules")
        conn.exec_driver_sql(
            "INSERT INTO lead_transition_rules (from_state, to_state) VALUES "
            + ", ".join(["(%s, %s)"] * len(rows)),
            tuple(v for row in rows for v in row),
        )
    log.info("seeded transition rules", count=len(rows))


def reset_schema() -> None:
    """DESTRUCTIVE: drop and recreate the public schema (dev/test only)."""
    with admin_engine().begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    log.info("schema reset")


def migrate(reset: bool = False) -> None:
    if reset:
        reset_schema()
    _ensure_app_role()
    Base.metadata.create_all(admin_engine())
    _apply_sql_files()
    _seed_transition_rules()
    log.info("migration complete")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Apply the RELAY schema")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the schema first (DESTRUCTIVE)",
    )
    args = parser.parse_args()
    try:
        migrate(reset=args.reset)
    finally:
        reset_engines()


if __name__ == "__main__":
    main()
