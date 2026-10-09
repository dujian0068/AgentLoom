#!/usr/bin/env python3
"""Check an explicitly configured workspace volume; no dotenv, database or app imports."""

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/runtime"))
from agentloom_runtime.workspace_store import PosixWorkspaceStore

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_PROBE_FILES = ("value", "pending", "lock")


def read_regular(parent_fd, name, *, maximum=1024):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("卷标记和探测文件必须是无硬链接的普通文件")
    fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or current.st_size > maximum:
            raise ValueError("卷标记或探测文件类型、大小不合法")
        content = os.read(fd, maximum + 1)
        if len(content) > maximum:
            raise ValueError("卷标记或探测文件过大")
        return content
    finally:
        os.close(fd)


def write_pending(probe_fd, content):
    fd = os.open(
        "pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=probe_fd
    )
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise ValueError("探测文件写入未取得进展")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace("pending", "value", src_dir_fd=probe_fd, dst_dir_fd=probe_fd)
    os.fsync(probe_fd)


def check_lock(probe_fd):
    first = os.open(
        "lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=probe_fd
    )
    # A second process also works on filesystems where emulated NFS locks have
    # process ownership semantics; a same-process second open is insufficient.
    contender = """import fcntl, os, stat, sys
fd = os.open('lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=int(sys.argv[1]))
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
    sys.exit(3)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(2)
sys.exit(0)
"""

    def attempt():
        try:
            return subprocess.run(
                [sys.executable, "-I", "-c", contender, str(probe_fd)],
                pass_fds=(probe_fd,),
                env={"PATH": os.defpath},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode
        except subprocess.TimeoutExpired:
            raise ValueError("本机锁探测子进程未及时完成") from None

    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if attempt() != 2:
            raise ValueError("本机独立进程未体现 advisory lock 互斥")
        fcntl.flock(first, fcntl.LOCK_UN)
        if attempt() != 0:
            raise ValueError("释放锁后本机独立进程仍不能取得锁")
    finally:
        os.close(first)


def probe(root_fd):
    """Touch only one exclusively created namespace; never recursively remove user data."""
    name = ".agentloom-probe-" + uuid4().hex
    os.mkdir(name, 0o700, dir_fd=root_fd)
    probe_fd = None
    try:
        probe_fd = os.open(name, PosixWorkspaceStore._DIRECTORY_FLAGS, dir_fd=root_fd)
        write_pending(probe_fd, b"first value\n")
        if read_regular(probe_fd, "value") != b"first value\n":
            raise ValueError("文件写入后的读回校验失败")
        old = os.open("value", _READ_FLAGS, dir_fd=probe_fd)
        try:
            write_pending(probe_fd, b"second value\n")
            if (
                os.read(old, 100) != b"first value\n"
                or read_regular(probe_fd, "value") != b"second value\n"
            ):
                raise ValueError("原子替换后的文件版本校验失败")
        finally:
            os.close(old)
        check_lock(probe_fd)
    finally:
        if probe_fd is None:
            # Opening failed: do not guess what is now under the created name.
            raise ValueError("无法打开随机探测目录，已保留目录供管理员检查")
        try:
            # Use the pinned directory descriptor, and delete only known probe
            # names. Unexpected contents are deliberately left for inspection.
            for filename in _PROBE_FILES:
                try:
                    os.unlink(filename, dir_fd=probe_fd)
                except FileNotFoundError:
                    pass
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            held = os.fstat(probe_fd)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise ValueError("探测目录身份发生变化，已停止清理")
            os.rmdir(name, dir_fd=root_fd)
        finally:
            os.close(probe_fd)
    return {
        "write_read": True,
        "atomic_replace": True,
        "advisory_lock_local": True,
        "cleaned": True,
    }


def check(root, backend, volume_id, *, run_probe=False):
    if backend not in ("local", "shared_posix"):
        raise ValueError("存储后端必须为 local 或 shared_posix")
    path = PosixWorkspaceStore._absolute(root)
    if path == Path("/"):
        raise ValueError("不能使用文件系统根目录作为工作区")
    if backend == "shared_posix" and (not volume_id or len(volume_id) > 200):
        raise ValueError("共享模式需要固定的卷标识")
    # No store instance: its constructor creates directories. Default checking
    # must only open existing parents and must reject symlinks at every level.
    with PosixWorkspaceStore._absolute_fd(path) as root_fd:
        if backend == "shared_posix":
            try:
                marker = read_regular(root_fd, ".agentloom-volume").decode("utf-8").strip()
            except UnicodeError:
                raise ValueError("卷标记必须是 UTF-8 文本") from None
            if marker != volume_id:
                raise ValueError("共享卷标识不匹配")
        checks = probe(root_fd) if run_probe else {}
    return {
        "ok": True,
        "backend": backend,
        "mode": "probe" if run_probe else "read_only",
        "volume_checked": backend == "shared_posix",
        "checks": checks,
        "scope": "single_node",
        "limitations": "仅校验本机配置与可选 I/O；不证明 NFS 类型、跨节点锁或集群调度。",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("AGENT_LOOM_WORKSPACE_ROOT"))
    parser.add_argument("--volume-id", default=os.environ.get("AGENT_LOOM_WORKSPACE_VOLUME_ID"))
    parser.add_argument(
        "--backend",
        choices=("local", "shared_posix"),
        default=os.environ.get("AGENT_LOOM_WORKSPACE_BACKEND", "shared_posix"),
    )
    parser.add_argument(
        "--probe", action="store_true", help="在独立随机目录执行并清理本机 I/O 探测"
    )
    args = parser.parse_args(argv)
    if not args.root:
        parser.error("请显式提供 --root 或 AGENT_LOOM_WORKSPACE_ROOT；不会读取 .env")
    try:
        result = check(args.root, args.backend, args.volume_id, run_probe=args.probe)
    except ValueError as exc:
        print("工作区检查失败：" + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print(
            "工作区检查失败：目录、卷标记或文件系统操作不可用；未创建默认工作区。", file=sys.stderr
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
