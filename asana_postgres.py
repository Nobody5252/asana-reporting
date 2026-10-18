#!/usr/bin/env python3
"""Optional Postgres storage for completed exporter snapshots; no Asana API calls."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import asana_time_export as exporter

DATA_FILES = {"entries.csv", "daily_totals.csv", "daily_report.csv", "monthly_report.csv", "project_context.json"}
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
REPORT_COLUMNS = ["Date", "Workspace", "Owner", "Metric", "Actual Time (Hours)", "Unit",
                  "entry_count", "duration_minutes", "unseen_entry_count", "unseen_minutes",
                  "coverage_status", "last_snapshot_at"]


class StorageError(Exception):
    """Safe diagnostic that never contains SQL, connection strings or credentials."""


@dataclass(frozen=True)
class Snapshot:
    digest: str
    manifest: dict
    entries: list[dict]
    project_context: dict
    exported_at: datetime


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
        raise StorageError("Invalid nonnegative integer in snapshot.")
    number = int(value)
    if number > 9223372036854775807:
        raise StorageError("Snapshot amount exceeds the supported integer range.")
    return number


def csv_rows(data, columns):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != columns:
        raise StorageError("Snapshot CSV columns do not match the supported schema.")
    result = list(reader)
    if any(set(row) != set(columns) or any(v is None for v in row.values()) for row in result):
        raise StorageError("Malformed CSV row in snapshot.")
    return result


def load_snapshot(folder, expected_owner, workspace):
    """Read once, verify every hash, then validate scope and reconciliation."""
    expected_owner, workspace = exporter.gid(expected_owner), exporter.gid(workspace)
    folder = Path(folder)
    if folder.name.startswith(".asana-incomplete-"):
        raise StorageError("Incomplete snapshots cannot be imported.")
    blobs, size = {}, 0
    try:
        for name in sorted(DATA_FILES | {"manifest.json"}):
            path = folder / name
            if path.is_symlink():
                raise StorageError("Snapshot files must not be symbolic links.")
            with path.open("rb") as handle:
                data = handle.read(MAX_SNAPSHOT_BYTES + 1)
            size += len(data)
            if size > MAX_SNAPSHOT_BYTES:
                raise StorageError("Snapshot exceeds the size limit.")
            blobs[name] = data
        manifest = json.loads(blobs["manifest.json"])
        if not isinstance(manifest, dict):
            raise StorageError("Invalid snapshot manifest.")
        if (manifest.get("schema_version") != 2 or manifest.get("status") != "complete"
                or manifest.get("core_snapshot_status") != "complete"):
            raise StorageError("Only completed exporter schema-v2 snapshots are supported.")
        if manifest.get("owner_gid") != expected_owner or manifest.get("workspace_gid") != workspace:
            raise StorageError("Snapshot owner/workspace does not match the expected scope.")
        if manifest.get("project_filter_gid") is not None or manifest.get("task_name_contains") is not None:
            raise StorageError("Narrowed snapshots cannot update the central owner-time store.")
        if manifest.get("expected_owner_gid") not in (None, expected_owner):
            raise StorageError("Snapshot contains a conflicting expected owner.")
        checksums = manifest.get("files_sha256")
        if not isinstance(checksums, dict) or set(checksums) != DATA_FILES:
            raise StorageError("Snapshot must list exactly the five supported data files.")
        if any(hashlib.sha256(blobs[name]).hexdigest() != checksums[name] for name in DATA_FILES):
            raise StorageError("Snapshot checksum verification failed.")
        start, end = exporter.api_date(manifest["start_date"]), exporter.api_date(manifest["end_date"])
        exporter.validate_window(start, end)
        observed = datetime.fromisoformat(manifest["exported_at_utc"].replace("Z", "+00:00"))
        if observed.tzinfo is None or observed > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise StorageError("Snapshot needs a timezone-aware, nonfuture export timestamp.")
        entries = csv_rows(blobs["entries.csv"], exporter.ENTRY_COLUMNS)
        for row in entries:
            for key in ("entry_gid", "workspace_gid", "assignee_gid"):
                exporter.gid(row[key])
            for key in ("created_by_gid", "attributed_project_gid", "task_gid", "reporting_project_gid"):
                if row[key]:
                    exporter.gid(row[key])
            exporter.api_date(row["work_date"])
            row["duration_minutes"] = integer(row["duration_minutes"])
            if row["hours"] != exporter.hours(row["duration_minutes"]):
                raise StorageError("Entry hours do not reconcile with integer minutes.")
        daily = csv_rows(blobs["daily_totals.csv"], exporter.DAILY_COLUMNS)
        for row in daily:
            row["duration_minutes"] = integer(row["duration_minutes"])
            row["entry_count"] = integer(row["entry_count"])
        exporter.validate_reconciliation(entries, daily, manifest)
        minutes = sum(row["duration_minutes"] for row in entries)
        if (type(manifest.get("entry_count")) is not int or manifest["entry_count"] != len(entries)
                or type(manifest.get("duration_minutes")) is not int
                or manifest["duration_minutes"] != minutes or minutes > 9223372036854775807
                or manifest.get("hours") != exporter.hours(minutes)):
            raise StorageError("Manifest totals do not reconcile with entry facts.")
        monthly = exporter.monthly_report(daily)
        expected_months = {r["Month"]: {"duration_minutes": r["duration_minutes"], "hours": r["hours"]} for r in monthly}
        if manifest.get("monthly_totals") != expected_months:
            raise StorageError("Manifest monthly totals do not reconcile.")
        for name, columns, expected in (("daily_report.csv", exporter.REPORT_COLUMNS, exporter.daily_report(daily)),
                                        ("monthly_report.csv", exporter.MONTHLY_REPORT_COLUMNS, monthly)):
            if csv_rows(blobs[name], columns) != [{k: str(exporter.csv_cell(row[k])) for k in columns} for row in expected]:
                raise StorageError("Human reports do not reconcile with daily totals.")
        context = json.loads(blobs["project_context.json"])
        if not isinstance(context, dict) or context.get("schema_version") != 1:
            raise StorageError("Invalid project-context document.")
        if type(manifest.get("task_details_requested")) is not bool:
            raise StorageError("Snapshot enrichment scope is missing or invalid.")
        return Snapshot(hashlib.sha256(blobs["manifest.json"]).hexdigest(), manifest, entries, context, observed)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, csv.Error):
        raise StorageError("Snapshot is unreadable or malformed; no import was made.") from None


def driver():
    try:
        import psycopg
        return psycopg
    except ImportError:
        raise StorageError("Postgres support requires the optional Postgres image or its dependencies.") from None


def connect():
    pg = driver()
    filename = os.environ.get("PGPASSWORD_FILE")
    if not filename or os.environ.get("PGPASSWORD"):
        raise StorageError("Supply only PGPASSWORD_FILE as the database password source.")
    try:
        with Path(filename).open(encoding="utf-8") as handle:
            raw = handle.read(4097)
        password = raw.strip()
        if not password or len(raw) > 4096 or any(ord(c) < 32 for c in password):
            raise StorageError("Database password file is empty or invalid.")
    except (OSError, UnicodeError, ValueError):
        raise StorageError("Cannot read the database password file.") from None
    settings = {key: os.environ.get("PG" + key.upper()) for key in ("host", "dbname", "user")}
    settings["dbname"] = os.environ.get("PGDATABASE")
    if not all(settings.values()):
        raise StorageError("Set PGHOST, PGDATABASE and PGUSER explicitly.")
    return pg.connect(**settings, port=os.environ.get("PGPORT", "5432"), password=password,
                      sslmode=os.environ.get("PGSSLMODE", "verify-full"), connect_timeout=10,
                      application_name="asana-reporting", autocommit=True)


def initialize(conn):
    sql = (Path(__file__).resolve().parent / "postgres" / "001_initial.sql").read_bytes()
    checksum = hashlib.sha256(sql).hexdigest()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(824415)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, checksum text NOT NULL)")
        rows = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        if rows:
            if rows != [(1, checksum)]:
                raise StorageError("Database schema version/checksum differs; migration review is required.")
            return
        conn.execute(sql.decode())
        conn.execute("INSERT INTO schema_migrations VALUES (1, %s)", (checksum,))


def record(core, context, visibility):
    return {"core": core, "context": context, "visibility_status": visibility}


def check_schema(conn):
    checksum = hashlib.sha256((Path(__file__).resolve().parent / "postgres" / "001_initial.sql").read_bytes()).hexdigest()
    if conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall() != [(1, checksum)]:
        raise StorageError("Database schema version/checksum differs; migration review is required.")


def import_snapshot(conn, snapshot):
    from psycopg.types.json import Jsonb
    m, when = snapshot.manifest, snapshot.exported_at
    owner, workspace = m["owner_gid"], m["workspace_gid"]
    inserted = updated = unseen = 0
    with conn.transaction():
        # Serialize modest imports, including cross-owner entry reassignments.
        conn.execute("SET LOCAL lock_timeout = '15s'")
        conn.execute("SELECT pg_advisory_xact_lock(824416)")
        check_schema(conn)
        prior = conn.execute("SELECT run_id FROM sync_runs WHERE snapshot_sha256 = %s", (snapshot.digest,)).fetchone()
        if prior:
            return {"run_id": prior[0], "already_imported": True, "inserted": 0, "updated": 0, "unseen": 0}
        watermark = conn.execute("SELECT exported_at FROM owner_watermarks WHERE workspace_gid = %s AND owner_gid = %s",
                                 (workspace, owner)).fetchone()
        if watermark and when <= watermark[0]:
            raise StorageError("Older or equally timed snapshots cannot overwrite newer owner observations. Re-fetch the window.")
        run = conn.execute("""INSERT INTO sync_runs (snapshot_sha256, workspace_gid, owner_gid,
            start_date, end_date, exported_at, entry_count, duration_minutes, manifest, project_context)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id""",
            (snapshot.digest, workspace, owner, m["start_date"], m["end_date"], when,
             len(snapshot.entries), m["duration_minutes"], Jsonb(m), Jsonb(snapshot.project_context))).fetchone()[0]
        for row in snapshot.entries:
            key = (workspace, row["entry_gid"])
            old = conn.execute("SELECT core, context, visibility_status, last_observed_at, context_observed_at FROM time_entries WHERE workspace_gid=%s AND entry_gid=%s", key).fetchone()
            if old and old[3] > when:
                raise StorageError("A newer observation already exists for an entry; import was rolled back.")
            core = {k: row[k] for k in exporter.CORE_COLUMNS}
            context_time = old[4] if old else None
            context = old[1] if old else {}
            if old and any(old[0][k] != core[k] for k in exporter.CORE_IDENTITY_COLUMNS):
                context, context_time = {}, None
            if m["task_details_requested"]:
                context = {k: row[k] for k in exporter.ENTRY_COLUMNS if k not in exporter.CORE_COLUMNS}
                context_time = when
            before = record(old[0], old[1], old[2]) if old else None
            after = record(core, context, "present")
            conn.execute("""INSERT INTO time_entries (workspace_gid, entry_gid, owner_gid, work_date,
                duration_minutes, core, context, context_observed_at, last_observed_at,
                first_seen_run, last_seen_run, last_observed_run)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_gid, entry_gid) DO UPDATE SET owner_gid=EXCLUDED.owner_gid,
                work_date=EXCLUDED.work_date, duration_minutes=EXCLUDED.duration_minutes,
                core=EXCLUDED.core, context=EXCLUDED.context, context_observed_at=EXCLUDED.context_observed_at,
                visibility_status='present', last_observed_at=EXCLUDED.last_observed_at,
                last_seen_run=EXCLUDED.last_seen_run, last_observed_run=EXCLUDED.last_observed_run""",
                (*key, owner, row["work_date"], row["duration_minutes"], Jsonb(core), Jsonb(context), context_time,
                 when, run, run, run))
            if before != after:
                conn.execute("""INSERT INTO time_entry_revisions
                    (workspace_gid, entry_gid, run_id, observed_at, previous_record, current_record)
                    VALUES (%s,%s,%s,%s,%s,%s)""", (*key, run, when, Jsonb(before), Jsonb(after)))
                if old:
                    updated += 1
                else:
                    inserted += 1
        missing = conn.execute("""SELECT entry_gid, core, context FROM time_entries
            WHERE workspace_gid=%s AND owner_gid=%s AND work_date BETWEEN %s AND %s
            AND last_seen_run <> %s AND visibility_status='present' AND last_observed_at <= %s""",
            (workspace, owner, m["start_date"], m["end_date"], run, when)).fetchall()
        for identifier, core, context in missing:
            conn.execute("""UPDATE time_entries SET visibility_status='unseen', last_observed_at=%s,
                last_observed_run=%s WHERE workspace_gid=%s AND entry_gid=%s""", (when, run, workspace, identifier))
            conn.execute("""INSERT INTO time_entry_revisions
                (workspace_gid, entry_gid, run_id, observed_at, previous_record, current_record)
                VALUES (%s,%s,%s,%s,%s,%s)""", (workspace, identifier, run, when,
                Jsonb(record(core, context, "present")), Jsonb(record(core, context, "unseen"))))
            unseen += 1
        conn.execute("""INSERT INTO owner_watermarks VALUES (%s,%s,%s)
            ON CONFLICT (workspace_gid, owner_gid) DO UPDATE SET exported_at=EXCLUDED.exported_at""", (workspace, owner, when))
    return {"run_id": run, "already_imported": False, "inserted": inserted, "updated": updated, "unseen": unseen}


def report_rows(conn, owner, workspace, start, end):
    owner, workspace = exporter.gid(owner), exporter.gid(workspace)
    exporter.validate_window(start, end)
    check_schema(conn)
    rows = conn.execute("""SELECT days.day::date, totals.entry_count, totals.duration_minutes,
        totals.unseen_entry_count, totals.unseen_minutes,
        (SELECT max(exported_at) FROM sync_runs WHERE owner_gid=%s AND workspace_gid=%s
         AND days.day::date BETWEEN start_date AND end_date)
        FROM generate_series(%s::date, %s::date, interval '1 day') AS days(day)
        LEFT JOIN daily_actual_time totals ON totals.work_date=days.day::date
            AND totals.owner_gid=%s AND totals.workspace_gid=%s ORDER BY days.day""",
        (owner, workspace, start, end, owner, workspace)).fetchall()
    result = []
    for day, count, minutes, missing, missing_minutes, covered in rows:
        result.append(dict(zip(REPORT_COLUMNS, [day.isoformat(), workspace, owner, "Actual Time",
            exporter.hours(int(minutes or 0)) if covered else "", "Hours", int(count or 0) if covered else "",
            int(minutes or 0) if covered else "", int(missing or 0) if covered else "",
            int(missing_minutes or 0) if covered else "", "observed" if covered else "not_collected",
            covered.isoformat() if covered else ""])))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Apply the versioned initial schema transactionally")
    ingest = commands.add_parser("import-snapshot", help="Validate and upsert one completed, unfiltered snapshot")
    ingest.add_argument("snapshot")
    ingest.add_argument("--expected-owner", required=True)
    ingest.add_argument("--workspace", required=True)
    report = commands.add_parser("report", help="Write provisional daily Actual Time from stored entries")
    report.add_argument("--owner", required=True)
    report.add_argument("--workspace", required=True)
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    report.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot, args.expected_owner, args.workspace) if args.command == "import-snapshot" else None
        if args.command == "report":
            start, end = exporter.api_date(args.start), exporter.api_date(args.end)
            exporter.validate_window(start, end)
            exporter.gid(args.owner)
            exporter.gid(args.workspace)
        pg = driver()
        try:
            with connect() as conn:
                if args.command == "init-db":
                    initialize(conn)
                    print("Postgres schema is ready (version 1).")
                elif snapshot:
                    print(json.dumps(import_snapshot(conn, snapshot), sort_keys=True))
                else:
                    rows = report_rows(conn, args.owner, args.workspace, start, end)
                    output = Path(args.output).expanduser().resolve()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
                            temporary = Path(handle.name)
                        exporter.write_csv(temporary, REPORT_COLUMNS, rows)
                        temporary.replace(output)
                    finally:
                        if temporary:
                            temporary.unlink(missing_ok=True)
                    print("Saved provisional daily Actual Time report; inspect coverage and unseen-entry columns.")
        except pg.Error:
            raise StorageError("Postgres operation failed; no import was committed. Check availability, schema and permissions.") from None
        return 0
    except (StorageError, exporter.ExportError) as error:
        print("Storage operation failed:", error, file=sys.stderr)
        return 1
    except (OSError, ValueError, OverflowError):
        print("Storage operation failed: local file or input error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
