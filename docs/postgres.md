# Optional Postgres storage

`asana_postgres.py` and an isolated local Postgres 18 development stack are
optional components of the same application on `main`. The core exporter is
unchanged; collect a completed snapshot first, then import it. This module makes
no Asana API calls and never needs an Asana token. No scheduler or web service is
installed, and no database port is published to the host.

## Start a local database

Run in the repository root with an account allowed to use Docker. Generate the
database password locally without printing or committing it:

```sh
python3 - <<'PY'
from pathlib import Path
import os, secrets
Path('secrets').mkdir(mode=0o700, exist_ok=True)
fd = os.open('secrets/postgres.password', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    handle.write(secrets.token_urlsafe(48) + '\n')
PY
export ASANA_PG_PASSWORD_FILE=./secrets/postgres.password
export LOCAL_UID=$(id -u)
export LOCAL_GID=$(id -g)
mkdir -p asana-exports-v2 postgres-reports
docker compose -f compose.postgres.yaml build store
docker compose -f compose.postgres.yaml up -d --wait postgres
docker compose -f compose.postgres.yaml run --rm store init-db
```

The password generator intentionally refuses to overwrite an existing secret.
Retain that file securely for the persistent database; generating a different
password does not rotate an initialized Postgres role. Compose mounts the
existing file rather than putting its value in container configuration or Git.

The named volume `asana-reporting-postgres_postgres_data` survives container
replacement and `docker compose down`. Do not remove it or use `down --volumes`
on this development stack when it contains records you need. The isolated test
suite uses separate temporary schemas and removes only those schemas.

## Collect, import and report

Use the unchanged exporter locally or through its standalone container:

```sh
python3 asana_time_export.py --token-file /approved/secrets/asana.token \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --start 2026-10-01 --end 2026-10-18 --non-interactive
```

Then substitute the completed directory name printed by the exporter:

```sh
docker compose -f compose.postgres.yaml run --rm store import-snapshot \
  /snapshots/COMPLETED_SNAPSHOT_DIRECTORY \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID
docker compose -f compose.postgres.yaml run --rm store report \
  --owner OWNER_GID --workspace WORKSPACE_GID \
  --start 2026-10-01 --end 2026-10-18 --output /data/daily.csv
```

Reports appear in the host `postgres-reports/` directory, which Git ignores.
They show Actual Time in Hours, integer minutes, collection coverage, last source
snapshot time, and any unseen entries/minutes that still require review.
Uncollected dates are blank rather than zero. Import ID columns as Text in Sheets
or Excel. All database entry IDs are text and all authoritative durations are
integer minutes. Reports include unseen records conservatively, with explicit
flags; they are provisional logged-time reports, not approved billing totals.

## Import guarantees and limits

- Only completed, unfiltered schema-v2 snapshots are accepted. All five file
  hashes, expected owner/workspace, unique IDs, dates, entry amounts, daily and
  monthly totals, and human reports must reconcile before contacting Postgres.
- The exact same snapshot is a no-op on replay. Overlapping snapshots update by
  `(workspace_gid, entry_gid)` and never add another copy of the hours.
- Core facts, creator, original attribution and optional context remain separate.
  Core-only refreshes preserve earlier context when core identity is unchanged.
  Core identity changes invalidate stale context; earlier values remain in history.
- Imports run in a transaction under an advisory lock. Edits, owner/date moves,
  enrichment changes and visibility changes append before/after revisions.
  A failure rolls back the run, entries, revisions and observation watermark.
- Import previously unseen snapshots in strictly increasing export-time order
  for each owner/workspace. Older/equally timed new observations are rejected.
  Re-fetch an older work-date window now when historical backfill is needed.
  Newer observations of a reassigned entry also prevent cross-owner rollback.
- A missing entry is marked `unseen` only inside a successful unfiltered owner
  and date scope. It is retained in totals and history. Access changes and true
  deletions cannot be distinguished automatically; review is required. Known
  older snapshot replays cannot resurrect entries. Dates outside a pull stay intact.
- Schema version 1 is applied atomically and checksum-checked. Future changes
  need an explicit migration; changing an applied SQL file is rejected.

The schema contains `schema_migrations`, `sync_runs`, `owner_watermarks`,
`time_entries`, `time_entry_revisions`, and a `daily_actual_time` view.
Each run retains its manifest and project-context provenance. There is no
automatic deletion, retention expiry, billing approval, period locking or
confirmed-deletion workflow in this initial integration.

## Integration tests

With the local database healthy:

```sh
docker compose -f compose.postgres.yaml --profile test build integration-tests
docker compose -f compose.postgres.yaml --profile test run --rm integration-tests
```

All fixtures use invented identities and entries. Each real-database test creates
and drops its own uniquely named schema, leaving imported records alone. The
ordinary host test command skips real-database tests unless explicitly enabled.
GitHub CI runs the complete suite against a disposable Postgres container with
no Asana credentials or business exports.

## Backups and remote deployment

Create a local logical backup from the database container:

```sh
docker compose -f compose.postgres.yaml exec -T postgres \
  pg_dump -U asana_reporting -d asana_reporting -Fc > /approved/private/asana-reporting.dump
```

The archive contains work data: encrypt it for off-host retention. A database
volume is not a backup. Test restoring into a separate database before adopting
this stack operationally; restore testing is not automated by this application.

This is a local development configuration. Its bootstrap/application role owns
the database, and TLS is disabled only on its isolated Docker network. A remote
deployment needs a separate limited application role, migrations by a privileged
operator, approved hosting, backups, and verified TLS. The standalone storage CLI
defaults to `PGSSLMODE=verify-full` and requires explicit `PGHOST`, `PGDATABASE`,
`PGUSER` and `PGPASSWORD_FILE`. Only the optional container installs Psycopg;
the host environment and simple exporter remain dependency-free.

Sources: [Psycopg installation](https://www.psycopg.org/psycopg3/docs/basic/install.html),
[Postgres advisory locking](https://www.postgresql.org/docs/current/explicit-locking.html),
[Postgres upserts](https://www.postgresql.org/docs/current/sql-insert.html).
