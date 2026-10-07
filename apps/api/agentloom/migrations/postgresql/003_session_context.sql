CREATE TABLE IF NOT EXISTS session_messages(session_id TEXT, seq BIGINT, run_id TEXT, instance TEXT, entry_key TEXT, kind TEXT, payload TEXT, created DOUBLE PRECISION, PRIMARY KEY(session_id,seq), UNIQUE(run_id,instance,entry_key));
CREATE INDEX IF NOT EXISTS session_messages_run ON session_messages(run_id,instance);
CREATE TABLE IF NOT EXISTS session_views(session_id TEXT, run_id TEXT, watermark BIGINT, payload TEXT, created DOUBLE PRECISION, PRIMARY KEY(session_id,run_id,watermark));
CREATE INDEX IF NOT EXISTS session_views_watermark ON session_views(session_id,watermark);
