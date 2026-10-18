# Docker usage

Run from the repository root. Docker Engine and Compose v2 must already be
installed and accessible to your user. This is a one-shot CLI job, not a web
service. There are no ports, background schedules or bundled databases.

## Build and test

```sh
docker build --target test -t asana-reporting:test .
docker build --target runtime -t asana-reporting:2.1.0 .
docker run --rm --network none asana-reporting:2.1.0 --help
```

The official Python base is pinned by multi-architecture digest. Update both its
tag and digest for maintenance, then rerun CI. Builds never need an Asana secret.
The build-context allowlist includes only source, build recipes, pinned optional
dependencies, schema and synthetic tests. The standalone runtime image contains
only the exporter, with no tests, database driver or private documents. Postgres
uses its own Dockerfile and Compose file; see [Postgres usage](postgres.md).

## Compose (Linux)

Prepare an existing approved token file containing only your token. Keep it
outside the checkout, restrict it to your account, and never paste it into a
command or `.env` file. Set these variables to paths/IDs, not token contents:

```sh
export ASANA_TOKEN_FILE=/absolute/approved/path/asana.token
export LOCAL_UID=$(id -u)
export LOCAL_GID=$(id -g)
mkdir -p asana-exports-v2
docker compose build
docker compose run --rm exporter --list-workspaces
docker compose run --rm exporter --workspace WORKSPACE_GID --expected-owner OWNER_GID --start 2026-10-01 --end 2026-10-18 --include-tasks
```

Replace the uppercase GID placeholders with numeric IDs. Compose mounts the
token as `/run/secrets/asana_token`; the exporter reads it through
`ASANA_ACCESS_TOKEN_FILE`. Compose file-backed secrets are local bind mounts,
not an encrypted secret store. Protect the host file and its backups.

Compose uses your supplied UID/GID so it can read an owner-only token file and
write reports into the prepared output directory. The defaults are 1000:1000;
override them as shown when necessary. Desktop file-sharing permissions on
Windows/macOS may need platform-specific configuration. Use absolute paths and
explicit dates when moving between machines.

The container filesystem is read-only except for `/data`, all capabilities are
dropped, and privilege escalation is disabled. The output remains on the host
under `asana-exports-v2/` after the container exits. There is no restart policy
or application retry loop beyond the exporter's bounded HTTP retries.

## Docker without Compose

```sh
mkdir -p asana-exports-v2
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$ASANA_TOKEN_FILE,dst=/run/secrets/asana_token,readonly" \
  --mount "type=bind,src=$PWD/asana-exports-v2,dst=/data" \
  asana-reporting:2.1.0 \
  --token-file /run/secrets/asana_token --non-interactive --output /data \
  --workspace WORKSPACE_GID --expected-owner OWNER_GID \
  --start 2026-10-01 --end 2026-10-18
```

The image's default UID/GID is 10001:10001; overriding the runtime user aligns
permissions with existing host files. Omitting task enrichment collects core
time with fewer requests. TLS verification stays enabled; a company-specific
CA/proxy design is separate work. The image sets `TZ=America/El_Salvador`;
explicit `--start` and `--end` make reporting windows portable.

## Ship an image archive

```sh
docker save asana-reporting:2.1.0 -o asana-reporting-2.1.0.tar
# On another machine with the same architecture:
docker load -i asana-reporting-2.1.0.tar
```

Only ship the built image/source, not the local token or export directories.
Cross-architecture images require a build for the destination architecture.
No registry publishing or infrastructure deployment is configured.

## CI

The GitHub workflow runs the offline regressions, builds the test/runtime
targets, checks non-root execution and certificate availability, and validates
Compose. Its container smoke tests disable networking and use no secrets.
Successful CI confirms container packaging, not live account access.
