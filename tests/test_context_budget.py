"""Proactive thresholds, bounded summaries and preservation without real models."""

import asyncio
import json
from copy import deepcopy

import pytest
from agentloom_runtime.budget import ContextBudgetError, Utf8TokenEstimator
from agentloom_runtime.context import BudgetCompactionPolicy
from agentloom_runtime.context_validation import validate_compaction
from agentloom_runtime.module_contracts import ContextInput, ModelRequest


class TextEstimator:
    module_id = "text-count-for-boundaries/v1"

    def estimate(self, messages, tools):
        return sum(len(message.get("content") or "") for message in messages) + sum(
            len(json.dumps(tool)) for tool in tools
        )


class Summarizer:
    def __init__(self, summary="已验证旧步骤的结果；保留约束，继续未完成事项。"):
        self.requests = []
        self.summary = summary

    async def __call__(self, request):
        self.requests.append(deepcopy(request))
        return {"role": "assistant", "content": self.summary}


def policy(config=None, *, estimator=None, budget=10000):
    return BudgetCompactionPolicy(
        config,
        {
            "context_window": budget + 1100,
            "max_output_tokens": 1000,
            "safety_margin_tokens": 100,
        },
        estimator or TextEstimator(),
    )


def context(size=8000, *, counters=None, tools=None, plan=None):
    return ContextInput(
        "T" * 50,
        plan or [],
        [
            {"role": "system", "content": "S" * 50},
            {"role": "user", "content": "T" * 50},
            {"role": "assistant", "content": "H" * (size - 100)},
        ],
        "main",
        tools or [],
        counters or {},
    )


def compact(selected, source, summarizer):
    return asyncio.run(selected.compact(source, summarizer))


def test_eighty_percent_equality_compacts_before_model_window_is_full():
    selected, source, model = policy(), context(), Summarizer()
    original = deepcopy(source.messages)
    result = compact(selected, source, model)
    assert result.metrics["trigger_reasons"] == ["context_ratio"]
    assert result.metrics["before_tokens"] == 8000
    assert result.metrics["after_tokens"] <= 6000
    assert result.metrics["target_reached"] is True
    assert result.messages[0] == original[0]
    assert result.messages[1]["content"] == source.task
    assert source.messages == original
    assert len(model.requests) == 1
    assert compact(selected, context(7999), Summarizer()) is None


@pytest.mark.parametrize(
    ("counters", "reasons"),
    [
        ({"user_turns": 3}, ["user_turns"]),
        ({"model_steps": 7, "baseline_model_steps": 3}, ["model_steps"]),
        ({"user_turns": 3, "model_steps": 4}, ["user_turns", "model_steps"]),
        ({"user_turns": 5, "baseline_user_turns": 3, "model_steps": 3}, None),
    ],
)
def test_turn_and_model_step_thresholds_are_independent_or_conditions(counters, reasons):
    selected = policy({"user_turns": 3, "model_steps": 4})
    source = context(7000, counters=counters)
    result = compact(selected, source, Summarizer())
    if reasons is None:
        assert result is None
    else:
        assert result.metrics["trigger_reasons"] == reasons
        assert result.metrics["user_turns"] == counters.get("user_turns", 0)
        assert result.metrics["model_steps"] == counters.get("model_steps", 0)
    assert source.counters == counters


def test_tool_schemas_output_reserve_and_safety_reduce_available_budget():
    selected = policy(estimator=Utf8TokenEstimator(), budget=6000)
    source = context(400)
    assert selected.effective_input_budget == 6000
    assert compact(selected, source, Summarizer()) is None
    source.tools.append({"type": "function", "description": "schema" * 900})
    model = Summarizer()
    with pytest.raises(ContextBudgetError, match="工具定义"):
        compact(selected, source, model)
    assert not model.requests
    assert selected.checkpoint_config()["model_profile"] == {
        "context_window": 7100,
        "max_output_tokens": 1000,
        "safety_margin_tokens": 100,
    }


def test_utf8_estimate_covers_serialized_fields_and_non_ascii_bytes():
    estimator = Utf8TokenEstimator()
    message = {"role": "assistant", "content": "中文", "reasoning_content": "reason" * 100}
    request = [message]
    base = estimator.estimate(request, [])
    assert base > len("中文".encode()) + 500
    assert estimator.estimate(request, [{"parameters": {"description": "工具" * 100}}]) > base


def test_oversized_completed_history_is_chunked_without_oversized_summary_calls():
    selected = policy(estimator=Utf8TokenEstimator())
    source = context(100)
    source.messages[-1]["content"] = "历史资料" * 1800
    original = deepcopy(source.messages)
    model = Summarizer()
    result = compact(selected, source, model)
    assert 1 < len(model.requests) <= 4
    fragments = []
    for request in model.requests:
        assert request.tools == []
        assert request.purpose == "compaction"
        assert selected.estimator.estimate(request.messages, []) <= 10000
        body = json.loads(request.messages[1]["content"])
        fragments.extend(item["data"] for item in body["history_parts"])
    assert "".join(fragments) == json.dumps([original[-1]], ensure_ascii=False)
    assert result.metrics["after_tokens"] < 8000
    assert source.messages == original


def test_summary_calls_are_bounded_and_failure_preserves_source_and_counters():
    selected = policy({"max_compaction_calls": 1}, estimator=Utf8TokenEstimator())
    source = context(100, counters={"model_steps": 20, "baseline_model_steps": 4})
    source.messages[-1]["content"] = "history " * 4000
    original = deepcopy(source)
    model = Summarizer()
    with pytest.raises(ContextBudgetError, match="调用预算"):
        compact(selected, source, model)
    assert len(model.requests) == 1
    assert source == original


def test_nonreducing_summary_fails_instead_of_slicing_output():
    selected, source = policy(), context()
    original = deepcopy(source)
    with pytest.raises(ContextBudgetError, match="未减少"):
        compact(selected, source, Summarizer("unreduced" * 4000))
    assert source == original


def test_final_summary_can_be_reduced_again_within_same_call_budget():
    selected, source = policy(), context(9000)
    requests = []

    async def model(request):
        requests.append(deepcopy(request))
        return {"content": "x" * 8500 if len(requests) == 1 else "concise facts"}

    result = compact(selected, source, model)
    assert result.metrics["summary_calls"] == 2
    assert result.metrics["after_tokens"] < 6000
    assert all(selected.estimator.estimate(request.messages, []) <= 10000 for request in requests)


def tool_group():
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "read-1", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "read-1", "content": "verified tool result"},
    ]


def test_recent_tool_call_and_result_are_retained_as_a_complete_group():
    selected, source = policy(), context()
    source.messages.extend(tool_group())
    result = compact(selected, source, Summarizer())
    assert result.messages[-2:] == tool_group()
    validate_compaction(source.messages, result.messages)


def test_incomplete_tool_group_is_never_split_or_submitted_to_summarizer():
    selected, source, model = policy(), context(), Summarizer()
    source.messages.append(tool_group()[0])
    original = deepcopy(source)
    with pytest.raises(ContextBudgetError, match="收齐结果"):
        compact(selected, source, model)
    assert not model.requests
    assert source == original


def test_oversized_completed_tool_group_uses_ordered_text_fragments_not_orphan_messages():
    selected, source = policy(estimator=Utf8TokenEstimator()), context(100)
    source.messages.pop()
    group = tool_group()
    group[-1]["content"] = "工具结果" * 1200
    source.messages.extend(group)
    model = Summarizer()
    result = compact(selected, source, model)
    parts = [
        part
        for request in model.requests
        for part in json.loads(request.messages[1]["content"])["history_parts"]
    ]
    assert len(parts) >= 2
    assert [part["part"] for part in parts] == list(range(len(parts)))
    assert "".join(part["data"] for part in parts) == json.dumps(group, ensure_ascii=False)
    assert all(
        [message["role"] for message in request.messages] == ["system", "user"]
        for request in model.requests
    )
    validate_compaction(source.messages, result.messages)


def test_task_plan_and_system_constraints_survive_verbatim():
    source = context(plan=[{"id": "p1", "status": "pending", "text": "do not change database"}])
    source.messages.insert(2, {"role": "system", "content": "additional exact constraint"})
    result = compact(policy(), source, Summarizer())
    assert result.messages[0] == source.messages[0]
    assert {"role": "user", "content": source.task} in result.messages
    assert {"role": "system", "content": "additional exact constraint"} in result.messages
    assert json.dumps(source.plan, ensure_ascii=False) in result.messages[2]["content"]


def test_count_trigger_without_reducible_history_does_not_create_successful_snapshot():
    source = context(100, counters={"user_turns": 3})
    source.messages.pop()
    model = Summarizer()
    assert compact(policy({"user_turns": 3}), source, model) is None
    assert not model.requests
    assert source.counters == {"user_turns": 3}


def test_irreducible_soft_threshold_defers_without_fake_baseline_or_model_call():
    source = ContextInput(
        "T" * 8100,
        [],
        [{"role": "system", "content": "S"}, {"role": "user", "content": "T" * 8100}],
        "main",
        [],
        {"user_turns": 3},
    )
    model = Summarizer()
    assert compact(policy({"user_turns": 3}), source, model) is None
    assert source.counters == {"user_turns": 3}
    assert not model.requests


def test_count_only_unhelpful_summary_defers_without_successful_snapshot():
    source = context(1000, counters={"user_turns": 3})
    model = Summarizer("longer-than-original" * 100)
    assert compact(policy({"user_turns": 3}), source, model) is None
    assert len(model.requests) == 1
    assert source.counters == {"user_turns": 3}


def test_hard_budget_guard_applies_to_all_request_purposes_and_tools():
    selected = policy()
    selected.validate_request(ModelRequest([{"content": "x" * 10000}], [], "verification"))
    with pytest.raises(ContextBudgetError, match="有效输入预算"):
        selected.validate_request(ModelRequest([{"content": "x" * 10001}], [], "verification"))
    with pytest.raises(ContextBudgetError, match="有效输入预算"):
        selected.validate_request(ModelRequest([{"content": "x" * 9999}], [{"name": "read"}]))


@pytest.mark.parametrize(
    "config",
    [
        {"context_ratio": 0},
        {"context_ratio": float("nan")},
        {"target_ratio": 0.8},
        {"model_steps": 0},
        {"user_turns": True},
        {"max_compaction_calls": 0},
        {"unknown": 1},
    ],
)
def test_invalid_policy_settings_are_rejected(config):
    with pytest.raises(ValueError):
        policy(config)


def test_config_binding_is_copied_versioned_and_contains_no_model_credentials():
    config = {"context_ratio": 0.85}
    profile = {"api_key": "not-in-bindings", "context_window": 64000}
    selected = BudgetCompactionPolicy(config, profile)
    config["context_ratio"] = 0.1
    profile["context_window"] = 1
    binding = selected.checkpoint_config()
    assert binding["policy"]["context_ratio"] == 0.85
    assert binding["model_profile"]["context_window"] == 64000
    assert binding["estimator"]["module_id"] == Utf8TokenEstimator.module_id
    assert "not-in-bindings" not in json.dumps(binding)
    binding["policy"]["context_ratio"] = 0.2
    assert selected.policy["context_ratio"] == 0.85
