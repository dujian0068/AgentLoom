"""Run and child lifecycle hooks share the durable operation boundary."""

from copy import deepcopy

from .hooks import execute_operation
from .hooks.manager import persist


class RuntimeLifecycle:
    def __init__(self, hooks, scope=None):
        self.hooks = hooks
        self.scope = deepcopy(scope or {})

    async def run(self, kind, instance, task, action, *, record, save, outcome=None):
        if kind not in {"run", "subagent"}:
            raise ValueError("Unknown runtime lifecycle")
        scope = {
            **self.scope,
            "operation_id": record["operation_id"],
            "instance_id": instance,
            "purpose": "action",
            "target": kind,
        }

        async def invoke(_):
            output = await action()
            actual_outcome = outcome() if outcome is not None else "complete"
            return {
                "output": output,
                "outcome": actual_outcome,
                "status": "blocked" if actual_outcome == "blocked" else "succeeded",
            }

        output = await execute_operation(
            self.hooks,
            kind,
            {"task": task},
            invoke,
            scope=scope,
            state=record.setdefault("operation", {}),
            save=save,
            # The operation is the resumable Loop, which already preserves model,
            # tool and child identities. This does not authorize their replay.
            resume_inflight=True,
        )
        record["completed"] = True
        persist(save)
        return output["output"]
