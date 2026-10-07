"""Shared persistence; scoped reads are mandatory at the service boundary."""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import database
from .database import IntegrityError as IntegrityError
from .database import backend as backend
from .database import close as close
from .database import lock as lock

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ.get("AGENT_LOOM_DATA", ROOT / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "agentloom.db"


def uid():
    return uuid.uuid4().hex


def conn():
    return database.connect(DB)


def transaction(scope):
    return database.transaction(DB, scope)


def migrate(connection):
    """Apply each numbered migration once in the caller's transaction."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied DOUBLE PRECISION)"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    directory = Path(__file__).parent / "migrations"
    if backend() == "postgresql":
        directory /= "postgresql"
    for file in sorted(directory.glob("*.sql")):
        version = int(file.name.split("_", 1)[0])
        if version in applied:
            continue
        if backend() == "postgresql":
            connection.execute(file.read_text())
            connection.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, time.time()))
            continue
        statement = ""
        for line in file.read_text().splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip() and not statement.strip().startswith("--"):
            raise RuntimeError("数据库迁移包含未完成的 SQL：" + file.name)
        connection.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, time.time()))


def init():
    if backend() == "sqlite":
        with conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
    with transaction("schema-migrations") as c:
        migrate(c)
        c.execute(
            "UPDATE runs SET status=CASE WHEN id IN (SELECT run_id FROM checkpoints) THEN 'interrupted' ELSE 'failed' END,error='服务重启，运行已中断；有检查点的任务可以恢复' WHERE status IN ('queued','running')"
        )


def query(sql, args=(), one=False):
    with database.connect(DB, transactional=False) as c:
        rows = [dict(x) for x in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    with database.connect(DB, transactional=False) as c:
        return c.execute(sql, args).rowcount


def resource(rid, space, kind=None):
    r = query("SELECT * FROM resources WHERE id=? AND space_id=?", (rid, space), True)
    if not r or kind and r["kind"] != kind:
        raise ValueError("资源不存在或无权访问")
    return {"id": r["id"], "kind": r["kind"], **json.loads(r["payload"])}


def put_resource(space, kind, payload, rid=None):
    rid = rid or uid()
    execute(
        "INSERT INTO resources(id,space_id,kind,payload,created) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, created=excluded.created",
        (rid, space, kind, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    return resource(rid, space)


def list_resources(space, kind):
    return [
        {"id": r["id"], "kind": r["kind"], **json.loads(r["payload"])}
        for r in query(
            "SELECT * FROM resources WHERE space_id=? AND kind=? ORDER BY created DESC",
            (space, kind),
        )
    ]


def _insert_event(connection, run, kind, payload):
    seq = connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO events VALUES(?,?,?,?,?)",
        (run, seq, kind, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    return seq


def event(run, kind, payload):
    with transaction("run:" + run) as c:
        return _insert_event(c, run, kind, payload)


def transition(run, status, kind, payload, output="", error=None):
    """Commit terminal state and its stream event together."""
    with transaction("run:" + run) as c:
        c.execute(
            "UPDATE runs SET status=?,output=?,error=? WHERE id=?", (status, output, error, run)
        )
        return _insert_event(c, run, kind, payload)


def finalize(run, status, kind, payload, output="", error=None):
    """Retry only terminal-state persistence, safely even after an unknown commit."""
    record = query("SELECT session_id FROM runs WHERE id=?", (run,), True)
    if not record:
        return
    sid = record["session_id"] or run
    with transaction("session:" + sid) as c:
        lock(c, "run:" + run)
        row = c.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()
        if not row or row["status"] not in ("queued", "running"):
            return
        c.execute(
            "UPDATE runs SET status=?,output=?,error=? WHERE id=?", (status, output, error, run)
        )
        seq = _insert_event(c, run, kind, payload)
        append_session_message(
            c,
            sid,
            run,
            "main",
            f"terminal-{seq}",
            "status",
            {
                "role": "user",
                "content": "历史任务结果（执行状态资料）：\n"
                + json.dumps(
                    {"status": status, "output": output, "error": error}, ensure_ascii=False
                ),
            },
        )


def append_session_message(connection, sid, rid, instance, key, kind, message):
    from .security import encrypt

    existing = connection.execute(
        "SELECT seq FROM session_messages WHERE run_id=? AND instance=? AND entry_key=?",
        (rid, instance, key),
    ).fetchone()
    if existing:
        return existing["seq"]
    seq = connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM session_messages WHERE session_id=?", (sid,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO session_messages VALUES(?,?,?,?,?,?,?,?)",
        (
            sid,
            seq,
            rid,
            instance,
            key,
            kind,
            encrypt(json.dumps(message, ensure_ascii=False)),
            time.time(),
        ),
    )
    return seq
