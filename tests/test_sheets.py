"""Synthetic Sheets payload and replay checks; no Google credentials or network."""
import copy
from datetime import datetime, timedelta
import io
from pathlib import Path
import urllib.error
import unittest
from unittest.mock import patch, MagicMock

import asana_sheets as sheets
from asana_postgres import StorageError
from test_postgres import Snapshots
from test_exporter import entry


class SheetsTests(Snapshots, unittest.TestCase):
    def prepare(self, raw=None):
        tables = sheets.report(self.load(self.snapshot(raw=raw)))
        metadata = {"sheets": [{"properties": {"title": title, "sheetId": i,
                     "gridProperties": {"rowCount": 100, "columnCount": 26}}}
                     for i, title in enumerate(sheets.TABS)]}
        return tables, metadata

    def test_totals_ids_and_optional_labels(self):
        tables, _ = self.prepare([entry(identifier="1234567890123456789", minutes=61, project=None)])
        self.assertEqual(tables["Daily"][1][1:], [1, 61, 61 / 60])
        self.assertEqual(tables["Monthly"][1][1:], [1, 61, 61 / 60])
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
        grid = {i: [["old data"]] * 100 for i in range(4)}
        for _ in range(2):
            for request in first:
                update = request["updateCells"]
                values = [[next(iter(c["userEnteredValue"].values())) for c in r["values"]] for r in update["rows"]]
                grid[update["range"]["sheetId"]] = values + [[]] * (100 - len(values))
        sheets.verify(tables, {title: grid[i] for i, title in enumerate(sheets.TABS)})

    def test_scope_stale_conflict_and_unmanaged_destinations_rejected(self):
        tables, metadata = self.prepare()
        for field in (*sheets.SCOPE, "format", "snapshot_sha256", "exported_at_utc"):
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
        request = sheets.checked_batch(tables, metadata, tables["Status"])[2]["updateCells"]
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
        metadata["sheets"][2]["properties"]["gridProperties"] = {"rowCount": 1,"columnCount": 2}
        requests = sheets.checked_batch(tables, metadata, tables["Status"])
        growth = requests[2]["updateSheetProperties"]
        self.assertEqual(growth["properties"]["gridProperties"], {"rowCount": 2,"columnCount": 12})
        targets = [request["updateCells"]["range"]["sheetId"] if "updateCells" in request
                   else request["updateSheetProperties"]["properties"]["sheetId"]
                   for request in requests]
        self.assertEqual(set(targets), {0, 1, 2, 3})
        self.assertNotIn(99, targets)


if __name__ == "__main__":
    unittest.main()
