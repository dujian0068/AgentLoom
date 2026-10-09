"""Host-owned Hook envelopes are strict without rewriting historical contracts."""

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict

import pytest
from agentloom_runtime.hooks import HookFailed, HookManager, HookPoint, HookPointRegistry
from agentloom_runtime.hooks.registry import builtin_points
from jsonschema import Draft202012Validator

PAYLOADS = {
    "model.chat.before": {
        "messages": [{"role": "user", "content": "task"}],
        "tools": [],
        "model_id": "chat-model",
        "purpose": "action",
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
    },
    "model.chat.after": {"role": "assistant", "content": "result"},
    "model.embedding.before": {
        "texts": [{"index": 0, "text": "document"}],
        "model_id": "embedding-model",
        "index_signature": "index-1",
        "dimensions": None,
        "encoding_format": "float",
        "purpose": "document",
    },
    "model.embedding.after": {"vectors": [[0.0, 1.0]], "indices": [0], "dimension": 2},
    "tool.before": {
        "name": "workspace_read",
        "tool_id": "workspace_read",
        "implementation_id": None,
        "parameters": {"type": "object"},
        "arguments": {"path": "task.txt"},
    },
    "tool.after": {"value": "result", "status": "succeeded", "evidence_arguments": {}},
    "context.prepare.before": {
        "task": "task",
        "plan": [],
        "messages": [],
        "tools": [],
        "additional_messages": [],
    },
    "context.prepare.after": {"messages": [], "metrics": None},
    "context.compact.before": {
        "task": "task",
        "plan": [],
        "messages": [],
        "tools": [],
        "counters": {},
    },
    "context.compact.after": {"compacted": False, "summary": None},
    "run.before": {"task": "task"},
    "run.after": {"output": "result", "status": "succeeded", "outcome": "complete"},
    "subagent.before": {"task": "child task"},
    "subagent.after": {"output": "result", "status": "blocked", "outcome": "blocked"},
    "operation.error": {
        "kind": "tool",
        "stage": "executing",
        "actual_status": "unknown",
        "error": {"type": "operation_failed", "stage": "executing"},
        "completed_before": 1,
        "completed_after": 0,
    },
    "operation.finally": {
        "kind": "tool",
        "stage": "completed",
        "actual_status": "succeeded",
        "error": None,
        "completed_before": 1,
        "completed_after": 1,
    },
}


def validator(point):
    return Draft202012Validator(HookPointRegistry().resolve(point).schema)


@pytest.mark.parametrize("point", PAYLOADS)
def test_current_boundary_envelopes_require_fields_and_reject_unknown_fields(point):
    contract = validator(point)
    payload = PAYLOADS[point]
    assert contract.is_valid(payload)
    assert not contract.is_valid({})
    assert not contract.is_valid({**payload, "unknown_field": "not allowed"})
    for required in contract.schema["required"]:
        incomplete = {key: value for key, value in payload.items() if key != required}
        assert not contract.is_valid(incomplete), (point, required)


def test_chat_reply_allows_text_or_calls_and_optional_metadata():
    contract = validator("model.chat.after")
    calls = [{"id": "call-1", "function": {"name": "search", "arguments": "{}"}}]
    for payload in (
        {"role": "assistant", "content": "result"},
        {"role": "assistant", "tool_calls": calls},
        {"role": "assistant", "content": None, "tool_calls": calls},
    ):
        assert contract.is_valid(payload)
        assert contract.is_valid({**payload, "usage": None, "annotations": {"source": "hook"}})
    assert not contract.is_valid({"role": "assistant"})
    assert not contract.is_valid({"content": "result"})


def test_compaction_schema_distinguishes_skipped_and_completed_results():
    contract = validator("context.compact.after")
    skipped = {"compacted": False, "summary": None}
    completed = {
        "compacted": True,
        "summary": "summary",
        "messages": [{"role": "user"}],
        "metrics": {},
        "summary_slot": {},
    }
    assert contract.is_valid(skipped)
    assert contract.is_valid(completed)
    assert contract.is_valid({**completed, "summary": None, "summary_slot": None})
    for field in ("messages", "metrics", "summary_slot"):
        assert not contract.is_valid(
            {key: value for key, value in completed.items() if key != field}
        )
        assert not contract.is_valid({**skipped, field: completed[field]})
    assert not contract.is_valid({**skipped, "summary": "created without compaction"})


@pytest.mark.parametrize("point", ("operation.error", "operation.finally"))
def test_diagnostics_match_actual_payload_without_invented_operation_or_status(point):
    contract = validator(point)
    assert contract.is_valid(PAYLOADS[point])
    assert not contract.is_valid({**PAYLOADS[point], "operation": "invented"})
    assert not contract.is_valid({**PAYLOADS[point], "status": "succeeded"})


def test_legacy_contracts_are_reconstructed_exactly_by_version():
    contracts = [asdict(point) for point in builtin_points(contract_version=1)]
    encoded = json.dumps(contracts, sort_keys=True, separators=(",", ":")).encode()
    # Recorded from the pre-versioning implementation, including all schemas and
    # HookPoint fields used by published/checkpoint chain identities.
    assert hashlib.sha256(encoded).hexdigest() == (
        "387278600f195746e6a537476e708aca9557c7cbc569e6fc4f17578b5f26c06b"
    )
    legacy = HookPointRegistry(contract_version=1)
    assert legacy.contract_version == 1
    assert legacy.snapshot().contract_version == 1
    assert [asdict(legacy.resolve(point["name"])) for point in contracts] == contracts
    assert Draft202012Validator(legacy.resolve("tool.before").schema).is_valid({})


@pytest.mark.parametrize("version", [None, True, False, 0, 3, "2", {}, []])
def test_only_known_host_contract_versions_can_be_selected(version):
    for constructor in (HookPointRegistry, builtin_points):
        with pytest.raises(ValueError, match="contract version"):
            constructor(contract_version=version)


def test_snapshot_keeps_host_custom_points_and_detaches_schemas():
    registry = HookPointRegistry()
    assert registry.contract_version == 2
    custom_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    registry.register(HookPoint("custom.before", "before", schema=custom_schema))
    snapshot = registry.snapshot()
    custom_schema["required"].clear()
    resolved = registry.resolve("tool.before")
    resolved.schema["required"].clear()
    assert snapshot.contract_version == 2
    assert snapshot.resolve("custom.before").schema["required"] == ["text"]
    assert snapshot.resolve("tool.before").schema["required"]


def test_manager_rejects_incomplete_payload_before_creating_checkpoint():
    manager = HookManager()
    state = {}
    saved = []
    with pytest.raises(HookFailed, match="point schema"):
        asyncio.run(
            manager.run(
                "tool.before",
                {"arguments": {}},
                scope={},
                state=state,
                save=lambda: saved.append(copy.deepcopy(state)),
            )
        )
    assert state == {} and saved == []
