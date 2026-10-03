"""Tool catalog and request handler; the model loop never selects implementations."""

import asyncio
import json

from jsonschema import ValidationError
from jsonschema.validators import validator_for

from .event_bus import EventBus
from .tool_contracts import TOOL_REQUEST_TOPIC, ToolDefinition, ToolOutcome, bindings


class ToolRegistry:
    def __init__(self):
        self._definitions = {}
        self._validators = {}

    def register(self, definition: ToolDefinition):
        if definition.name in self._definitions:
            raise ValueError("工具名称重复：" + definition.name)
        if definition.timeout <= 0:
            raise ValueError("工具超时必须大于零")
        validator = validator_for(definition.parameters)
        validator.check_schema(definition.parameters)
        self._definitions[definition.name] = definition
        self._validators[definition.name] = validator(definition.parameters)

    def resolve(self, name, config, instance):
        definition = self._definitions.get(name)
        if definition is None or not definition.available(config, instance):
            raise ValueError("工具未授权")
        return definition

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
        self.registry = registry or ToolRegistry()
        self.bus = bus or EventBus()
        self._owns_bus = bus is None
        self.bus.register(TOOL_REQUEST_TOPIC, self._execute)

    def definitions(self, config, instance):
        return self.registry.definitions(config, instance)

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
            if (
                context.instance == "main"
                and context.config["mode"] == "plan"
                and not context.frame["plan"]
                and not definition.before_plan
            ):
                raise ValueError("Plan 策略需要先调用 update_plan 创建执行计划")
            async with asyncio.timeout(definition.timeout):
                result = await definition.handler(context, arguments)
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except asyncio.CancelledError:
            context.emit("tool.cancelled", trace)
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
        if self._owns_bus:
            await self.bus.close()
