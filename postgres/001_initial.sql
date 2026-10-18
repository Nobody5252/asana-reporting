CREATE TABLE sync_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_sha256 text NOT NULL UNIQUE CHECK (length(snapshot_sha256) = 64),
    workspace_gid text NOT NULL,
    owner_gid text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL CHECK (end_date >= start_date),
    exported_at timestamptz NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    entry_count bigint NOT NULL CHECK (entry_count >= 0),
    duration_minutes bigint NOT NULL CHECK (duration_minutes >= 0),
    manifest jsonb NOT NULL,
    project_context jsonb NOT NULL
);

CREATE TABLE owner_watermarks (
    workspace_gid text NOT NULL,
    owner_gid text NOT NULL,
    exported_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_gid, owner_gid)
);

CREATE TABLE time_entries (
    workspace_gid text NOT NULL,
    entry_gid text NOT NULL,
    owner_gid text NOT NULL,
    work_date date NOT NULL,
    duration_minutes bigint NOT NULL CHECK (duration_minutes >= 0),
    core jsonb NOT NULL,
    context jsonb NOT NULL DEFAULT '{}',
    context_observed_at timestamptz,
    visibility_status text NOT NULL DEFAULT 'present'
        CHECK (visibility_status IN ('present', 'unseen')),
    last_observed_at timestamptz NOT NULL,
    first_seen_run bigint NOT NULL REFERENCES sync_runs,
    last_seen_run bigint NOT NULL REFERENCES sync_runs,
    last_observed_run bigint NOT NULL REFERENCES sync_runs,
    PRIMARY KEY (workspace_gid, entry_gid)
);
CREATE INDEX time_entries_owner_dates ON time_entries (workspace_gid, owner_gid, work_date);

CREATE TABLE time_entry_revisions (
    revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_gid text NOT NULL,
    entry_gid text NOT NULL,
    run_id bigint NOT NULL REFERENCES sync_runs,
    observed_at timestamptz NOT NULL,
    previous_record jsonb,
    current_record jsonb NOT NULL,
    FOREIGN KEY (workspace_gid, entry_gid) REFERENCES time_entries
);
CREATE INDEX time_entry_revisions_entry ON time_entry_revisions (workspace_gid, entry_gid, revision_id);

CREATE VIEW daily_actual_time AS
SELECT workspace_gid, owner_gid, work_date,
       count(*) AS entry_count,
       sum(duration_minutes) AS duration_minutes,
       count(*) FILTER (WHERE visibility_status = 'unseen') AS unseen_entry_count,
       coalesce(sum(duration_minutes) FILTER (WHERE visibility_status = 'unseen'), 0) AS unseen_minutes
FROM time_entries
GROUP BY workspace_gid, owner_gid, work_date;
