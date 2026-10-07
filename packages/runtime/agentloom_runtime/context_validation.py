"""Validate replacement message structure before committing a plugin result."""

import json


def validate_compaction(original, replacement):
    if not isinstance(replacement, list) or not replacement or replacement[0] != original[0]:
        raise ValueError("压缩结果必须保留原始系统指令")
    expected = set()
    try:
        json.dumps(replacement, allow_nan=False)
        for message in replacement:
            role = message["role"]
            if role not in ("system", "user", "assistant", "tool"):
                raise ValueError()
            if role == "tool":
                call_id = message["tool_call_id"]
                if call_id not in expected:
                    raise ValueError()
                expected.remove(call_id)
            elif expected:
                raise ValueError()
            calls = message.get("tool_calls")
            if calls:
                if role != "assistant":
                    raise ValueError()
                ids = [call["id"] for call in calls]
                if any(not isinstance(value, str) or not value for value in ids):
                    raise ValueError()
                expected = set(ids)
                if len(expected) != len(ids):
                    raise ValueError()
        if expected:
            raise ValueError()
    except (TypeError, KeyError, ValueError):
        raise ValueError("压缩结果破坏了完整消息或工具调用/结果关联") from None
