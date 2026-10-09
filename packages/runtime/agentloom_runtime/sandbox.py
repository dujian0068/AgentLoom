"""Disposable execution sandboxes mount one workspace, never the shared volume root."""

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from .workspace import storage_call
from .workspace_store import PosixWorkspaceStore

MAX_OUTPUT_BYTES = 256 * 1024


def _mount_path(path):
    path = Path(path).absolute()
    if any(char in str(path) for char in (",", "\n", "\r", "\x00")):
        raise ValueError("沙箱挂载路径含不支持的字符")
    return str(path)


def docker_arguments(executable, workspace, skills, cmd, name):
    """Deployment configuration is trusted; model input supplies only the command."""
    if type(cmd) is not str or not cmd.strip() or len(cmd) > 20000 or "\x00" in cmd:
        raise ValueError("沙箱命令不能为空或超过20000字符")
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0:
        raise RuntimeError("沙箱要求平台以非 root 服务用户运行，工作区和沙箱使用同一 UID/GID")
    host = os.environ.get("AGENT_LOOM_DOCKER_HOST", "unix:///var/run/docker.sock")
    if not host.startswith("unix:///") or any(c in host for c in ("\n", "\r", "\x00")):
        raise ValueError("沙箱只支持本机 Docker Unix socket，共享目录必须已挂载到该 Worker")
    image = os.environ.get("AGENT_LOOM_SANDBOX_IMAGE", "python:3.12-slim")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,250}", image):
        raise ValueError("沙箱镜像配置不合法")
    args = [
        executable,
        "--host",
        host,
        "run",
        "--name",
        name,
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--mount",
        f"type=bind,src={_mount_path(workspace)},dst=/workspace,bind-propagation=rprivate",
        "--workdir",
        "/workspace",
    ]
    runtime = os.environ.get("AGENT_LOOM_SANDBOX_RUNTIME", "")
    if runtime:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", runtime):
            raise ValueError("沙箱 runtime 配置不合法")
        args += ["--runtime", runtime]
    for skill in skills:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", skill["id"]):
            raise ValueError("技能挂载标识不合法")
        path = Path(skill["path"]).absolute()
        with PosixWorkspaceStore._absolute_fd(path):
            pass
        args += [
            "--mount",
            f"type=bind,src={_mount_path(path)},dst=/skills/{skill['id']},readonly,bind-propagation=rprivate",
        ]
    return [*args, image, "sh", "-c", "umask 077\n" + cmd]


async def _capture(process):
    captured = bytearray()
    while True:
        chunk = await process.stdout.read(8192)
        if not chunk:
            break
        captured.extend(chunk)
        if len(captured) > MAX_OUTPUT_BYTES:
            raise RuntimeError("沙箱命令输出超过256KB，已停止执行；请将大结果写入文件")
    await process.wait()
    decoded = captured.decode(errors="replace")
    return {
        "exit_code": process.returncode,
        "output": decoded[-12000:],
        "truncated": len(decoded) > 12000,
    }


async def _cleanup(executable, host, name, process, environment):
    naturally_finished = process is not None and process.returncode is not None
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

        # Drain discarded output after killing the CLI: waiting on a process
        # whose pipe is full can otherwise prevent shutdown indefinitely.
        async def drain():
            while await process.stdout.read(8192):
                pass
            await process.wait()

        await asyncio.wait_for(drain(), 5)
    cleanup = await asyncio.create_subprocess_exec(
        executable,
        "--host",
        host,
        "rm",
        "-f",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=environment,
    )
    try:
        output, _ = await asyncio.wait_for(cleanup.communicate(), 10)
    except BaseException:
        if cleanup.returncode is None:
            cleanup.kill()
            await cleanup.wait()
        raise
    if cleanup.returncode and not (naturally_finished and b"No such container" in output):
        raise RuntimeError("无法确认沙箱已清理，工作区已隔离，请管理员核对容器状态")


async def command(workspace, skills, cmd, *, timeout=60):
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("脚本执行需要 Docker，当前环境未安装；不会在宿主机执行命令")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("沙箱执行时限必须为1到120秒")
    store = workspace if hasattr(workspace, "lock") else PosixWorkspaceStore(Path(workspace))
    name = "agentloom-" + uuid4().hex
    root = await storage_call(store.prepare)
    args = docker_arguments(executable, root, skills, cmd, name)
    lease = store.lock()
    acquired = False
    begun = False

    def acquire():
        nonlocal acquired
        lease.__enter__()
        acquired = True

    def begin():
        nonlocal begun
        store.begin_execution(name)
        begun = True

    process = None
    launcher = None
    try:
        await storage_call(acquire)
        with tempfile.TemporaryDirectory(prefix="agentloom-docker-") as directory:
            environment = {
                "PATH": os.defpath,
                "HOME": directory,
                "DOCKER_CONFIG": directory,
                "LANG": "C.UTF-8",
            }
            try:
                await storage_call(begin)
                launcher = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=environment,
                    )
                )
                try:
                    process = await asyncio.shield(launcher)
                except asyncio.CancelledError:
                    # A cancelled await must not orphan a CLI that can submit a
                    # late create request after our cleanup. Settle launch first.
                    settler = asyncio.create_task(asyncio.wait_for(asyncio.shield(launcher), 10))
                    try:
                        while not settler.done():
                            try:
                                await asyncio.shield(settler)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        settler.result()
                        process = launcher.result()
                    except BaseException:
                        launcher.cancel()
                    raise
                try:
                    return await asyncio.wait_for(_capture(process), timeout)
                except TimeoutError:
                    raise RuntimeError("沙箱命令执行超时；文件可能已变化，请检查后继续") from None
            finally:
                # Creation can be cancelled after Docker accepted the request but
                # before asyncio returns the process handle. Always reconcile name.
                if begun:

                    async def reconcile():
                        if launcher is not None:
                            await asyncio.wait_for(
                                _cleanup(executable, args[2], name, process, environment), 20
                            )
                            if process is None:
                                raise RuntimeError("无法确认沙箱启动状态，工作区已隔离")
                        await storage_call(store.finish_execution, name)

                    worker = asyncio.create_task(reconcile())
                    cancelled = bool(asyncio.current_task().cancelling())
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            cancelled = True
                        except Exception:
                            break
                    try:
                        worker.result()
                    except BaseException:
                        await storage_call(store.quarantine)
                        if not cancelled:
                            raise
                    if cancelled:
                        raise asyncio.CancelledError
    finally:
        if acquired:
            await storage_call(lease.__exit__, None, None, None)
