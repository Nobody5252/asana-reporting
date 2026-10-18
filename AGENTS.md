# Asana Reporting project instructions

- Read README.md and VALIDATION.md before continuing. If the local private
  HANDOFF.md exists, read it too; it is intentionally excluded from Git.
- Keep credentials, real exports, diagnostic reports, private handoffs and
  organization-specific context out of Git and distribution artifacts.
- Keep v1 and all existing outputs unchanged. This directory is v2 development.
- Own time is individual time-entry assignee == token owner. Preserve creator
  separately. Never include a colleague's entries based on task/project ownership.
- Count owner entries even without task/project labels. Enrichment never supplies
  minutes; integer entry minutes are the only time source. Never force chart totals.
- Never search email, browser storage, password managers or unrelated files for
  credentials. Use only a secret source explicitly designated by the user.
  Live tests require explicit user authorization. Never print, commit, upload or
  bake credentials into images. Keep live outputs local and excluded from Git.
- Keep verified TLS, GET-only fixed-host API requests, no redirects or secret logs.
- Use mocked transports and synthetic fixtures for automated tests; do not copy
  real work data into fixtures. CI must run without live credentials or API calls.
  Docker packaging is supported; hosting, scheduling and infrastructure remain
  separate work requiring user authorization.
- Parent/project context is current, optional, bounded and cached. Preserve raw
  attribution separately. Flag ambiguity/permissions without inventing clients.
- Save complete snapshots atomically with checksums and reconciliation. Failed
  pulls cannot stand in for complete data or imply deletion in future storage.
- Main contains the collector and optional Postgres/Sheets functionality together,
  as requested by the user on October 18, 2026. Keep the core exporter dependency-
  free; optional integrations must not require a database or Google credentials
  for CSV-only use. Update the overview and runbooks when behavior changes.
  Do not publish container images unless requested. Keep private notes local.
