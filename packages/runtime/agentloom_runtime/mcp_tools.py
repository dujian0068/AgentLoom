import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


@asynccontextmanager
async def session(service, secret=""):
    if service.get("source") == "hosted":
        if not shutil.which("docker"):
            raise RuntimeError("托管工具需要 Docker，当前环境未安装")
        args = [
            "run",
            "--rm",
            "-i",
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
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            service["image"],
        ]
        transport = stdio_client(StdioServerParameters(command="docker", args=args))
    else:
        headers = {"Authorization": "Bearer " + secret} if secret else {}
        transport = (
            sse_client(service["endpoint"], headers=headers, timeout=30, sse_read_timeout=60)
            if service.get("transport") == "sse"
            else streamablehttp_client(
                service["endpoint"],
                headers=headers,
                timeout=30,
                sse_read_timeout=60,
                httpx_client_factory=lambda headers=None, timeout=None, auth=None: (
                    httpx.AsyncClient(
                        headers=headers,
                        timeout=timeout or 60,
                        auth=auth,
                        trust_env=False,
                        follow_redirects=False,
                    )
                ),
            )
        )
    async with transport as streams:
        async with ClientSession(streams[0], streams[1]) as client:
            await client.initialize()
            yield client


async def discover(service, secret=""):
    async with asyncio.timeout(45):
        async with session(service, secret) as client:
            tools = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(
                    {"name": x.name, "description": x.description or "", "schema": x.inputSchema}
                    for x in page.tools
                )
                cursor = page.nextCursor
                if not cursor:
                    break
            return tools


async def call(service, name, args, secret=""):
    async with asyncio.timeout(90):
        async with session(service, secret) as client:
            result = await client.call_tool(name, args)
            return {
                "is_error": result.isError,
                "content": [x.model_dump(mode="json") for x in result.content],
            }


async def build_tool(root, image):
    if not shutil.which("docker"):
        raise RuntimeError("托管工具需要 Docker，当前环境未安装；代码已保留")
    root = Path(root)
    if not (root / "tool.py").is_file():
        raise ValueError("工具包根目录需要 tool.py，使用 SDK 启动 stdio MCP 服务")
    (root / "Dockerfile").write_text(
        'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install --no-cache-dir "mcp>=1.12,<2" && if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi\nUSER 65534:65534\nCMD ["python", "tool.py"]\n'
    )
    p = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "-t",
        image,
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 180)
    except BaseException:
        p.kill()
        await p.wait()
        raise
    if p.returncode:
        raise RuntimeError("工具构建失败：" + out.decode(errors="replace")[-3000:])
    return out.decode(errors="replace")[-6000:]
