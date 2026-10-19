# Validation and implemented stopping point

Version: 2.1.0. Updated on October 19, 2026.

Run from this project root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

**Current suite: 88 tests.** Ordinary offline runs pass 79 tests and explicitly
skip nine Postgres integration tests. GitHub CI also runs those nine tests
against a disposable real database. Both jobs passed on October 18, 2026.
See [repository Actions](https://github.com/Nobody5252/asana-reporting/actions)
for current runs and the sections below for coverage.

The original **54 exporter/runtime tests** use mocked HTTP transports and disposable local output
folders. The inherited v1 test suite is retained with the write-failure fixture
updated to provide a valid daily range for the stronger reconciliation check.
The separate v2 suite uses only invented identities, IDs and client names.

Coverage includes:

- Colleague-owned entries excluded even if created by the token owner; entries
  owned by the token owner retained when created by a colleague or logged on a
  colleague-assigned task. Missing entry assignee fails rather than guessing.
- Exact dates, one-day padded API filters, leap years, invalid dates, relative
  dates, maximum ranges and zero days/months. Minutes are summed before rounding.
- Pagination offsets/repetition, identical and conflicting duplicate IDs,
  consistency across detail reads and no use of task/parent rollup totals.
- Null/omitted tasks, optional missing task/project display names, blank original
  projects, detail 403/404, required task-name filter failures and hard failure on
  malformed data, authentication/network errors or changed core records.
- Direct and inherited project context, multiple project ambiguity, task cache,
  explicit attribution priority, inaccessible parents, missing fields, cycles,
  bounded depth and totals unaffected by enrichment. The anonymized 2h/1h/2h
  pattern resolves two context labels and retains all five hours.
- Fixed GET paths, numeric IDs, header-injection rejection, verified TLS,
  disabled proxy-environment use, no redirects, no TLS key logging, bounded
  timeouts, retries and sanitized errors. Hidden prompt echo fallback fails.
- CSV formula escaping, daily/monthly display labels and hours unit, owner/date/
  amount reconciliation, immutable snapshot names, checksums for all data files,
  no completed snapshot after failure, partial-context summary, and a guard
  against persisting credential text echoed in returned data.

Additional runtime coverage includes designated secret files (including BOM and
trailing newline), explicit-file precedence, conflicting environment sources,
empty/malformed/unreadable/oversized files, non-echoing failures and expected-owner
rejection before time collection or publication.

## Approved local live validation

The user explicitly authorized reading a designated local token file and live
GET-only API tests. The credential owner matched the preserved baseline owner.
Two v2 exports of the same historical window, with enrichment disabled/enabled,
matched the v1 entry IDs, dates, ownership, creator, original attribution, integer
minutes and daily totals. All five output checksums in each manifest verified.
Blank task/project context retained every minute. Partial context was reported
separately. Checksums confirmed v1 source and existing outputs were unchanged.

Credentials, real exports, exact business totals and the detailed reconciliation
report remain outside Git and Docker build contexts. The original private
diagnostic and its 21 offline tests remain local; those tests also passed.

## Container validation

Both Compose configurations validate locally. User-authorized elevation restored
local Docker access on this branch. The standalone and Postgres images built
locally, and a live standalone-container export reconciled with the saved baseline.
GitHub validation passed on October 18, 2026.
It built the offline-test and runtime targets, checked non-root execution and TLS
trust roots, ran a network-disabled CLI smoke test, and checked Compose.
Container packaging was first verified in GitHub CI and subsequently on the
local Docker host. Live authenticated tests never run in CI. No registry
publishing or remote application deployment is configured.

## Postgres validation

All capabilities belong together on `main` under the October 18 user
instruction. The core retains the validated collection/ownership logic and now
adds calendar-month date selection. Postgres support is a separate optional module and image,
with no new dependencies in the exporter runtime or host Python environment.

Before Sheets was added, all **69 tests passed** in the optional image against local Postgres 18:
54 original regressions, six snapshot-validation tests, and nine database
integration tests. Without Postgres, 60 tests run and nine explicitly skip.
The database tests use uniquely named disposable schemas and synthetic data.

Coverage includes repeated/overlapping snapshots, amount edits, date moves,
owner reassignment, before/after revisions, stale observations, missing-entry
flags, reappearance, uncollected versus zero days, schema-checksum protection,
context preservation/invalidation, and full rollback after a database failure.

Approved real local snapshots were then imported in source-time order into the
development database. Repeating the latest import made zero changes. Database
entry counts, integer minutes and the 30-day report reconciled with the source
exports without duplicate hours. Detailed work-data evidence stays in ignored
`local-validation/`, and reports stay in ignored `postgres-reports/`.
The development database configuration has a named persistent volume, an isolated network and no published
host ports. Production roles, backups/restore acceptance, scheduling and billing
approval remain separate work described in `docs/postgres.md`.

Core snapshot status and enrichment status are separate: a successful complete
owner/time snapshot may explicitly report partial optional context. A network or
authentication failure aborts; it does not produce a misleading complete export.
In the current CLI an enrichment network failure also aborts publication;
independent core/enrichment jobs remain future architecture work.

## Sheets integration

The suite now includes seventeen synthetic Sheets tests: integer-minute totals,
literal names and long text IDs, repeated replacement and trailing-row clearing,
scope/freshness protection, zero-entry windows, readback mismatch detection,
bounded grid growth, unrelated tabs, HTTP target/auth and error redaction.
There are now 88 tests total: 79 pass offline and nine require opt-in Postgres.
Six additional rolling-report tests cover independent month/week/day totals,
seven-day filtering, a week crossing month boundaries, incomplete source coverage,
zero days, forward-date refreshes, stale report dates and explicit v1 migration
with identity checks. Daily includes its summary plus individual entries;
Monthly now includes separate full previous-month/current month-to-date totals
and their daily breakdown. Calendar tests cover missing early-month data, leap
February, January year rollover, month-to-date-only mode, explicit layout
migration and collector argument conflicts. The collector defaults to calendar
coverage; arbitrary day counts require explicit selection.

The rolling layout was also exercised with an authorized live snapshot and the
connected Google Sheets transport. All five tabs matched the generated report,
including month-to-date daily totals, the seven-day entry list, and a zero-day
Daily summary with no stale entry rows. A separate full previous-month live pull
matched the combined calendar export's entry IDs, dates, ownership and integer
minutes. This verifies that the previous month is not truncated by a rolling
lookback. Real values, identities and file IDs
remain in ignored local evidence only.

A user-authorized native Google Sheets test imported a dedicated workbook and
applied the module's generated replacement batch twice through the connected
Drive tools. Exact four-tab readback reconciled every value with the validated
snapshot, including text IDs. Entry counts/minutes remained unchanged across
replays. All tabs were visually checked in native Sheets. Private evidence is
in ignored `local-validation/sheets-test/`. This establishes the connector
integration, not provisioned Google credentials inside Docker.

The optional Sheets image built locally and passed a network-disabled CLI smoke
test. Container preparation reproduced the exact live-tested report JSON. The
standard test image passed all 67 offline tests, with nine database tests skipped.
