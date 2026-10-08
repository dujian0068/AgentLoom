"""Durable model boundary. Hooks receive business data, never a provider credential."""

import inspect
import math
from copy import deepcopy

from .hooks import execute_operation
from .module_contracts import ModelRequest, normalize_model_response


def model_payload(request, model_id):
    return {
        "messages": deepcopy(request.messages),
        "tools": deepcopy(request.tools),
        "model_id": model_id,
        "purpose": request.purpose,
        **{key: request.options.get(key) for key in ("temperature", "top_p", "max_tokens")},
    }


def payload_request(payload, instance):
    return ModelRequest(
        deepcopy(payload["messages"]),
        deepcopy(payload["tools"]),
        payload["purpose"],
        instance,
        {
            key: payload[key]
            for key in ("temperature", "top_p", "max_tokens")
            if payload[key] is not None
        },
    )


class ModelHookBoundary:
    def __init__(self, hooks, *, model_id, scope=None, max_output_tokens=None):
        self.hooks = hooks
        self.model_id = model_id
        self.scope = deepcopy(scope or {})
        self.max_output_tokens = max_output_tokens

    async def invoke(
        self,
        request,
        invoke,
        *,
        state,
        save,
        validate_budget=None,
        prepare_request=None,
        validate_dispatch=None,
    ):
        original = model_payload(request, self.model_id)

        def validate_input(payload):
            for key in ("tools", "model_id", "purpose"):
                if payload[key] != original[key]:
                    raise ValueError("模型 Hook 不可修改模型绑定、工具权限或调用目的")
            messages = payload["messages"]
            # Until optional ContextBlocks have explicit grants, original messages
            # are mandatory. Extensions can append user-level material only.
            if (
                not isinstance(messages, list)
                or messages[: len(original["messages"])] != original["messages"]
            ):
                raise ValueError("模型 Hook 不可修改已有系统指令、任务或历史消息")
            for message in messages[len(original["messages"]) :]:
                if (
                    not isinstance(message, dict)
                    or set(message) != {"role", "content"}
                    or message["role"] != "user"
                    or not isinstance(message["content"], str)
                ):
                    raise ValueError("模型 Hook 附加消息必须是 user 文本，不能伪造工具或系统消息")
            for key, maximum in (("temperature", 2), ("top_p", 1)):
                value = payload[key]
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= maximum
                ):
                    raise ValueError("模型采样参数超出允许范围")
            limit = payload["max_tokens"]
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit <= 0
                or self.max_output_tokens is None
                or limit > self.max_output_tokens
            ):
                raise ValueError("模型 Hook 输出上限必须在已发布输出预算内")

        async def prepare_input(payload):
            if prepare_request is not None:
                prepared = await prepare_request(payload_request(payload, request.instance))
                payload["messages"] = deepcopy(prepared.messages)
            return payload

        async def validate_prepared(payload):
            if validate_budget is not None:
                result = validate_budget(payload_request(payload, request.instance))
                if inspect.isawaitable(result):
                    await result
            if validate_dispatch is not None:
                result = validate_dispatch(payload_request(payload, request.instance))
                if inspect.isawaitable(result):
                    await result

        def validate_output(payload):
            normalized = normalize_model_response(payload)
            for key in ("usage", "finish_reason", "provider_request_id", "annotations"):
                if key in payload:
                    normalized[key] = deepcopy(payload[key])
            return normalized

        async def action(payload):
            reply = await invoke(payload_request(payload, request.instance))
            if not isinstance(reply, dict):
                return {"invalid_response": deepcopy(reply)}
            return {
                key: deepcopy(value)
                for key, value in reply.items()
                if key
                in (
                    "role",
                    "content",
                    "tool_calls",
                    "reasoning_content",
                    "usage",
                    "finish_reason",
                    "provider_request_id",
                )
            }

        return await execute_operation(
            self.hooks,
            "model.chat",
            original,
            action,
            scope={
                **self.scope,
                "operation_id": state["operation_id"],
                "instance_id": request.instance,
                "purpose": request.purpose,
                "target": self.model_id,
            },
            state=state,
            save=save,
            validate_input=validate_input,
            validate_output=validate_output,
            prepare_input=prepare_input,
            validate_prepared=validate_prepared,
        )
