"""Synthetic Sheets payload and replay checks; no Google credentials or network."""
import copy
from datetime import date, datetime, timedelta
import io
from pathlib import Path
import urllib.error
import unittest
from unittest.mock import patch, MagicMock

import asana_sheets as sheets
from asana_postgres import StorageError
from test_postgres import DAY, Snapshots
from test_exporter import entry


class SheetsTests(Snapshots, unittest.TestCase):
    def prepare(self, raw=None, end=DAY):
        start = min(end.replace(day=1), end - timedelta(days=6))
        tables = sheets.report(self.load(self.snapshot(raw=raw, start=start, end=end)))
        metadata = {"sheets": [{"properties": {"title": title, "sheetId": i,
                     "gridProperties": {"rowCount": 100, "columnCount": 26}}}
                     for i, title in enumerate(sheets.TABS)]}
        return tables, metadata

    def test_totals_ids_and_optional_labels(self):
        tables, _ = self.prepare([entry(identifier="1234567890123456789", minutes=61, project=None)])
        self.assertEqual(tables["Daily"][1][1:], [1, 61, 61 / 60])
        self.assertEqual(tables["Monthly"][-1][1:], [1, 61, 61 / 60])
        self.assertEqual(tables["Entries"][1][6], "1234567890123456789")

    def test_replay_replaces_and_clears_tail_without_append(self):
        tables, metadata = self.prepare()
        first = sheets.checked_batch(tables, metadata, tables["Status"])
        self.assertEqual(first, sheets.checked_batch(tables, metadata, tables["Status"]))
        for request in first:
            self.assertEqual(set(request), {"updateCells"})
            self.assertEqual(request["updateCells"]["range"]["endRowIndex"], 100)
            self.assertEqual(request["updateCells"]["fields"], "userEnteredValue")
        # Simulate the documented updateCells replacement including shorter/empty refreshes.
        grid = {i: [["old data"]] * 100 for i in range(len(sheets.TABS))}
        for _ in range(2):
            for request in first:
                update = request["updateCells"]
                values = [[next(iter(c["userEnteredValue"].values())) for c in r["values"]] for r in update["rows"]]
                grid[update["range"]["sheetId"]] = values + [[]] * (100 - len(values))
        sheets.verify(tables, {title: grid[i] for i, title in enumerate(sheets.TABS)})

    def test_scope_stale_conflict_and_unmanaged_destinations_rejected(self):
        tables, metadata = self.prepare()
        for field in (*sheets.SCOPE, "format", "as_of_date", "snapshot_sha256", "exported_at_utc"):
            prior = copy.deepcopy(tables["Status"])
            for row in prior[1:]:
                if row[0] == field:
                    row[1] = "invalid"
            with self.assertRaises(StorageError):
                sheets.checked_batch(tables, metadata, prior)
        prior = copy.deepcopy(tables["Status"])
        for row in prior:
            if row[0] == "exported_at_utc":
                row[1] = (datetime.fromisoformat(row[1]) + timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(StorageError, "Older"):
            sheets.checked_batch(tables, metadata, prior)

    def test_external_names_are_literal_and_ids_are_strings(self):
        tables, metadata = self.prepare()
        tables["Entries"][1][5] = '=IMPORTDATA("https://example.invalid")'
        request = sheets.checked_batch(tables, metadata, tables["Status"])[3]["updateCells"]
        self.assertIn("stringValue", request["rows"][1]["values"][5]["userEnteredValue"])
        self.assertIn("stringValue", request["rows"][1]["values"][6]["userEnteredValue"])
        self.assertIn("numberValue", request["rows"][1]["values"][2]["userEnteredValue"])

    def test_zero_window_and_readback_mismatch(self):
        tables, _ = self.prepare([])
        self.assertEqual(tables["Daily"][1][1:], [0, 0, 0])
        self.assertEqual(tables["Monthly"][1][1:], [0, 0, 0])
        observed = copy.deepcopy(tables)
        observed["Monthly"][1][2] = 99
        with self.assertRaisesRegex(StorageError, "readback"):
            sheets.verify(tables, observed)

    def test_http_target_auth_and_error_redaction(self):
        token = Path(self.directory.name) / "google.token"
        token.write_text("synthetic-google-secret")
        opener = MagicMock()
        with patch("asana_sheets.urllib.request.build_opener", return_value=opener):
            client = sheets.SheetsClient("synthetic_sheet_id", token)
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"values": [["ok"]]}')
        self.assertEqual(client.read("Status", "A1:B2"), [["ok"]])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "sheets.googleapis.com")
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-google-secret")
        self.assertNotIn("synthetic-google-secret", request.full_url)
        opener.open.side_effect = urllib.error.HTTPError(request.full_url, 401,
            "synthetic-google-secret", {}, io.BytesIO(b"private response"))
        with self.assertRaises(StorageError) as raised:
            client.request(":batchUpdate", {"requests": []})
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertIsNone(sheets.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_grid_growth_and_unrelated_tab_preservation(self):
        tables, metadata = self.prepare()
        tables["Entries"][1][5] = "Task 99 is valid cell content"
        metadata["sheets"].append({"properties": {"title": "Manual notes", "sheetId": 99}})
        metadata["sheets"][3]["properties"]["gridProperties"] = {"rowCount": 1,"columnCount": 2}
        requests = sheets.checked_batch(tables, metadata, tables["Status"])
        growth = requests[3]["updateSheetProperties"]
        self.assertEqual(growth["properties"]["gridProperties"], {"rowCount": 2,"columnCount": 12})
        targets = [request["updateCells"]["range"]["sheetId"] if "updateCells" in request
                   else request["updateSheetProperties"]["properties"]["sheetId"]
                   for request in requests]
        self.assertEqual(set(targets), set(range(len(sheets.TABS))))
        self.assertNotIn(99, targets)

    def test_month_week_and_daily_have_distinct_windows(self):
        tables, _ = self.prepare([
            entry("100", 60, "2026-10-01"), entry("101", 120, "2026-10-08"),
            entry("102", 30, "2026-10-09"), entry("103", 90, "2026-10-15")])
        status = dict(tables["Status"][1:])
        self.assertEqual(len(tables["Monthly"]), 16)
        self.assertEqual(len(tables["Week"]), 8)
        self.assertEqual([status[k] for k in ("month_minutes", "week_minutes", "daily_minutes")],
                         [300, 120, 90])
        self.assertEqual([row[6] for row in tables["Entries"][1:]], ["102", "103"])
        self.assertEqual(tables["Daily"][1][1:], [1, 90, 1.5])
        self.assertEqual(tables["Daily"][3], tables["Entries"][0])
        self.assertEqual([row[6] for row in tables["Daily"][4:]], ["103"])
        self.assertEqual(tables["Monthly"][2][1:], [0, 0, 0])

    def test_week_crosses_month_boundary_without_inflating_month_to_date(self):
        tables, _ = self.prepare([
            entry("100", 60, "2026-10-28"), entry("101", 120, "2026-10-31"),
            entry("102", 30, "2026-11-01"), entry("103", 90, "2026-11-03")],
            end=date(2026, 11, 3))
        status = dict(tables["Status"][1:])
        self.assertEqual((status["week_start"], status["month_start"]), ("2026-10-28", "2026-11-01"))
        self.assertEqual((status["month_minutes"], status["week_minutes"]), (120, 300))
        self.assertEqual(len(tables["Entries"]), 5)
        self.assertEqual(len(tables["Monthly"]), 4)

    def test_incomplete_month_or_week_coverage_is_rejected(self):
        with self.assertRaisesRegex(StorageError, "cover"):
            sheets.report(self.load(self.snapshot()))
        with self.assertRaisesRegex(StorageError, "cover"):
            sheets.report(self.load(self.snapshot(raw=[], start=date(2026, 11, 1), end=date(2026, 11, 3))))

    def test_daily_with_no_time_keeps_zero_summary_and_no_old_entries(self):
        tables, _ = self.prepare([entry(day="2026-10-14")])
        self.assertEqual(tables["Daily"][1][1:], [0, 0, 0])
        self.assertEqual(len(tables["Daily"]), 4)
        self.assertEqual(len(tables["Entries"]), 2)
        self.assertEqual(sheets.table_width(tables["Daily"]), 12)

    def test_rolling_forward_is_allowed_but_report_date_cannot_move_back(self):
        tables, metadata = self.prepare([])
        newer, _ = self.prepare([], end=DAY + timedelta(days=1))
        sheets.checked_batch(newer, metadata, tables["Status"])
        stale = copy.deepcopy(newer)
        for row in stale["Status"]:
            if row[0] == "as_of_date":
                row[1] = "2026-10-14"
        with self.assertRaisesRegex(StorageError, "Older report dates"):
            sheets.checked_batch(stale, metadata, tables["Status"])

    def test_legacy_layout_requires_explicit_migration_and_preserves_identity_guard(self):
        tables, metadata = self.prepare()
        prior = copy.deepcopy(tables["Status"])
        for row in prior:
            if row[0] == "format":
                row[1] = "asana-sheets-v1"
        with self.assertRaises(StorageError):
            sheets.checked_batch(tables, metadata, prior)
        sheets.checked_batch(tables, metadata, prior, migrate_layout=True)
        for row in prior:
            if row[0] == "owner_gid":
                row[1] = "999"
        with self.assertRaises(StorageError):
            sheets.checked_batch(tables, metadata, prior, migrate_layout=True)


if __name__ == "__main__":
    unittest.main()
