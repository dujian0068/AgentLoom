"""Ordered source journal and reusable model views; caller holds the session lock.

Source records are append-only. A view can cover only an observed ordered prefix,
so resuming an old Run cannot replace history that it never inherited.
"""

import json
import time
from copy import deepcopy

from agentloom import store as db
from agentloom.security import decrypt, encrypt

append = db.append_session_message


def sync_records(connection, sid, rid, state, redact=lambda value: value):
    for instance, frame in state.get("frames", {}).items():
        for record in frame.get("context", {}).get("records", []):
            append(
                connection,
                sid,
                rid,
                instance,
                str(record["seq"]),
                record["kind"],
                redact(record["message"]),
            )


def save_checkpoint(rid, state, redact=lambda value: value):
    row = db.query("SELECT session_id FROM runs WHERE id=?", (rid,), True)
    if not row:
        raise ValueError("运行不存在")
    sid = row["session_id"]
    with db.transaction("session:" + sid) as c:
        db.lock(c, "run:" + rid)
        sync_records(c, sid, rid, state, redact)
        c.execute(
            "INSERT INTO checkpoints(run_id,payload,updated) VALUES(?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
            (rid, encrypt(json.dumps(state, ensure_ascii=False)), time.time()),
        )
        save_view(c, sid, rid, state, redact)


def save_view(connection, sid, rid, state, redact=lambda value: value):
    frame = state.get("frames", {}).get("main", {})
    context = frame.get("context", {})
    if not context.get("compactions") or frame.get("pending") or context.get("pending_inputs"):
        return
    inherited = context.get("history_metadata", {}).get("watermark", 0)
    rows = connection.execute(
        "SELECT seq,run_id FROM session_messages WHERE session_id=? AND instance='main' ORDER BY seq",
        (sid,),
    ).fetchall()
    observed = [r["seq"] for r in rows if r["seq"] <= inherited or r["run_id"] == rid]
    if not observed or observed != [r["seq"] for r in rows if r["seq"] <= max(observed)]:
        return
    watermark = max(observed)
    # Status journal items are outside the Runtime view; do not claim they are covered.
    source_keys = connection.execute(
        "SELECT entry_key FROM session_messages WHERE run_id=? AND instance='main' AND seq>?",
        (rid, inherited),
    ).fetchall()
    if any(not r["entry_key"].isdigit() for r in source_keys):
        return
    payload = {
        "messages": redact(frame["messages"][1:]),
        "baseline_user_turns": context.get("baseline_user_turns", 0),
        "baseline_model_steps": context.get("baseline_model_steps", 0),
        "revision": context["compactions"][-1]["revision"],
    }
    connection.execute(
        "INSERT INTO session_views VALUES(?,?,?,?,?) "
        "ON CONFLICT(session_id,run_id,watermark) DO NOTHING",
        (sid, rid, watermark, encrypt(json.dumps(payload, ensure_ascii=False)), time.time()),
    )


def _adopt_legacy(connection, sid):
    """Adopt surviving legacy records once; never invent already discarded history."""
    runs = connection.execute(
        "SELECT r.*,c.payload AS checkpoint FROM runs r LEFT JOIN checkpoints c ON c.run_id=r.id "
        "WHERE r.session_id=? AND NOT EXISTS(SELECT 1 FROM session_messages m WHERE m.run_id=r.id) "
        "ORDER BY r.created,r.id",
        (sid,),
    ).fetchall()
    from agentloom_runtime.context_manager import JournalContextManager

    for run in runs:
        state = json.loads(decrypt(run["checkpoint"])) if run["checkpoint"] else {}
        frame = state.get("frames", {}).get("main")
        if frame:
            context = JournalContextManager().restore(
                frame["messages"], frame.get("context"), frame["task"]
            )
            frame["context"] = context.state
            sync_records(connection, sid, run["id"], state)
            connection.execute(
                "UPDATE checkpoints SET payload=? WHERE run_id=?",
                (encrypt(json.dumps(state, ensure_ascii=False)), run["id"]),
            )
        else:
            append(
                connection,
                sid,
                run["id"],
                "main",
                "1",
                "user_input",
                {"role": "user", "content": run["input"]},
            )
        if run["output"]:
            append(
                connection,
                sid,
                run["id"],
                "main",
                "legacy-output",
                "status",
                {"role": "assistant", "content": run["output"]},
            )
        append(
            connection,
            sid,
            run["id"],
            "main",
            "legacy-status",
            "status",
            {
                "role": "user",
                "content": f"历史任务状态：{run['status']}。此任务升级前的过程记录可能不完整。",
            },
        )


def source_message(row):
    message = json.loads(decrypt(row["payload"]))
    scope = row["run_id"] + ":" + row["instance"] + ":"
    for call in message.get("tool_calls") or []:
        call["id"] = scope + call["id"]
    if message.get("role") == "tool":
        message["tool_call_id"] = scope + message["tool_call_id"]
    return message


def project(messages):
    """Close historical pending groups without executing them; renumber call IDs.

    Late results after an interrupted group are evidence text, never executable calls.
    The original journal remains unchanged.
    """
    result, pending = [], {}

    def close_pending():
        for old_id, new_id in pending.items():
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": new_id,
                    "content": json.dumps(
                        {
                            "status": "unknown",
                            "message": "历史工具调用未保存完整结果，不可推定成功，也不会自动重放。",
                            "original_call_id": old_id,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        pending.clear()

    for ordinal, source in enumerate(messages):
        message = deepcopy(source)
        if message.get("role") == "tool":
            original = message.get("tool_call_id")
            if original in pending:
                message["tool_call_id"] = pending.pop(original)
                result.append(message)
            else:
                result.append(
                    {
                        "role": "user",
                        "content": "历史工具返回资料（先前调用已中断或缺失）：\n"
                        + json.dumps(message, ensure_ascii=False),
                    }
                )
            continue
        close_pending()
        if message.get("tool_calls"):
            for index, call in enumerate(message["tool_calls"]):
                original = call["id"]
                call["id"] = f"history_{ordinal}_{index}"
                pending[original] = call["id"]
        if message.get("role") != "system":
            result.append(message)
    close_pending()
    return result


def inherit(connection, sid):
    _adopt_legacy(connection, sid)
    rows = connection.execute(
        "SELECT * FROM session_messages WHERE session_id=? AND instance='main' ORDER BY seq",
        (sid,),
    ).fetchall()
    metadata = {
        "watermark": rows[-1]["seq"] if rows else 0,
        "user_turns": sum(r["kind"] == "user_input" for r in rows),
        "model_steps": sum(r["kind"] == "model" for r in rows),
        "baseline_user_turns": 0,
        "baseline_model_steps": 0,
    }
    view = connection.execute(
        "SELECT * FROM session_views WHERE session_id=? AND watermark<=? ORDER BY watermark DESC LIMIT 1",
        (sid, metadata["watermark"]),
    ).fetchone()
    messages, after = [], 0
    if view:
        payload = json.loads(decrypt(view["payload"]))
        messages, after = payload["messages"], view["watermark"]
        metadata.update(
            {key: payload[key] for key in ("baseline_user_turns", "baseline_model_steps")}
        )
        metadata["view"] = {
            "run_id": view["run_id"],
            "watermark": after,
            "revision": payload["revision"],
        }
    messages.extend(source_message(r) for r in rows if r["seq"] > after)
    return project(messages), metadata


def read_messages(sid, user, after=0, limit=100, instance=None):
    from fastapi import HTTPException

    if not db.query(
        "SELECT id FROM sessions WHERE id=? AND space_id=? AND user_id=?",
        (sid, user["space_id"], user["user_id"]),
        True,
    ):
        raise HTTPException(404, "会话不存在")
    if after < 0 or not 1 <= limit <= 200:
        raise ValueError("消息游标或分页大小不合法")
    sql = "SELECT * FROM session_messages WHERE session_id=? AND seq>?"
    args = [sid, after]
    if instance is not None:
        sql += " AND instance=?"
        args.append(instance)
    rows = db.query(sql + " ORDER BY seq LIMIT ?", [*args, limit])
    return {
        "items": [
            {
                "seq": r["seq"],
                "run_id": r["run_id"],
                "instance": r["instance"],
                "kind": r["kind"],
                "message": json.loads(decrypt(r["payload"])),
            }
            for r in rows
        ],
        "next_after": rows[-1]["seq"] if rows else after,
    }
