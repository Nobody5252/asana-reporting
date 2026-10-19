# Asana Reporting

One application for collecting each consultant's own logged Asana time and
producing daily and monthly Actual Time reports. CSV files, optional Postgres
storage and optional Google Sheets publishing live together on `main`. They
can be used together with the same validated snapshot; they are not mutually
exclusive versions of the product. The core exporter still needs no packages.

**Start with the [application and implementation overview](docs/application-guide.md)**
for technical stakeholders and deployment operators. It explains API/Python data
flow, snapshots, optional outputs, deployment diagrams, cloud setup,
Linux/Windows/macOS prerequisites, token provisioning and command-line usage.

Engineering documentation:

- [Deployment design and production requirements](docs/deployment.md)
- [Postgres setup, imports and reports](docs/postgres.md)
- [Google Sheets setup and refresh](docs/sheets.md)
- [Docker builds and runtime secret mounts](docs/docker.md)
- [Validation evidence and current limits](VALIDATION.md)

The CSV collector, Postgres importer and Sheets publisher are implemented and
tested. A six-person coordinator, automatic Google token renewal,
scheduled hosting, a consolidated dashboard and a web UI remain
proposed work. Logged time is not an approved invoice or a billability decision.

Report each consultant's own logged Asana time for reporting and billing.
Every consultant uses their own token. Own time means the **time entry's
`assignee` equals the token owner returned by `/users/me`**. `created_by` identifies
who recorded it and is preserved separately. Neither task assignee nor project
membership determines whose time is counted. No option exports another owner.

All matching individual entries count, including entries with no task, project,
or client label. Task/project context is optional. The exporter does not adjust
totals to match My tasks or portfolio charts, which can have different scopes
and include colleagues' time. Integer `duration_minutes` is authoritative; hours
are calculated from summed minutes and displayed to six decimal places.

This continues the v2 package. V1 and previous real exports remain outside this
repository. Private handoff notes and targeted diagnostics stay local and are
excluded from Git and container builds.

## Run locally

Python 3.10+ and standard libraries only. No installation or background service.
Use the same computer's intended work calendar/timezone for relative dates.

```sh
python3 asana_time_export.py --list-workspaces
python3 asana_time_export.py --workspace WORKSPACE_GID --calendar-months 2 --include-tasks
python3 asana_time_export.py --workspace WORKSPACE_GID --start 2026-10-01 --end 2026-10-18 --include-tasks
python3 asana_time_export.py --workspace WORKSPACE_GID --date yesterday
# Pass a designated file path, never the token itself.
python3 asana_time_export.py --token-file /approved/secrets/asana.token --expected-owner OWNER_GID --workspace WORKSPACE_GID --calendar-months 2 --end 2026-10-19 --non-interactive
```

With no date-range options, collection covers the **full previous calendar month
plus the current month to date**. For example, an October 19 run starts September 1.
`--calendar-months 1` selects month-to-date reporting; near month start it also
collects the prior days needed by Week/Entries. `--end` can anchor either mode to
an explicit report date. Arbitrary `--start`/`--days` ranges remain available for
targeted exports, but incomplete calendar coverage is rejected by the Sheets publisher.

On Windows, use an approved Python interpreter, such as `python`, if `py -3`
is unavailable. The tool prompts without echoing the token and never writes it.
A designated UTF-8 token file (optionally with BOM/trailing newline) can instead
be supplied through `--token-file` or `ASANA_ACCESS_TOKEN_FILE`. An explicit
`--token-file` takes precedence. Setting both environment-only token sources is
rejected. Unreadable, empty, oversized or malformed files fail without fallback.
The v1 `ASANA_ACCESS_TOKEN` input remains supported. `--non-interactive` fails
if no credential source is available. No scheduling is installed.
Never paste tokens into chat, source, command arguments, documentation or logs.
Use `--expected-owner OWNER_GID` in repeatable runs to reject a credential for the
wrong consultant before collecting entries. Paths and GIDs are not token values.

## Docker

See [Docker usage](docs/docker.md) for builds, runtime secret mounts, Compose,
output permissions and portable image archives. The runtime runs without root,
contains only the exporter, and writes reports to a mounted `/data` directory.
It exposes no ports and runs once per invocation. CI tests and builds use only
synthetic fixtures; no Asana credential or live data is uploaded to CI.

One workspace is selected automatically only when `/users/me` lists exactly one.
Otherwise pass its numeric GID. Date ranges are inclusive, limited to 366 days.
The API query pads one day at each edge, then filters the exact `entered_on`
dates locally. Task due dates, completion dates and names do not determine dates.

Omit `--include-tasks` for the smallest API workload and core totals only. With
it, each retained entry gets a detail GET; tasks without explicit time attribution
can get task/ancestor GETs, cached within that run, to add project context.
Do not edit time during a run. The API is not an atomic server snapshot.

## Files

Default output is `asana-exports-v2` **beside the v2 script**, independent of the
terminal's working directory. `--output /approved/local/path` overrides it.
Each run publishes a new timestamped directory containing:

| File | Purpose |
| --- | --- |
| `daily_report.csv` | Human daily report: `Metric` = Actual Time, `Actual Time (Hours)`, `Unit` = Hours, plus stable `entry_count`, `duration_minutes`, `hours` |
| `monthly_report.csv` | Same report by calendar month within the requested window; partial months stay partial |
| `daily_totals.csv` | Stable machine daily reconciliation rows, including requested zero days |
| `entries.csv` | Exactly one row per retained entry ID; owner/creator, original attribution, optional context and statuses |
| `project_context.json` | Whitelisted task/ancestor provenance, candidate IDs/names, termination reasons and depth |
| `manifest.json` | Owner, workspace, dates/filters, total/monthly amounts, enrichment summary and SHA-256 checksums for the five data files |

CSV is UTF-8 with BOM. Import GIDs as **Text** to preserve long IDs. External
names that could be spreadsheet formulas receive a leading apostrophe. JSON
preserves names as data. Completed directories are published by a final rename
only after writes and reconciliation pass. Failures publish no finished snapshot.
Snapshots are append-only at directory level: no old output is rewritten.
Use local storage; network/sync filesystem rename guarantees can differ.

Do not add overlapping snapshots together. Use one complete comparable snapshot
for a window. Entry IDs deduplicate within each run. The optional Postgres layer
upserts stable entry IDs and records revisions instead of appending totals.

## Optional task and project context

Original `attributed_project_gid` / `attributed_project_name` remain unchanged.
Resolved `reporting_project_gid` / `reporting_project_name` are separate, with
`reporting_project_source` and `reporting_project_status`:

1. Explicit time-entry attribution takes priority (`time_entry_attribution`,
   `resolved_explicit`). No task-project lookup is needed for that resolution.
2. Otherwise, inspect the task's direct projects and all retrieved parent levels.
   A complete chain with exactly one distinct project ID resolves to `task_direct`
   or `task_inherited` with status `resolved`.
3. Multiple candidates produce `ambiguous`; inaccessible/incomplete ancestry
   produces `incomplete`; a complete chain with none produces `no_project`.
   All resolved project fields stay blank when no unambiguous resolution exists.

Current memberships are context, not evidence of historical membership when the
time was entered. Traversal stops at ten parent levels, cycles, absent parent
fields or inaccessible records. Names may be unavailable while an ID is known.
Different nonempty names for the same project ID remain in provenance and leave
its display name blank. No task rollups, parent totals or cumulative Actual Time
fields are ever added. Multi-project membership never multiplies minutes.

`task_relationship_status` distinguishes absent/null/object relationships.
`task_enrichment_status` and `project_enrichment_status` explain unavailable
context. Optional detail/task HTTP 403/404 responses preserve core entries and
are reported as unavailable. A missing/null task preserves every minute.

Authentication/network/TLS failures, malformed responses, conflicting duplicate
core records or changes in core entry identity/date/ownership/amount/attribution
abort without publishing a snapshot. Optional display names are not core snapshot
requirements. The core snapshot can be complete with `enrichment_summary.status`
equal to `partial`; this is explicit unavailable context, **not missing time**.

`--task-name-contains TEXT` intentionally narrows the report. Required unavailable
task names (including missing/null tasks or detail access failures) fail safely
rather than silently excluding entries. `--project GID` intentionally filters
**explicit time attribution only**; it excludes blanks and is unsuitable for a
complete own-time billing total. Omit both filters for overall person-owned time.

## Security and failure behavior

Only GET requests to fixed `https://app.asana.com/api/1.0` paths, numeric IDs,
verified TLS, no redirects, no environment proxies, no TLS key logging. Socket
timeout defaults to 30 seconds (maximum 120). Up to five transient retries by
default, bounded to ten; excessive Retry-After waits fail safely. Pagination
uses returned offsets, rejects repetition, ignores returned URL destinations,
and stops at a fixed page limit. Error bodies and headers are not logged.

Do not disable TLS verification. This package does not change security/trust settings.
Personal tokens may grant broader permissions than the code's GET-only behavior.
Production multi-user scoped OAuth and approved secret provisioning remain future
decisions. Never share consultants' tokens or use a colleague's token.

## Offline validation

Optional Google Sheets reporting is available through `asana_sheets.py` and
`Dockerfile.sheets`. It refreshes five tabs from validated snapshots: Monthly
shows a full previous-month total and current month-to-date total, followed by
their daily breakdowns; Week and Entries cover the last seven days;
Daily contains the report date's totals and individual entries; Status records
coverage and freshness. Windows roll forward without a database. See
[Sheets setup and refresh](docs/sheets.md).
The connected Google account was used for a live integration test; unattended
container authentication and scheduling are not configured.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The original 54 exporter regressions remain. Storage validation and opt-in real
Postgres integration tests are in `tests/test_postgres.py`. Approved local live validation also
reconciled core-only and enriched exports with the preserved v1 baseline; private
outputs are excluded from Git. Tests use invented IDs/names,
including an anonymized three-entry 2h + 1h + 2h pattern whose total remains 5h
while two entries gain direct context and one remains blank. See
[VALIDATION.md](VALIDATION.md).

API references: [time entries](https://developers.asana.com/reference/time-tracking-entries),
[tasks and direct projects/parents](https://developers.asana.com/reference/tasks),
[authentication](https://developers.asana.com/docs/authentication),
[personal tokens](https://developers.asana.com/docs/personal-access-token).
