import json
import math
import re

from agentloom_runtime.provider import embeddings

from . import store as db
from .security import decrypt


def terms(text):
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(word[i : i + 2] for i in range(max(1, len(word) - 1)))
    return " ".join(words)


def split_text(text):
    lines = text.splitlines()
    start = 0
    chunk = []
    size = 0
    for i, line in enumerate(lines):
        if chunk and size + len(line) > 1800:
            yield start + 1, i, "\n".join(chunk)
            start = i
            chunk = []
            size = 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        yield start + 1, len(lines), "\n".join(chunk)


def embedding_signature(model):
    return model["base_url"].rstrip("/") + "|" + model["model_id"]


async def add_document(space, kb, name, text):
    if not text.strip():
        raise ValueError("文件内容为空")
    chunks = list(split_text(text))
    vectors = [None] * len(chunks)
    signature = ""
    if kb.get("embedding_id"):
        model = db.resource(kb["embedding_id"], space, "models")
        if model["purpose"] != "embedding":
            raise ValueError("请选择向量化模型连接")
        vectors = []
        for i in range(0, len(chunks), 32):
            vectors.extend(
                await embeddings(
                    model, [x[2] for x in chunks[i : i + 32]], decrypt(model["secret"])
                )
            )
        signature = embedding_signature(model)
    did = db.uid()
    with db.transaction("resource:" + kb["id"]) as c:
        current = c.execute(
            "SELECT payload FROM resources WHERE id=? AND space_id=? AND kind='wiki'",
            (kb["id"], space),
        ).fetchone()
        if current is None:
            raise ValueError("知识库不存在或无权访问")
        payload = json.loads(current["payload"])
        revision = payload.get("revision", 0) + 1
        c.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,0)",
            (did, kb["id"], space, name, text, revision),
        )
        for (start, end, content), vec in zip(chunks, vectors):
            cid = db.uid()
            c.execute(
                "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    did,
                    kb["id"],
                    space,
                    name,
                    content,
                    start,
                    end,
                    revision,
                    json.dumps(vec) if vec else None,
                    signature,
                ),
            )
            c.execute("INSERT INTO chunk_fts VALUES(?,?)", (cid, terms(content)))
        payload["revision"] = revision
        payload["status"] = "ready"
        payload["retrieval"] = "hybrid" if signature else "keyword"
        c.execute(
            "UPDATE resources SET payload=? WHERE id=? AND space_id=?",
            (json.dumps(payload), kb["id"], space),
        )
    return did


async def search(space, libraries, query):
    allowed = []
    for kb in libraries:
        docs = kb.get("documents")
        if docs is None:
            docs = [
                d["id"]
                for d in db.query(
                    "SELECT id FROM documents WHERE kb_id=? AND space_id=? AND deleted=0",
                    (kb["id"], space),
                )
            ]
        allowed.extend(docs)
    if not allowed:
        return []
    params = ",".join("?" for _ in allowed)
    rows = db.query(
        f"SELECT c.* FROM chunks c JOIN documents d ON c.document_id=d.id WHERE c.space_id=? AND c.document_id IN ({params}) AND d.deleted=0",
        (space, *allowed),
    )
    ids = {r["id"] for r in rows}
    rank = {}
    tokens = list(dict.fromkeys(terms(query).split()))[:30]
    if tokens:
        if db.backend() == "postgresql":
            # PostgreSQL splits SQL identifiers at underscores. Preserve the
            # adjacent-word matching used by SQLite's quoted FTS5 phrases.
            clauses = []
            for token in tokens:
                parts = [part for part in token.split("_") if part]
                if parts:
                    clauses.append("(" + " <-> ".join("'" + x + "'" for x in parts) + ")")
            match = " | ".join(clauses)
            hits = (
                db.query(
                    f"""SELECT f.id, ts_rank_cd(to_tsvector('simple', f.terms), q.query) AS score
                    FROM chunk_fts f
                    CROSS JOIN to_tsquery('simple', ?) AS q(query)
                    JOIN chunks c ON c.id=f.id
                    JOIN documents d ON c.document_id=d.id
                    WHERE to_tsvector('simple', f.terms) @@ q.query
                      AND c.space_id=? AND c.document_id IN ({params}) AND d.deleted=0
                    ORDER BY score DESC, f.id LIMIT 50""",
                    (match, space, *allowed),
                )
                if match
                else []
            )
        else:
            match = " OR ".join('"' + x + '"' for x in tokens)
            hits = db.query(
                f"SELECT f.id,bm25(chunk_fts) AS score FROM chunk_fts f JOIN chunks c ON c.id=f.id JOIN documents d ON c.document_id=d.id WHERE chunk_fts MATCH ? AND c.space_id=? AND c.document_id IN ({params}) AND d.deleted=0 ORDER BY score LIMIT 50",
                (match, space, *allowed),
            )
        for i, h in enumerate(hits):
            rank[h["id"]] = 1 / (60 + i + 1)
    vector_groups = {}
    for kb in libraries:
        if kb.get("embedding_id"):
            model = db.resource(kb["embedding_id"], space, "models")
            vector_groups[embedding_signature(model)] = model
    for signature, model in vector_groups.items():
        qvec = (await embeddings(model, [query], decrypt(model["secret"])))[0]
        qnorm = math.sqrt(sum(x * x for x in qvec)) or 1
        scores = []
        for r in rows:
            if not r["vector"] or r["embedding_id"] != signature:
                continue
            v = json.loads(r["vector"])
            if len(v) != len(qvec):
                continue
            cos = sum(x * y for x, y in zip(v, qvec)) / (
                qnorm * (math.sqrt(sum(x * x for x in v)) or 1)
            )
            if cos > 0:
                scores.append((cos, r["id"]))
        for i, (_, cid) in enumerate(sorted(scores, reverse=True)[:30]):
            rank[cid] = rank.get(cid, 0) + 1 / (60 + i + 1)
    lookup = {r["id"]: r for r in rows}
    return [
        {
            "id": cid,
            "document_id": lookup[cid]["document_id"],
            "file": lookup[cid]["name"],
            "start_line": lookup[cid]["start_line"],
            "end_line": lookup[cid]["end_line"],
            "content": lookup[cid]["content"],
            "score": score,
        }
        for cid, score in sorted(rank.items(), key=lambda x: x[1], reverse=True)[:6]
        if cid in ids
    ]
