"""Host adapters bind limited tool capabilities to format-1 checkpoints.

Only this layer holds frames, global state and the recursive loop callback.
Required saves precede observation; a failed save restores the local mutation.
"""

import copy
from collections.abc import Mapping

from .planning import validate_plan
from .tool_contracts import ToolContext, ToolPersistenceError, readonly, safe_observer


def _config_copy(value):
    if isinstance(value, Mapping):
        return {key: _config_copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_config_copy(item) for item in value]
    return copy.deepcopy(value)


def _save(persist):
    try:
        persist()
    except ToolPersistenceError:
        raise
    except Exception as exc:
        raise ToolPersistenceError("工具执行检查点保存失败，任务已停止，可恢复后重试") from exc


class _PlanEditor:
    def __init__(self, frame, instance, persist, emit):
        self._frame, self._instance = frame, instance
        self._persist, self._emit = persist, emit

    def read(self):
        return readonly(self._frame["plan"])

    def update(self, steps, explanation):
        previous = self._frame["plan"]
        steps = validate_plan(steps, explanation, previous)
        old = {step["id"]: step for step in previous}
        self._frame["plan"] = steps
        try:
            _save(self._persist)
        except ToolPersistenceError:
            self._frame["plan"] = previous
            raise
        self._emit(
            "plan.updated" if old else "plan.created",
            {"instance": self._instance, "steps": copy.deepcopy(steps), "explanation": explanation},
        )
        for i, step in enumerate(steps):
            if step["status"] != old.get(step["id"], {}).get("status") and step["status"] in (
                "in_progress",
                "completed",
            ):
                self._emit(
                    "step.started" if step["status"] == "in_progress" else "step.completed",
                    {"instance": self._instance, "index": i, "name": step["step"]},
                )
        return {"steps": copy.deepcopy(steps)}


class _InvocationState:
    def __init__(self, pending, persist):
        self._pending, self._persist = pending, persist

    def get(self, key, default=None):
        return copy.deepcopy(self._pending.get(key, default))

    def set(self, key, value):
        present, previous = key in self._pending, self._pending.get(key)
        self._pending[key] = copy.deepcopy(value)
        try:
            _save(self._persist)
        except ToolPersistenceError:
            if present:
                self._pending[key] = previous
            else:
                self._pending.pop(key, None)
            raise


class _CitationRecorder:
    def __init__(self, state, persist):
        self._state, self._persist = state, persist

    def record(self, rows):
        citations = self._state["citations"]
        previous = copy.deepcopy(citations)
        updated = copy.deepcopy(previous)
        for row in rows:
            updated[row["id"]] = copy.deepcopy(
                {key: value for key, value in row.items() if key != "content"}
            )
        # Engine.citations retains this dictionary, so preserve its identity.
        citations.clear()
        citations.update(updated)
        try:
            _save(self._persist)
        except ToolPersistenceError:
            citations.clear()
            citations.update(previous)
            raise
        return copy.deepcopy(list(updated.values()))


class _ChildRunner:
    def __init__(self, config, state, pending, persist, emit, run_child, model_id, instance):
        self._config = _config_copy(config)
        self._state, self._pending = state, pending
        self._persist, self._emit, self._run_child = persist, emit, run_child
        self._model_id, self._instance = model_id, instance

    async def run(self, subagent_id, task):
        if self._instance != "main":
            raise ValueError("子 Agent 不可继续委派子任务")
        child = next(
            (item for item in self._config.get("subs", []) if item["id"] == subagent_id), None
        )
        if not child:
            raise ValueError("子 Agent 配置不存在")
        child_id = self._pending.get("child_instance")
        if not child_id:
            count = self._state["delegations"]
            if count >= 8:
                raise ValueError("达到子任务数量限制（8 个）")
            child_id = f"sub-{count + 1}"
            self._state["delegations"] = count + 1
            self._pending["child_instance"] = child_id
            try:
                _save(self._persist)
            except ToolPersistenceError:
                self._state["delegations"] = count
                self._pending.pop("child_instance", None)
                raise
            self._emit(
                "subagent.created",
                {"id": child_id, "name": child["name"], "task": task, "model": self._model_id},
            )
        try:
            result = await self._run_child(copy.deepcopy(child), task, (), child_id)
        except ToolPersistenceError:
            raise
        except Exception as error:
            self._emit("subagent.failed", {"id": child_id, "error": str(error)})
            raise
        outcome = self._state["frames"][child_id]["outcome"]
        self._emit(
            "subagent.completed",
            {"id": child_id, "name": child["name"], "output": result, "outcome": outcome},
        )
        return {"status": outcome, "output": result}


def build_tool_context(
    *,
    config,
    frame,
    state,
    pending,
    instance,
    root_workspace,
    emit,
    persist,
    run_child,
    model_id="",
    authorize_tool=None,
):
    observe = safe_observer(emit)
    return ToolContext(
        config=config,
        instance=instance,
        root_workspace=root_workspace,
        emit=observe,
        plan=_PlanEditor(frame, instance, persist, observe),
        children=_ChildRunner(
            config, state, pending, persist, observe, run_child, model_id, instance
        ),
        citations=_CitationRecorder(state, persist),
        invocation=_InvocationState(pending, persist),
        authorize_tool=authorize_tool or (lambda definition: None),
    )
