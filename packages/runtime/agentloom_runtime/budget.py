"""Provider-independent input budgets and a deliberately conservative estimate."""

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol


class ContextBudgetError(RuntimeError):
    """Preparation stopped without changing the original history; it can be retried."""


class TokenEstimator(Protocol):
    module_id: str

    def estimate(self, messages: list[dict], tools: list[dict]) -> int: ...


@dataclass(frozen=True)
class Utf8TokenEstimator:
    """Count UTF-8 JSON bytes plus framing, not provider-reported tokenizer usage.

    Counting every byte is intentionally more conservative than dividing by a
    language-dependent characters/token ratio. Provider framing and tokenizers
    can differ; deployments may inject a versioned provider-specific estimator.
    """

    module_id = "utf8-bytes-with-framing/v1"

    def estimate(self, messages: list[dict], tools: list[dict]) -> int:
        payload = json.dumps(
            {"messages": messages, "tools": tools},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return len(payload.encode("utf-8")) + 32 * len(messages) + 32 * len(tools)


DEFAULT_CONTEXT_POLICY = {
    "context_ratio": 0.8,
    "target_ratio": 0.6,
    "user_turns": None,
    "model_steps": None,
    "max_compaction_calls": 4,
}

# Compatibility fallback, never a claim about a provider/model's advertised window.
DEFAULT_MODEL_PROFILE = {
    "context_window": 32768,
    "max_output_tokens": 4096,
    "safety_margin_tokens": 1024,
}


def positive_integer(value, name, *, zero=False):
    minimum = 0 if zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必须是{'非负' if zero else '正'}整数")
    return value


def normalized_policy(value=None):
    result = deepcopy(DEFAULT_CONTEXT_POLICY)
    result.update(value or {})
    if set(result) != set(DEFAULT_CONTEXT_POLICY):
        raise ValueError("包含未知上下文压缩配置")
    for name in ("context_ratio", "target_ratio"):
        number = result[name]
        if (
            isinstance(number, bool)
            or not isinstance(number, (float, int))
            or not math.isfinite(number)
            or not 0 < number <= 1
        ):
            raise ValueError(f"{name} 必须在 0 到 1 之间")
        result[name] = float(number)
    if result["target_ratio"] >= result["context_ratio"]:
        raise ValueError("压缩目标必须小于触发比例")
    for name in ("user_turns", "model_steps"):
        if result[name] is not None:
            positive_integer(result[name], name)
    positive_integer(result["max_compaction_calls"], "max_compaction_calls")
    return result


def normalized_profile(value=None):
    result = deepcopy(DEFAULT_MODEL_PROFILE)
    # Model records may also carry provider, endpoint, credentials, etc. Only
    # these public budget fields are retained in compaction checkpoint bindings.
    result.update({key: value[key] for key in result if value and key in value})
    positive_integer(result["context_window"], "context_window")
    positive_integer(result["max_output_tokens"], "max_output_tokens")
    positive_integer(result["safety_margin_tokens"], "safety_margin_tokens", zero=True)
    if result["max_output_tokens"] + result["safety_margin_tokens"] >= result["context_window"]:
        raise ValueError("模型上下文必须大于输出预留与安全余量之和")
    return result


def estimate_tokens(estimator, messages, tools):
    result = estimator.estimate(deepcopy(messages), deepcopy(tools))
    return positive_integer(result, "Token 估计结果", zero=True)
