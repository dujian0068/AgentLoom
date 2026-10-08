from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_ratio: float = Field(default=0.8, gt=0, le=1, strict=True)
    target_ratio: float = Field(default=0.6, gt=0, lt=1, strict=True)
    user_turns: int | None = Field(default=None, gt=0, strict=True)
    model_steps: int | None = Field(default=None, gt=0, strict=True)
    max_compaction_calls: int = Field(default=4, ge=1, le=16, strict=True)

    @model_validator(mode="after")
    def validate_ratios(self):
        if self.target_ratio >= self.context_ratio:
            raise ValueError("压缩目标比例必须低于触发比例")
        return self


class HookBindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")
    hook_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")
    point: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    version: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int = Field(default=100, ge=-10000, le=10000, strict=True)
    timeout: float = Field(default=1.0, gt=0, le=300, strict=True)
    failure_policy: Literal["block", "continue"] = "block"
    config: dict[str, Any] = Field(default_factory=dict)
    purposes: list[str] | None = Field(default=None, max_length=20)
    instances: list[Literal["main", "child"]] = Field(
        default_factory=lambda: ["main", "child"], min_length=1, max_length=2
    )
    targets: list[str] = Field(default_factory=list, max_length=100)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    model: str = ""
    prompt: str = Field(default="", max_length=30000)
    mode: Literal["react", "plan"] = "react"
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    skills: list[str] = Field(default_factory=list, max_length=30)
    tools: list[str] = Field(default_factory=list, max_length=30)
    wiki: list[str] = Field(default_factory=list, max_length=20)
    subs: list[Child] = Field(default_factory=list, max_length=10)
    hooks: list[HookBindingInput] = Field(default_factory=list, max_length=32)


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["manual", "provider", "official_manifest", "platform_default"] = "manual"
    source_url: str = Field(default="", max_length=500)
    verified_at: str = Field(default="", max_length=32)
    provider_max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    chat_compatible: bool | None = None


class ModelDiscoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepseek", "openai"]
    base_url: str = Field(max_length=1000)
    api_key: str = Field(default="", max_length=500)
    resource_id: str | None = Field(default=None, max_length=100)


class ModelInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["deepseek", "openai"]
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str
    api_key: str = Field(default="", max_length=500)
    purpose: Literal["chat", "embedding"] = "chat"
    context_window: int = Field(default=32768, gt=0, strict=True)
    max_output_tokens: int = Field(default=4096, gt=0, strict=True)
    safety_margin_tokens: int = Field(default=1024, ge=0, strict=True)
    metadata: ModelMetadata = Field(default_factory=ModelMetadata)

    @model_validator(mode="after")
    def validate_budget(self):
        if self.purpose == "chat" and self.metadata.chat_compatible is False:
            raise ValueError("该模型不支持当前 Agent 使用的聊天接口")
        if self.max_output_tokens + self.safety_margin_tokens >= self.context_window:
            raise ValueError("输出预留与安全边距之和必须小于模型上下文窗口")
        if (
            self.metadata.provider_max_output_tokens is not None
            and self.max_output_tokens > self.metadata.provider_max_output_tokens
        ):
            raise ValueError("最大输出不能超过已识别的供应商输出上限")
        return self


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
    retry_unknown_models: bool = Field(default=False, strict=True)
