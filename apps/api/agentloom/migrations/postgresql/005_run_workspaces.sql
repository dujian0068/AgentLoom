CREATE TABLE IF NOT EXISTS run_workspaces(run_id TEXT PRIMARY KEY, space_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL, binding TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS run_workspaces_session ON run_workspaces(space_id,agent_id,session_id);
