"""Only invented identities/data and mocked HTTP transports; never live API calls."""
import csv
from datetime import date
import getpass
import hashlib
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import asana_time_export as app
from test_exporter import entry, client, FakeOpener


DAY = date(2026, 10, 15)


def task(identifier="77", projects=None, parent=None, assignee="2"):
    return {"gid": identifier, "name": "Mock task", "projects": projects or [],
        "parent": {"gid": parent, "name": "Mock parent"} if parent else None,
        "assignee": {"gid": assignee, "name": "Mock colleague"} if assignee else None}


def project(identifier="9", name="Mock project"):
    return {"gid": identifier, "name": name}


def http_error(status):
    return HTTPError(app.API, status, "DO-NOT-LOG-PRIVATE-BODY", {}, None)


def collect(api, **kwargs):
    return app.collect_entries(api, "1", "8", DAY, DAY, **kwargs)


class V2Tests(unittest.TestCase):
    def test_null_and_omitted_task_retained_with_explicit_states(self):
        for state in ("absent", "null"):
            detail = entry(project=None)
            if state == "null":
                detail["task"] = None
            api, transport, _ = client([{"data": [entry(project=None)]}, {"data": detail}])
            rows = collect(api, include_tasks=True)
            self.assertEqual(rows[0]["task_enrichment_status"], "task_" + state)
            self.assertEqual(rows[0]["task_relationship_status"], state)
            self.assertEqual(rows[0]["duration_minutes"], 60)
            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(app.enrichment_summary(rows, True, True)["status"], "partial")

    def test_missing_projects_and_creator_distinction_do_not_remove_time(self):
        own = entry(project=None, creator="2")
        colleague = entry("101", owner="2", creator="1", task="77")
        own["task"] = {"gid": "77", "name": "Task assigned to colleague", "assignee": {"gid": "2"}}
        api, _, _ = client([{"data": [own, colleague]}])
        rows = collect(api)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assignee_gid"], "1")
        self.assertEqual(rows[0]["created_by_gid"], "2")
        self.assertEqual(rows[0]["attributed_project_gid"], "")
        self.assertEqual(sum(r["duration_minutes"] for r in rows), 60)

    def test_missing_and_null_task_name_are_optional_without_name_filter(self):
        for value in (None, ""):
            detail = entry(task="77")
            detail["task"]["name"] = value
            api, _, _ = client([{"data": [entry()]}, {"data": detail}])
            rows = collect(api, include_tasks=True)
            self.assertEqual(rows[0]["task_enrichment_status"], "name_unavailable")
            self.assertEqual(rows[0]["duration_minutes"], 60)

    def test_required_task_name_failures_and_filter_do_not_silently_exclude(self):
        missing_name = entry(task="77")
        del missing_name["task"]["name"]
        for detail in (entry(), {**entry(), "task": None}, missing_name):
            api, _, _ = client([{"data": [entry()]}, {"data": detail}])
            with self.assertRaisesRegex(app.ExportError, "Task names required"):
                collect(api, task_name_contains="call")
        for status in (403, 404):
            api, _, _ = client([{"data": [entry()]}, http_error(status)])
            with self.assertRaises(app.ExportError):
                collect(api, task_name_contains="call")

    def test_optional_detail_forbidden_or_not_found_keeps_core_time(self):
        for status in (403, 404):
            api, _, _ = client([{"data": [entry(project=None)]}, http_error(status)])
            rows = collect(api, include_tasks=True)
            self.assertEqual(rows[0]["task_enrichment_status"], "detail_http_" + str(status))
            self.assertEqual(rows[0]["duration_minutes"], 60)
            self.assertEqual(app.enrichment_summary(rows, True, True)["status"], "partial")

    def test_optional_enrichment_auth_network_and_malformed_fail_hard(self):
        for failure in (http_error(401), URLError("private network info"), {"data": []},
                        {"data": {**entry(), "task": "invalid"}}):
            api, _, _ = client([{"data": [entry()]}, failure])
            with self.assertRaises(app.ExportError):
                collect(api, include_tasks=True)

    def test_core_detail_changes_fail_including_creator(self):
        for detail in (entry(creator="3", task="77"), entry(owner="2", task="77"),
                       entry(project=None, task="77"), entry(day="2026-10-14", task="77")):
            api, _, _ = client([{"data": [entry()]}, {"data": detail}])
            with self.assertRaisesRegex(app.ExportError, "changed"):
                collect(api, include_tasks=True)

    def test_optional_display_names_are_not_core_snapshot_requirements(self):
        detail = entry(task="77")
        for relationship in ("assignee", "created_by", "attributable_to"):
            del detail[relationship]["name"]
        api, _, _ = client([{"data": [entry()]}, {"data": detail}])
        rows = collect(api, include_tasks=True)
        self.assertEqual(rows[0]["duration_minutes"], 60)
        self.assertEqual(rows[0]["attributed_project_name"], "Client project")

    def test_direct_project_context_keeps_original_attribution_blank(self):
        api, _, _ = client([{"data": [entry(project=None)]}, {"data": entry(project=None, task="77")},
                            {"data": task(projects=[project()])}])
        rows = collect(api, include_tasks=True)
        self.assertEqual(rows[0]["attributed_project_gid"], "")
        self.assertEqual(rows[0]["reporting_project_gid"], "9")
        self.assertEqual(rows[0]["reporting_project_source"], "task_direct")
        self.assertEqual(rows[0]["duration_minutes"], 60)

    def test_inherited_project_and_task_cache_never_multiply_minutes(self):
        core = [entry(project=None), entry("101", minutes=30, project=None)]
        api, transport, _ = client([{"data": core}, {"data": entry(project=None, task="77")},
            {"data": task(parent="78")}, {"data": task("78", projects=[project()])},
            {"data": entry("101", minutes=30, project=None, task="77")}])
        context = app.ProjectContext(api)
        rows = collect(api, include_tasks=True, context=context)
        self.assertEqual(sum(r["duration_minutes"] for r in rows), 90)
        self.assertTrue(all(r["reporting_project_source"] == "task_inherited" for r in rows))
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(len(context.tasks), 2)
        self.assertEqual(context.roots["77"]["chain"][0]["assignee"]["gid"], "2")

    def test_ambiguous_projects_leave_resolved_fields_blank(self):
        api, _, _ = client([{"data": [entry(project=None)]}, {"data": entry(project=None, task="77")},
            {"data": task(projects=[project(), project("10")])}])
        rows = collect(api, include_tasks=True)
        self.assertEqual(rows[0]["reporting_project_status"], "ambiguous")
        self.assertEqual(rows[0]["reporting_project_gid"], "")
        self.assertEqual(rows[0]["reporting_project_name"], "")
        self.assertEqual(sum(r["duration_minutes"] for r in rows), 60)

    def test_explicit_attribution_has_priority_without_unnecessary_task_lookup(self):
        api, transport, _ = client([{"data": [entry()]}, {"data": entry(task="77")}])
        rows = collect(api, include_tasks=True)
        self.assertEqual(rows[0]["reporting_project_gid"], "9")
        self.assertEqual(rows[0]["reporting_project_source"], "time_entry_attribution")
        self.assertEqual(rows[0]["reporting_project_status"], "resolved_explicit")
        self.assertEqual(len(transport.requests), 2)

    def test_parent_permissions_cycles_and_depth_limits_keep_time(self):
        tails = [([{ "data": task(parent="78")}, http_error(403)], "http_403"),
                 ([{"data": task(parent="78")}, http_error(404)], "http_404"),
                 ([{"data": task(parent="77")}], "cycle")]
        for tail, status in tails:
            api, _, _ = client([{"data": [entry(project=None)]}, {"data": entry(project=None, task="77")}] + tail)
            rows = collect(api, include_tasks=True)
            self.assertEqual(rows[0]["project_enrichment_status"], status)
            self.assertEqual(rows[0]["reporting_project_gid"], "")
            self.assertEqual(rows[0]["duration_minutes"], 60)
        api, transport, _ = client([{"data": [entry(project=None)]}, {"data": entry(project=None, task="77")},
            {"data": task(parent="78")}, {"data": task("78", parent="79")}])
        with patch.object(app, "MAX_PARENT_DEPTH", 1):
            rows = collect(api, include_tasks=True)
        self.assertEqual(rows[0]["project_enrichment_status"], "depth_limit")
        self.assertEqual(len(transport.requests), 4)

    def test_incomplete_parent_projects_never_guess_single_visible_candidate(self):
        for changed in ("projects", "parent"):
            data = task(projects=[project()])
            del data[changed]
            api, _, _ = client([{"data": data}])
            result = app.ProjectContext(api).resolve("77")
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reporting_project_gid"], "")

    def test_parent_network_or_bad_task_data_fails_hard(self):
        for last in (URLError("private"), http_error(401), {"data": task("999")},
                     {"data": {**task(), "projects": {}}}, {"data": {**task(), "parent": {"gid": "bad"}}}):
            api, _, _ = client([{"data": last["data"]}] if isinstance(last, dict) else [last])
            with self.assertRaises(app.ExportError):
                app.ProjectContext(api).resolve("77")

    def test_three_unattributed_entry_pattern_keeps_five_hours(self):
        # Invented IDs/names model the verified 2h + 1h + 2h diagnostic pattern.
        core = [entry("100", 120, project=None), entry("101", 60, project=None), entry("102", 120, project=None)]
        api, _, _ = client([{"data": core}, {"data": entry("100", 120, project=None, task="70")},
            {"data": task("70", projects=[project("90", "Client A")])},
            {"data": entry("101", 60, project=None, task="71")},
            {"data": task("71", projects=[project("91", "Client B")])},
            {"data": entry("102", 120, project=None, task="72")}, {"data": task("72")}])
        rows = collect(api, include_tasks=True)
        self.assertEqual(sum(r["duration_minutes"] for r in rows), 300)
        self.assertEqual([r["reporting_project_gid"] for r in rows], ["90", "91", ""])
        self.assertTrue(all(r["attributed_project_gid"] == "" for r in rows))
        daily = app.daily_totals(rows, {"gid": "1"}, "8", DAY, DAY)
        self.assertEqual(daily[0]["hours"], "5.000000")

    def test_reports_hours_month_boundaries_and_zero_month(self):
        rows = [app.normalize(entry(minutes=1, day="2026-09-30"), "8"), app.normalize(entry("101", 59, day="2026-10-01"), "8")]
        daily = app.daily_totals(rows, {"gid": "1", "name": "Owner"}, "8", date(2026, 9, 30), date(2026, 11, 1))
        months = app.monthly_report(daily)
        self.assertEqual([r["duration_minutes"] for r in months], [1, 59, 0])
        self.assertEqual(sum(r["duration_minutes"] for r in months), 60)
        human = app.daily_report(daily)
        self.assertEqual(human[0]["Metric"], "Actual Time")
        self.assertEqual(human[0]["Unit"], "Hours")
        self.assertEqual(human[0]["Actual Time (Hours)"], human[0]["hours"])

    def test_snapshot_files_checksums_and_no_false_full_enrichment(self):
        api, _, _ = client([{"data": [entry(project=None)]}, http_error(403)])
        rows = collect(api, include_tasks=True)
        daily = app.daily_totals(rows, {"gid": "1"}, "8", DAY, DAY)
        manifest = {"owner_gid": "1", "workspace_gid": "8", "start_date": DAY.isoformat(), "end_date": DAY.isoformat(),
                    "task_details_requested": True, "project_context_requested": True}
        with tempfile.TemporaryDirectory() as folder:
            snapshot = app.save_snapshot(folder, rows, daily, manifest)
            saved = json.loads((snapshot / "manifest.json").read_text())
            self.assertEqual(saved["core_snapshot_status"], "complete")
            self.assertEqual(saved["enrichment_summary"]["status"], "partial")
            self.assertEqual(saved["duration_minutes"], 60)
            self.assertEqual(len(list(snapshot.iterdir())), 6)
            for filename, expected in saved["files_sha256"].items():
                self.assertEqual(hashlib.sha256((snapshot / filename).read_bytes()).hexdigest(), expected)
            with (snapshot / "daily_report.csv").open(encoding="utf-8-sig") as handle:
                self.assertEqual(next(csv.DictReader(handle))["Actual Time (Hours)"], "1.000000")

    def test_snapshot_rejects_foreign_owner_wrong_totals_or_secret(self):
        rows = [app.normalize(entry(), "8")]
        daily = app.daily_totals(rows, {"gid": "1"}, "8", DAY, DAY)
        manifest = {"owner_gid": "1", "workspace_gid": "8", "start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
        with tempfile.TemporaryDirectory() as folder:
            for changed in ({**manifest, "owner_gid": "2"}, {**manifest, "workspace_gid": "9"}):
                with self.assertRaises(app.ExportError):
                    app.save_snapshot(folder, rows, daily, changed)
            with self.assertRaises(app.ExportError):
                app.save_snapshot(folder, rows, [{**daily[0], "duration_minutes": 61}], manifest)
            with self.assertRaises(app.ExportError):
                app.save_snapshot(folder, rows, daily, manifest, secret="Client project")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_strict_dates_leap_year_padding_and_maximum_window(self):
        p = app.parser()
        self.assertEqual(app.date_range(p.parse_args(["--date", "2024-02-29"])), (date(2024, 2, 29),) * 2)
        for value in ("2026-02-29", "2026-1-01", "2026-10-15T00:00:00", "0001-01-01", "9999-12-31"):
            with self.assertRaises(app.ExportError):
                app.date_range(p.parse_args(["--date", value]))
        for start, end in ((date(2026, 10, 16), DAY), (date(2025, 1, 1), DAY), (date.min, DAY)):
            api, transport, _ = client([])
            with self.assertRaises(app.ExportError):
                app.collect_entries(api, "1", "8", start, end)
            self.assertEqual(transport.requests, [])

    def test_https_no_redirect_no_proxy_environment_no_keylogging(self):
        fake = FakeOpener([])
        with patch.object(app, "build_opener", return_value=fake) as build:
            app.AsanaClient("offline-only-placeholder")
        handlers = build.call_args.args
        proxy = next(h for h in handlers if isinstance(h, app.ProxyHandler))
        https = next(h for h in handlers if isinstance(h, app.HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        self.assertIsNone(https._context.keylog_filename)
        self.assertTrue(any(isinstance(h, app.NoRedirect) for h in handlers))

    def test_fixed_paths_and_token_header_injection_and_limits(self):
        api, transport, _ = client([])
        for path in ("https://evil.invalid", "/tasks/77/notes", "/tasks/77?x=1", "/users/2", "/../users/me"):
            with self.assertRaises(app.ExportError):
                api.get(path)
        self.assertEqual(transport.requests, [])
        for token in ("bad\r\nHeader: value", "", "é"):
            with self.assertRaises(app.ExportError):
                app.AsanaClient(token, opener=FakeOpener([]))
        for timeout in (float("nan"), float("inf"), 0, 121):
            with self.assertRaises(app.ExportError):
                app.AsanaClient("offline", timeout=timeout, opener=FakeOpener([]))

    def test_hidden_prompt_is_not_allowed_to_echo(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.stdin.isatty", return_value=True), \
                patch.object(app.getpass, "getpass", side_effect=getpass.GetPassWarning("echo")):
            with self.assertRaises(app.ExportError):
                app.read_token(False)


if __name__ == "__main__":
    unittest.main()
