"""Application-owned agent harness: act, observe, revise and verify.

Checkpoints preserve conversation and tool boundaries for recovery. A
resumed uncertain external action is observed instead of automatically replayed.
"""

import asyncio
import json
import uuid
from copy import deepcopy
from dataclasses import asdict

from .context_manager import COUNTERS
from .execution_services import build_tool_context
from .hooked_context import CompactionHookBoundary, ContextHookBoundary
from .hooks import HookError, HookManager
from .lifecycle_hooks import RuntimeLifecycle
from .model_hooks import ModelHookBoundary, payload_request
from .module_contracts import CompletionInput, ContextInput, ModelRequest
from .modules import RuntimeModules
from .tool_contracts import TOOL_REQUEST_TOPIC, ToolOutcome, ToolPersistenceError, ToolRequest


class Engine:
    def __init__(
        self,
        config,
        workspace,
        emit,
        checkpoint=None,
        save=None,
        *,
        tools,
        modules: RuntimeModules,
        model_id="",
        hook_scope=None,
        max_output_tokens=None,
    ):
        self.config = deepcopy(config)
        self.workspace = workspace
        self.emit = emit
        self.tools = tools
        self.modules = modules
        self.model_id = model_id
        self.hook_scope = deepcopy(hook_scope or {})
        self.model_boundary = ModelHookBoundary(
            modules.hooks,
            model_id=model_id,
            scope=self.hook_scope,
            max_output_tokens=max_output_tokens,
        )
        self.context_boundary = ContextHookBoundary(modules.hooks, self.hook_scope)
        self.lifecycle = RuntimeLifecycle(modules.hooks, self.hook_scope)
        self._closed = False
        self._active_task = None
        self._close_task = None
        self.save = save or (lambda state: None)
        self.state = checkpoint or {
            "format": 1,
            "calls": 0,
            "total_calls": 0,
            "delegations": 0,
            "citations": {},
            "frames": {},
        }
        if self.state.get("format") != 1:
            raise ValueError("不支持此运行检查点版本")
        bindings = modules.bindings()
        bindings["tools"] = tools.bindings()
        if checkpoint is not None and any(
            item["implementation_id"] is None for item in bindings["tools"]
        ):
            raise ValueError("恢复需要工具声明 implementation_id 版本；请显式迁移未版本化工具")
        if checkpoint is not None and "modules" in checkpoint:
            saved_bindings = deepcopy(checkpoint["modules"])
            if "hooks" not in saved_bindings:
                if bindings["hooks"] != {
                    "implementation": "hooks/v1",
                    "config": HookManager().checkpoint_config(),
                }:
                    raise ValueError("旧检查点不能追加 Hook 链；请显式迁移")
                saved_bindings["hooks"] = bindings["hooks"]
            if "context" not in saved_bindings:
                if bindings["context"] != {"implementation": "journal-context/v1", "config": {}}:
                    raise ValueError("旧检查点的上下文模块需要显式迁移")
                saved_bindings["context"] = bindings["context"]
            if saved_bindings != bindings:
                raise ValueError("检查点模块版本或配置不兼容；请使用原模块配置恢复")
        if checkpoint is not None and "modules" not in checkpoint:
            if bindings["hooks"]["config"] != HookManager().checkpoint_config():
                raise ValueError("旧检查点不能追加 Hook 链；请显式迁移")
            builtin_ids = {
                "model": "chat-completions/v1",
                "compaction": "character-handoff/v1",
                "completion": "evidence-review/v1",
                "strategy": "plan/v1" if config["mode"] == "plan" else "react/v1",
                "context": "journal-context/v1",
                "hooks": "hooks/v1",
            }
            if any(
                bindings[name]["implementation"] != value for name, value in builtin_ids.items()
            ):
                raise ValueError("旧检查点只支持默认模块恢复；自定义模块需要显式迁移")
            if bindings["compaction"]["config"] != {
                "context_chars": 60000,
                "keep_recent_chars": 16000,
            } or bindings["limits"] != {"max_model_calls": 96, "max_iterations": 64}:
                raise ValueError("旧检查点的策略预算需要显式迁移")
            if any(
                not item["implementation_id"].startswith("builtin-tools/v1:")
                for item in bindings["tools"]
            ):
                raise ValueError("旧检查点的工具集合需要显式迁移")
        self.state["modules"] = bindings
        self.state.setdefault("operations", {})
        self.state.setdefault("lifecycles", {})
        self.citations = self.state["citations"]
        self.blocked = False

    def persist(self):
        try:
            self.save(self.state)
        except ToolPersistenceError:
            raise
        except Exception as exc:
            raise ToolPersistenceError("运行检查点保存失败，任务已停止，可恢复后重试") from exc

    def resume(self, instruction="", *, retry_unknown_models=False):
        if retry_unknown_models:
            for operation in self.state["operations"].values():
                if (
                    operation.get("kind") == "model.chat"
                    and "raw_output" not in operation
                    and operation.get("actual_status") in {"running", "unknown"}
                ):
                    operation["actual_status"] = "not_started"
                    operation["retry_authorizations"] = operation.get("retry_authorizations", 0) + 1
        self.state["calls"] = 0
        for frame in self.state["frames"].values():
            frame["iterations"] = 0
        frame = self.state["frames"].get("main")
        if frame:
            if self.state["lifecycles"].get("main", {}).get("completed"):
                self.state.setdefault("lifecycle_history", []).append(
                    self.state["lifecycles"].pop("main")
                )
                frame.pop("review_models", None)
            frame.pop("outcome", None)
            frame.pop("output", None)
            if instruction:
                self.modules.context.queue_input(self._context(frame), instruction)
                frame["task"] += "\n用户补充：" + instruction
                frame["candidate"] = None
                frame.pop("review_models", None)
        elif instruction:
            self.state.setdefault("pending_inputs", []).append({"content": instruction})
        self.persist()

    def _context(self, frame):
        context = self.modules.context.restore(
            frame["messages"], frame.get("context"), frame["task"]
        )
        frame["context"] = context.state
        return context

    def _operation(self, slots, key):
        operation_id = slots.setdefault(key, uuid.uuid4().hex)
        return self.state["operations"].setdefault(operation_id, {"operation_id": operation_id})

    async def model(self, request: ModelRequest, operation=None):
        if self._closed:
            raise RuntimeError("Runtime 已关闭")
        if operation is None:
            operation = self._operation({}, "direct")

        async def prepare(prepared):
            if self.state["calls"] >= self.modules.limits.max_model_calls:
                raise RuntimeError("达到本次执行预算，可以恢复继续")
            if prepared.purpose != "action" or prepared.messages == request.messages:
                return prepared
            frame = self.state["frames"].get(prepared.instance)
            if frame is None:
                return prepared
            boundary = CompactionHookBoundary(
                self.modules.hooks,
                {
                    **self.hook_scope,
                    "instance_id": prepared.instance,
                    "parent_operation_id": operation["operation_id"],
                },
                self.modules.compaction,
                operation.setdefault("reprepare", {}),
                self.persist,
            )
            result = await boundary.compact(
                ContextInput(
                    frame["task"],
                    deepcopy(frame["plan"]),
                    deepcopy(prepared.messages),
                    prepared.instance,
                    deepcopy(prepared.tools),
                    {key: frame["context"][key] for key in COUNTERS},
                ),
                self._policy_model(
                    "compaction", prepared.instance, operation.setdefault("reprepare_models", {})
                ),
            )
            if result is None:
                return prepared
            return ModelRequest(
                result.messages,
                prepared.tools,
                prepared.purpose,
                prepared.instance,
                prepared.options,
            )

        return await self.model_boundary.invoke(
            request,
            self._invoke_model,
            state=operation,
            save=self.persist,
            validate_budget=getattr(self.modules.compaction, "validate_request", None),
            prepare_request=prepare,
            validate_dispatch=self._validate_model_dispatch,
        )

    def _validate_model_dispatch(self, request):
        limit = self.modules.limits.max_model_calls
        if self.state["calls"] >= limit:
            raise RuntimeError(f"达到本次执行预算（{limit} 次模型调用），可以恢复继续")

    async def _invoke_model(self, request):
        self._validate_model_dispatch(request)
        self.state["calls"] += 1
        self.state["total_calls"] += 1
        self.persist()
        self.emit(
            "model.started",
            {
                "instance": request.instance,
                "purpose": request.purpose,
                "iteration": self.state["calls"],
            },
        )
        return await self.modules.model.invoke(deepcopy(request))

    async def execute(self, task, history=(), history_metadata=None):
        if self._closed:
            raise RuntimeError("Runtime 已关闭")
        if self._active_task is not None:
            raise RuntimeError("同一 Runtime 不能并发执行多个主任务")
        self._active_task = asyncio.current_task()
        try:
            while True:
                result = await self._lifecycle(
                    "run",
                    "main",
                    task,
                    lambda: self.loop(
                        self.config, task, history, history_metadata=history_metadata
                    ),
                )
                frame = self.state["frames"]["main"]
                if not self._context(frame).state.get("pending_inputs"):
                    break
                # Complete a saved run.after first, then handle any user input
                # accepted while that result was waiting for postprocessing.
                self.state.setdefault("lifecycle_history", []).append(
                    self.state["lifecycles"].pop("main")
                )
                for key in ("outcome", "output", "review_models"):
                    frame.pop(key, None)
                frame["candidate"] = None
                self.persist()
            self.blocked = self.state["frames"]["main"].get("outcome") == "blocked"
            return result
        finally:
            self._active_task = None

    def prompt(self, config, instance):
        return "\n".join(
            (
                config["prompt"],
                self.modules.strategy.instructions(deepcopy(config), instance),
                self.tools.instructions(config, instance),
            )
        )

    async def compact(self, frame, instance, tools):
        cycle = frame.setdefault("cycle", {})
        prepare = cycle.setdefault("prepare", {})
        metrics, messages = await self.context_boundary.prepare(
            self.modules.context,
            self._context(frame),
            frame["task"],
            frame["plan"],
            instance,
            tools,
            self.modules.compaction,
            self._policy_model("compaction", instance, prepare.setdefault("models", {})),
            operation_state=prepare,
            save=self.persist,
        )
        if metrics is not None:
            self.persist()
            self.emit("context.compacted", {"instance": instance, **metrics})
        return messages

    async def review(self, frame, candidate, instance):
        self.emit("completion.checking", {"instance": instance})
        context = CompletionInput(
            frame["task"],
            deepcopy(frame["plan"]),
            deepcopy(frame["evidence"]),
            deepcopy(frame["messages"]),
            candidate,
            instance,
        )
        decision = await self.modules.completion.review(
            context,
            self._policy_model("verification", instance, frame.setdefault("review_models", {})),
        )
        result = asdict(decision)
        self.emit("completion.checked", {"instance": instance, **result})
        return result

    def _policy_model(self, purpose, instance, slots=None):
        slots = slots if slots is not None else {}
        cursor = 0

        async def invoke(request):
            nonlocal cursor
            if request.tools:
                raise ValueError("压缩和完成检查不能调用行动工具")
            operation = self._operation(slots, str(cursor))
            cursor += 1
            return await self.model(
                ModelRequest(request.messages, [], purpose, instance, request.options), operation
            )

        invoke.fork = lambda key: self._policy_model(
            purpose, instance, slots.setdefault("fork:" + key, {})
        )
        return invoke

    async def loop(self, config, task, history=(), instance="main", history_metadata=None):
        if instance != "main":
            return await self._lifecycle(
                "subagent",
                instance,
                task,
                lambda: self._loop(config, task, history, instance, history_metadata),
            )
        return await self._loop(config, task, history, instance, history_metadata)

    async def _lifecycle(self, kind, instance, task, action):
        record = self.state["lifecycles"].setdefault(instance, {"operation_id": uuid.uuid4().hex})
        record.setdefault("task", self.state["frames"].get(instance, {}).get("task", task))
        output = await self.lifecycle.run(
            kind,
            instance,
            record["task"],
            action,
            record=record,
            save=self.persist,
            outcome=lambda: self.state["frames"].get(instance, {}).get("outcome"),
        )
        # resume() clears the main terminal marker. Recovering only an after Hook
        # must restore the completed Loop's fact without invoking it again.
        frame = self.state["frames"].get(instance)
        raw = record["operation"]["raw_output"]
        if frame is not None:
            frame.update(outcome=raw["outcome"], output=raw["output"])
            self.persist()
        return output

    async def _loop(self, config, task, history=(), instance="main", history_metadata=None):
        if self._closed:
            raise RuntimeError("Runtime 已关闭")
        tools = self.tools.definitions(config, instance)
        frame = self.state["frames"].get(instance)
        if frame is None:
            context = self.modules.context.create(
                task, self.prompt(config, instance), history, history_metadata
            )
            frame = {
                "task": task,
                "messages": context.messages,
                "context": context.state,
                "plan": [],
                "evidence": [],
                "iterations": 0,
                "pending": None,
                "candidate": None,
            }
            self.state["frames"][instance] = frame
            if instance == "main":
                for item in self.state.pop("pending_inputs", []):
                    self.modules.context.queue_input(context, item["content"])
                    frame["task"] += "\n用户补充：" + item["content"]
            self.persist()
        context = self._context(frame)
        if frame.get("steering"):
            # Upgrade the old single steering slot into the durable queue.
            self.modules.context.queue_input(context, frame.pop("steering"))
            self.persist()
        if frame.get("outcome"):
            return frame["output"]
        while frame["iterations"] < self.modules.limits.max_iterations:
            if frame["pending"]:
                await self.pending_tools(config, frame, instance)
            pending_action = frame.get("cycle", {}).get("action")
            if pending_action and context.state.get("pending_inputs"):
                operation = self.state["operations"][pending_action]
                if (
                    operation.get("actual_status") == "not_started"
                    and operation.get("before", {}).get("status") == "rejected"
                ):
                    # A new user input may satisfy a previous explicit rejection.
                    # This starts a new logical request; uncertain Hooks stay blocked.
                    self._archive_cycle(frame)
                    pending_action = None
            if not pending_action and self.modules.context.drain_inputs(context):
                # Pending model outcomes must be consumed before newer inputs can
                # replace their prepared source. Normal safe-boundary supplements
                # start a fresh preparation, preserving the old operation audit.
                cycle = frame.get("cycle", {})
                action_id = cycle.get("action")
                if action_id is None:
                    self._archive_cycle(frame)
                frame["candidate"] = None
                frame.pop("review_models", None)
                self.persist()
            if frame["candidate"] is not None:
                candidate = frame["candidate"]
                result = await self.review(frame, candidate, instance)
                if result["decision"] in ("complete", "blocked"):
                    output = candidate
                    if result["decision"] == "blocked":
                        output += "\n\n任务尚未完成：" + result["reason"]
                        if result.get("next_action"):
                            output += "\n需要：" + result["next_action"]
                    frame.update(outcome=result["decision"], output=output)
                    self.persist()
                    return output
                frame["candidate"] = None
                frame.pop("review_models", None)
                self.modules.context.append(
                    context,
                    {
                        "role": "user",
                        "content": "完成检查发现仍有工作："
                        + result["reason"]
                        + "\n请继续执行："
                        + result.get("next_action", ""),
                    },
                    "feedback",
                )
                self.persist()
            pending_action = frame.get("cycle", {}).get("action")
            if pending_action:
                operation = self.state["operations"][pending_action]
                request = payload_request(operation["original_input"], instance)
            else:
                messages = await self.compact(frame, instance, tools)
                operation = self._operation(frame.setdefault("cycle", {}), "action")
                request = ModelRequest(messages, tools, instance=instance)
            frame["iterations"] += 1
            message = await self.model(request, operation)
            raw_message = deepcopy(operation["raw_output"])
            message = {
                k: v
                for k, v in message.items()
                if k in ("role", "content", "tool_calls", "reasoning_content")
            }
            message["role"] = "assistant"
            raw_message = {
                key: value
                for key, value in raw_message.items()
                if key in ("role", "content", "tool_calls", "reasoning_content")
            }
            self.modules.context.append(
                context, raw_message, "model", visible_message=message, model_step=True
            )
            if message.get("tool_calls"):
                frame["pending"] = {"calls": message["tool_calls"], "index": 0, "stage": "ready"}
            elif feedback := self.modules.strategy.candidate_feedback(
                deepcopy(frame["plan"]), instance
            ):
                self.modules.context.append(
                    context, {"role": "user", "content": feedback}, "feedback"
                )
            else:
                candidate = (message.get("content") or "").strip()
                if not candidate:
                    raise RuntimeError("模型未返回有效动作或回答，可以恢复重试")
                frame["candidate"] = candidate
            self._archive_cycle(frame)
            self.persist()
        raise RuntimeError(
            f"达到本次执行轮次限制（{self.modules.limits.max_iterations} 次），可以恢复继续"
        )

    def _archive_cycle(self, frame):
        cycle = frame.pop("cycle", None)
        if cycle:
            self.state.setdefault("context_operations", []).append(cycle.get("prepare", {}))

    async def pending_tools(self, config, frame, instance):
        pending = frame["pending"]
        while pending["index"] < len(pending["calls"]):
            call = pending["calls"][pending["index"]]
            name = call["function"]["name"]
            uncertain = pending["stage"] == "executing"
            pending["stage"] = "executing"
            request_id = pending.setdefault("request_id", uuid.uuid4().hex)
            invocation_state = self.tools.invocation_state(pending)
            self.persist()
            context = build_tool_context(
                config=config,
                frame=frame,
                state=self.state,
                pending=invocation_state,
                instance=instance,
                root_workspace=self.workspace,
                emit=self.emit,
                persist=self.persist,
                run_child=self.loop,
                model_id=self.model_id,
                authorize_tool=lambda definition: self.modules.strategy.authorize_tool(
                    definition,
                    deepcopy(frame["plan"]),
                    instance,
                ),
            )
            request = ToolRequest(call["id"], name, call["function"]["arguments"], uncertain)
            try:
                outcome = await self.tools.bus.request(
                    TOOL_REQUEST_TOPIC,
                    request,
                    context=context,
                    correlation_id=request_id,
                    timeout=600,
                )
            except asyncio.CancelledError:
                # Keep the in-flight request in the checkpoint for safe recovery.
                raise
            except (ToolPersistenceError, HookError):
                raise
            except Exception as exc:
                outcome = ToolOutcome({"error": str(exc) or "工具请求未完成"}, "failed")
                self.emit(
                    "tool.failed",
                    {
                        "instance": instance,
                        "name": name,
                        "request_id": request_id,
                        "call_id": call["id"],
                        "error": outcome.value["error"],
                    },
                )
            result, status = outcome.value, outcome.status
            content = json.dumps(result, ensure_ascii=False)
            raw_content = (
                json.dumps(outcome.raw_value, ensure_ascii=False)
                if outcome.has_raw_value
                else content
            )
            self.modules.context.append(
                self._context(frame),
                {"role": "tool", "tool_call_id": call["id"], "content": raw_content},
                "tool",
                visible_message={
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": content[:24000]
                    + (
                        "\n[结果过长，仅展示前段；必要时按文件继续读取]"
                        if len(content) > 24000
                        else ""
                    ),
                },
            )
            frame["evidence"].append(
                {
                    "tool": name,
                    "arguments": outcome.evidence_arguments,
                    "status": status,
                    "result": content[:3000],
                }
            )
            frame["evidence"] = frame["evidence"][-20:]
            pending.update(index=pending["index"] + 1, stage="ready")
            hook_operation = pending.get("handler_state", {}).get("__tool_runtime_operation")
            if hook_operation is not None:
                self.state.setdefault("tool_operations", {})[request_id] = deepcopy(hook_operation)
            pending.pop("handler_state", None)
            pending.pop("request_id", None)
            self.persist()
        frame["pending"] = None
        self.persist()

    async def close(self):
        if self._active_task is asyncio.current_task():
            raise RuntimeError("不能在执行中的模块内部关闭所属 Runtime")
        if self._close_task is not None:
            await asyncio.shield(self._close_task)
            return
        self._closed = True
        self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self):
        if self._active_task is not None:
            self._active_task.cancel()
            await asyncio.gather(self._active_task, return_exceptions=True)
        # Release dependencies only after requests have stopped. Subsequent close
        # callers observe the same completion or explicit cleanup failure.
        await self.tools.close()
        await self.modules.aclose()
