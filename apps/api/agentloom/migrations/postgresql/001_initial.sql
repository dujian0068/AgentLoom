-- PostgreSQL schema. Text embeddings keep the deployment independent of pgvector.
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, password TEXT);
CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY, name TEXT, invite TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS members(user_id TEXT, space_id TEXT, role TEXT, PRIMARY KEY(user_id,space_id));
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id TEXT, space_id TEXT, kind TEXT, name TEXT, expires DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, space_id TEXT, kind TEXT, payload TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, space_id TEXT, config TEXT, latest INTEGER DEFAULT 0, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS versions(agent_id TEXT, version INTEGER, snapshot TEXT, created DOUBLE PRECISION, PRIMARY KEY(agent_id,version));
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, kb_id TEXT, space_id TEXT, name TEXT, content TEXT, revision INTEGER, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT, kb_id TEXT, space_id TEXT, name TEXT, content TEXT, start_line INTEGER, end_line INTEGER, revision INTEGER, vector TEXT, embedding_id TEXT);
CREATE TABLE IF NOT EXISTS chunk_fts(id TEXT PRIMARY KEY, terms TEXT);
CREATE INDEX IF NOT EXISTS chunk_fts_terms_gin ON chunk_fts USING GIN (to_tsvector('simple', terms));
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, space_id TEXT, user_id TEXT, agent_id TEXT, version INTEGER);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, space_id TEXT, user_id TEXT, agent_id TEXT, version INTEGER, session_id TEXT, input TEXT, output TEXT DEFAULT '', status TEXT, error TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS events(run_id TEXT, seq INTEGER, kind TEXT, payload TEXT, created DOUBLE PRECISION, PRIMARY KEY(run_id,seq));
