"""In-process request/reply bus with cancellation ownership and observation events."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .observation import ObservationScalar, freeze_observation, snapshot_metadata

logger = logging.getLogger(__name__)
_ACTIVE_BUSES: ContextVar[tuple[int, ...]] = ContextVar("event_bus_execution", default=())


@dataclass(frozen=True)
class Event:
    topic: str
    correlation_id: str
    payload: Any
    context: Any = None


Handler = Callable[[Event], Awaitable[Any]]
Observer = Callable[[Event], Awaitable[None]]


class UnknownTopicError(LookupError):
    """No request handler has been registered for this topic."""


@dataclass
class _Pending:
    future: asyncio.Future
    task: asyncio.Task
    finished: asyncio.Future
    topic: str


class EventBus:
    """Route requests to one handler per topic; observers never replace the reply.

    Each request owns a handler task, so a handler may await another request without
    blocking a shared worker. Cancellation is cooperative: a handler must propagate
    cancellation and finish its cleanup before the requesting coroutine can return.
    There is deliberately no retry, persistence, or cross-process transport here.
    """

    def __init__(self, *, observer_timeout: float = 1.0):
        if observer_timeout <= 0:
            raise ValueError("Observer timeout must be positive")
        self._observer_timeout = observer_timeout
        self._handlers: dict[str, Handler] = {}
        self._observers: dict[str, list[Observer]] = {}
        self._pending: dict[str, _Pending] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def register(self, topic: str, async_handler: Handler) -> Callable[[], None]:
        """Register one owner; its release handle is valid only after requests drain."""
        if self._closed:
            raise RuntimeError("Event bus is closed")
        if topic in self._handlers:
            raise ValueError(f"Request handler already registered for topic: {topic}")

        # A distinct identity also protects later registrations of the same callable.
        async def registration(event: Event):
            return await async_handler(event)

        self._handlers[topic] = registration

        def unregister():
            if self._handlers.get(topic) is not registration:
                return
            if any(request.topic == topic for request in self._pending.values()):
                raise RuntimeError(f"Cannot unregister topic with active requests: {topic}")
            del self._handlers[topic]

        return unregister

    def subscribe(self, topic: str, async_observer: Observer) -> Callable[[], None]:
        if self._closed:
            raise RuntimeError("Event bus is closed")
        observers = self._observers.setdefault(topic, [])

        # Keep a distinct wrapper for each subscription, including repeat subscribers.
        async def subscription(event: Event):
            await async_observer(event)

        observers.append(subscription)

        def unsubscribe():
            if subscription in observers:
                observers.remove(subscription)

        return unsubscribe

    async def publish(
        self,
        topic: str,
        payload: Any,
        *,
        context: Any = None,
        correlation_id: str | None = None,
    ) -> None:
        """Observe immutable JSON snapshots; arbitrary objects are rejected.

        Execution context belongs only to request handlers. Callers publishing
        observations must supply JSON-shaped context data explicitly.
        """
        payload = freeze_observation(payload)
        context = freeze_observation(context)
        correlation_id = correlation_id or uuid4().hex
        observers = [*self._observers.get(topic, [])]
        if topic != "*":
            observers.extend(self._observers.get("*", []))
        tasks = [
            asyncio.create_task(
                self._observe(observer, Event(topic, correlation_id, payload, context))
            )
            for observer in observers
        ]
        if not tasks:
            return
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for result in results:
            if isinstance(result, BaseException):
                # Observers may be instrumentation plugins. Do not expose their
                # exception contents, which may include tool arguments or secrets.
                logger.warning(
                    "Event observer failed for topic %s (%s)", topic, type(result).__name__
                )

    async def _observe(self, observer: Observer, event: Event) -> None:
        token = _ACTIVE_BUSES.set((*_ACTIVE_BUSES.get(), id(self)))
        try:
            await asyncio.wait_for(observer(event), self._observer_timeout)
        finally:
            _ACTIVE_BUSES.reset(token)

    async def request(
        self,
        topic: str,
        payload: Any,
        *,
        context: Any = None,
        correlation_id: str | None = None,
        timeout: float | None = 90,
        observation_metadata: Mapping[str, ObservationScalar] | None = None,
    ) -> Any:
        if self._closed:
            raise RuntimeError("Event bus is closed")
        handler = self._handlers.get(topic)
        if handler is None:
            raise UnknownTopicError(f"No request handler registered for topic: {topic}")
        correlation_id = correlation_id or uuid4().hex
        if correlation_id in self._pending:
            raise ValueError(f"Request correlation ID already active: {correlation_id}")
        metadata = snapshot_metadata(observation_metadata)
        metadata["request_id"] = correlation_id
        event = Event(topic, correlation_id, payload, context)
        future = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._dispatch(event, handler, future, metadata))
        pending = _Pending(future, task, asyncio.get_running_loop().create_future(), topic)
        self._pending[correlation_id] = pending
        outcome = "completed"
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await asyncio.shield(future)
        except TimeoutError:
            # A handler's own TimeoutError is a failed request, not the bus deadline.
            outcome = "timed_out" if deadline.expired() else "failed"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            try:
                if not task.done() and not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    # Consume an exception if cancellation/deadline won the reply race.
                    future.exception()
                await self._notify(event, outcome, metadata)
            finally:
                self._pending.pop(correlation_id, None)
                pending.finished.set_result(None)

    async def _dispatch(
        self, event: Event, handler: Handler, future: asyncio.Future, metadata: dict
    ) -> None:
        token = _ACTIVE_BUSES.set((*_ACTIVE_BUSES.get(), id(self)))
        try:
            try:
                await self._notify(event, "started", metadata)
                result = await handler(event)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
        finally:
            _ACTIVE_BUSES.reset(token)

    async def _notify(self, event: Event, outcome: str, metadata: dict) -> None:
        await self.publish(
            f"request.{outcome}",
            {"topic": event.topic},
            context=metadata,
            correlation_id=event.correlation_id,
        )

    async def close(self) -> None:
        if id(self) in _ACTIVE_BUSES.get():
            raise RuntimeError("Cannot close event bus from its handler or observer")
        self._closed = True
        await self._cancel_requests(list(self._pending.values()))

    async def cancel_topic(self, topic: str) -> None:
        """Cancel admitted requests for one owner, including notification cleanup.

        This does not close the bus or prevent new requests. The owner must first
        stop accepting work, then cancel and release its registration; release
        still refuses if another request arrived during cleanup.
        """
        if id(self) in _ACTIVE_BUSES.get():
            raise RuntimeError("Cannot cancel a topic from its bus handler or observer")
        await self._cancel_requests(
            [request for request in self._pending.values() if request.topic == topic]
        )

    @staticmethod
    async def _cancel_requests(pending: list[_Pending]) -> None:
        for request in pending:
            if not request.task.cancelling():
                request.task.cancel()
            request.future.cancel()
        await asyncio.gather(*(request.task for request in pending), return_exceptions=True)
        # The owning request also emits its terminal notification. Wait for that
        # cleanup so close does not leave observer tasks behind.
        await asyncio.gather(*(asyncio.shield(request.finished) for request in pending))
