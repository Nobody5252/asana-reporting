# Google Sheets output

`asana_sheets.py` reads a completed, unfiltered exporter snapshot. It uses the
same checksum, ownership and integer-minute reconciliation validator as optional
Postgres storage, without importing a database driver or contacting Asana.
The original exporter and its dependency-free runtime remain unchanged.

The dedicated reporting workbook has four managed tabs:

- Daily: all dates in the inclusive export window, including zero days.
- Monthly: totals **within the export window**, not necessarily full months.
- Entries: individual owner entries, minutes, hours, optional context and IDs.
- Status: owner/workspace/window, snapshot digest, source timestamp and totals.

Unavailable project/task names never remove minutes. Dates use the Asana work
date as a numeric spreadsheet date. IDs and external text use explicit string
values; uploaded text cannot execute as formulas. Hours derive from minutes.

## Preparation and first creation

```sh
mkdir -p local-validation/sheets
python3 asana_sheets.py /path/to/completed-snapshot \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --output local-validation/sheets/report.json
```

The JSON contains private work data. Keep it in an ignored local folder.
`--output` creates a new file exclusively, rather than overwriting existing data.

For the first live test, the report was converted to a styled workbook and
imported into a new native Google Sheet in the connected user's ChatGPT folder.
The connected Google Drive tools then applied `checked_batch()` requests from
this module, twice, and `verify()` compared all four tabs to the source report.
The live data, workbook, target ID and readback evidence remain ignored locally.
Native Sheets visual verification covered all four tabs. Sharing was not widened.

First creation uses the connected spreadsheet workflow. This CLI refreshes an
already initialized reporting workbook; it deliberately refuses arbitrary blank
or unrelated workbooks. It does not create or share Drive files.

## Refresh from a script or container

Supply a Google OAuth access token explicitly in a local secret file. It must
have Sheets write scope and permission to edit the destination workbook. An
Asana personal token cannot authenticate to Google. The Google connection in
Codex does not provide credentials to the container.

```sh
python3 asana_sheets.py /path/to/completed-snapshot \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --spreadsheet-id GOOGLE_SPREADSHEET_ID \
  --access-token-file /path/to/google-access-token

docker build -f Dockerfile.sheets -t asana-reporting:sheets .
docker run --rm --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user "$(id -u):$(id -g)" \
  -v /absolute/completed-snapshot:/snapshot:ro \
  -v /absolute/google-access-token:/run/secrets/google-token:ro \
  asana-reporting:sheets /snapshot \
  --expected-owner OWNER_GID --workspace WORKSPACE_GID \
  --spreadsheet-id GOOGLE_SPREADSHEET_ID \
  --access-token-file /run/secrets/google-token
```

The token is never accepted on the command line, printed, or baked into an
image. Requests use verified TLS, no redirects, no environment proxy and no
TLS key logging. Errors omit HTTP bodies. The short-lived token is not renewed
by this module. A production service identity or OAuth refresh integration and
scheduling remain to be configured; neither was provisioned by this test.
The standalone HTTP transport is covered offline; the authenticated live test
used the connected Drive transport with the same generated replacement payload.

## Refresh contract

Each workbook is dedicated to one owner, workspace and exact date window.
Refresh rejects scope changes, older observations and conflicting snapshots
with the same timestamp. To change the window, create another dedicated report
until a rolling-window/multi-owner policy is implemented.

One atomic Sheets batch replaces values in the four managed tab ranges and
clears trailing rows. Replays do not append entries or hours. Other tabs and
formatting are preserved. Do not put manual notes inside these managed tabs.
Use a separate tab for analysis or notes. Larger refreshes grow the grid as
needed and remain bounded to 100,000 rows and one million cells per managed tab.

Use one writer at a time: the freshness read and batch write are not a lock or
compare-and-swap. After a timeout, read back before retrying. Successful CLI
publication includes an exact four-tab readback. A readback failure can mean
the write succeeded and verification failed; it does not imply rollback.

For six owners, aggregate validated owner data in Postgres or define a separate
workbook/feed per owner. Do not point six independent writers at the same four
tabs. This first integration is a snapshot report, not a historical database.

API behavior: [Sheets updateCells](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#UpdateCellsRequest).
