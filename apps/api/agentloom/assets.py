import asyncio
import re
import shutil
import subprocess
from pathlib import PurePosixPath
from urllib.parse import urlparse

import yaml

from .store import DATA, uid

MAX_TOTAL = 30 * 1024 * 1024


def safe_path(root, name):
    parts = PurePosixPath(name.replace("\\", "/"))
    if parts.is_absolute() or ".." in parts.parts or not parts.parts:
        raise ValueError("文件路径不合法")
    p = root.joinpath(*parts.parts)
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError("路径超出资源目录")
    return p


async def write_uploads(files):
    root = DATA / "assets" / uid()
    root.mkdir(parents=True)
    total = 0
    try:
        if len(files) > 500:
            raise ValueError("文件数量不能超过 500")
        for file in files:
            blob = await file.read(MAX_TOTAL + 1)
            total += len(blob)
            if total > MAX_TOTAL:
                raise ValueError("资源包不能超过 30MB")
            p = safe_path(root, file.filename or "file")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blob)
        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


async def git_import(url, subdir="", ref=""):
    if urlparse(url).scheme != "https" or urlparse(url).username or urlparse(url).password:
        raise ValueError("仅支持不含凭据的 HTTPS Git 地址")
    root = DATA / "assets" / uid()
    root.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "-c", "protocol.file.allow=never", "clone", "--depth", "1"]
    if ref:
        if ref.startswith("-"):
            raise ValueError("分支名不合法")
        args += ["--branch", ref]
    args += ["--", url, str(root)]
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(p.communicate(), 90)
        except BaseException:
            p.kill()
            await p.wait()
            raise
        if p.returncode:
            raise ValueError("Git 导入失败，请检查地址及分支（第一版支持公开仓库）")
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        shutil.rmtree(root / ".git", ignore_errors=True)
        base = safe_path(root, subdir) if subdir else root
        if not base.is_dir():
            raise ValueError("Git 子目录不存在")
        files = list(root.rglob("*"))
        if any(p.is_symlink() for p in files):
            raise ValueError("导入包不允许符号链接")
        if sum(p.stat().st_size for p in files if p.is_file()) > MAX_TOTAL:
            raise ValueError("仓库内容超过 30MB")
        return base, sha
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def skill_metadata(root):
    found = list(root.rglob("SKILL.md"))
    if len(found) != 1:
        raise ValueError("每次导入一个技能目录，必须且仅有一个 SKILL.md")
    base = found[0].parent
    content = found[0].read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.S)
    if not match:
        raise ValueError("SKILL.md 需要 YAML frontmatter，包含 name 与 description")
    meta = yaml.safe_load(match.group(1))
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("name"), str)
        or not isinstance(meta.get("description"), str)
    ):
        raise ValueError("技能需要字符串 name 与 description")
    return {
        "name": meta["name"],
        "description": meta["description"],
        "content": content,
        "path": str(base),
        "files": [str(x.relative_to(base)) for x in base.rglob("*") if x.is_file()],
        "version": uid()[:8],
        "metadata": meta,
        "status": "ready",
    }
