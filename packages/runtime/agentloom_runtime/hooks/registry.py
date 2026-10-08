"""Versioned extension and Hook-point catalogs, frozen when a manager is built."""

import copy
import hashlib
import inspect
import json
import types
from dataclasses import replace

from jsonschema import Draft202012Validator

from ..observation import freeze_observation
from .contracts import HookDefinition, HookPoint

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def json_copy(value, *, limit=MAX_PAYLOAD_BYTES):
    freeze_observation(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("Hook data exceeds the configured size limit")
    return json.loads(encoded)


def identifier(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{label} must be a nonempty identifier of at most 200 characters")
    return value


def handler_code_hash(handler):
    """Fingerprint the function's code; dependency versions need an explicit hash."""
    try:
        source = inspect.getsource(handler).encode("utf-8")
    except (OSError, TypeError):

        def code_data(code):
            return {
                "bytecode": code.co_code.hex(),
                "names": code.co_names,
                "variables": code.co_varnames,
                "constants": [
                    code_data(value) if isinstance(value, types.CodeType) else repr(value)
                    for value in code.co_consts
                ],
            }

        source = json.dumps(code_data(handler.__code__), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(source).hexdigest()


class HookRegistry:
    def __init__(self):
        self._definitions = {}

    def register(self, definition: HookDefinition):
        if not isinstance(definition, HookDefinition):
            raise TypeError("Expected HookDefinition")
        identifier(definition.hook_id, "Hook ID")
        identifier(definition.version, "Hook version")
        if not inspect.iscoroutinefunction(definition.handler):
            raise ValueError("Trusted Hook handlers must be async functions")
        if type(definition.replay_safe) is not bool or type(definition.observation) is not bool:
            raise ValueError("Hook replay_safe and observation must be booleans")
        if definition.code_hash is not None:
            identifier(definition.code_hash, "Hook code hash")
        schema = json_copy(definition.config_schema)
        Draft202012Validator.check_schema(schema)
        key = (definition.hook_id, definition.version)
        if key in self._definitions:
            raise ValueError("Hook ID and version already registered")
        self._definitions[key] = HookDefinition(
            definition.hook_id,
            definition.version,
            definition.handler,
            schema,
            definition.replay_safe,
            definition.observation,
            definition.code_hash or handler_code_hash(definition.handler),
        )
        return definition

    def resolve(self, hook_id, version=None):
        if version is not None:
            definition = self._definitions.get((hook_id, version))
        else:
            matches = [d for (name, _), d in self._definitions.items() if name == hook_id]
            if len(matches) > 1:
                raise ValueError("A Hook binding must select an explicit version")
            definition = matches[0] if matches else None
        if definition is None:
            raise ValueError("Hook binding references an unregistered version")
        # The callable is an injected capability, not checkpoint data. In
        # particular deepcopy would silently clone a bound method's owner.
        return replace(definition, config_schema=copy.deepcopy(definition.config_schema))


class HookPointRegistry:
    def __init__(self, *, include_builtins=True):
        self._points = {}
        if include_builtins:
            for point in builtin_points():
                self.register(point)

    def register(self, point: HookPoint):
        if not isinstance(point, HookPoint):
            raise TypeError("Expected HookPoint")
        identifier(point.name, "Hook point")
        if point.name in self._points:
            raise ValueError("Hook point already registered")
        if point.phase not in {"before", "after", "error", "finally"}:
            raise ValueError("Invalid Hook phase")
        if point.allow_reject and point.phase != "before":
            raise ValueError("Only before Hooks can reject an operation")
        if point.observation_only and (point.writable_fields or point.allow_reject):
            raise ValueError("Observation-only points cannot mutate or reject")
        if any(not isinstance(key, str) or not key for key in point.writable_fields):
            raise ValueError("Writable fields must be top-level names")
        schema = json_copy(point.schema)
        Draft202012Validator.check_schema(schema)
        self._points[point.name] = HookPoint(
            point.name,
            point.phase,
            tuple(point.writable_fields),
            point.allow_reject,
            point.observation_only,
            schema,
        )
        return point

    def resolve(self, name):
        try:
            return copy.deepcopy(self._points[name])
        except KeyError:
            raise ValueError("Unknown Hook point") from None

    def snapshot(self):
        result = HookPointRegistry(include_builtins=False)
        result._points = copy.deepcopy(self._points)
        return result


def builtin_points():
    points = (
        HookPoint(
            "model.chat.before", "before", ("messages", "temperature", "top_p", "max_tokens"), True
        ),
        HookPoint("model.chat.after", "after", ("content", "annotations")),
        HookPoint("model.embedding.before", "before", ("texts",), True),
        HookPoint("model.embedding.after", "after", observation_only=True),
        HookPoint("tool.before", "before", ("arguments",), True),
        HookPoint("tool.after", "after", ("value", "annotations")),
        HookPoint("context.prepare.before", "before", ("additional_messages",), True),
        HookPoint("context.prepare.after", "after", observation_only=True),
        HookPoint("context.compact.before", "before", allow_reject=True),
        HookPoint("context.compact.after", "after", ("summary",)),
        HookPoint("run.before", "before", allow_reject=True),
        HookPoint("run.after", "after", ("annotations",)),
        HookPoint("subagent.before", "before", allow_reject=True),
        HookPoint("subagent.after", "after", ("annotations",)),
        HookPoint("operation.error", "error", observation_only=True),
        HookPoint("operation.finally", "finally", observation_only=True),
    )
    string = {"type": "string"}
    nullable_string = {"type": ["string", "null"]}
    object_value = {"type": "object"}
    nullable_object = {"type": ["object", "null"]}
    array = {"type": "array"}
    messages = {"type": "array", "items": {"type": "object", "required": ["role"]}}
    annotations = {"annotations": object_value}
    status = {
        "enum": ["not_started", "running", "succeeded", "failed", "unknown", "cancelled", "blocked"]
    }
    schemas = {
        "model.chat.before": {
            "messages": messages,
            "tools": array,
            "model_id": nullable_string,
            "purpose": string,
            "temperature": {"type": ["number", "null"], "minimum": 0, "maximum": 2},
            "top_p": {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 1},
            "max_tokens": {"type": ["integer", "null"], "minimum": 1},
        },
        "model.chat.after": {
            "role": {"const": "assistant"},
            "content": nullable_string,
            "tool_calls": array,
            "reasoning_content": nullable_string,
            "usage": nullable_object,
            "finish_reason": nullable_string,
            "provider_request_id": nullable_string,
            **annotations,
        },
        "model.embedding.before": {
            "texts": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["index", "text"],
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "text": {"type": "string", "minLength": 1},
                    },
                },
            },
            "model_id": string,
            "index_signature": string,
            "dimensions": {"type": ["integer", "null"], "minimum": 1},
            "encoding_format": {"const": "float"},
            "purpose": {"enum": ["document", "query"]},
        },
        "model.embedding.after": {
            "vectors": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}},
            },
            "indices": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "dimension": {"type": "integer", "minimum": 1},
            "usage": nullable_object,
            "provider_request_id": nullable_string,
        },
        "tool.before": {
            "name": string,
            "tool_id": string,
            "implementation_id": nullable_string,
            "parameters": object_value,
            "arguments": object_value,
        },
        "tool.after": {
            "value": {},
            "status": status,
            "evidence_arguments": object_value,
            **annotations,
        },
        "context.prepare.before": {
            "task": string,
            "plan": array,
            "messages": messages,
            "tools": array,
            "additional_messages": messages,
        },
        "context.prepare.after": {"messages": messages, "metrics": nullable_object},
        "context.compact.before": {
            "task": string,
            "plan": array,
            "messages": messages,
            "tools": array,
            "counters": object_value,
        },
        "context.compact.after": {
            "compacted": {"type": "boolean"},
            "messages": messages,
            "metrics": object_value,
            "summary_slot": nullable_object,
            "summary": nullable_string,
        },
        "run.before": {"task": string},
        "run.after": {"output": {}, "status": status, **annotations},
        "subagent.before": {"task": string},
        "subagent.after": {"output": {}, "status": status, **annotations},
        "operation.error": {
            "kind": string,
            "operation": string,
            "stage": string,
            "status": status,
            "actual_status": status,
            "error": nullable_object,
            "completed_before": {"type": "integer", "minimum": 0},
            "completed_after": {"type": "integer", "minimum": 0},
        },
    }
    schemas["operation.finally"] = schemas["operation.error"]
    # Capability validators additionally check required fields, immutable protocol
    # groups, authorization and budgets against the exact versioned operation.
    return tuple(
        replace(point, schema={"type": "object", "properties": schemas[point.name]})
        for point in points
    )
