import asyncio
import copy
import time
from unittest.mock import patch

import pytest
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookContext,
    HookDefinition,
    HookFailed,
    HookManager,
    HookRegistry,
)


def build_manager(handlers, *, total_timeout=10, hook_timeout=20):
    registry = HookRegistry()
    bindings = []
    for name, handler in handlers:
        registry.register(HookDefinition(name, "v1", handler, replay_safe=True))
        bindings.append(HookBinding(name, name, "run.before", timeout=hook_timeout))
    return HookManager(registry, bindings, total_timeout=total_timeout)


@pytest.mark.parametrize("jump", [-3600, 3600])
def test_wall_clock_jump_does_not_change_hook_budget(jump):
    async def scenario():
        observed = {}

        async def hook(context, payload):
            before = context.remaining_seconds()
            observed["timestamp"] = context.deadline
            with patch("time.time", return_value=time.time() + jump):
                after = context.remaining_seconds()
                observed["wall_remaining"] = context.deadline - time.time()
            assert 0 < after <= before <= 10
            assert before - after < 0.1
            return Continue()

        manager = build_manager([("clock", hook)])
        wall_started = time.time()
        await manager.run(
            "run.before", {"task": "Inspect clock budgets"}, scope={}, state={}, save=lambda: None
        )
        assert wall_started < observed["timestamp"] <= time.time() + 10
        assert abs(observed["wall_remaining"] - (10 - jump)) < 1
        await manager.aclose()

    asyncio.run(scenario())


def test_later_hook_only_receives_remaining_pipeline_budget():
    async def scenario():
        loop = asyncio.get_running_loop()
        clock = [100.0]
        observed = []
        invocation_timeouts = []

        async def first(context, payload):
            observed.append((context.deadline_monotonic, context.remaining_seconds()))
            clock[0] += 6
            return Continue()

        async def second(context, payload):
            observed.append((context.deadline_monotonic, context.remaining_seconds()))
            return Continue()

        async def invoke(handler, context, payload, timeout):
            invocation_timeouts.append(timeout)
            return await handler(context, payload)

        manager = build_manager([("first", first), ("second", second)])
        manager.executor.invoke = invoke
        with patch.object(loop, "time", side_effect=lambda: clock[0]):
            await manager.run(
                "run.before",
                {"task": "Share one stage budget"},
                scope={},
                state={},
                save=lambda: None,
            )
        assert observed == [(110.0, 10.0), (110.0, 4.0)]
        assert invocation_timeouts == [10.0, 4.0]
        await manager.aclose()

    asyncio.run(scenario())


def test_recovery_recalculates_deadline_without_persisting_clock_values():
    async def scenario():
        loop = asyncio.get_running_loop()
        clock = [100.0]
        observed = []
        completed = []
        saved = []
        state = {}

        async def first(context, payload):
            completed.append(context.binding_id)
            return Continue()

        async def interrupted(context, payload):
            observed.append((context.deadline_monotonic, context.remaining_seconds()))
            if len(observed) == 1:
                raise RuntimeError("Interrupted before returning")
            return Continue()

        async def invoke(handler, context, payload, timeout):
            return await handler(context, payload)

        manager = build_manager([("completed", first), ("retry", interrupted)])
        manager.executor.invoke = invoke
        payload = {"task": "Resume the unfinished Hook"}
        with patch.object(loop, "time", side_effect=lambda: clock[0]):
            with pytest.raises(HookFailed):
                await manager.run(
                    "run.before",
                    payload,
                    scope={},
                    state=state,
                    save=lambda: saved.append(copy.deepcopy(state)),
                )
            state = copy.deepcopy(saved[-1])
            clock[0] = 900.0
            await manager.run("run.before", payload, scope={}, state=state, save=lambda: None)
        assert observed == [(110.0, 10.0), (910.0, 10.0)]
        assert completed == ["completed"]
        assert state["records"][1]["attempt"] == 2
        assert "deadline" not in repr(saved)
        await manager.aclose()

    asyncio.run(scenario())


def test_legacy_context_fails_explicitly_instead_of_using_wall_clock():
    context = HookContext("run.before", "legacy", {}, {}, time.time() + 10)
    with pytest.raises(RuntimeError, match="no monotonic deadline"):
        context.remaining_seconds()


def test_remaining_budget_is_clamped_at_zero():
    async def scenario():
        context = HookContext(
            "run.before",
            "elapsed",
            {},
            {},
            time.time() + 10,
            deadline_monotonic=asyncio.get_running_loop().time() - 1,
        )
        assert context.remaining_seconds() == 0

    asyncio.run(scenario())
