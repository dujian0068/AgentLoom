"""Request/reply ownership, observation and cancellation without external services."""

import asyncio

import pytest
from agentloom_runtime.event_bus import Event, EventBus, UnknownTopicError


def test_concurrent_requests_keep_correlated_results_and_context():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        seen = []
        context = object()

        async def handler(event):
            nonlocal count
            assert event.context is context
            count += 1
            if count == 20:
                entered.set()
            await release.wait()
            return event.payload

        async def observe(event):
            seen.append(event)

        bus.register("tools.execute", handler)
        bus.subscribe("*", observe)
        inputs = [{"id": index} for index in range(20)]
        tasks = [
            asyncio.create_task(bus.request("tools.execute", item, context=context))
            for item in inputs
        ]
        await entered.wait()
        assert bus.pending_count == 20
        release.set()
        results = await asyncio.gather(*reversed(tasks))
        assert all(result is original for result, original in zip(results, reversed(inputs)))
        started = {event.correlation_id for event in seen if event.topic == "request.started"}
        completed = {event.correlation_id for event in seen if event.topic == "request.completed"}
        assert len(started) == 20 and started == completed
        assert all(event.payload == {"topic": "tools.execute"} for event in seen)
        assert bus.pending_count == 0
        await bus.close()

    asyncio.run(scenario())


def test_unknown_topic_duplicate_handler_and_active_correlation_rejected():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(event):
            entered.set()
            await release.wait()
            return "ok"

        bus.register("work", handler)
        with pytest.raises(ValueError, match="already registered"):
            bus.register("work", handler)
        with pytest.raises(UnknownTopicError, match="missing"):
            await bus.request("missing", {})
        first = asyncio.create_task(bus.request("work", {}, correlation_id="same"))
        await entered.wait()
        with pytest.raises(ValueError, match="already active"):
            await bus.request("work", {}, correlation_id="same")
        release.set()
        assert await first == "ok"
        assert await bus.request("work", {}, correlation_id="same") == "ok"
        assert bus.pending_count == 0
        await bus.close()

    asyncio.run(scenario())


def test_cancel_waits_for_handler_cleanup_without_background_work():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        cancelled = []

        async def handler(event):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish_cleanup.wait()

        async def observer(event):
            cancelled.append(event)

        bus.register("work", handler)
        bus.subscribe("request.cancelled", observer)
        task = asyncio.create_task(bus.request("work", {}))
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        assert not task.done()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bus.pending_count == 0 and len(cancelled) == 1
        assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]
        await bus.close()

    asyncio.run(scenario())


def test_timeout_cancels_handler_and_emits_only_deadline_outcome(monkeypatch):
    async def scenario():
        bus = EventBus()
        cleaned = asyncio.Event()
        events = []
        original_timeout = asyncio.timeout
        deadlines = []

        async def handler(event):
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def observer(event):
            events.append(event.topic)

        def controlled_deadline(timeout):
            # The handler expires the actual timeout after entering, without sleeps.
            deadline = original_timeout(None)
            deadlines.append(deadline)
            return deadline

        monkeypatch.setattr("agentloom_runtime.event_bus.asyncio.timeout", controlled_deadline)
        bus.register("work", handler)
        bus.subscribe("*", observer)
        with pytest.raises(TimeoutError):
            await bus.request("work", {}, timeout=0)
        assert cleaned.is_set() and bus.pending_count == 0
        assert events == ["request.started", "request.timed_out"]
        await bus.close()

    asyncio.run(scenario())


def test_nested_requests_do_not_block_and_cancel_together():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def child(event):
            if event.payload == "ready":
                return {"result": "child"}
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def parent(event):
            return await bus.request("child", event.payload)

        bus.register("parent", parent)
        bus.register("child", child)
        assert await bus.request("parent", "ready") == {"result": "child"}
        task = asyncio.create_task(bus.request("parent", "waiting"))
        await entered.wait()
        assert bus.pending_count == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and bus.pending_count == 0
        await bus.close()

    asyncio.run(scenario())


def test_observers_cannot_replace_reply_or_fail_requests_and_can_unsubscribe(caplog):
    async def scenario():
        bus = EventBus()
        observed = []

        async def broken(event):
            raise RuntimeError("private-error-data")

        async def observer(event):
            observed.append(event)
            return "observer reply is ignored"

        async def handler(event):
            return "handler reply"

        bus.register("work", handler)
        unsubscribe = bus.subscribe("request.started", broken)
        stop = bus.subscribe("*", observer)
        assert await bus.request("work", "private-tool-data") == "handler reply"
        assert [event.topic for event in observed] == ["request.started", "request.completed"]
        assert all(event.payload == {"topic": "work"} for event in observed)
        unsubscribe()
        unsubscribe()
        stop()
        await bus.publish("other", {})
        assert len(observed) == 2
        await bus.close()

    asyncio.run(scenario())
    assert "Event observer failed" in caplog.text
    assert "private-error-data" not in caplog.text


@pytest.mark.parametrize(
    "exception", [ValueError("handler error"), TimeoutError("provider timeout")]
)
def test_handler_exceptions_are_returned_without_retry(exception):
    async def scenario():
        bus = EventBus()
        calls = []
        events = []

        async def handler(event):
            calls.append(event)
            raise exception

        async def observer(event):
            events.append(event.topic)

        bus.register("work", handler)
        bus.subscribe("*", observer)
        with pytest.raises(type(exception)) as raised:
            await bus.request("work", {})
        assert raised.value is exception
        assert len(calls) == 1
        assert events == ["request.started", "request.failed"]
        assert bus.pending_count == 0
        await bus.close()

    asyncio.run(scenario())


def test_close_cleans_pending_handlers_and_prevents_new_requests():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def handler(event):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        bus.register("work", handler)
        task = asyncio.create_task(bus.request("work", {}))
        await entered.wait()
        await bus.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and bus.pending_count == 0
        with pytest.raises(RuntimeError, match="closed"):
            await bus.request("work", {})
        with pytest.raises(RuntimeError, match="closed"):
            bus.register("new", handler)
        await bus.close()

    asyncio.run(scenario())


def test_publish_supports_explicit_correlation_and_frozen_events():
    async def scenario():
        from dataclasses import FrozenInstanceError

        bus = EventBus()
        seen = []

        async def observer(event):
            seen.append(event)

        bus.subscribe("notice", observer)
        context = object()
        await bus.publish("notice", {"value": 1}, context=context, correlation_id="notice-id")
        assert seen == [Event("notice", "notice-id", {"value": 1}, context)]
        with pytest.raises(FrozenInstanceError):
            seen[0].topic = "changed"
        await bus.close()

    asyncio.run(scenario())


def test_close_does_not_cancel_already_running_handler_cleanup_twice():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()

        async def handler(event):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        bus.register("work", handler)
        request = asyncio.create_task(bus.request("work", {}))
        await entered.wait()
        request.cancel()
        await cleaning.wait()
        closing = asyncio.create_task(bus.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cleaned.is_set() and bus.pending_count == 0

    asyncio.run(scenario())


def test_slow_observers_are_bounded_and_cancelled_without_affecting_reply(caplog):
    async def scenario():
        bus = EventBus(observer_timeout=0.001)
        cleaned = []

        async def observer(event):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(event.topic)

        async def handler(event):
            return "ok"

        bus.register("work", handler)
        bus.subscribe("*", observer)
        assert await bus.request("work", {}) == "ok"
        assert cleaned == ["request.started", "request.completed"]
        assert bus.pending_count == 0
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        await bus.close()

    asyncio.run(scenario())
    assert "TimeoutError" in caplog.text


def test_close_waits_for_terminal_observers_to_finish():
    async def scenario():
        bus = EventBus()
        entered = asyncio.Event()
        observed = []

        async def handler(event):
            entered.set()
            await asyncio.Event().wait()

        async def observer(event):
            await asyncio.sleep(0)
            observed.append(event.topic)

        bus.register("work", handler)
        bus.subscribe("request.cancelled", observer)
        request = asyncio.create_task(bus.request("work", {}))
        await entered.wait()
        await bus.close()
        assert observed == ["request.cancelled"]
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_internal_close_is_rejected_without_deadlocking_handlers_or_observers(caplog):
    async def scenario():
        bus = EventBus()

        async def handler(event):
            await bus.close()

        async def observer(event):
            await bus.close()

        bus.register("work", handler)
        bus.subscribe("*", observer)
        with pytest.raises(RuntimeError, match="from its handler or observer"):
            await bus.request("work", {})
        assert bus.pending_count == 0
        await bus.close()

    asyncio.run(scenario())
    assert "Event observer failed" in caplog.text
