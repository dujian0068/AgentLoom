from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default="空间管理员", max_length=100)
    invite: str = ""


class Child(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    prompt: str = Field(default="", max_length=20000)
    skills: list[str] = Field(default_factory=list, max_length=30)
    tools: list[str] = Field(default_factory=list, max_length=30)
    wiki: list[str] = Field(default_factory=list, max_length=20)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    model: str = ""
    prompt: str = Field(default="", max_length=30000)
    mode: Literal["react", "plan"] = "react"
    skills: list[str] = Field(default_factory=list, max_length=30)
    tools: list[str] = Field(default_factory=list, max_length=30)
    wiki: list[str] = Field(default_factory=list, max_length=20)
    subs: list[Child] = Field(default_factory=list, max_length=10)


class ModelInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["deepseek", "openai"]
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str
    api_key: str = Field(default="", max_length=500)
    purpose: Literal["chat", "embedding"] = "chat"


class ToolInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    endpoint: str
    api_key: str = ""
    transport: Literal["streamable-http", "sse"] = "streamable-http"


class GitInput(BaseModel):
    url: str
    subdir: str = ""
    ref: str = ""
    name: str = ""


class KBInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    embedding_id: str = ""


class RunInput(BaseModel):
    input: str = Field(min_length=1, max_length=20000)
    version: int | None = None
    session_id: str | None = None
    stream: bool = False


class ResumeInput(BaseModel):
    input: str = Field(default="", max_length=20000)
    stream: bool = False
