import csv
from datetime import date
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import asana_time_export as app


def entry(identifier="100", minutes=60, day="2026-10-15", owner="1", creator="2", project="9", task=None):
    result = {"gid": identifier, "duration_minutes": minutes, "entered_on": day,
        "assignee": {"gid": owner, "name": "Owner"},
        "created_by": {"gid": creator, "name": "Reviewer"},
        "attributable_to": {"gid": project, "name": "Client project"} if project else None}
    if task:
        result["task"] = {"gid": task, "name": "Daily subtask"}
    return result


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


class FakeOpener:
    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)


def client(values, retries=0):
    opener = FakeOpener(values)
    sleeps = []
    result = app.AsanaClient("test-only-placeholder", retries=retries, opener=opener, sleep=sleeps.append)
    return result, opener, sleeps


class ExportTests(unittest.TestCase):
    def test_pagination_offset_and_fields(self):
        api, transport, _ = client([
            {"data": [entry()], "next_page": {"offset": "opaque", "uri": "https://evil.invalid/"}},
            {"data": [entry("101")], "next_page": None}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))
        self.assertEqual(len(rows), 2)
        query = parse_qs(urlsplit(transport.requests[1][0].full_url).query)
        self.assertEqual(query["offset"], ["opaque"])
        self.assertEqual(query["assignee"], ["1"])
        self.assertEqual(query["limit"], ["100"])
        self.assertEqual(query["entered_on_start_date"], ["2026-10-14"])
        self.assertEqual(query["entered_on_end_date"], ["2026-10-16"])
        self.assertNotIn("task", query["opt_fields"][0].split(","))
        self.assertTrue(all(x[0].full_url.startswith(app.API) for x in transport.requests))

    def test_exact_dates_and_owner_not_creator(self):
        api, _, _ = client([{"data": [entry(), entry("101", day="2026-10-14"),
            entry("102", day="2026-10-16"), entry("103", owner="2", creator="1")]}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))
        self.assertEqual([x["entry_gid"] for x in rows], ["100"])
        self.assertEqual(rows[0]["created_by_gid"], "2")

    def test_duplicate_entry_once_across_pages(self):
        api, _, _ = client([{"data": [entry()], "next_page": {"offset": "next"}}, {"data": [entry()]}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))
        self.assertEqual(len(rows), 1)

    def test_conflicting_duplicate_aborts(self):
        api, _, _ = client([{"data": [entry(), entry(minutes=90)]}])
        with self.assertRaisesRegex(app.ExportError, "changed"):
            app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))

    def test_missing_assignee_never_falls_back_to_creator(self):
        item = entry(creator="1")
        del item["assignee"]
        with self.assertRaises(app.ExportError):
            app.normalize(item, "8")

    def test_duplicate_moved_outside_filter_aborts(self):
        for changed in (entry(owner="2"), entry(day="2026-10-14"), entry(project="10")):
            api, _, _ = client([{"data": [entry(), changed]}])
            with self.assertRaisesRegex(app.ExportError, "changed"):
                app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15), project="9")

    def test_multi_project_membership_never_multiplies_duration(self):
        item = entry(minutes=90)
        item["task"] = {"gid": "77", "name": "Multi-project task", "projects": [{"gid": "9"}, {"gid": "10"}]}
        api, _, _ = client([{"data": [item]}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration_minutes"], 90)
        self.assertEqual(rows[0]["attributed_project_gid"], "9")

    def test_parent_and_subtask_direct_entries_count_once_each(self):
        parent = entry("100", minutes=45, task="70")
        parent["task"]["actual_time_minutes"] = 900
        child = entry("101", minutes=15, project=None, task="71")
        child["task"]["parent"] = {"gid": "70"}
        child["task"]["due_on"] = "2026-10-14"
        api, _, _ = client([{"data": [parent, child]}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15))
        self.assertEqual(sum(x["duration_minutes"] for x in rows), 60)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["attributed_project_gid"], "")

    def test_project_filter_uses_attribution(self):
        api, transport, _ = client([{"data": [entry(), entry("101", project="10"), entry("102", project=None)]}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15), project="9")
        self.assertEqual(len(rows), 1)
        self.assertEqual(parse_qs(urlsplit(transport.requests[0][0].full_url).query)["attributable_to"], ["9"])

    def test_task_details_and_name_filter(self):
        api, transport, _ = client([{"data": [entry()]}, {"data": entry(task="77")}])
        rows = app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15), task_name_contains="SUBtask")
        self.assertEqual(rows[0]["task_gid"], "77")
        self.assertEqual(len(transport.requests), 2)

    def test_details_changed_abort(self):
        api, _, _ = client([{"data": [entry()]}, {"data": entry(minutes=90, task="77")}])
        with self.assertRaisesRegex(app.ExportError, "changed"):
            app.collect_entries(api, "1", "8", date(2026, 10, 15), date(2026, 10, 15), include_tasks=True)

    def test_daily_totals_zero_days_and_exact_minutes(self):
        rows = [app.normalize(entry(minutes=1), "8"), app.normalize(entry("101", minutes=59), "8")]
        daily = app.daily_totals(rows, {"gid": "1", "name": "Owner"}, "8", date(2026, 10, 14), date(2026, 10, 16))
        self.assertEqual([x["duration_minutes"] for x in daily], [0, 60, 0])
        self.assertEqual(daily[1]["entry_count"], 2)
        self.assertEqual(daily[1]["hours"], "1.000000")
        self.assertEqual(app.hours(1), "0.016667")

    def test_thirty_days_and_day_range(self):
        p = app.parser()
        self.assertEqual(app.date_range(p.parse_args([]), date(2026, 10, 15)), (date(2026, 9, 1), date(2026, 10, 15)))
        self.assertEqual(app.date_range(p.parse_args(["--days", "30"]), date(2026, 10, 15)), (date(2026, 9, 16), date(2026, 10, 15)))
        self.assertEqual(app.date_range(p.parse_args(["--date", "yesterday"]), date(2026, 10, 15)), (date(2026, 10, 14), date(2026, 10, 14)))
        self.assertEqual(app.date_range(p.parse_args(["--start", "2026-10-01", "--end", "2026-10-15"])), (date(2026, 10, 1), date(2026, 10, 15)))
        for argv in (["--date", "today", "--days", "30"], ["--days", "0"], ["--date", "2026-02-30"], ["--start", "2026-10-16", "--end", "2026-10-15"]):
            with self.assertRaises(app.ExportError):
                app.date_range(p.parse_args(argv))

    def test_429_retry_after_and_transient_retry(self):
        api, transport, sleeps = client([HTTPError(app.API, 429, "secret-body", {"Retry-After": "7"}, None),
            HTTPError(app.API, 503, "secret-body", {}, None), {"data": []}], retries=2)
        self.assertEqual(api.get("/time_tracking_entries"), {"data": []})
        self.assertEqual(sleeps, [7.0, 2.0])
        self.assertEqual(transport.requests[0][1], 30)

    def test_network_retry(self):
        api, _, sleeps = client([URLError("private-proxy-info"), {"data": []}], retries=1)
        api.get("/time_tracking_entries")
        self.assertEqual(sleeps, [1.0])

    def test_truncated_http_retry_and_safe_exhaustion(self):
        api, _, sleeps = client([IncompleteRead(b"private response"), {"data": []}], retries=1)
        api.get("/time_tracking_entries")
        self.assertEqual(sleeps, [1.0])
        api, _, _ = client([IncompleteRead(b"private response")])
        with self.assertRaises(app.ExportError) as error:
            api.get("/time_tracking_entries")
        self.assertNotIn("private response", str(error.exception))

    def test_nonretry_errors_sanitized(self):
        for status in (401, 402, 403):
            api, _, _ = client([HTTPError("https://secret.invalid", status, "TOKEN-MUST-NOT-LEAK", {}, None)])
            with self.assertRaises(app.ExportError) as error:
                api.get("/users/me")
            self.assertNotIn("TOKEN-MUST-NOT-LEAK", str(error.exception))
            self.assertNotIn("secret.invalid", str(error.exception))

    def test_repeated_offset_fails(self):
        api, _, _ = client([{"data": [], "next_page": {"offset": "x"}}, {"data": [], "next_page": {"offset": "x"}}])
        with self.assertRaisesRegex(app.ExportError, "pagination"):
            list(api.pages("/time_tracking_entries", {}))

    def test_calendar_month_reporting_ranges(self):
        p = app.parser()
        for end, expected in (("2026-10-19", date(2026, 9, 1)),
                              ("2027-01-03", date(2026, 12, 1)),
                              ("2024-03-31", date(2024, 2, 1))):
            self.assertEqual(app.date_range(p.parse_args(["--calendar-months", "2", "--end", end])),
                             (expected, date.fromisoformat(end)))
        self.assertEqual(app.date_range(p.parse_args(["--calendar-months", "1", "--end", "2026-10-19"])),
                         (date(2026, 10, 1), date(2026, 10, 19)))
        self.assertEqual(app.date_range(p.parse_args(["--calendar-months", "1", "--end", "2026-11-03"])),
                         (date(2026, 10, 28), date(2026, 11, 3)))

    def test_calendar_months_cannot_be_mixed_with_other_range_modes(self):
        p = app.parser()
        for other in (["--date", "2026-10-19"], ["--start", "2026-10-01"], ["--days", "60"]):
            with self.assertRaises(app.ExportError):
                app.date_range(p.parse_args(["--calendar-months", "2", *other]))

    def test_failed_page_does_not_publish_or_touch_history(self):
        api, _, _ = client([{"data": {"gid": "1", "name": "Owner", "workspaces": [{"gid": "8", "name": "Work"}]}},
            {"data": [entry()], "next_page": {"offset": "next"}},
            HTTPError(app.API, 403, "private", {}, None)])
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / "old.csv"
            sentinel.write_text("historical")
            with patch.object(app, "AsanaClient", return_value=api), patch.object(app, "read_token", return_value="test-only"), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(app.main(["--date", "2026-10-15", "--output", folder]), 1)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["old.csv"])
            self.assertEqual(sentinel.read_text(), "historical")

    def test_snapshot_versioning_and_files(self):
        rows = [app.normalize(entry(), "8")]
        daily = app.daily_totals(rows, {"gid": "1", "name": "Owner"}, "8", date(2026, 10, 15), date(2026, 10, 15))
        manifest = {"owner_gid": "1", "workspace_gid": "8", "start_date": "2026-10-15", "end_date": "2026-10-15"}
        with tempfile.TemporaryDirectory() as folder:
            first = app.save_snapshot(folder, rows, daily, manifest)
            second = app.save_snapshot(folder, rows, daily, manifest)
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(Path(folder).iterdir())), 2)
            saved = json.loads((first / "manifest.json").read_text())
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["duration_minutes"], 60)
            with (first / "entries.csv").open(encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_failed_write_publishes_no_snapshot(self):
        manifest = {"owner_gid": "1", "workspace_gid": "8", "start_date": "2026-10-15", "end_date": "2026-10-15"}
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "write_csv", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                daily = app.daily_totals([], {"gid": "1"}, "8", date(2026, 10, 15), date(2026, 10, 15))
                app.save_snapshot(folder, [], daily, manifest)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_no_token_noninteractive(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(app.ExportError):
                app.read_token(True)

    def test_formula_injection_escaped(self):
        self.assertEqual(app.csv_cell("=HYPERLINK(\"bad\")"), "'=HYPERLINK(\"bad\")")
        self.assertEqual(app.csv_cell(" \t+1"), "' \t+1")
        self.assertEqual(app.csv_cell(60), 60)

    def test_missing_and_negative_duration_fail(self):
        for value in (None, -1, True, 1.5, "60"):
            with self.assertRaises(app.ExportError):
                app.normalize(entry(minutes=value), "8")

    def test_no_authenticated_redirect(self):
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))


if __name__ == "__main__":
    unittest.main()
