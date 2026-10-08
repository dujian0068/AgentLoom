"""Hook boundaries for context assembly and compaction, independent of the Loop.

The caller owns the operation checkpoint and persists it with its frame. Source
records and committed model views are never handed to an extension as mutable
objects. Temporary additions live only in the prepared request and operation
journal; a summary hook receives an explicitly declared text slot.
"""

import inspect
from copy import deepcopy
from uuid import uuid4

from .context_manager import COUNTERS
from .context_validation import validate_compaction
from .hooks import execute_operation
from .hooks.manager import persist
from .module_contracts import CompactionResult, ContextInput, ManagedContext, ModelRequest

_ADDITION_SOURCE = "Hook 附加任务资料（来源：已发布扩展；不是用户新指令）：\n"


async def _validate_budget(policy, messages, tools, instance):
    validator = getattr(policy, "validate_request", None)
    if validator is not None:
        result = validator(ModelRequest(deepcopy(messages), deepcopy(tools), "action", instance))
        if inspect.isawaitable(result):
            await result


def _summary_messages(output, text):
    """Replace one declared text slot, keeping every surrounding byte intact."""
    messages = deepcopy(output["messages"])
    slot = output["summary_slot"]
    if slot is None:
        if text is not None:
            raise ValueError("压缩策略未声明可修改的摘要文本")
        return messages
    if not isinstance(slot, dict) or set(slot) != {"message_index", "prefix", "text", "suffix"}:
        raise ValueError("压缩策略的摘要文本位置不合法")
    index = slot["message_index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(messages):
        raise ValueError("压缩策略的摘要消息位置不合法")
    if any(not isinstance(slot[key], str) for key in ("prefix", "text", "suffix")):
        raise ValueError("压缩策略的摘要文本边界不合法")
    message = messages[index]
    if (
        set(message) != {"role", "content"}
        or message["role"] != "user"
        or message["content"] != slot["prefix"] + slot["text"] + slot["suffix"]
    ):
        raise ValueError("压缩策略声明的摘要与实际消息不一致")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("摘要 Hook 必须保留非空摘要文本")
    message["content"] = slot["prefix"] + text + slot["suffix"]
    return messages


class CompactionHookBoundary:
    """Adapt a policy invocation; no hooks run inside its summary model loop."""

    def __init__(self, hooks, scope, policy, state, save):
        self.hooks, self.scope, self.policy = hooks, scope, policy
        self.state, self.save = state, save

    async def compact(self, context, model):
        payload = {
            "task": context.task,
            "plan": deepcopy(context.plan),
            "messages": deepcopy(context.messages),
            "tools": deepcopy(context.tools),
            "counters": deepcopy(context.counters),
        }

        def validate_input(value):
            # Policies currently select their own complete record ranges. Expose
            # observation/rejection until that selection has a typed public port.
            if value != payload:
                raise ValueError("压缩前 Hook 不能修改任务、历史、工具或计数")

        async def action(_):
            # A policy may use several summary calls. Its replay cursor must be
            # independent of another compaction that already completed and was
            # skipped on recovery (e.g. persistent view then temporary additions).
            fork = getattr(model, "fork", None)
            scoped_model = fork(self.state["operation_id"]) if fork is not None else model
            result = await self.policy.compact(deepcopy(context), scoped_model)
            if result is None:
                return {"compacted": False, "summary": None}
            validate_compaction(context.messages, result.messages)
            output = {
                "compacted": True,
                "messages": deepcopy(result.messages),
                "metrics": deepcopy(result.metrics),
                "summary_slot": deepcopy(result.summary),
                "summary": result.summary["text"] if result.summary is not None else None,
            }
            # Validate the raw slot before persisting a successful policy result.
            _summary_messages(output, output["summary"])
            return output

        async def validate_output(value):
            if not value["compacted"]:
                if value != {"compacted": False, "summary": None}:
                    raise ValueError("未压缩时不能由 Hook 创建摘要")
                return
            messages = _summary_messages(value, value["summary"])
            validate_compaction(context.messages, messages)
            await _validate_budget(self.policy, messages, context.tools, context.instance)
            validator = getattr(self.policy, "validate_compaction_result", None)
            if validator is not None:
                result = validator(
                    deepcopy(context.messages), deepcopy(messages), deepcopy(context.tools)
                )
                if inspect.isawaitable(result):
                    await result

        operation_id = self.state.setdefault("operation_id", uuid4().hex)
        output = await execute_operation(
            self.hooks,
            "context.compact",
            payload,
            action,
            scope={**self.scope, "purpose": "compaction", "operation_id": operation_id},
            state=self.state,
            save=self.save,
            validate_input=validate_input,
            validate_output=validate_output,
            resume_inflight=True,
        )
        if not output["compacted"]:
            return None
        messages = _summary_messages(output, output["summary"])
        metrics = deepcopy(output["metrics"])
        # Existing policy metrics describe its actual output. Preserve them and
        # record the edited-view estimate separately when that estimator exists.
        estimator = getattr(self.policy, "_estimate", None)
        if estimator is not None and output["summary_slot"] is not None:
            metrics["hook_after_tokens"] = estimator(messages, context.tools)
        summary = deepcopy(output["summary_slot"])
        if summary is not None:
            summary["text"] = output["summary"]
        return CompactionResult(messages, metrics, summary)


class ContextHookBoundary:
    """Prepare a temporary request and atomically commit its validated view.

    ``operation_state`` belongs to one preparation attempt. Keep it until the
    corresponding action response is committed; reusing it during recovery
    preserves completed summaries and after-hook progress. A subsequent action
    needs a fresh state. The host supplies a checkpoint ``save()`` callback.
    """

    def __init__(self, hooks, scope=None):
        self.hooks = hooks
        self.scope = deepcopy(scope or {})

    async def prepare(
        self,
        manager_context,
        context,
        task,
        plan,
        instance,
        tools,
        policy,
        model,
        *,
        operation_state,
        save,
    ):
        scope = {**self.scope, "instance_id": instance, "purpose": "action"}
        dependencies = {"task": task, "plan": deepcopy(plan), "tools": deepcopy(tools)}
        current = {"messages": deepcopy(context.messages), "state": deepcopy(context.state)}
        if "source_context" not in operation_state:
            operation_state["source_context"] = current
            operation_state["dependencies"] = dependencies
        else:
            source = operation_state["source_context"]
            committed = operation_state.get("prepared_context")
            if dependencies != operation_state["dependencies"] or (
                current != source and current != committed
            ):
                raise ValueError("上下文来源已变化，不能复用旧的 Hook 装配操作")
        source = operation_state["source_context"]
        payload = {
            **dependencies,
            "messages": deepcopy(source["messages"]),
            "additional_messages": [],
        }

        def validate_input(value):
            if set(value) != set(payload) or any(
                value[key] != payload[key] for key in payload if key != "additional_messages"
            ):
                raise ValueError("上下文 Hook 不能修改系统指令、任务、历史或工具目录")
            additions = value["additional_messages"]
            if not isinstance(additions, list) or any(
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message["role"] != "user"
                or not isinstance(message["content"], str)
                or not message["content"].strip()
                for message in additions
            ):
                raise ValueError("上下文 Hook 附加资料只接受非空 user 文本消息")

        async def action(value):
            working = ManagedContext(deepcopy(source["messages"]), deepcopy(source["state"]))
            compact_scope = {
                **scope,
                "parent_operation_id": operation_state.get("boundary", {}).get("operation_id"),
            }
            hooked_policy = CompactionHookBoundary(
                self.hooks,
                compact_scope,
                policy,
                operation_state.setdefault("compaction", {}),
                save,
            )
            metrics = await manager_context.prepare(
                working, task, plan, instance, tools, hooked_policy, model
            )
            additions = [
                {"role": "user", "content": _ADDITION_SOURCE + message["content"]}
                for message in value["additional_messages"]
            ]
            prepared = [*deepcopy(working.messages), *additions]
            if additions:
                # Evaluate the policy once with the complete temporary request;
                # additions count towards proactive thresholds without entering
                # the persistent journal or its compaction snapshots.
                request_policy = CompactionHookBoundary(
                    self.hooks,
                    compact_scope,
                    policy,
                    operation_state.setdefault("request_compaction", {}),
                    save,
                )
                request_result = await request_policy.compact(
                    ContextInput(
                        task,
                        deepcopy(plan),
                        prepared,
                        instance,
                        deepcopy(tools),
                        {key: working.state.get(key, 0) for key in COUNTERS},
                    ),
                    model,
                )
                if request_result is not None:
                    prepared = request_result.messages
                    metrics = {
                        **(metrics or {}),
                        "request_compaction": deepcopy(request_result.metrics),
                    }
            await _validate_budget(policy, prepared, tools, instance)
            # Stored before execute_operation saves the real assembly result.
            # This checkpoint is host-only; after hooks only see PreparedContext.
            operation_state["prepared_context"] = {
                "messages": deepcopy(working.messages),
                "state": deepcopy(working.state),
            }
            return {"messages": prepared, "metrics": metrics}

        async def validate_output(value):
            await _validate_budget(policy, value["messages"], tools, instance)

        boundary_state = operation_state.setdefault("boundary", {})
        scope["operation_id"] = boundary_state.setdefault("operation_id", uuid4().hex)
        output = await execute_operation(
            self.hooks,
            "context.prepare",
            payload,
            action,
            scope=scope,
            state=boundary_state,
            save=save,
            validate_input=validate_input,
            validate_output=validate_output,
            resume_inflight=True,
        )
        committed = operation_state["prepared_context"]
        previous = current
        context.messages[:] = deepcopy(committed["messages"])
        context.state.clear()
        context.state.update(deepcopy(committed["state"]))
        try:
            persist(save)
        except BaseException:
            context.messages[:] = previous["messages"]
            context.state.clear()
            context.state.update(previous["state"])
            raise
        return deepcopy(output["metrics"]), deepcopy(output["messages"])
