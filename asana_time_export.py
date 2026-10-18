#!/usr/bin/env python3
"""Read-only, dependency-free Asana time-entry export for the token owner.

Python 3.10+. See README.md. Never pass a token on the command line.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import getpass
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import sys
import tempfile
import time
import uuid
import warnings
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

VERSION = "2.1.0"
API = "https://app.asana.com/api/1.0"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PARENT_DEPTH = 10
MAX_PAGES = 10000
TASK_FIELDS = "gid,name,projects.gid,projects.name,parent.gid,parent.name,assignee.gid,assignee.name"
ENTRY_FIELDS = ",".join(("duration_minutes", "entered_on", "assignee",
    "assignee.name", "created_by", "created_by.name", "attributable_to",
    "attributable_to.name"))
DETAIL_FIELDS = ENTRY_FIELDS + ",task,task.name"
CORE_COLUMNS = ["entry_gid", "work_date", "workspace_gid", "assignee_gid",
    "assignee_name", "created_by_gid", "created_by_name", "attributed_project_gid",
    "attributed_project_name", "duration_minutes", "hours"]
CORE_IDENTITY_COLUMNS = ["entry_gid", "work_date", "workspace_gid", "assignee_gid",
    "created_by_gid", "attributed_project_gid", "duration_minutes"]
ENTRY_COLUMNS = CORE_COLUMNS + ["task_gid", "task_name", "task_relationship_status",
    "task_enrichment_status", "project_enrichment_status", "reporting_project_gid",
    "reporting_project_name", "reporting_project_source", "reporting_project_status"]
DAILY_COLUMNS = ["work_date", "workspace_gid", "assignee_gid", "assignee_name",
    "entry_count", "duration_minutes", "hours"]
REPORT_COLUMNS = ["Date", "Consultant", "Metric", "Actual Time (Hours)", "Unit",
    "entry_count", "duration_minutes", "hours"]
MONTHLY_REPORT_COLUMNS = ["Month"] + REPORT_COLUMNS[1:]


class ExportError(Exception):
    """Safe, user-readable error; never contains HTTP bodies or credentials."""


class AsanaHTTPError(ExportError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer credential to another URL.


def gid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise ExportError("An Asana ID is missing or invalid; use its numeric GID.")
    return value


def api_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ExportError("A work date is missing or invalid; expected YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ExportError("Invalid work date; expected a real YYYY-MM-DD date.") from None


def retry_delay(value, attempt):
    if value:
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                seconds = (target - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 2 ** attempt
        if math.isfinite(seconds):
            return max(0.0, seconds)
    return float(min(2 ** attempt, 60))


class AsanaClient:
    def __init__(self, token, timeout=30, retries=5, opener=None, sleep=time.sleep):
        if (not isinstance(token, str) or not token or len(token) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ExportError("Token is empty or contains unexpected characters.")
        if not math.isfinite(timeout) or not 0 < timeout <= 120 or not 0 <= retries <= 10:
            raise ExportError("Use a timeout of at most 120 seconds and 0 to 10 retries.")
        self._token = token
        self.timeout = timeout
        self.retries = retries
        if opener is None:
            # Direct SSLContext avoids SSLKEYLOGFILE-driven secret logging.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise ExportError("Verified TLS is required.")
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        self.opener = opener
        self.sleep = sleep

    def get(self, path, params=None):
        # Callers supply only fixed API paths and validated numeric GIDs.
        if not isinstance(path, str) or not re.fullmatch(
                r"/(?:users/me|time_tracking_entries(?:/[0-9]{1,32})?|tasks/[0-9]{1,32})", path):
            raise ExportError("Invalid API path.")
        url = API + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={"Authorization": "Bearer " + self._token,
            "Accept": "application/json", "User-Agent": "AsanaTimeCSV/" + VERSION},
            method="GET")
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    if getattr(response, "status", 200) != 200:
                        raise ExportError("Unexpected HTTP status; no export saved.")
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ExportError("Asana response exceeded the size limit; no export saved.")
                    payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict) or "data" not in payload:
                    raise ExportError("Asana returned an unexpected response; no export saved.")
                return payload
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status in {408, 429, 500, 502, 503, 504} and attempt < self.retries:
                    delay = retry_delay(retry_after, attempt)
                    if delay > 300:
                        raise ExportError("Asana asks for a wait over five minutes. Retry later; no export saved.") from None
                    self.sleep(delay)
                    continue
                messages = {
                    400: "Asana rejected the filters or fields. Check the workspace/project IDs and current API documentation.",
                    401: "Asana rejected the token. Enter a valid personal token for your own account.",
                    402: "Your Asana plan or organization does not permit this time-tracking request. Ask your Asana admin to check access.",
                    403: "Your account cannot read this data, or company policy blocks API access. Ask your Asana admin; do not use a coworker's token.",
                    404: "An Asana item was not found or is no longer accessible. Check IDs and rerun if data changed.",
                    429: "Asana is still rate-limiting after retries. Wait and rerun.",
                }
                raise AsanaHTTPError(status, messages.get(status, "Asana request failed with HTTP " + str(status) + ". Retry later or contact your admin.")) from None
            except (URLError, OSError, HTTPException) as exc:
                if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
                    raise ExportError("TLS certificate verification failed. Ask IT to check the connection; do not disable verification.") from None
                if attempt < self.retries:
                    self.sleep(retry_delay(None, attempt))
                    continue
                raise ExportError("Network request failed after retries. Check connectivity, VPN and company proxy settings.") from None
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                raise ExportError("Asana returned invalid JSON; no export saved. Retry later.") from None

    def pages(self, path, params):
        query = dict(params, limit=100)
        seen_offsets = set()
        for _ in range(MAX_PAGES):
            result = self.get(path, query)
            data = result["data"]
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise ExportError("Unexpected Asana page format; no export saved.")
            yield from data
            next_page = result.get("next_page")
            if next_page is None:
                return
            offset = next_page.get("offset") if isinstance(next_page, dict) else None
            if not isinstance(offset, str) or not offset or len(offset) > 4096 or offset in seen_offsets:
                raise ExportError("Invalid or repeated Asana pagination offset; no export saved.")
            seen_offsets.add(offset)
            query["offset"] = offset
        raise ExportError("Asana pagination exceeded the page limit; no export saved.")


def relation(entry, key, required=False):
    value = entry.get(key)
    if value is None and not required:
        return {"gid": "", "name": "", "state": "null" if key in entry else "absent"}
    if not isinstance(value, dict):
        raise ExportError("A required Asana relationship is missing; no export saved.")
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        raise ExportError("An Asana relationship name has an invalid type; no export saved.")
    return {"gid": gid(value.get("gid")), "name": name or "", "state": "object"}


def normalize(entry, workspace):
    if not isinstance(entry, dict):
        raise ExportError("Unexpected time-entry format; no export saved.")
    identifier = gid(entry.get("gid"))
    work_date = api_date(entry.get("entered_on"))
    minutes = entry.get("duration_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ExportError("A time entry has missing or invalid duration_minutes; no export saved.")
    person = relation(entry, "assignee", required=True)
    creator = relation(entry, "created_by")
    project = relation(entry, "attributable_to")
    task = relation(entry, "task")
    row = dict(zip(CORE_COLUMNS, [identifier, work_date.isoformat(), gid(workspace),
        person["gid"], person["name"], creator["gid"], creator["name"],
        project["gid"], project["name"], minutes, hours(minutes)]))
    row.update(task_gid=task["gid"], task_name=task["name"], task_relationship_status=task["state"],
        task_enrichment_status="not_requested", project_enrichment_status="not_requested",
        reporting_project_gid=project["gid"], reporting_project_name=project["name"],
        reporting_project_source="time_entry_attribution" if project["gid"] else "",
        reporting_project_status="resolved_explicit" if project["gid"] else "not_requested")
    return row


def hours(minutes):
    return str((Decimal(minutes) / Decimal(60)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def task_record(data, expected_gid):
    if not isinstance(data, dict) or data.get("gid") != expected_gid:
        raise ExportError("Task context response has an unexpected ID or format; no export saved.")
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise ExportError("A task name has an invalid type; no export saved.")
    if "projects" not in data or data["projects"] is None:
        projects, project_state = [], "null" if "projects" in data else "absent"
    else:
        if not isinstance(data["projects"], list):
            raise ExportError("Task projects have an invalid type; no export saved.")
        projects = [relation({"project": item}, "project", required=True) for item in data["projects"]]
        project_state = "array"
    return {"gid": expected_gid, "name": name or "", "projects": projects,
        "projects_state": project_state, "parent": relation(data, "parent"),
        "assignee": relation(data, "assignee")}


class ProjectContext:
    """Current project context only. Never supplies time or ownership filters."""
    def __init__(self, client):
        self.client = client
        self.tasks = {}
        self.roots = {}

    def task(self, identifier):
        identifier = gid(identifier)
        if identifier not in self.tasks:
            try:
                payload = self.client.get("/tasks/" + identifier, {"opt_fields": TASK_FIELDS})
                self.tasks[identifier] = task_record(payload["data"], identifier)
            except AsanaHTTPError as error:
                if error.status not in (403, 404):
                    raise
                self.tasks[identifier] = {"gid": identifier, "unavailable": "http_" + str(error.status)}
        return self.tasks[identifier]

    def resolve(self, root):
        root = gid(root)
        if root in self.roots:
            return self.roots[root]
        current, depth = root, 0
        chain, seen, candidates = [], set(), {}
        project_fields_complete = True
        while True:
            if current in seen:
                termination = "cycle"
                break
            if depth > MAX_PARENT_DEPTH:
                termination = "depth_limit"
                break
            seen.add(current)
            task = self.task(current)
            if "unavailable" in task:
                termination = task["unavailable"]
                break
            chain.append({"depth": depth, **task})
            if task["projects_state"] != "array":
                project_fields_complete = False
            for project in task["projects"]:
                candidate = candidates.setdefault(project["gid"], {"gid": project["gid"], "observations": []})
                candidate["observations"].append({"name": project["name"], "source_task_gid": current,
                    "depth": depth, "source": "task_direct" if depth == 0 else "task_inherited"})
            parent = task["parent"]
            if parent["state"] == "null":
                termination = "root_reached"
                break
            if parent["state"] == "absent":
                termination = "parent_absent"
                break
            current, depth = parent["gid"], depth + 1
        complete = termination == "root_reached" and project_fields_complete
        selected, name, source = "", "", ""
        if len(candidates) > 1:
            status = "ambiguous"
        elif not complete:
            status = "incomplete"
        elif not candidates:
            status = "no_project"
        else:
            selected = next(iter(candidates))
            observations = candidates[selected]["observations"]
            # Multiple names for the same stable ID are preserved in the trace;
            # only a consistent nonempty name is displayed in the CSV.
            names = {item["name"] for item in observations if item["name"]}
            name = next(iter(names)) if len(names) == 1 else ""
            source = "task_direct" if any(item["depth"] == 0 for item in observations) else "task_inherited"
            status = "resolved"
        result = {"root_task_gid": root, "chain": chain, "termination": termination,
            "stopped_task_gid": current, "stopped_depth": depth, "complete": complete,
            "project_fields_complete": project_fields_complete,
            "candidates": list(candidates.values()), "status": status,
            "reporting_project_gid": selected, "reporting_project_name": name,
            "reporting_project_source": source}
        self.roots[root] = result
        return result


def validate_window(start, end):
    if (type(start) is not date or type(end) is not date or start > end
            or (end - start).days > 365 or start <= date.min or end >= date.max):
        raise ExportError("Choose an inclusive range of 1 to 366 days with query padding available.")


def collect_entries(client, owner_gid, workspace, start, end, project=None,
                    include_tasks=False, task_name_contains=None, context=None):
    validate_window(start, end)
    owner_gid, workspace = gid(owner_gid), gid(workspace)
    if project:
        gid(project)
    if task_name_contains is not None and (not isinstance(task_name_contains, str) or not task_name_contains.strip()):
        raise ExportError("--task-name-contains cannot be empty.")
    query = {"workspace": workspace, "assignee": owner_gid,
        # Exact inclusive filtering below avoids depending on undocumented boundary semantics.
        "entered_on_start_date": (start - timedelta(days=1)).isoformat(),
        "entered_on_end_date": (end + timedelta(days=1)).isoformat(),
        "opt_fields": ENTRY_FIELDS}
    if project:
        query["attributable_to"] = project
    by_id = {}
    seen_rows = {}
    for entry in client.pages("/time_tracking_entries", query):
        row = normalize(entry, workspace)
        key = row["entry_gid"]
        if key in seen_rows and any(seen_rows[key][field] != row[field] for field in CORE_IDENTITY_COLUMNS):
            raise ExportError("A time entry changed between pages. Rerun to obtain a consistent export.")
        seen_rows[key] = row
        if not (start.isoformat() <= row["work_date"] <= end.isoformat()):
            continue
        if row["assignee_gid"] != owner_gid:
            continue
        if project and row["attributed_project_gid"] != project:
            continue
        by_id[key] = row
    if include_tasks or task_name_contains:
        context = context if context is not None else ProjectContext(client)
        for key, row in list(by_id.items()):
            try:
                payload = client.get("/time_tracking_entries/" + key, {"opt_fields": DETAIL_FIELDS})
            except AsanaHTTPError as error:
                if error.status not in (403, 404) or task_name_contains:
                    raise
                row["task_enrichment_status"] = "detail_http_" + str(error.status)
                row["project_enrichment_status"] = "detail_unavailable"
                if not row["attributed_project_gid"]:
                    row["reporting_project_status"] = "incomplete"
                continue
            if not isinstance(payload["data"], dict):
                raise ExportError("Unexpected time-entry detail format; no export saved.")
            detail = normalize(payload["data"], workspace)
            for field in CORE_IDENTITY_COLUMNS:
                if detail[field] != row[field]:
                    raise ExportError("A time entry changed during detail retrieval. Rerun the export.")
            for field in ("task_gid", "task_name", "task_relationship_status"):
                row[field] = detail[field]
            if not row["task_gid"]:
                row["task_enrichment_status"] = "task_" + row["task_relationship_status"]
                row["project_enrichment_status"] = "no_task"
                if not row["attributed_project_gid"]:
                    row["reporting_project_status"] = "no_task"
            else:
                row["task_enrichment_status"] = "available" if row["task_name"] else "name_unavailable"
                if include_tasks and not row["attributed_project_gid"]:
                    resolved = context.resolve(row["task_gid"])
                    row["project_enrichment_status"] = "complete" if resolved["complete"] else resolved["termination"]
                    if not resolved["project_fields_complete"]:
                        row["project_enrichment_status"] = "project_fields_unavailable"
                    if not row["task_name"] and resolved["chain"]:
                        row["task_name"] = resolved["chain"][0]["name"]
                        if row["task_name"]:
                            row["task_enrichment_status"] = "available"
                    for field in ("reporting_project_gid", "reporting_project_name", "reporting_project_source"):
                        row[field] = resolved[field]
                    row["reporting_project_status"] = resolved["status"]
                elif row["attributed_project_gid"]:
                    row["project_enrichment_status"] = "not_needed_explicit_attribution"
            if task_name_contains and not row["task_name"]:
                raise ExportError("Task names required by --task-name-contains are unavailable; no export saved.")
            if task_name_contains and task_name_contains.casefold() not in row["task_name"].casefold():
                del by_id[key]
    return sorted(by_id.values(), key=lambda row: (row["work_date"], row["entry_gid"]))


def daily_totals(entries, owner, workspace, start, end):
    totals = {}
    for row in entries:
        item = totals.setdefault(row["work_date"], [0, 0])
        item[0] += row["duration_minutes"]
        item[1] += 1
    result = []
    day = start
    while day <= end:
        minutes, count = totals.get(day.isoformat(), [0, 0])
        result.append(dict(zip(DAILY_COLUMNS, [day.isoformat(), workspace, owner["gid"],
            owner.get("name", ""), count, minutes, hours(minutes)])))
        day += timedelta(days=1)
    return result


def daily_report(daily):
    return [{"Date": row["work_date"], "Consultant": row["assignee_name"],
        "Metric": "Actual Time", "Actual Time (Hours)": row["hours"], "Unit": "Hours",
        "entry_count": row["entry_count"], "duration_minutes": row["duration_minutes"],
        "hours": row["hours"]} for row in daily]


def monthly_report(daily):
    months = {}
    for row in daily:
        key = row["work_date"][:7]
        item = months.setdefault(key, {"Month": key, "Consultant": row["assignee_name"],
            "Metric": "Actual Time", "Unit": "Hours", "entry_count": 0, "duration_minutes": 0})
        item["entry_count"] += row["entry_count"]
        item["duration_minutes"] += row["duration_minutes"]
    for item in months.values():
        item["hours"] = item["Actual Time (Hours)"] = hours(item["duration_minutes"])
    return list(months.values())


def enrichment_summary(entries, requested, project_requested=False):
    task_counts = dict(Counter(row["task_enrichment_status"] for row in entries))
    project_counts = dict(Counter(row["project_enrichment_status"] for row in entries))
    partial = any(row["task_enrichment_status"] != "available" or (project_requested and
        row["project_enrichment_status"] not in ("complete", "not_needed_explicit_attribution"))
        for row in entries)
    return {"requested": requested, "status": ("partial" if partial else "complete") if requested else "not_requested",
        "task_status_counts": task_counts, "project_status_counts": project_counts,
        "entries_without_task_name": sum(not row["task_name"] for row in entries),
        "entries_without_original_project_attribution": sum(not row["attributed_project_gid"] for row in entries),
        "entries_without_resolved_reporting_project": sum(not row["reporting_project_gid"] for row in entries),
        "note": "Unavailable context is not missing time. All retained entry minutes are included."}


def validate_reconciliation(entries, daily, manifest):
    if len({row["entry_gid"] for row in entries}) != len(entries):
        raise ExportError("Duplicate entry IDs in snapshot; no export saved.")
    totals = {}
    for row in entries:
        if row["assignee_gid"] != manifest["owner_gid"] or row["workspace_gid"] != manifest["workspace_gid"]:
            raise ExportError("Snapshot ownership/workspace reconciliation failed; no export saved.")
        if not manifest["start_date"] <= row["work_date"] <= manifest["end_date"]:
            raise ExportError("Snapshot date reconciliation failed; no export saved.")
        amount, count = totals.get(row["work_date"], (0, 0))
        totals[row["work_date"]] = amount + row["duration_minutes"], count + 1
    expected_dates = set()
    day, end = api_date(manifest["start_date"]), api_date(manifest["end_date"])
    validate_window(day, end)
    while day <= end:
        expected_dates.add(day.isoformat())
        day += timedelta(days=1)
    if len(daily) != len(expected_dates) or {r["work_date"] for r in daily} != expected_dates:
        raise ExportError("Daily date reconciliation failed; no export saved.")
    for row in daily:
        amount, count = totals.get(row["work_date"], (0, 0))
        if (row["duration_minutes"] != amount or row["entry_count"] != count or row["hours"] != hours(amount)
                or row["assignee_gid"] != manifest["owner_gid"] or row["workspace_gid"] != manifest["workspace_gid"]):
            raise ExportError("Daily amount/ownership reconciliation failed; no export saved.")


def csv_cell(value):
    # Treat externally supplied names as text, never spreadsheet formulas.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_cell(row[key]) for key in columns})
        handle.flush()
        os.fsync(handle.fileno())


def save_snapshot(output, entries, daily, manifest, context=None, secret=None):
    """Publish the whole completed directory only after all files are written."""
    validate_reconciliation(entries, daily, manifest)
    context_report = {"schema_version": 1, "membership_basis": "Current task/ancestor projects, not historical time attribution",
        "max_parent_depth": MAX_PARENT_DEPTH, "root_tasks": context.roots if context else {}}
    if secret:
        def contains_secret(value):
            if isinstance(value, str):
                return secret in value
            if isinstance(value, dict):
                return any(contains_secret(k) or contains_secret(v) for k, v in value.items())
            if isinstance(value, (tuple, list)):
                return any(contains_secret(v) for v in value)
            return False
        if contains_secret([entries, daily, manifest, context_report]):
            raise ExportError("Refused to write response data containing credential text.")
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".asana-incomplete-", dir=output))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = (f"asana-{manifest['owner_gid']}-{manifest['workspace_gid']}-"
            f"{manifest['start_date']}_{manifest['end_date']}-{stamp}-{uuid.uuid4().hex[:6]}")
    final = output / name
    try:
        write_csv(stage / "daily_totals.csv", DAILY_COLUMNS, daily)
        write_csv(stage / "daily_report.csv", REPORT_COLUMNS, daily_report(daily))
        monthly = monthly_report(daily)
        write_csv(stage / "monthly_report.csv", MONTHLY_REPORT_COLUMNS, monthly)
        write_csv(stage / "entries.csv", ENTRY_COLUMNS, entries)
        with (stage / "project_context.json").open("w", encoding="utf-8") as handle:
            json.dump(context_report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        manifest = dict(manifest, status="complete", exporter_version=VERSION,
            core_snapshot_status="complete", schema_version=2,
            exported_at_utc=datetime.now(timezone.utc).isoformat(),
            entry_count=len(entries), duration_minutes=sum(x["duration_minutes"] for x in entries),
            hours=hours(sum(x["duration_minutes"] for x in entries)),
            monthly_totals={row["Month"]: {"duration_minutes": row["duration_minutes"], "hours": row["hours"]} for row in monthly},
            enrichment_summary=enrichment_summary(entries, manifest.get("task_details_requested", False), manifest.get("project_context_requested", False)),
            files_sha256={filename: hashlib.sha256((stage / filename).read_bytes()).hexdigest()
                for filename in ("daily_totals.csv", "daily_report.csv", "monthly_report.csv", "entries.csv", "project_context.json")})
        with (stage / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        stage.rename(final)
        return final
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def date_range(args, today=None):
    today = today or date.today()
    def parse(text):
        if text == "today":
            return today
        if text == "yesterday":
            return today - timedelta(days=1)
        return api_date(text)
    if args.date:
        if args.start or args.end or args.days is not None:
            raise ExportError("Use --date alone, or --start/--end, or --days [--end].")
        start = end = parse(args.date)
    else:
        end = parse(args.end) if args.end else today
        if args.start:
            if args.days is not None:
                raise ExportError("Choose --start or --days, not both.")
            start = parse(args.start)
        else:
            days = 30 if args.days is None else args.days
            if not 1 <= days <= 366:
                raise ExportError("--days must be between 1 and 366.")
            start = end - timedelta(days=days - 1)
    if start > end or (end - start).days > 365:
        raise ExportError("Choose an inclusive range of 1 to 366 days, with start <= end.")
    if start <= date.min or end >= date.max:
        raise ExportError("Date range must leave room for one day of query padding.")
    return start, end


def parser():
    p = argparse.ArgumentParser(description="Export daily Asana logged hours attributed only to your personal token's owner.")
    p.add_argument("--workspace", help="Workspace/organization numeric GID; required if you belong to more than one")
    p.add_argument("--expected-owner", help="Abort if the credential owner differs from this numeric user GID")
    p.add_argument("--list-workspaces", action="store_true", help="Print your account and workspace IDs, then exit")
    p.add_argument("--date", help="One work date: YYYY-MM-DD, today or yesterday")
    p.add_argument("--start", help="Inclusive start date: YYYY-MM-DD")
    p.add_argument("--end", help="Inclusive end date; defaults to this computer's local today")
    p.add_argument("--days", type=int, help="Number of inclusive days, default 30; may combine with --end")
    p.add_argument("--project", help="Only entries attributed to this project GID (not all member tasks)")
    p.add_argument("--include-tasks", action="store_true", help="Best-effort task names and direct/inherited project context; never required to count time")
    p.add_argument("--task-name-contains", help="Case-insensitive task-name substring; fetches details, not an Asana saved search")
    p.add_argument("--output", default=str(Path(__file__).resolve().parent / "asana-exports-v2"), help="Local output directory; defaults to asana-exports-v2 beside this script")
    p.add_argument("--token-file", help="Read a token from this local file or mounted runtime secret; never pass the token itself")
    p.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting if no token source is available")
    p.add_argument("--timeout", type=float, default=30, help="Seconds per request, default 30")
    p.add_argument("--retries", type=int, default=5, help="Retries per transient failure, default 5")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def read_token(non_interactive, token_file=None):
    # An explicit file takes precedence. Ambiguous environment-only sources fail.
    source = token_file if token_file is not None else os.environ.get("ASANA_ACCESS_TOKEN_FILE")
    if source is not None:
        if not source or (token_file is None and os.environ.get("ASANA_ACCESS_TOKEN", "").strip()):
            raise ExportError("Choose one token source: a token file or ASANA_ACCESS_TOKEN.")
        try:
            with Path(source).expanduser().open("r", encoding="utf-8-sig") as handle:
                contents = handle.read(4097)
            if len(contents) > 4096:
                raise ExportError("The token file exceeds the size limit.")
            token = contents.strip()
        except (OSError, UnicodeError, ValueError):
            raise ExportError("Cannot read the token file. Check its path, permissions and UTF-8 encoding.") from None
    else:
        token = os.environ.get("ASANA_ACCESS_TOKEN", "").strip()
    if not token:
        if source is not None:
            raise ExportError("The token file is empty; no request was made.")
        if non_interactive or not sys.stdin.isatty():
            raise ExportError("Provide a token file, set ASANA_ACCESS_TOKEN securely, or use an interactive hidden prompt.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Your Asana personal token (hidden): ").strip()
        except (getpass.GetPassWarning, EOFError):
            raise ExportError("A secure hidden prompt is unavailable. Use an approved environment-variable/secret setup.") from None
    if not token or len(token) > 4096 or not token.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ExportError("The token is empty or contains unexpected characters; check the value without sharing it.")
    return token


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        start, end = date_range(args)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120 or not 0 <= args.retries <= 10:
            raise ExportError("Use a positive finite timeout of at most 120 seconds and 0 to 10 retries.")
        if args.workspace:
            gid(args.workspace)
        if args.expected_owner:
            gid(args.expected_owner)
        if args.project:
            gid(args.project)
        if args.task_name_contains is not None and not args.task_name_contains.strip():
            raise ExportError("--task-name-contains cannot be empty.")
        client = AsanaClient(read_token(args.non_interactive, args.token_file), args.timeout, args.retries)
        owner = client.get("/users/me", {"opt_fields": "name,workspaces.name"})["data"]
        if not isinstance(owner, dict):
            raise ExportError("Unexpected account response; no export saved.")
        gid(owner.get("gid"))
        if args.expected_owner and owner["gid"] != args.expected_owner:
            raise ExportError("Credential owner does not match --expected-owner; no export saved.")
        if owner.get("name") is not None and not isinstance(owner["name"], str):
            raise ExportError("Unexpected account name type; no export saved.")
        workspaces = owner.get("workspaces", [])
        if not isinstance(workspaces, list) or any(not isinstance(w, dict) for w in workspaces):
            raise ExportError("Unexpected workspace response; no export saved.")
        for available_workspace in workspaces:
            gid(available_workspace.get("gid"))
            if available_workspace.get("name") is not None and not isinstance(available_workspace["name"], str):
                raise ExportError("Unexpected workspace name type; no export saved.")
        if args.list_workspaces:
            print("Token owner:", owner.get("name", ""), "(" + owner["gid"] + ")")
            for workspace in workspaces:
                print(gid(workspace.get("gid")), workspace.get("name", ""), sep="\t")
            return 0
        workspace = args.workspace
        if not workspace:
            if len(workspaces) != 1:
                raise ExportError("Choose a workspace: run --list-workspaces, then pass --workspace GID.")
            workspace = gid(workspaces[0].get("gid"))
        context = ProjectContext(client)
        entries = collect_entries(client, owner["gid"], workspace, start, end,
            args.project, args.include_tasks, args.task_name_contains, context)
        daily = daily_totals(entries, owner, workspace, start, end)
        manifest = {"owner_gid": owner["gid"], "expected_owner_gid": args.expected_owner, "workspace_gid": workspace,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "project_filter_gid": args.project, "task_name_contains": args.task_name_contains,
            "task_details_requested": bool(args.include_tasks or args.task_name_contains),
            "project_context_requested": args.include_tasks,
            "date_basis": "Asana entered_on; relative dates use the running computer's local calendar",
            "scope": "Own time: time-entry assignee equals token owner; creator/task assignee/project membership do not control inclusion",
            "reporting_project_policy": "Explicit time attribution first; otherwise one unambiguous task/ancestor project; blank context never removes time",
            "chart_scope": "My tasks charts may include colleagues or different filters; no chart-matching adjustment is applied"}
        path = save_snapshot(args.output, entries, daily, manifest, context, client._token)
        minutes = sum(row["duration_minutes"] for row in entries)
        print(f"Saved {len(entries)} entries, {minutes} minutes ({hours(minutes)} hours).")
        print("Actual Time report (Hours):", path / "daily_report.csv")
        print("Monthly Actual Time (Hours):", path / "monthly_report.csv")
        print("Daily totals:", path / "daily_totals.csv")
        print("Audit entries:", path / "entries.csv")
        summary = enrichment_summary(entries, manifest["task_details_requested"], args.include_tasks)
        print("Optional context:", summary["status"], "| entries without task names:", summary["entries_without_task_name"],
            "| entries without resolved project:", summary["entries_without_resolved_reporting_project"])
        print("Unavailable context is not missing time; totals use retained owner-assigned entry minutes.")
        if not entries:
            print("No matching recorded entries. Zero rows in daily totals mean no recorded time, not proof of no work.")
        return 0
    except ExportError as exc:
        print("Export failed:", str(exc), file=sys.stderr)
        return 1
    except (OSError, OverflowError):
        print("Export failed: a local file/date operation failed. Check dates, output access, disk space, and close open CSVs. No partial snapshot was published.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Export cancelled. No partial snapshot was published.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
