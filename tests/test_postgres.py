"""Snapshot validation plus opt-in integration tests against a real local Postgres."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import asana_postgres as store
import asana_time_export as exporter
from test_exporter import entry

DAY = date(2026, 10, 15)


class Snapshots:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.counter = 0
        self.base_time = datetime.now(timezone.utc) - timedelta(hours=2)

    def snapshot(self, raw=None, owner="1", start=DAY, end=DAY, enrichment=False):
        self.counter += 1
        rows = [exporter.normalize(r, "8") for r in (raw if raw is not None else [entry()])]
        daily = exporter.daily_totals(rows, {"gid": owner, "name": "Synthetic consultant"}, "8", start, end)
        manifest = {"owner_gid": owner, "workspace_gid": "8", "start_date": start.isoformat(),
                    "end_date": end.isoformat(), "project_filter_gid": None, "task_name_contains": None,
                    "task_details_requested": enrichment, "project_context_requested": enrichment}
        folder = exporter.save_snapshot(Path(self.directory.name), rows, daily, manifest)
        m = json.loads((folder / "manifest.json").read_text())
        m["exported_at_utc"] = (self.base_time + timedelta(seconds=self.counter)).isoformat()
        (folder / "manifest.json").write_text(json.dumps(m))
        return folder

    def load(self, folder, owner="1"):
        return store.load_snapshot(folder, owner, "8")

    def manifest_change(self, folder, **changes):
        path = folder / "manifest.json"
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))


class SnapshotValidationTests(Snapshots, unittest.TestCase):
    def test_valid_complete_snapshot_and_zero_days(self):
        snapshot = self.load(self.snapshot())
        self.assertEqual(snapshot.entries[0]["duration_minutes"], 60)
        self.assertEqual(self.load(self.snapshot(raw=[])).entries, [])

    def test_integrity_and_exact_checksum_allowlist(self):
        for filename in ("entries.csv", "daily_totals.csv", "daily_report.csv", "monthly_report.csv", "project_context.json"):
            folder = self.snapshot()
            with (folder / filename).open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(store.StorageError, "checksum"):
                self.load(folder)
        folder = self.snapshot()
        m = json.loads((folder / "manifest.json").read_text())
        m["files_sha256"]["../../outside"] = "not-a-hash"
        self.manifest_change(folder, files_sha256=m["files_sha256"])
        with self.assertRaisesRegex(store.StorageError, "exactly"):
            self.load(folder)

    def test_failed_filtered_and_wrong_owner_snapshots_rejected(self):
        for change in ({"status": "failed"}, {"core_snapshot_status": "partial"},
                       {"project_filter_gid": "9"}, {"task_name_contains": "label"},
                       {"owner_gid": "2"}, {"workspace_gid": "7"}):
            folder = self.snapshot()
            self.manifest_change(folder, **change)
            with self.assertRaises(store.StorageError):
                self.load(folder)

    def test_manifest_amounts_and_timezone_must_reconcile(self):
        for change in ({"duration_minutes": 1}, {"entry_count": True}, {"hours": "999"},
                       {"monthly_totals": {}}, {"exported_at_utc": "2026-10-18T00:00:00"}):
            folder = self.snapshot()
            self.manifest_change(folder, **change)
            with self.assertRaises(store.StorageError):
                self.load(folder)

    def test_rehashed_malformed_csv_still_fails(self):
        folder = self.snapshot()
        path = folder / "entries.csv"
        path.write_bytes(path.read_bytes().replace(b"60,1.000000", b"-1,1.000000"))
        m = json.loads((folder / "manifest.json").read_text())
        m["files_sha256"]["entries.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.manifest_change(folder, files_sha256=m["files_sha256"])
        with self.assertRaises(store.StorageError):
            self.load(folder)

    def test_symlink_and_incomplete_directory_rejected(self):
        folder = self.snapshot()
        path = folder / "entries.csv"
        target = folder / "renamed.csv"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(store.StorageError, "symbolic"):
            self.load(folder)
        with self.assertRaisesRegex(store.StorageError, "Incomplete"):
            self.load(folder.parent / ".asana-incomplete-synthetic")


@unittest.skipUnless(os.environ.get("ASANA_TEST_POSTGRES") == "1", "Opt-in local Postgres required")
class PostgresIntegrationTests(Snapshots, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from psycopg import sql
        self.conn = store.connect()
        self.schema = "test_" + uuid.uuid4().hex
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_database)
        store.initialize(self.conn)

    def cleanup_database(self):
        from psycopg import sql
        try:
            self.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            self.conn.close()

    def count(self, table):
        from psycopg import sql
        return self.conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]

    def test_replay_and_overlapping_snapshots_do_not_duplicate_hours(self):
        path = self.snapshot()
        first = store.import_snapshot(self.conn, self.load(path))
        replay = store.import_snapshot(self.conn, self.load(path))
        overlap = store.import_snapshot(self.conn, self.load(self.snapshot()))
        self.assertEqual(first["inserted"], 1)
        self.assertTrue(replay["already_imported"])
        self.assertEqual(overlap["updated"], 0)
        self.assertEqual(self.count("sync_runs"), 2)
        self.assertEqual(self.count("time_entries"), 1)
        self.assertEqual(self.count("time_entry_revisions"), 1)
        self.assertEqual(store.report_rows(self.conn, "1", "8", DAY, DAY)[0]["duration_minutes"], 60)

    def test_edit_and_date_move_preserve_revisions(self):
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        updated = store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry(minutes=90)])))
        self.assertEqual(updated["updated"], 1)
        next_day = DAY + timedelta(days=1)
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry(minutes=90, day=next_day.isoformat())], start=DAY, end=next_day)))
        reports = store.report_rows(self.conn, "1", "8", DAY, next_day)
        self.assertEqual([r["duration_minutes"] for r in reports], [0, 90])
        self.assertEqual(self.count("time_entry_revisions"), 3)

    def test_absence_is_flagged_not_deleted_and_reappearance_is_recorded(self):
        original = self.snapshot()
        store.import_snapshot(self.conn, self.load(original))
        missing = store.import_snapshot(self.conn, self.load(self.snapshot(raw=[])))
        self.assertEqual(missing["unseen"], 1)
        row = store.report_rows(self.conn, "1", "8", DAY, DAY)[0]
        self.assertEqual((row["duration_minutes"], row["unseen_minutes"], row["unseen_entry_count"]), (60, 60, 1))
        # Replaying a previously imported snapshot cannot resurrect an unseen row.
        self.assertTrue(store.import_snapshot(self.conn, self.load(original))["already_imported"])
        self.assertEqual(self.conn.execute("SELECT visibility_status FROM time_entries").fetchone()[0], "unseen")
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        self.assertEqual(self.conn.execute("SELECT visibility_status FROM time_entries").fetchone()[0], "present")
        self.assertEqual(self.count("time_entry_revisions"), 3)

    def test_unqueried_dates_and_other_consultants_are_untouched(self):
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry("200", owner="2")], owner="2"), owner="2"))
        tomorrow = DAY + timedelta(days=1)
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[], start=tomorrow, end=tomorrow)))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM time_entries WHERE visibility_status='present'").fetchone()[0], 2)
        self.assertEqual(store.report_rows(self.conn, "1", "8", tomorrow, tomorrow)[0]["duration_minutes"], 0)
        unknown = DAY - timedelta(days=1)
        self.assertEqual(store.report_rows(self.conn, "1", "8", unknown, unknown)[0]["duration_minutes"], "")

    def test_reassignment_changes_owner_without_copying_entry(self):
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry(owner="2")], owner="2"), owner="2"))
        self.assertEqual(self.count("time_entries"), 1)
        self.assertEqual(self.conn.execute("SELECT owner_gid FROM time_entries").fetchone()[0], "2")
        self.assertEqual(store.report_rows(self.conn, "1", "8", DAY, DAY)[0]["duration_minutes"], 0)

    def test_stale_snapshots_and_cross_owner_old_data_roll_back(self):
        stale = self.snapshot()
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry(owner="2")], owner="2"), owner="2"))
        with self.assertRaisesRegex(store.StorageError, "newer observation"):
            store.import_snapshot(self.conn, self.load(stale))
        self.assertEqual(self.count("sync_runs"), 1)
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        with self.assertRaisesRegex(store.StorageError, "Older"):
            store.import_snapshot(self.conn, self.load(stale))

    def test_database_error_rolls_back_entire_import(self):
        import psycopg
        self.conn.execute("ALTER TABLE time_entries ADD CHECK (entry_gid <> '101')")
        snapshot = self.load(self.snapshot(raw=[entry(), entry("101")]))
        with self.assertRaises(psycopg.errors.CheckViolation):
            store.import_snapshot(self.conn, snapshot)
        self.assertEqual(self.count("sync_runs"), 0)
        self.assertEqual(self.count("time_entries"), 0)
        self.assertEqual(self.count("time_entry_revisions"), 0)
        self.assertEqual(self.count("owner_watermarks"), 0)

    def test_core_only_refresh_preserves_context_but_core_change_invalidates_it(self):
        store.import_snapshot(self.conn, self.load(self.snapshot(enrichment=True)))
        self.assertTrue(self.conn.execute("SELECT context FROM time_entries").fetchone()[0])
        store.import_snapshot(self.conn, self.load(self.snapshot()))
        self.assertTrue(self.conn.execute("SELECT context FROM time_entries").fetchone()[0])
        store.import_snapshot(self.conn, self.load(self.snapshot(raw=[entry(project=None)])))
        self.assertEqual(self.conn.execute("SELECT context FROM time_entries").fetchone()[0], {})

    def test_migration_is_idempotent_and_detects_schema_drift(self):
        store.initialize(self.conn)
        self.assertEqual(self.count("schema_migrations"), 1)
        self.conn.execute("UPDATE schema_migrations SET checksum='drift'")
        with self.assertRaisesRegex(store.StorageError, "checksum"):
            store.initialize(self.conn)


if __name__ == "__main__":
    unittest.main()
