"""Resolved module bindings, checkpoint compatibility and owned resource cleanup."""

import json
from copy import deepcopy
from dataclasses import asdict, dataclass

from .module_contracts import (
    CompactionPolicy,
    CompletionPolicy,
    ExecutionLimits,
    ExecutionStrategy,
    ModelGateway,
)


@dataclass(frozen=True)
class RuntimeModules:
    model: ModelGateway
    compaction: CompactionPolicy
    completion: CompletionPolicy
    strategy: ExecutionStrategy
    limits: ExecutionLimits

    def bindings(self):
        bindings = {}
        for name in ("model", "compaction", "completion", "strategy"):
            module = getattr(self, name)
            identity = getattr(module, "module_id", None)
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError(f"模块 {name} 必须声明带版本的 module_id")
            config = getattr(module, "checkpoint_config", lambda: {})()
            bindings[name] = {"implementation": identity, "config": deepcopy(config)}
        bindings["limits"] = asdict(self.limits)
        # Keep durable bindings JSON-only, independent of in-memory plugin objects.
        return json.loads(json.dumps(bindings, allow_nan=False))

    async def aclose(self):
        errors, seen = [], set()
        for module in (self.strategy, self.completion, self.compaction, self.model):
            if id(module) in seen:
                continue
            seen.add(id(module))
            close = getattr(module, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Runtime 模块关闭失败", errors)
