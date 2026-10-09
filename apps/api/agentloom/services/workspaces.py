"""Resolve server-owned application/session scopes on a pinned storage volume."""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from agentloom import store as db

_SCOPE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_BINDING_VERSION = "session-workspace/v1"


def _segment(value):
    if not isinstance(value, str) or not _SCOPE.fullmatch(value):
        raise ValueError("工作区作用域不合法")
    return value


def _storage():
    backend = os.environ.get("AGENT_LOOM_WORKSPACE_BACKEND", "local")
    configured = os.environ.get("AGENT_LOOM_WORKSPACE_ROOT")
    root = Path(configured) if configured else db.DATA / "workspaces"
    if backend == "local":
        root = root.resolve()
        volume = "local:" + hashlib.sha256(str(root).encode()).hexdigest()
    elif backend == "shared_posix":
        volume = os.environ.get("AGENT_LOOM_WORKSPACE_VOLUME_ID", "").strip()
        if not configured or not root.is_absolute() or not volume or len(volume) > 200:
            raise ValueError("共享工作区需要绝对挂载路径和固定的卷标识")
        # Never create the configured mount or marker: that could turn a missing
        # shared mount into an apparently healthy, node-local workspace.
        if root.is_symlink() or not root.is_dir():
            raise ValueError("共享工作区挂载不可用")
        try:
            fd = os.open(root / ".agentloom-volume", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024:
                    raise ValueError("共享工作区卷标识不合法")
                marker = os.read(fd, 1025).decode("utf-8").strip()
            finally:
                os.close(fd)
        except (OSError, UnicodeError):
            raise ValueError("共享工作区卷标识不可读取，请检查共享挂载") from None
        if marker != volume:
            raise ValueError("共享工作区卷标识不匹配，请检查共享挂载")
        root = root.resolve()
    else:
        raise ValueError("不支持的工作区存储后端")
    return root, backend, volume


def _scope(row):
    return {
        "space_id": _segment(row["space_id"]),
        "agent_id": _segment(row["agent_id"]),
        "session_id": _segment(row["session_id"]),
        "run_id": _segment(row["id"]),
    }


def pin_run(connection, run_id):
    """Pin a newly inserted run in the same admission transaction as its session."""
    row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError("运行不存在")
    scope = _scope(row)
    _, backend, volume = _storage()
    binding = {"implementation": _BINDING_VERSION, "backend": backend, "volume_id": volume, **scope}
    saved = connection.execute(
        "SELECT binding FROM run_workspaces WHERE run_id=?", (run_id,)
    ).fetchone()
    if saved:
        if json.loads(saved["binding"]) != binding:
            raise ValueError("运行已绑定其他工作区配置")
        return binding
    previous = connection.execute(
        "SELECT binding FROM run_workspaces WHERE space_id=? AND agent_id=? AND session_id=? LIMIT 1",
        (scope["space_id"], scope["agent_id"], scope["session_id"]),
    ).fetchone()
    if previous:
        old = json.loads(previous["binding"])
        if any(old.get(key) != binding[key] for key in ("implementation", "backend", "volume_id")):
            raise ValueError("同一会话必须继续使用原共享工作区，不能切换存储卷")
    connection.execute(
        "INSERT INTO run_workspaces VALUES(?,?,?,?,?,?)",
        (
            run_id,
            scope["space_id"],
            scope["agent_id"],
            scope["session_id"],
            json.dumps(binding),
            time.time(),
        ),
    )
    return binding


def resolve_run(row):
    """Return (provider, main path); old runs retain their original run directory."""
    from agentloom_runtime.workspace import WorkspaceProvider

    saved = db.query("SELECT * FROM run_workspaces WHERE run_id=?", (row["id"],), True)
    if saved is None:
        return None, db.DATA / "runs" / _segment(row["id"])
    scope = _scope(row)
    binding = json.loads(saved["binding"])
    if (
        binding.get("implementation") != _BINDING_VERSION
        or any(binding.get(key) != value for key, value in scope.items())
        or any(saved[key] != scope[key] for key in ("space_id", "agent_id", "session_id"))
    ):
        raise ValueError("工作区绑定与运行作用域不一致")
    root, backend, volume = _storage()
    if binding.get("backend") != backend or binding.get("volume_id") != volume:
        raise ValueError("当前存储卷与运行工作区不一致，请恢复原存储配置")
    session = (
        root
        / "spaces"
        / scope["space_id"]
        / "agents"
        / scope["agent_id"]
        / "sessions"
        / scope["session_id"]
    )
    lock_id = hashlib.sha256(
        json.dumps(
            {key: scope[key] for key in ("space_id", "agent_id", "session_id")}, sort_keys=True
        ).encode()
    ).hexdigest()

    def validate_storage():
        if _storage() != (root, backend, volume):
            raise ValueError("运行期间工作区存储配置发生变化，请恢复原共享挂载")

    provider = WorkspaceProvider(
        main_root=session / "main",
        children_root=session / "runs" / scope["run_id"] / "children",
        lock_root=root / ".agentloom-locks" / lock_id,
        binding=binding,
        validate_storage=validate_storage,
    )
    return provider, provider.main_root


def _legacy_store(path):
    from agentloom_runtime.workspace_store import PosixWorkspaceStore

    return PosixWorkspaceStore(path)


def list_artifacts(row, *, limit=1000):
    """Expose session main files and this run's children, never physical paths."""
    provider, path = resolve_run(row)
    if provider is None:
        roots = [("", _legacy_store(path))]
    else:
        roots = [("", provider.for_instance("main"))]
        # Child instance names come from our own directory layout. Never let a
        # filename choose a different run/session scope or a parent mount.
        children = _legacy_store(provider.children_root)
        try:
            entries = children.list(limit=100, include_hidden=True)["entries"]
        except FileNotFoundError:
            entries = []
        for entry in entries:
            if entry["type"] == "directory" and re.fullmatch(r"sub-[1-8]", entry["path"]):
                roots.append(
                    ("subagents/" + entry["path"] + "/", provider.for_instance(entry["path"]))
                )
    result = []
    for prefix, store in roots:
        try:
            entries = store.list(recursive=True, limit=limit, include_hidden=True)["entries"]
        except FileNotFoundError:
            continue
        result.extend(prefix + entry["path"] for entry in entries if entry["type"] == "file")
        if len(result) >= limit:
            break
    return sorted(result[:limit])


def read_artifact(row, name):
    """Return bounded bytes from a safely opened file, without reopening paths."""
    provider, path = resolve_run(row)
    if provider is None:
        store = _legacy_store(path)
    elif name.startswith("subagents/"):
        parts = name.split("/", 2)
        if len(parts) != 3 or not re.fullmatch(r"sub-[1-8]", parts[1]):
            raise ValueError("子任务文件路径不合法")
        store, name = provider.for_instance(parts[1]), parts[2]
    else:
        store = provider.for_instance("main")
    return store.read_bytes(name, max_bytes=2 * 1024 * 1024)
