"""Temporary UI QA server. Never run this against user data."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DB_BACKEND"] = "sqlite"
os.environ["AGENT_LOOM_DATA"] = str(ROOT / "work/qa")
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "packages/runtime"))
import uvicorn
from agentloom.app import app
from agentloom_runtime import provider
from fastapi.testclient import TestClient


def action(name, args):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "qa-call",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


async def fixture_model(model, messages, tools, secret):
    text = json.dumps(messages, ensure_ascii=False)
    waiting = "等待项目名" in text and "项目叫织点" not in text
    if "TASK_COMPLETION_REVIEW" in messages[0]["content"]:
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "decision": "blocked" if waiting else "complete",
                    "reason": "需要项目名" if waiting else "文件已读回验证",
                    "next_action": "补充项目名" if waiting else "",
                }
            ),
        }
    if "CONTEXT_COMPACTION" in messages[0]["content"]:
        return {"role": "assistant", "content": "保留当前任务与已经生成的 ui-report.md。"}
    results = [json.loads(x["content"]) for x in messages if x["role"] == "tool"]
    plans = [x["steps"] for x in results if isinstance(x, dict) and "steps" in x]
    if "本任务采用 Plan 策略" in messages[0]["content"] and not plans:
        return action(
            "update_plan",
            {
                "steps": [{"id": "report", "step": "生成报告并验证", "status": "in_progress"}],
                "explanation": "开始处理任务",
            },
        )
    if waiting:
        return {"role": "assistant", "content": "请补充项目名。"}
    if not any(isinstance(x, dict) and x.get("file") == "ui-report.md" for x in results):
        return action(
            "workspace_write",
            {"path": "ui-report.md", "content": "# UI 测试报告\n这是替身模型的验证产物。"},
        )
    if not any(isinstance(x, str) and "UI 测试报告" in x for x in results):
        return action("workspace_read", {"path": "ui-report.md"})
    if plans and plans[-1][0]["status"] != "completed":
        return action(
            "update_plan",
            {
                "steps": [{"id": "report", "step": "生成报告并验证", "status": "completed"}],
                "explanation": "文件已生成并读回验证",
            },
        )
    return {
        "role": "assistant",
        "content": "这是 UI 集成测试输出。已生成并读回 ui-report.md；真实供应商需要配置 Key 后联调。",
    }


provider.chat = fixture_model
with TestClient(app) as client:
    if client.get("/api/auth/status").json()["needs_setup"]:
        client.post(
            "/api/auth/setup",
            json={"email": "qa@example.test", "name": "测试成员", "password": "qa-local-only-2026"},
        )
    else:
        client.post(
            "/api/auth/login", json={"email": "qa@example.test", "password": "qa-local-only-2026"}
        )
    if not client.get("/api/resources/models").json():
        client.post(
            "/api/models",
            json={
                "name": "集成测试模型",
                "provider": "openai",
                "model_id": "qa-fixture",
                "base_url": "https://example.test/v1",
                "api_key": "qa-dummy-key",
                "purpose": "chat",
            },
        )
uvicorn.run(app, host="127.0.0.1", port=8877)
