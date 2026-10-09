"""Persist real operation results before output Hooks, enabling safe recovery."""

import asyncio
from uuid import uuid4

from ..tool_contracts import ToolPersistenceError
from .contracts import HookRecoveryRequired
from .manager import digest, persist, scope_snapshot, validate_payload
from .registry import json_copy


async def execute_operation(
    manager,
    kind,
    payload,
    action,
    *,
    scope,
    state,
    save,
    validate_input=None,
    validate_output=None,
    prepare_input=None,
    validate_prepared=None,
    resume_inflight=False,
):
    """Execute one stable logical operation, not one network attempt.

    The caller owns ``state`` inside its checkpoint and provides a synchronous
    ``save()``. An actual result is saved in ``raw_output`` before any after Hook.
    ``effective_output`` is the separately validated model/application projection.
    Reusing this state never repeats a completed actual operation. Resuming an
    unknown action requires the caller's explicit replay policy.
    """
    scope = scope_snapshot(scope)
    operation_id = scope.get("operation_id") or state.get("operation_id") or uuid4().hex
    if state.get("operation_id", operation_id) != operation_id:
        raise HookRecoveryRequired("Operation ID does not match its saved checkpoint")
    scope["operation_id"] = operation_id
    state.setdefault("operation_id", operation_id)
    chain = digest(manager.checkpoint_config())
    if not state or set(state) <= {"operation_id"}:
        state.update(
            format=1,
            kind=kind,
            scope=scope,
            chain=chain,
            stage="before",
            actual_status="not_started",
            original_input=json_copy(payload),
            before={},
            after={},
            attempts=[],
        )
        persist(save)
    elif (
        state.get("format") != 1
        or state.get("kind") != kind
        or state.get("scope") != scope
        or state.get("chain") != chain
    ):
        raise HookRecoveryRequired("Operation identity or published Hook chain changed")
    if state.get("stage") == "completed":
        return json_copy(state["effective_output"])
    if len(state["attempts"]) >= 128:
        raise HookRecoveryRequired("Operation exceeded its bounded recovery history")
    attempt = {"number": len(state["attempts"]) + 1, "error": {}, "finally": {}}
    state["attempts"].append(attempt)
    original_error = None
    try:
        if "final_input" not in state:
            state["stage"] = "before"
            value = await manager.run(
                kind + ".before",
                state["original_input"],
                scope=scope,
                state=state["before"],
                save=save,
                validate=validate_input,
            )
            if "before_input" not in state:
                state["before_input"] = await validate_payload(value, validate_input)
                persist(save)
            value = json_copy(state["before_input"])
            if prepare_input is not None:
                # Host-owned, resumable preparation (e.g. a bounded budget recheck)
                # may reshape the model request after the Hook pipeline has finished.
                # Its own nested operations must be checkpointed by the host.
                state["stage"] = "preparing"
                persist(save)
                value = await prepare_input(value)
            state["final_input"] = await validate_payload(value, validate_prepared)
            persist(save)
        if "raw_output" not in state:
            if state["actual_status"] in {"running", "unknown", "failed", "succeeded"}:
                if not resume_inflight:
                    raise HookRecoveryRequired(
                        "Actual operation result is unknown; reconcile it before recovery"
                    )
            # A recovered final input still passes current mandatory host checks
            # immediately before dispatch, without repeating Hooks or preparation.
            state["final_input"] = await validate_payload(state["final_input"], validate_prepared)
            state["stage"] = "executing"
            state["actual_status"] = "running"
            persist(save)
            try:
                raw = await action(json_copy(state["final_input"]))
                # Capture all returned data before validation or output transformations.
                raw = json_copy(raw)
            except BaseException:
                state["actual_status"] = "unknown"
                raise
            state["raw_output"] = raw
            status = raw.get("status") if isinstance(raw, dict) else None
            state["actual_status"] = (
                status if status in {"failed", "unknown", "cancelled"} else "succeeded"
            )
            state["stage"] = "after"
            persist(save)
        state["stage"] = "after"
        if state["actual_status"] in {"failed", "unknown", "cancelled"}:
            await _notify(manager, "operation.error", kind, scope, state, attempt["error"], save)
        # Validation failures retain the returned fact and stop at postprocessing.
        output = await validate_payload(state["raw_output"], validate_output)
        output = await manager.run(
            kind + ".after",
            output,
            scope=scope,
            state=state["after"],
            save=save,
            validate=validate_output,
        )
        state["effective_output"] = await validate_payload(output, validate_output)
        state["stage"] = "completed"
        persist(save)
        return json_copy(state["effective_output"])
    except BaseException as exc:
        original_error = exc
        # Structured metadata only; arbitrary exception strings may contain secrets.
        state["last_error"] = {
            "type": "cancelled" if isinstance(exc, asyncio.CancelledError) else "operation_failed",
            "stage": state["stage"],
        }
        try:
            persist(save)
        except ToolPersistenceError:
            if not isinstance(exc, (asyncio.CancelledError, ToolPersistenceError)):
                raise
        # Persistence failure means no further required writes are trustworthy.
        if not isinstance(exc, ToolPersistenceError):
            await _notify(
                manager,
                "operation.error",
                kind,
                scope,
                state,
                attempt["error"],
                save,
                suppress=True,
            )
        raise
    finally:
        if not isinstance(original_error, ToolPersistenceError):
            await _notify(
                manager,
                "operation.finally",
                kind,
                scope,
                state,
                attempt["finally"],
                save,
                suppress=original_error is not None,
            )


async def _notify(
    manager, point, kind, scope, operation_state, pipeline_state, save, *, suppress=False
):
    payload = {
        "kind": kind,
        "stage": operation_state["stage"],
        "actual_status": operation_state["actual_status"],
        "error": operation_state.get("last_error"),
        "completed_before": operation_state["before"].get("cursor", 0),
        "completed_after": operation_state["after"].get("cursor", 0),
    }
    # A single exit may report an actual failure then an after-Hook failure; preserve
    # the first diagnostic pipeline without recursively running it a second time.
    if pipeline_state:
        return
    try:
        await manager.run(point, payload, scope=scope, state=pipeline_state, save=save)
    except asyncio.CancelledError:
        if not suppress:
            raise
    except ToolPersistenceError:
        if not suppress:
            raise
    except Exception:
        # Diagnostic Hooks never replace actual results or the original exception.
        return
