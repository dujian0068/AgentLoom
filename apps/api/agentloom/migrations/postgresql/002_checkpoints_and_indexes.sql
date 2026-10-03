CREATE TABLE IF NOT EXISTS checkpoints(run_id TEXT PRIMARY KEY, payload TEXT, updated DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS runs_space_status ON runs(space_id, status);
CREATE INDEX IF NOT EXISTS runs_session_created ON runs(session_id, created);
CREATE INDEX IF NOT EXISTS resources_space_kind ON resources(space_id, kind);
CREATE INDEX IF NOT EXISTS documents_library_space ON documents(kb_id, space_id, deleted);
