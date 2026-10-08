CREATE TABLE IF NOT EXISTS embedding_operations(id TEXT PRIMARY KEY, space_id TEXT NOT NULL, kb_id TEXT NOT NULL, actor_id TEXT, run_id TEXT, purpose TEXT NOT NULL, index_signature TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS embedding_operations_scope ON embedding_operations(space_id,kb_id,created);
CREATE TABLE IF NOT EXISTS knowledge_imports(id TEXT PRIMARY KEY, space_id TEXT NOT NULL, kb_id TEXT NOT NULL, actor_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_imports_scope ON knowledge_imports(space_id,kb_id,created);
