#!/usr/bin/env bash
# Run a throwaway local Postgres 16 cluster without Docker (Linux).
# Useful where Docker is unavailable (CI sandboxes, minimal VMs).
# Data lives in .pgdata/ (gitignored). Listens on 127.0.0.1:5433.
#
#   scripts/dev_pg.sh start | stop | status
#
# Then point the app at it:
#   RELAY_DATABASE_URL=postgresql+psycopg://relay@127.0.0.1:5433/relay
set -euo pipefail

PG_BIN="${PG_BIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${PGDATA:-$(pwd)/.pgdata}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-relay}"

if [[ ! -x "$PG_BIN/initdb" ]]; then
  echo "PostgreSQL 16 server binaries not found at $PG_BIN" >&2
  echo "Install with: sudo apt-get install postgresql postgresql-contrib" >&2
  exit 1
fi

run_as() {
  # Postgres refuses to run as root; drop to the postgres user when needed.
  if [[ "$(id -u)" == "0" ]]; then
    sudo -u postgres "$@"
  else
    "$@"
  fi
}

case "${1:-}" in
  start)
    if [[ ! -d "$PGDATA" ]]; then
      mkdir -p "$PGDATA"
      if [[ "$(id -u)" == "0" ]]; then
        chown postgres:postgres "$PGDATA"
      fi
      chmod 700 "$PGDATA" || true
      run_as "$PG_BIN/initdb" -D "$PGDATA" -U "$PGUSER" --auth=trust
    fi
    run_as "$PG_BIN/pg_ctl" -D "$PGDATA" \
      -o "-p $PGPORT -k /tmp" -l "$PGDATA/server.log" start
    for db in relay relay_test; do
      run_as "$PG_BIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" \
        -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='$db'" \
        | grep -q 1 || \
      run_as "$PG_BIN/psql" -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" \
        -d postgres -c "CREATE DATABASE $db"
    done
    echo "Postgres ready on 127.0.0.1:$PGPORT (databases: relay, relay_test)"
    ;;
  stop)
    run_as "$PG_BIN/pg_ctl" -D "$PGDATA" stop -m fast
    ;;
  status)
    run_as "$PG_BIN/pg_ctl" -D "$PGDATA" status
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
