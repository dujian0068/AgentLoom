"""Bounded, cooperative execution for explicitly trusted async Python extensions."""

import asyncio

from .contracts import HookFailed


def _consume(task):
    if not task.cancelled():
        task.exception()


class HookExecutor:
    def __init__(self):
        self._tasks = set()
        self._closed = False

    async def invoke(self, handler, context, payload, timeout):
        if self._closed:
            raise HookFailed("Hook executor is closed")
        task = asyncio.create_task(handler(context, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_consume)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
            if not done:
                raise TimeoutError("Hook execution exceeded its budget")
            if task.cancelled():
                # A handler cancelling itself is a failed Hook, not host cancellation.
                raise HookFailed("Hook handler cancelled its own execution")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                # An uncooperative trusted function cannot hold the boundary open forever.
                # Isolation, rather than task cancellation, is required for untrusted code.
                await asyncio.wait({task}, timeout=0.1)

    async def aclose(self):
        self._closed = True
        pending = set(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=0.1)
            if remaining:
                raise HookFailed("Trusted Hook did not cooperate with cancellation")
