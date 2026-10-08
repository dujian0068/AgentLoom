import json
import math
import re

from agentloom_runtime.provider import embedding_response as embeddings

from . import store as db
from .services import embedding


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
    from .services.knowledge_imports import import_document

    result = await import_document(space, kb, name, text, invoke=embeddings)
    return result["id"]


async def search(
    space,
    libraries,
    query,
    *,
    actor_id=None,
    run_id=None,
    embedding_operation_id=None,
    embedding_operation_ids=None,
    retry_unknown=False,
):
    if embedding_operation_id and embedding_operation_ids is not None:
        raise ValueError("不能同时提供单次与多知识库的向量化操作 ID")
    if embedding_operation_ids is not None and (
        type(embedding_operation_ids) is not dict
        or set(embedding_operation_ids) != {kb["id"] for kb in libraries if kb.get("embedding_id")}
        or any(type(value) is not str or not value for value in embedding_operation_ids.values())
        or len(set(embedding_operation_ids.values())) != len(embedding_operation_ids)
    ):
        raise ValueError("向量化操作 ID 必须与本次知识库绑定一一对应")
    allowed = []
    for kb in libraries:
        db.resource(kb["id"], space, "wiki")
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
    selected_operation = None
    if embedding_operation_id:
        selected_operation = embedding.get_operation(space, embedding_operation_id, actor_id)
        if selected_operation["purpose"] != "query" or selected_operation["kb_id"] not in {
            kb["id"] for kb in libraries
        }:
            raise ValueError("恢复的向量化操作不属于本次查询")
    for kb in libraries:
        if not kb.get("embedding_id"):
            continue
        signature = embedding.index_signature(space, kb)
        kb_rows = [row for row in rows if row["kb_id"] == kb["id"]]
        if not kb_rows:
            continue
        if any(row["vector"] and row["embedding_id"] != signature for row in kb_rows):
            raise ValueError("文档向量与知识库索引版本不一致，请重建索引")
        operation_id = (
            embedding_operation_id
            if selected_operation and selected_operation["kb_id"] == kb["id"]
            else None
        )
        if embedding_operation_ids is not None:
            operation_id = embedding_operation_ids[kb["id"]]
        vectors, _ = await embedding.embed(
            space,
            kb,
            [query],
            purpose="query",
            actor_id=actor_id,
            run_id=run_id,
            operation_id=operation_id,
            retry_unknown=retry_unknown,
            invoke=embeddings,
        )
        qvec = vectors[0]
        if kb.get("embedding_dimension") not in (None, len(qvec)):
            raise ValueError("查询向量维度与知识库索引不一致，请重建索引")
        qnorm = math.sqrt(sum(x * x for x in qvec)) or 1
        scores = []
        for row in kb_rows:
            if not row["vector"]:
                continue
            vector = json.loads(row["vector"])
            if len(vector) != len(qvec):
                raise ValueError("文档向量维度与查询不一致，请重建索引")
            cosine = sum(x * y for x, y in zip(vector, qvec)) / (
                qnorm * (math.sqrt(sum(x * x for x in vector)) or 1)
            )
            if cosine > 0:
                scores.append((cosine, row["id"]))
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
