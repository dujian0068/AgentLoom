"""Tool catalog and request handler; the model loop never selects implementations."""

import asyncio
import copy
import json
from contextlib import contextmanager
from dataclasses import replace

from jsonschema import ValidationError
from jsonschema.validators import validator_for

from .event_bus import EventBus
from .tool_contracts import (
    TOOL_REQUEST_TOPIC,
    ToolDefinition,
    ToolOutcome,
    ToolPersistenceError,
    bindings,
)


class ToolRegistry:
    def __init__(self):
        self._definitions = {}
        self._validators = {}
        self._frozen = False
        self._implementation_namespace = None

    @property
    def frozen(self):
        return self._frozen

    def freeze(self):
        self._frozen = True
        return self

    @contextmanager
    def implementation_namespace(self, namespace):
        """Explicitly version one registration batch; later custom tools stay unversioned."""
        if self._frozen:
            raise RuntimeError("工具注册表已冻结，运行期间不可注册工具")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("工具实现命名空间不能为空")
        previous = self._implementation_namespace
        self._implementation_namespace = namespace
        try:
            yield self
        finally:
            self._implementation_namespace = previous

    def register(self, definition: ToolDefinition):
        if self._frozen:
            raise RuntimeError("工具注册表已冻结，运行期间不可注册工具")
        if definition.name in self._definitions:
            raise ValueError("工具名称重复：" + definition.name)
        if definition.timeout <= 0:
            raise ValueError("工具超时必须大于零")
        implementation_id = definition.implementation_id
        if implementation_id is None and self._implementation_namespace is not None:
            handler = definition.handler
            module = getattr(handler, "__module__", type(handler).__module__)
            name = getattr(handler, "__qualname__", type(handler).__qualname__)
            implementation_id = f"{self._implementation_namespace}:{module}.{name}"
        if implementation_id is not None and (
            not isinstance(implementation_id, str) or not implementation_id.strip()
        ):
            raise ValueError("工具实现版本不能为空")
        # Callables retain their identity; callers cannot mutate the registered schema.
        definition = replace(
            definition,
            parameters=copy.deepcopy(definition.parameters),
            evidence_fields=tuple(definition.evidence_fields),
            implementation_id=implementation_id,
        )
        validator = validator_for(definition.parameters)
        validator.check_schema(definition.parameters)
        self._definitions[definition.name] = definition
        self._validators[definition.name] = validator(definition.parameters)

    def resolve(self, name, config, instance):
        definition = self._definitions.get(name)
        if definition is None or not definition.available(config, instance):
            raise ValueError("工具未授权")
        return replace(definition, parameters=copy.deepcopy(definition.parameters))

    def bindings(self):
        """Detached, deterministic checkpoint manifest; None is explicitly unversioned."""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "parameters": copy.deepcopy(definition.parameters),
                "before_plan": definition.before_plan,
                "resume_inflight": definition.resume_inflight,
                "timeout": definition.timeout,
                "evidence_fields": list(definition.evidence_fields),
                "implementation_id": definition.implementation_id,
            }
            for _, definition in sorted(self._definitions.items())
        ]

    def definitions(self, config, instance):
        return [
            definition.model_schema()
            for definition in self._definitions.values()
            if definition.available(config, instance)
        ]

    def validate(self, name, arguments):
        try:
            self._validators[name].validate(arguments)
        except ValidationError as exc:
            # Avoid echoing the full argument body (it can contain sensitive content).
            location = ".".join(str(part) for part in exc.absolute_path) or "参数"
            raise ValueError("工具参数不合法：" + location) from None


class ToolRuntime:
    def __init__(self, snapshot, registry=None, bus=None):
        self.snapshot = snapshot
        self.registry = (registry or ToolRegistry()).freeze()
        self.bus = bus or EventBus()
        self._owns_bus = bus is None
        self._unregister = self.bus.register(TOOL_REQUEST_TOPIC, self._execute)
        self._closed = False
        self._closing = False

    def definitions(self, config, instance):
        return self.registry.definitions(config, instance)

    def bindings(self):
        return self.registry.bindings()

    def instructions(self, config, instance):
        hints = []
        skills = bindings(self.snapshot, config, "skills")
        if skills:
            hints.append(
                "可用技能（按需 skill_load）："
                + json.dumps(
                    [
                        {key: skill[key] for key in ("id", "name", "description")}
                        for skill in skills
                    ],
                    ensure_ascii=False,
                )
            )
        if instance == "main" and config["subs"]:
            hints.append(
                "可委派子 Agent："
                + json.dumps(
                    [
                        {key: child[key] for key in ("id", "name", "description")}
                        for child in config["subs"]
                    ],
                    ensure_ascii=False,
                )
            )
        return "\n".join(hints)

    @staticmethod
    def invocation_state(pending):
        state = pending.setdefault("handler_state", {})
        # Read old format-1 checkpoints without teaching the loop about delegation.
        if "child_instance" in pending:
            state.setdefault("child_instance", pending.pop("child_instance"))
        return state

    async def _execute(self, event):
        if self._closed or self._closing:
            raise RuntimeError("工具运行时已关闭")
        request, context = event.payload, event.context
        trace = {
            "instance": context.instance,
            "name": request.name,
            "call_id": request.call_id,
            "request_id": event.correlation_id,
        }
        context.emit("tool.started", trace)
        evidence = {}
        try:
            definition = self.registry.resolve(request.name, context.config, context.instance)
            arguments = json.loads(request.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须为 JSON 对象")
            self.registry.validate(request.name, arguments)
            evidence = {key: arguments.get(key) for key in definition.evidence_fields}
            if request.uncertain and not definition.resume_inflight:
                context.emit("tool.uncertain", trace)
                raise RuntimeError(
                    "上次调用在执行中中断，结果未知；先检查文件或外部状态，避免重复有副作用的操作"
                )
            context.authorize_tool(definition)
            async with asyncio.timeout(definition.timeout):
                result = await definition.handler(context, arguments)
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except asyncio.CancelledError:
            context.emit("tool.cancelled", trace)
            raise
        except ToolPersistenceError:
            raise
        except Exception as exc:
            error = (
                "工具执行超时，结果可能未确认；请先核对实际状态再决定后续动作"
                if isinstance(exc, TimeoutError)
                else str(exc)
            )
            context.emit("tool.failed", {**trace, "error": error})
            return ToolOutcome({"error": error}, "failed", evidence)
        context.emit("tool.completed", {**trace, "result": result})
        return ToolOutcome(result, "succeeded", evidence)

    async def close(self):
        if self._closed:
            return
        self._closing = True
        try:
            if self._owns_bus:
                await self.bus.close()
            else:
                await self.bus.cancel_topic(TOOL_REQUEST_TOPIC)
            self._unregister()
            self._closed = True
        finally:
            self._closing = False
