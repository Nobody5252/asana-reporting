# Deployment design and engineering requirements

Updated October 18, 2026. See the [manager overview](application-guide.md) first.
The following operating design is proposed unless explicitly marked implemented.
No scheduled service, cloud infrastructure or web application is provisioned.

## Application boundaries

The single application has three command-line modules. One completed snapshot
can feed both optional consumers; CSV output remains available independently.

- `asana_time_export.py`: standard-library Asana collector and snapshot writer.
- `asana_postgres.py`: snapshot validation, optional database import and daily CSV reporting.
- `asana_sheets.py`: snapshot-to-Sheets report preparation and publication.

The default Docker image remains the lightweight collector. `Dockerfile.postgres`
adds pinned Psycopg dependencies; `Dockerfile.sheets` includes the publisher and
snapshot validator without a database driver. Separate image recipes are
packaging choices, not mutually exclusive products or separate source branches.
No combined coordinator CLI currently exists.

The collector targets fixed Asana API paths using GET only. The Sheets publisher
uses Google's fixed API host for reads and batch writes. Both verify TLS, reject
redirects, avoid environment proxies and omit raw HTTP bodies from errors.
Do not confuse the collector's read-only Asana access with the publisher's
authorized Google write access.

## Existing CLI workflows

Use Python 3.10+ for the standard-library tools, or the pinned Python 3.13 images.
Install the pinned optional dependencies or use the Postgres image for DB commands.
Uppercase identifiers below are placeholders, not credential values.

### Collect once

```sh
python3 asana_time_export.py \
  --token-file /approved/secrets/consultant-asana.token \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --days 60 --end yesterday --include-tasks \
  --output /approved/private/snapshots --non-interactive
```

Use the completed snapshot directory printed by the collector in later stages.
The 60-day lookback is an example, not a retention or billing-period policy.
The collector accepts at most 366 inclusive days in one run. Historical backfill
must split larger periods and preserve source observation ordering.

### Optionally import and report from Postgres

Configure `PGHOST`, `PGDATABASE`, `PGUSER` and `PGPASSWORD_FILE` explicitly.
Remote connections default to `PGSSLMODE=verify-full`; supply an approved trust
configuration. Only the isolated local development network disables DB TLS.

```sh
python3 asana_postgres.py init-db
python3 asana_postgres.py import-snapshot /approved/private/snapshots/COMPLETED_RUN \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID
python3 asana_postgres.py report \
  --owner OWNER_GID --workspace WORKSPACE_GID \
  --start 2026-10-01 --end 2026-10-17 \
  --output /approved/private/reports/daily.csv
```

The existing schema stores runs, observation watermarks, current entries and
entry revisions, plus a daily reporting view. Import identity is
`(workspace_gid, entry_gid)`, independent of display names. Replays are no-ops;
new observations for an owner must advance the observation timestamp. Records
missing from a comparable successful pull remain flagged as unseen. They remain
in provisional totals until reviewed. The current report command selects one
owner and produces daily CSV; a combined management view/export is future work.

### Optionally publish the same snapshot to Sheets

Initialize a dedicated native workbook through the connected spreadsheet creation
workflow first. It must contain the four supported tabs and valid Status scope.

```sh
python3 asana_sheets.py /approved/private/snapshots/COMPLETED_RUN \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --spreadsheet-id GOOGLE_SPREADSHEET_ID \
  --access-token-file /approved/secrets/google-access-token
```

Use `--output /approved/private/report.json` instead to prepare report data
offline. The current publisher accepts an explicit short-lived OAuth token;
it does not mint/renew tokens, create workbooks, consolidate owners, consume
database reporting views or change a workbook's owner/workspace/date window.
The live integration used the connected Google transport with this module's
payload and exact readback. Standalone HTTP transport tests are synthetic.

## Proposed scheduled coordinator

Add a dedicated orchestration module rather than changing the collector's
ownership semantics. Its configuration should map a stable consultant key to
expected Asana owner/workspace IDs, a secret reference and approved destinations.
Keep secret values out of that configuration, source, reports and logs.

1. Determine the logical run date in `America/El_Salvador`; anchor retries to
   that date. Select yesterday as the end date and the approved correction window.
2. Acquire a cross-execution lease/lock and establish a run ID. A single task
   setting alone does not prevent overlapping job executions.
3. Collect the six owners with explicit identity checks, bounded API retries
   and independent failure records. Begin sequentially at this scale.
4. Retain each complete validated snapshot on approved durable storage.
5. Import enabled Postgres outputs and publish enabled individual Sheet outputs.
   Track destination stages independently; a DB transaction cannot roll back a
   successful Google write, and a Google batch cannot roll back six workbooks.
6. Construct the consolidated feed from validated current observations or a
   defined database reporting view. Deduplicate by stable entry identity and
   resolve source freshness/ownership conflicts explicitly.
7. Publish the master feed and per-owner source timestamps/coverage, then release
   the lock. Mark a fully current report only when all required inputs succeeded.
8. Alert the operator for failure, stale data or verification mismatch. Read back
   ambiguous Google writes before retrying. Re-publish only failed destinations
   from the retained run when safe; do not blindly re-import older observations.

The current collector aborts publication on enrichment network failures. Treat
independent core/enrichment retries as additional design work, not existing behavior.

## Scheduling and hosting

Recommended managed candidate: Cloud Scheduler calls the Cloud Run Jobs run API
with a dedicated invoking identity; the job uses a separate runtime identity.
Configure `0 0 * * *` and `America/El_Salvador`, and set the container reporting
timezone too. API acceptance of a job start is not proof the execution completed;
monitor the job outcome and application coverage.

The runtime would need approved Asana/Google egress, secrets access, snapshot
storage and, if selected, connectivity to Postgres. Add an orchestration image;
the current three images invoke their individual modules, not an end-to-end job.
Prefer workload identity/runtime credentials over long-lived service-account
key files when the approved platform supports the required Google Sheets scope.
Credential acquisition still needs implementation and a live acceptance test.

An approved existing server can run Docker from a systemd timer/cron job instead.
AWS scheduled ECS/Fargate tasks are another container option. Choose based on
the operator's platform access, permitted data location and maintenance budget.
Do not expose an HTTP service just to schedule a one-off batch.

## Google Workspace requirements

Have the organization create/own the destination. Grant the chosen writer edit
access to each intended workbook. A service account can use direct file sharing;
no domain-wide delegation is needed for that case. Cloud IAM permission is not
spreadsheet permission. Check external sharing rules for the service-account
address and app controls for any user OAuth integration. A browser session or
permission to share externally does not by itself provision runtime credentials.

The existing publisher only needs Sheets API operations for an initialized known
spreadsheet ID. Creating, moving or sharing Drive files is a separate workflow
and may require additional API authorization. Never request broad Drive access
merely to refresh cell values.

## Rolling windows and retained history

Before daily Sheets automation, replace the exact-window-only contract with a
deliberate feed contract. Preserve owner/workspace checks and reject stale runs.
Define the visible date range and retain/archive earlier data before replacement.
Refresh all open billing periods and define how corrections outside the lookback
are requested. Never append overlapping exports or sum repeated daily snapshots.

For a Sheets-only rollout, a rolling feed can be a working report, but it is not
a durable history unless archival and correction logic are added. For retained
history, use Postgres plus backups and a reporting adapter to refresh the master
Sheet from current database records. That adapter is not implemented yet.
Keep approved billing evidence immutable and represent later adjustments under
an agreed process; period locking is not implemented.

## Dashboard paths

For a spreadsheet dashboard, directly publish a normalized master feed and build
pivots/formulas/charts outside managed ranges. Alternatively, explicitly authorize
six bounded IMPORTRANGE connections; show each source's last refresh and accept
propagation delays. Destination editors can use an authorized import connection
to access more source ranges, so align dashboard permissions with the sources.
Choose one defined row grain: individual entry facts for aggregation, not a mix
of entries, daily totals and monthly totals that counts the same hours repeatedly.
Use stable person/project IDs and an approved account mapping; do not join by names alone.

For a future web viewer, add a read-only reporting API backed by Postgres,
company-approved authentication and explicit viewer authorization. Support
person/date/project/account filters, CSV downloads, and coverage indicators.
Do not place Asana or Google credentials in browser code. Apply the existing
CSV text-escaping policy and server-side access checks to downloads. Backups,
monitoring, dependency updates and sign-in maintenance become ongoing duties.

## Acceptance and operating ownership

Test the six-person identity map, empty days versus failed days, late edits,
owner/date moves, missing labels, replay, unseen entries, stale observations,
expired Google credentials, destination failures and overlapping executions.
Require exact source/destination entry counts and integer-minute reconciliation.
Demonstrate a recoverable interrupted run, a visible partial run, alert delivery
and database restoration where selected. Define a named operator, retention,
rotation/revocation and a monthly infrastructure budget before production use.

Platform prerequisites, token provisioning and native/container commands are in
the [operating guide](application-guide.md#9-prerequisites-and-asana-token-setup).

## References

- [Scheduled Cloud Run jobs](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)
- [Google credential and direct-sharing guidance](https://developers.google.com/workspace/guides/create-credentials)
- [Service accounts and Workspace domains](https://docs.cloud.google.com/iam/docs/service-account-overview)
- [IMPORTRANGE permissions and refresh behavior](https://support.google.com/docs/answer/3093340)
- [Validation evidence](../VALIDATION.md), [Postgres](postgres.md), [Sheets](sheets.md), [Docker](docker.md)
