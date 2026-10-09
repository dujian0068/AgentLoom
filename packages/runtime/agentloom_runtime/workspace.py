"""Host-owned workspace scopes and replaceable storage implementations."""

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from .workspace_store import PosixWorkspaceStore


class WorkspaceStorage(Protocol):
    root: Path

    def read(self, path, **options): ...
    def write(self, path, content, **options): ...
    def list(self, path=".", **options): ...
    def stat(self, path): ...
    def glob(self, pattern, **options): ...
    def grep(self, pattern, **options): ...
    def edit(self, path, old_text, new_text, **options): ...
    def mkdir(self, path): ...
    def delete(self, path, **options): ...
    def read_bytes(self, path, **options): ...
    def prepare(self): ...
    def lock(self): ...
    def begin_execution(self, container_name): ...
    def finish_execution(self, container_name): ...
    def quarantine(self): ...


class WorkspaceProvider:
    """Resolve an instance without accepting tenant IDs or roots from the model."""

    def __init__(
        self,
        *,
        main_root,
        children_root,
        lock_root,
        binding,
        store_factory=None,
        validate_storage=None,
    ):
        self.main_root = Path(main_root).absolute()
        self.children_root = Path(children_root).absolute()
        self.lock_root = Path(lock_root).absolute()
        if self.children_root.is_relative_to(self.main_root) or self.main_root.is_relative_to(
            self.children_root
        ):
            raise ValueError("主工作区与子 Agent 工作区必须分开挂载")
        if self.lock_root.is_relative_to(self.main_root) or self.lock_root.is_relative_to(
            self.children_root
        ):
            raise ValueError("工作区锁目录不能暴露给沙箱")
        self.binding = json.loads(json.dumps(binding, ensure_ascii=False, allow_nan=False))
        self.store_factory = store_factory or PosixWorkspaceStore
        self.validate_storage = validate_storage

    @classmethod
    def standalone(cls, root):
        root = Path(root).absolute()
        identity = hashlib.sha256(str(root).encode()).hexdigest()
        return cls(
            main_root=root,
            children_root=root.parent / (".agentloom-children-" + identity[:24]),
            lock_root=root.parent / ".agentloom-locks",
            binding={"backend": "posix/v1", "layout": "standalone/v1", "storage_id": identity},
        )

    def for_instance(self, instance):
        if type(instance) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance):
            raise ValueError("非法工作区实例")
        root = self.main_root if instance == "main" else self.children_root / instance
        if self.validate_storage is not None:
            self.validate_storage()
        identity = deepcopy(self.binding)
        if instance == "main":
            identity.pop("run_id", None)
        identity["instance"] = instance
        return self.store_factory(
            root,
            lock_root=self.lock_root,
            reserved_paths=("subagents",) if instance == "main" else (),
            lock_key=json.dumps(identity, sort_keys=True),
        )

    def checkpoint_binding(self):
        return deepcopy(self.binding)


async def storage_call(function, *args, **kwargs):
    """A cancelled request waits for filesystem I/O before releasing its scope."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Never leave a background write racing the next resumed request.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
