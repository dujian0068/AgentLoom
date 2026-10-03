"""Process-local task registries; durable progress lives in the database."""

import asyncio

TASKS: dict[str, asyncio.Task] = {}
BUILDS: dict[str, asyncio.Task] = {}
AUTH_ATTEMPTS: dict[str, list[float]] = {}

# Terminal writes awaiting a database reconnect; no external actions are replayed.
PENDING_FINALIZATIONS: dict[str, dict] = {}
