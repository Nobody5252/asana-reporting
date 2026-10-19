#!/usr/bin/env python3
"""Publish a validated snapshot to a dedicated Google Sheets reporting workbook.

No database or Google SDK required. Offline preparation writes report.json.
Publishing requires an explicitly supplied, short-lived Google OAuth access token.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from asana_postgres import StorageError, load_snapshot

TABS = ("Daily", "Week", "Monthly", "Entries", "Status")
SCOPE = ("workspace_gid", "owner_gid", "report_policy")
FORMAT = "asana-sheets-v2"
POLICY = "month-to-date-last-seven-days-daily"
EPOCH = date(1899, 12, 30)


def report(snapshot):
    m = snapshot.manifest
    as_of = date.fromisoformat(m["end_date"])
    month_start = as_of.replace(day=1)
    week_start = as_of - timedelta(days=6)
    if date.fromisoformat(m["start_date"]) > min(month_start, week_start):
        raise StorageError("Snapshot must cover month-to-date and all seven reporting days.")
    daily = defaultdict(lambda: [0, 0])
    detail = [["Work date", "Person", "Minutes", "Hours", "Project", "Task",
               "Entry ID", "Owner ID", "Creator ID", "Task ID", "Project ID", "Project status"]]
    for row in sorted(snapshot.entries, key=lambda r: (r["work_date"], r["entry_gid"])):
        minutes = row["duration_minutes"]
        bucket = daily[row["work_date"]]
        bucket[0] += 1
        bucket[1] += minutes
        if row["work_date"] < week_start.isoformat():
            continue
        detail.append([(date.fromisoformat(row["work_date"]) - EPOCH).days,
                       row["assignee_name"], minutes, minutes / 60,
                       row["reporting_project_name"], row["task_name"], row["entry_gid"],
                       row["assignee_gid"], row["created_by_gid"], row["task_gid"],
                       row["reporting_project_gid"], row["reporting_project_status"]])
    def summary(start):
        return [["Work date", "Entries", "Minutes", "Hours"]] + [
            [(day - EPOCH).days, *daily[day.isoformat()], daily[day.isoformat()][1] / 60]
            for day in (start + timedelta(days=i) for i in range((as_of - start).days + 1))]

    week, monthly, today = summary(week_start), summary(month_start), summary(as_of)
    today_details = [row for row in detail[1:] if row[0] == (as_of - EPOCH).days]
    status = {"format": FORMAT, "report_policy": POLICY,
              **{key: m[key] for key in ("workspace_gid", "owner_gid", "start_date", "end_date")},
              "as_of_date": as_of.isoformat(), "month_start": month_start.isoformat(),
              "week_start": week_start.isoformat(),
              "snapshot_sha256": snapshot.digest, "exported_at_utc": m["exported_at_utc"],
              "entry_count": len(snapshot.entries), "duration_minutes": m["duration_minutes"],
              "hours": m["duration_minutes"] / 60,
              "context_status": m.get("enrichment_summary", {}).get("status", "not_requested"),
              "source": "Validated Asana exporter snapshot; own time by entry assignee",
              "refresh_policy": "Managed tabs replaced in full; one writer; do not edit these tabs"}
    for label, rows in (("month", monthly), ("week", week), ("daily", today)):
        count, minutes = sum(row[1] for row in rows[1:]), sum(row[2] for row in rows[1:])
        status.update({label + "_entry_count": count, label + "_minutes": minutes,
                       label + "_hours": minutes / 60})
    return {"Daily": [*today, [], detail[0], *today_details],
            "Week": week, "Monthly": monthly,
            "Entries": detail, "Status": [["Field", "Value"], *map(list, status.items())]}


def table_width(rows):
    return max(len(row) for row in rows)


def checked_batch(tables, metadata, previous_status, *, migrate_layout=False):
    """Build one atomic replace, clearing stale trailing rows; never append time.

    The five managed tabs belong exclusively to this feed. Other tabs are untouched.
    Caller must serialize writers: Sheets batchUpdate has no compare-and-swap.
    """
    current = dict(tables["Status"][1:])
    prior = dict(previous_status[1:])
    legacy = migrate_layout and prior.get("format") == "asana-sheets-v1"
    keys = ("workspace_gid", "owner_gid") if legacy else SCOPE
    if (current.get("format") != FORMAT or current.get("report_policy") != POLICY or
            (not legacy and prior.get("format") != FORMAT) or
            any(prior.get(k) != current[k] for k in keys)):
        raise StorageError("Destination is not the same managed owner/workspace/report policy.")
    try:
        old_day = date.fromisoformat(prior["end_date"] if legacy else prior["as_of_date"])
        new_day = date.fromisoformat(current["as_of_date"])
        if old_day > new_day:
            raise StorageError("Older report dates cannot replace this report.")
        before = datetime.fromisoformat(prior["exported_at_utc"].replace("Z", "+00:00"))
        after = datetime.fromisoformat(current["exported_at_utc"].replace("Z", "+00:00"))
        if before.tzinfo is None or after.tzinfo is None:
            raise ValueError()
        if before > after or (before == after and prior.get("snapshot_sha256") != current["snapshot_sha256"]):
            raise StorageError("Older or conflicting snapshots cannot replace this report.")
    except (KeyError, ValueError, TypeError):
        raise StorageError("Destination refresh timestamp is invalid.") from None
    sheets = {s["properties"]["title"]: s["properties"] for s in metadata["sheets"]}
    if not set(TABS) <= set(sheets):
        raise StorageError("Destination is missing a managed report tab.")
    requests = []
    for title in TABS:
        rows, properties = tables[title], sheets[title]
        sid, width = properties["sheetId"], table_width(rows)
        grid = properties["gridProperties"]
        height = max(len(rows), grid["rowCount"])
        if height > 100000 or height * width > 1000000:
            raise StorageError("Managed tab exceeds the bounded refresh size.")
        if height > grid["rowCount"] or width > grid["columnCount"]:
            requests.append({"updateSheetProperties": {"properties": {"sheetId": sid,
                "gridProperties": {"rowCount": height, "columnCount": max(width, grid["columnCount"])}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        requests.append({"updateCells": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": height,
                      "startColumnIndex": 0, "endColumnIndex": width},
            "rows": [{"values": [{"userEnteredValue": {
                "numberValue" if type(v) in (int, float) else "stringValue": v}}
                for v in row]} for row in rows], "fields": "userEnteredValue"}})
    return requests


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SheetsClient:
    def __init__(self, spreadsheet_id, token_file):
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", spreadsheet_id):
            raise StorageError("Invalid spreadsheet ID.")
        try:
            token = Path(token_file).read_text().strip()
            if not token or len(token) > 8192 or any(c.isspace() for c in token):
                raise ValueError()
        except (OSError, UnicodeError, ValueError):
            raise StorageError("Cannot read the designated Google access-token file.") from None
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id
        self.token = token
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(self, suffix, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + suffix, data=data, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise StorageError("Google response exceeds the size limit.")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            raise StorageError(f"Google Sheets returned HTTP {error.code}; no response body logged.") from None
        except (OSError, ValueError):
            raise StorageError("Google Sheets request failed; refresh/read back before retrying.") from None

    def read(self, title, bounds):
        cell_range = urllib.parse.quote(f"'{title}'!{bounds}", safe="")
        return self.request("/values/" + cell_range + "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])


def verify(tables, observed):
    # The API omits trailing empty cells/rows. Normalize only that representation.
    def trim(rows):
        result = []
        for row in rows:
            row = list(row)
            while row and row[-1] == "":
                row.pop()
            result.append(row)
        while result and not result[-1]:
            result.pop()
        return result
    if any(trim(tables[title]) != trim(observed[title]) for title in TABS):
        raise StorageError("Google Sheets readback differs from the validated report.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", type=Path, help="Write private report JSON for workbook creation/connector use")
    parser.add_argument("--spreadsheet-id", help="Refresh an existing dedicated, initialized reporting Sheet")
    parser.add_argument("--access-token-file", type=Path, help="Explicit short-lived Google OAuth token file")
    parser.add_argument("--migrate-layout", action="store_true",
                        help="Explicitly migrate a v1 workbook after its five report tabs are initialized")
    args = parser.parse_args(argv)
    try:
        tables = report(load_snapshot(args.snapshot, args.expected_owner, args.workspace))
        if bool(args.spreadsheet_id) != bool(args.access_token_file):
            raise StorageError("Publishing requires both --spreadsheet-id and --access-token-file.")
        if not args.output and not args.spreadsheet_id:
            raise StorageError("Choose --output for preparation or supply publishing options.")
        if args.output:
            # Exclusive creation avoids overwriting source data accidentally.
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(tables, handle, ensure_ascii=False)
        if args.spreadsheet_id:
            client = SheetsClient(args.spreadsheet_id, args.access_token_file)
            metadata = client.request("?fields=sheets.properties")
            previous = client.read("Status", "A1:B40")
            requests = checked_batch(tables, metadata, previous, migrate_layout=args.migrate_layout)
            client.request(":batchUpdate", {"requests": requests})
            observed = {title: client.read(title, f"A1:{chr(64 + table_width(tables[title]))}{max(40, len(tables[title]) + 1)}")
                        for title in TABS}
            verify(tables, observed)
        print("Sheets report prepared" + ("; refresh and readback verified." if args.spreadsheet_id else "."))
        return 0
    except (StorageError, OSError, ValueError, KeyError, TypeError) as error:
        # Only our safe diagnostics may be displayed; never raw HTTP/JSON errors.
        print(str(error) if isinstance(error, StorageError) else "Sheets report failed; check inputs and read back before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
