"""Deterministic, checkpointed Hook pipelines at explicitly registered boundaries."""

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
from dataclasses import asdict

from jsonschema import Draft202012Validator

from ..observation import freeze_observation
from ..tool_contracts import ToolPersistenceError
from .contracts import (
    SCOPE_FIELDS,
    Continue,
    HookBinding,
    HookContext,
    HookFailed,
    HookRecoveryRequired,
    HookRejected,
    PatchInput,
    PatchOutput,
    Reject,
)
from .executor import HookExecutor
from .registry import HookPointRegistry, HookRegistry, identifier, json_copy


def persist(save):
    try:
        result = save()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Hook checkpoint callback must be synchronous")
    except ToolPersistenceError:
        raise
    except Exception:
        raise ToolPersistenceError("Hook checkpoint could not be persisted") from None


def canonical_purpose(value):
    return "completion" if value == "verification" else value


def scope_snapshot(scope):
    if type(scope) is not dict or set(scope) - SCOPE_FIELDS:
        raise ValueError("Hook scope accepts only declared identity fields")
    result = json_copy(scope, limit=16 * 1024)
    if any(value is not None and type(value) not in (str, int) for value in result.values()):
        raise ValueError("Hook identity fields must be strings, integers or null")
    result.setdefault("instance_id", "main")
    if "purpose" in result:
        result["purpose"] = canonical_purpose(result["purpose"])
    return result


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


async def validate_payload(payload, validator):
    value = json_copy(payload)
    if type(value) is not dict:
        raise HookFailed("Hook boundary payload must be an object")
    if validator is None:
        return value
    # This callback belongs to the host (authorization, budget, schema), not the
    # extension. Preserve its useful error type; _apply's caller sanitizes errors
    # arising from an actual Hook patch before exposing HookFailed.
    candidate = validator(copy.deepcopy(value))
    if inspect.isawaitable(candidate):
        candidate = await candidate
    if candidate is not None:
        value = json_copy(candidate)
        if type(value) is not dict:
            raise ValueError("Validator must return an object or None")
    return value


class HookManager:
    module_id = "hooks/v1"

    def __init__(self, registry=None, bindings=(), points=None, total_timeout=10):
        if (
            isinstance(total_timeout, bool)
            or not isinstance(total_timeout, (int, float))
            or not math.isfinite(total_timeout)
            or total_timeout <= 0
            or total_timeout > 300
        ):
            raise ValueError("Hook pipeline timeout must be within (0, 300] seconds")
        self.total_timeout = float(total_timeout)
        self.points = (points or HookPointRegistry()).snapshot()
        self.executor = HookExecutor()
        self._bindings = []
        self._closed = False
        registry = registry or HookRegistry()
        identities = set()
        for order, original in enumerate(bindings):
            if not isinstance(original, HookBinding):
                raise TypeError("Expected HookBinding")
            binding = copy.deepcopy(original)
            identifier(binding.binding_id, "Hook binding ID")
            if binding.binding_id in identities:
                raise ValueError("Hook binding IDs must be unique")
            identities.add(binding.binding_id)
            point = self.points.resolve(binding.point)
            definition = registry.resolve(binding.hook_id, binding.version)
            if type(binding.priority) is not int:
                raise ValueError("Hook priority must be an integer")
            if (
                isinstance(binding.timeout, bool)
                or not isinstance(binding.timeout, (int, float))
                or not math.isfinite(binding.timeout)
                or not 0 < binding.timeout <= 300
            ):
                raise ValueError("Hook timeout must be within (0, 300] seconds")
            if binding.failure_policy not in {"block", "continue"}:
                raise ValueError("Invalid Hook failure policy")
            if binding.failure_policy == "continue" and not definition.observation:
                raise ValueError("Only pure observation Hooks may continue after failure")
            if point.observation_only and not definition.observation:
                raise ValueError("Observation-only points require an observation Hook")
            for values in (binding.purposes, binding.instances, binding.targets):
                if values is not None and (
                    not isinstance(values, (tuple, list))
                    or any(not isinstance(value, str) or not value for value in values)
                ):
                    raise ValueError("Hook match fields must be sequences of identifiers")
            if not binding.instances or set(binding.instances) - {"main", "child"}:
                raise ValueError("Hook instances must select main and/or child")
            config = json_copy(binding.config, limit=256 * 1024)
            if type(config) is not dict:
                raise ValueError("Hook configuration must be an object")
            if not Draft202012Validator(definition.config_schema).is_valid(config):
                raise ValueError("Hook binding configuration failed its schema")
            # Freeze both catalog contracts and bindings, including original insertion order.
            signature = {
                **asdict(binding),
                "version": definition.version,
                "config": config,
                "config_schema": definition.config_schema,
                "code_hash": definition.code_hash,
                "replay_safe": definition.replay_safe,
                "observation": definition.observation,
                "point_contract": asdict(point),
                "order": order,
            }
            self._bindings.append((binding, definition, json_copy(signature)))
        self._bindings.sort(key=lambda entry: (entry[0].priority, entry[2]["order"]))

    def checkpoint_config(self):
        return json_copy(
            {
                "total_timeout": self.total_timeout,
                "bindings": [entry[2] for entry in self._bindings],
            }
        )

    def _matching(self, point, scope):
        instance = "main" if scope.get("instance_id") in (None, "main") else "child"
        for binding, definition, signature in self._bindings:
            if binding.point != point or instance not in binding.instances:
                continue
            purposes = binding.purposes
            if purposes is None and point.startswith("model.embedding."):
                purposes = ("document", "query")
            elif purposes is None and point.startswith("model."):
                purposes = ("action",)
            if purposes is not None and canonical_purpose(scope.get("purpose")) not in {
                canonical_purpose(value) for value in purposes
            }:
                continue
            if binding.targets and scope.get("target") not in binding.targets:
                continue
            yield binding, definition, signature

    async def run(self, point, payload, *, scope, state, save, validate=None):
        if self._closed:
            raise HookFailed("Hook manager is closed")
        contract = self.points.resolve(point)
        scope = scope_snapshot(scope)
        original = json_copy(payload)
        if type(original) is not dict:
            raise HookFailed("Hook payload must be an object")
        if not Draft202012Validator(contract.schema).is_valid(original):
            raise HookFailed("Hook payload failed the point schema")
        matching = list(self._matching(point, scope))
        chain = digest([entry[2] for entry in matching])
        if not state:
            state.update(
                format=1,
                point=point,
                chain=chain,
                scope=scope,
                input_digest=digest(original),
                current=original,
                cursor=0,
                records=[],
                status="pending",
            )
            persist(save)
        elif (
            state.get("format") != 1
            or state.get("point") != point
            or state.get("chain") != chain
            or state.get("scope") != scope
            or state.get("input_digest") != digest(original)
        ):
            raise HookRecoveryRequired("Hook pipeline identity or published chain changed")
        if state.get("status") == "rejected":
            rejection = state["rejection"]
            raise HookRejected(**rejection)
        if state.get("status") == "completed":
            return json_copy(state["current"])
        deadline = asyncio.get_running_loop().time() + self.total_timeout
        while state["cursor"] < len(matching):
            index = state["cursor"]
            binding, definition, _ = matching[index]
            previous = next((r for r in state["records"] if r["index"] == index), None)
            if previous and previous["status"] in {"running", "failed", "cancelled"}:
                if not definition.replay_safe:
                    raise HookRecoveryRequired(
                        f"Hook {binding.binding_id} needs reconciliation before recovery"
                    )
            record = {
                "index": index,
                "binding_id": binding.binding_id,
                "hook_id": definition.hook_id,
                "version": definition.version,
                "status": "running",
                "attempt": (previous or {}).get("attempt", 0) + 1,
            }
            state["records"] = [r for r in state["records"] if r["index"] != index] + [record]
            state["status"] = "running"
            persist(save)
            started = asyncio.get_running_loop().time()
            remaining = max(0, deadline - started)
            context = HookContext(
                point,
                binding.binding_id,
                freeze_observation(binding.config),
                freeze_observation(scope),
                time.time() + min(binding.timeout, remaining),
            )
            try:
                if remaining <= 0:
                    raise TimeoutError("Hook pipeline budget exhausted")
                decision = await self.executor.invoke(
                    definition.handler,
                    context,
                    freeze_observation(state["current"]),
                    min(binding.timeout, remaining),
                )
                effective = await self._apply(
                    decision, contract, definition, state["current"], validate, binding.binding_id
                )
            except asyncio.CancelledError:
                record["status"] = "cancelled"
                state["status"] = "cancelled"
                # Failure to checkpoint cancellation must not replace cancellation itself.
                try:
                    persist(save)
                except ToolPersistenceError:
                    pass
                raise
            except ToolPersistenceError:
                raise
            except HookRejected as exc:
                record["status"] = "rejected"
                state["status"] = "rejected"
                state["rejection"] = {
                    "code": exc.code,
                    "reason": exc.reason,
                    "binding_id": binding.binding_id,
                }
                persist(save)
                raise
            except Exception as exc:
                record["error"] = "timeout" if isinstance(exc, TimeoutError) else "hook_failed"
                record["duration_ms"] = round(
                    (asyncio.get_running_loop().time() - started) * 1000, 3
                )
                if contract.phase in {"error", "finally"} or (
                    definition.observation and binding.failure_policy == "continue"
                ):
                    record["status"] = "ignored"
                    state["cursor"] += 1
                    persist(save)
                    continue
                record["status"] = "failed"
                state["status"] = "failed"
                persist(save)
                raise HookFailed(f"Hook {binding.binding_id} failed at {point}") from None
            record["duration_ms"] = round((asyncio.get_running_loop().time() - started) * 1000, 3)
            record["status"] = "completed"
            record["decision"] = type(decision).__name__
            record["patch_fields"] = (
                sorted(decision.patch) if isinstance(decision, (PatchInput, PatchOutput)) else []
            )
            state["current"] = effective
            state["cursor"] += 1
            persist(save)
        state["status"] = "completed"
        persist(save)
        return json_copy(state["current"])

    async def _apply(self, decision, point, definition, value, validate, binding_id):
        if isinstance(decision, Continue):
            return json_copy(value)
        if point.observation_only or definition.observation:
            raise HookFailed("Observation Hooks may only return Continue")
        if isinstance(decision, Reject):
            if not point.allow_reject or point.phase != "before":
                raise HookFailed("Hook cannot reject at this point")
            if not isinstance(decision.code, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,80}", decision.code
            ):
                raise HookFailed("Hook rejection code is invalid")
            if not isinstance(decision.reason, str) or not 1 <= len(decision.reason) <= 512:
                raise HookFailed("Hook rejection reason is invalid")
            raise HookRejected(decision.code, decision.reason, binding_id=binding_id)
        expected = PatchInput if point.phase == "before" else PatchOutput
        if not isinstance(decision, expected) or point.phase not in {"before", "after"}:
            raise HookFailed("Hook returned an unsupported decision")
        patch = json_copy(decision.patch, limit=1024 * 1024)
        if type(patch) is not dict or set(patch) - set(point.writable_fields):
            raise HookFailed("Hook patch changes a protected field")
        candidate = {**json_copy(value), **patch}
        if not Draft202012Validator(point.schema).is_valid(candidate):
            raise HookFailed("Hook patch failed the point schema")
        return await validate_payload(candidate, validate)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self.executor.aclose()
