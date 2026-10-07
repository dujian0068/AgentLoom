"""Pure plan validation; host services own state changes and persistence."""

import copy


def validate_plan(steps, explanation, previous):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12 or not isinstance(explanation, str):
        raise ValueError("计划需要 1–12 个步骤和变更原因")
    for step in steps:
        if not isinstance(step, dict) or any(
            not isinstance(step.get(k), str) or not step[k].strip()
            for k in ("id", "step", "status")
        ):
            raise ValueError("每步需要 id、step、status")
        if (
            step["status"] not in ("pending", "in_progress", "completed", "cancelled")
            or len(step["step"]) > 1000
        ):
            raise ValueError("计划状态或步骤长度不合法")
    if (
        len({s["id"] for s in steps}) != len(steps)
        or sum(s["status"] == "in_progress" for s in steps) > 1
    ):
        raise ValueError("步骤 ID 不可重复，同一实例最多一个进行中的步骤")
    if previous and not explanation.strip():
        raise ValueError("更新计划必须说明原因")
    return copy.deepcopy(steps)
