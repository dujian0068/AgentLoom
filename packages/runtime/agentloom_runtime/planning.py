"""Planning policy for the task harness."""


def update_plan(engine, frame, args, instance):
    steps = args["steps"]
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 12
        or not isinstance(args.get("explanation"), str)
    ):
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
    old = {s["id"]: s for s in frame["plan"]}
    if old and not args["explanation"].strip():
        raise ValueError("更新计划必须说明原因")
    event = "plan.updated" if old else "plan.created"
    frame["plan"] = steps
    engine.emit(event, {"instance": instance, "steps": steps, "explanation": args["explanation"]})
    for i, step in enumerate(steps):
        if step["status"] != old.get(step["id"], {}).get("status") and step["status"] in (
            "in_progress",
            "completed",
        ):
            engine.emit(
                "step.started" if step["status"] == "in_progress" else "step.completed",
                {"instance": instance, "index": i, "name": step["step"]},
            )
    return {"steps": steps}
