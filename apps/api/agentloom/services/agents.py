"""Agents service functions."""

import json

from agentloom_runtime.budget import normalized_profile
from fastapi import HTTPException

from agentloom import store as db
from agentloom.services.hooks import published_hook_manifest


def get_agent(aid, space):
    row = db.query("SELECT * FROM agents WHERE id=? AND space_id=?", (aid, space), True)
    if not row:
        raise HTTPException(404, "Agent 不存在")
    return {"id": row["id"], "published": row["latest"], **json.loads(row["config"])}


def snapshot(config, space):
    model = db.resource(config["model"], space, "models")
    if model["purpose"] != "chat":
        raise ValueError("Agent 必须选择聊天模型")
    if "context_policy" in config:
        # Freeze effective defaults when reusing a model created before budgets existed.
        model.update(normalized_profile(model))
    result = {
        "config": config,
        "model_obj": model,
        "hook_manifest": published_hook_manifest(config),
    }
    for kind in ("skills", "tools", "wiki"):
        ids = set(config[kind])
        for child in config["subs"]:
            ids.update(child[kind])
        result[kind] = []
        for rid in ids:
            value = db.resource(rid, space, kind)
            if kind == "tools" and value["status"] != "ready":
                raise ValueError("工具尚未就绪：" + value["name"])
            if kind == "wiki":
                value["documents"] = [
                    x["id"]
                    for x in db.query(
                        "SELECT id FROM documents WHERE kb_id=? AND space_id=? AND deleted=0",
                        (rid, space),
                    )
                ]
                if not value["documents"]:
                    raise ValueError("知识库没有可检索文档：" + value["name"])
            result[kind].append(value)
    if len(set(x["id"] for x in config["subs"])) != len(config["subs"]):
        raise ValueError("子 Agent ID 重复")
    return result
