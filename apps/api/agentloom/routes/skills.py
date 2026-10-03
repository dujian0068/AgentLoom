"""Skills API endpoints."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from agentloom import store as db
from agentloom.assets import git_import, safe_path, skill_metadata, write_uploads
from agentloom.dependencies import auth
from agentloom.schema import GitInput
from agentloom.security import public

router = APIRouter(tags=["skills"])


async def store_skill(root, space, source, commit=""):
    value = skill_metadata(root)
    value.update(source=source, commit=commit)
    return public(db.put_resource(space, "skills", value))


@router.post("/api/skills/upload")
async def skill_upload(files: list[UploadFile] = File(...), user=Depends(auth)):
    return await store_skill(await write_uploads(files), user["space_id"], "upload")


@router.post("/api/skills/git")
async def skill_git(payload: GitInput, user=Depends(auth)):
    root, commit = await git_import(payload.url, payload.subdir, payload.ref)
    return await store_skill(root, user["space_id"], "git", commit)


@router.get("/api/skills/{rid}/file")
def skill_file(rid: str, path: str = "SKILL.md", user=Depends(auth)):
    skill = db.resource(rid, user["space_id"], "skills")
    p = safe_path(Path(skill["path"]), path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")
