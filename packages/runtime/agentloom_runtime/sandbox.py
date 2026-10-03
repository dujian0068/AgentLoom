import asyncio
import shutil


async def command(workspace, skills, cmd):
    if not shutil.which("docker"):
        raise RuntimeError("脚本执行需要 Docker，当前环境未安装；不会在宿主机执行技能脚本")
    name = "agentloom-" + workspace.name
    args = [
        "docker",
        "run",
        "--name",
        name,
        "--rm",
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
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,nosuid,size=32m",
        "-v",
        f"{workspace}:/workspace",
        "-w",
        "/workspace",
    ]
    for s in skills:
        args += ["-v", f"{s['path']}:/skills/{s['id']}:ro"]
    args += ["python:3.12-slim", "sh", "-c", cmd]
    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        return {"exit_code": p.returncode, "output": out.decode(errors="replace")[-12000:]}
    finally:
        if p.returncode is None:
            p.kill()
            await p.wait()
        cleanup = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()
