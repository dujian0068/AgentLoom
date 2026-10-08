# Hooks 运行时使用说明 v0.1

更新：2026-10-09。本文描述当前代码接口；完整目标设计见[统一 Hooks 设计](hooks-design-v0.1.md)。

**已实现可信 Python Hooks 的注册、版本绑定、有序处理、字段校验、超时、取消和检查点恢复。** 同一个 `HookManager` 注入模型、工具、上下文、运行与子任务边界；Loop 不根据扩展 ID 分支。平台 API 可以绑定部署时注册的可信扩展，发布时冻结版本清单。团队上传 / Git 导入 Hook、管理页面和隔离 Worker 尚未开放。

Embedding 也复用这一机制，由知识库固定处理链，并在文档入库与查询时执行。索引版本、API 和恢复用法见 [Embedding Hooks 与知识库索引](embedding-hooks-v0.1.md)。

## 1. 模块与职责

| 模块 | 代码位置 | 职责 |
| --- | --- | --- |
| HookPointRegistry | `packages/runtime/agentloom_runtime/hooks/registry.py` | 挂点 Schema、可写字段、允许的返回动作。 |
| HookRegistry | 同上 | 注册可信异步函数、代码版本、配置 Schema、重算声明。 |
| HookManager | `hooks/manager.py` | 冻结绑定、匹配作用域、稳定排序、逐次校验补丁、保存管道进度。 |
| HookExecutor | `hooks/executor.py` | 执行可信函数，限制单次时间，传播取消并关闭残留任务。 |
| execute_operation | `hooks/operation.py` | 包围真实动作，区分最终输入、真实结果和有效视图，管理错误与清理。 |
| 执行边界 | `model_hooks.py`、`embedding_hooks.py`、`tool_runtime.py`、`hooked_context.py`、`lifecycle_hooks.py` | 提供业务载荷，执行不可绕过的授权、协议与预算校验；运行生命周期独立于 Loop。 |

`EventBus.subscribe` 仍是观察通知。改变参数或阻止动作必须使用 Hooks；观察者不会变成执行拦截器。

## 2. 最小接入示例

```python
from agentloom_runtime.hooks import (
    HookBinding, HookDefinition, HookManager, HookRegistry, PatchInput,
)
from agentloom_runtime.runtime import create_engine

async def clamp_limit(ctx, call):
    arguments = dict(call["arguments"])
    arguments["limit"] = min(arguments["limit"], ctx.config["max_limit"])
    return PatchInput({"arguments": arguments})

registry = HookRegistry()
registry.register(HookDefinition(
    "clamp-limit", "v1", clamp_limit,
    replay_safe=True,
    config_schema={
        "type": "object",
        "properties": {"max_limit": {"type": "integer", "minimum": 1}},
        "required": ["max_limit"],
        "additionalProperties": False,
    },
))
hooks = HookManager(registry, [HookBinding(
    "limit-orders", "clamp-limit", "tool.before",
    version="v1", targets=("orders-search",),
    config={"max_limit": 100}, timeout=1.0,
)])

# snapshot、workspace、emit、decrypt、search、save 由宿主提供。
engine = create_engine(
    snapshot, workspace, emit, decrypt, search,
    hooks=hooks,
    hook_scope={"run_id": run_id, "published_revision": revision},
    save=save,
)
```

`orders-search` 是工具的稳定 `ToolDefinition.tool_id`，不一定等于模型函数名。未声明 `tool_id` 的工具使用已注册名称；MCP 工具使用 `mcp:{资源完整 ID}:{远端工具名}`。真实工具必须已经注册并授权，绑定 Hook 不授予工具权限。

载荷采用只读映射，通过 `call["arguments"]` 读取；`ctx.config` 和 `ctx.scope` 同样深只读。`ctx.run_id`、`ctx.instance_id`、`ctx.operation_id` 等是作用域的便利访问方式。契约不传入 Engine、模型 Key、数据库连接、工具调用回调或环境变量。可信 Python 函数仍拥有宿主进程权限，不能用这个机制直接运行不受信任的上传代码。

`PatchInput` / `PatchOutput` 按**顶层字段替换**。修改 `arguments` 时应复制并保留仍需使用的参数；嵌套字典不会自动递归合并。

完整可运行样例见 [`examples/hooks/demo.py`](../examples/hooks/demo.py)：

```bash
PYTHON_DOTENV_DISABLED=1 PYTHONPATH=packages/runtime .venv/bin/python examples/hooks/demo.py
```

它使用确定性模型与内存检查点，不需要 Key、数据库或网络。部署时应使用宿主的真实持久化 `save` 回调。

### 在平台 API 中使用

由部署者编写可信启动入口，在服务开始接收请求前注册扩展。例如项目中的 `trusted_api.py`：

```python
from agentloom.services.hooks import register_trusted_hook
from agentloom_runtime.hooks import Continue, HookDefinition, PatchOutput

async def trim_answer(ctx, reply):
    if reply["content"] is None:
        return Continue()
    return PatchOutput({"content": reply["content"].strip()})

register_trusted_hook(HookDefinition(
    "trim-answer", "v1", trim_answer, replay_safe=True,
))

from agentloom.app import app  # 供 ASGI 服务器使用
```

从仓库根目录以该入口启动，而非默认的 `agentloom.app:app`：

```bash
PYTHONPATH=apps/api:packages/runtime:. .venv/bin/uvicorn trusted_api:app --host 127.0.0.1 --port 8766
```

每个 API Worker 都必须注册相同的可信版本。API 请求只选择注册表中的 ID 与配置，不允许指定 Python 路径、上传包或执行动态 import。

创建或更新 Agent 时，在完整 `AgentConfig` 请求体中加入 `hooks`（更新仍是完整 PUT，不是局部 PATCH）：

```json
{
  "hooks": [{
    "binding_id": "clean-answer",
    "hook_id": "trim-answer",
    "version": "v1",
    "point": "model.chat.after",
    "purposes": ["action"],
    "instances": ["main", "child"],
    "priority": 100,
    "timeout": 1.0,
    "failure_policy": "block",
    "config": {}
  }]
}
```

之后调用 `POST /api/agents/{id}/publish`。发布会解析扩展并保存 `hook_manifest`，包含具体版本、代码哈希、Schema、配置与排序。未注册扩展或非法配置不能发布。运行使用这份清单核对部署代码；缺少版本或代码发生变化会在调用模型前报错。新版本可与旧版本并存，旧 Agent 继续解析原版本；省略版本仅在发布时没有歧义时允许，不代表运行时跟随最新版本。

## 3. 已接入挂点与可写范围

| 挂点 | 当前允许的结果与限制 |
| --- | --- |
| `model.chat.before` | `Continue`、`Reject`，或修改追加的 user 文本、`temperature`、`top_p`、`max_tokens`。原有消息、模型绑定、tools / Schema、purpose 受保护；输出上限不能超过发布预算。 |
| `model.chat.after` | 只修改 `content` 和 `annotations`；tool_calls、用量和真实状态保持原值，最后再次验证模型协议。 |
| `model.embedding.before` | 只修改带固定索引的 `texts`，或 `Reject`；条数、索引位置、模型与索引签名受保护。绑定由知识库固定。 |
| `model.embedding.after` | 只读向量、数量 / 维度、用量及请求标识，需要 `observation=True`；真实响应先保存，完成校验后才进入观察阶段。 |
| `tool.before` | 只修改 `arguments`，或 `Reject`。每次修改以及实际执行前均验证 Schema 与授权；具体 Handler 继续检查路径等限制。 |
| `tool.after` | 只修改 `value` 和 `annotations`。真实成功 / 失败、调用身份和 `evidence_arguments` 不可改写。 |
| `context.prepare.before` | 添加 `additional_messages`（user 文本），或 `Reject`。附加材料只进入本次请求，并带扩展来源提示；不自动追加到永久对话。 |
| `context.prepare.after` | 只读观察，需要 `observation=True`。 |
| `context.compact.before` | 观察或 `Reject`；当前不开放任意选择消息覆盖范围。 |
| `context.compact.after` | 只修改策略明确声明的 `summary` 文本；任务、计划、工具关联、覆盖状态保持不变，并重检预算。 |
| `run.before` / `subagent.before` | 观察或 `Reject`；任务文本和发布身份不可修改。 |
| `run.after` / `subagent.after` | 允许附加 `annotations`，不修改已完成任务的结果与状态。 |
| `operation.error` / `operation.finally` | 只读诊断，需要 `observation=True`；不能替换结果、重试或改变实际状态。 |

尚未引入可选 ContextBlock 的独立授权清单，因此模型前置 Hook 当前采用更严格规则：保留所有既有消息，只允许追加 user 文本。自定义压缩策略若不声明摘要位置，其返回视图对后置 Hook 保持只读。

`annotations` 保存在操作 / 管道记录里，不会自动拼入回答正文或模型对话。需要改变模型可见内容时，应使用对应挂点明确允许的 `content` 或 `value` 字段。

模型前置 Hook 的增量纳入最终预算校验；跨主动阈值时由宿主进行有界的重新准备，使用已保存补丁，不重放整个 Hook 链。硬预算校验是平台规则，不能由 Hook 关闭。

## 4. 匹配、版本与错误策略

- `priority` 越小越先执行，同优先级按传入绑定的顺序执行；各阶段各自排序。
- `binding_id` 必须唯一。同一扩展需要运行两次时，显式创建两个绑定。
- `instances=("main", "child")` 默认覆盖主任务与子任务。子任务使用自己的 `instance_id`、资源和工作区授权。
- Chat 的 `purposes` 筛选 `action`、`compaction`、`completion`；内部既有 `verification` 显式映射为 `completion`。Chat 挂点不填 purposes 时只匹配 `action`，防止回答改写污染压缩摘要或完成检查 JSON。Embedding 默认匹配 `document` 与 `query`，通过知识库管理绑定，不继承 Agent 的业务 Hook。
- `targets` 筛选边界的稳定目标：工具为 tool_id，模型为 model_id。配置字段按扩展的 JSON Schema 验证。
- `timeout` 单位为秒，默认 1 秒；`HookManager.total_timeout` 默认每阶段管道 10 秒。可信函数仍需遵循异步协作式取消，进程内执行不提供恶意代码隔离。
- `failure_policy="block"` 是默认值。只有声明 `observation=True` 的观察扩展可以选 `continue`；观察扩展只能返回 `Continue`。错误 / 清理阶段的普通 Hook 异常仅形成诊断，不替换原始失败。
- `replay_safe=False` 是默认值。只有可安全重算的纯数据转换才设为 `True`；有外部副作用的扩展不可仅为了通过恢复检查而开启它。

模块检查点包含扩展 ID / 版本、代码哈希、配置、匹配、顺序、错误策略和挂点契约。默认哈希来自函数源码或字节码；依赖包、全局配置和资源版本需要部署者提供覆盖这些内容的显式 `code_hash`。恢复时配置变更会被拒绝；不要用同一个版本号替换实现。旧检查点只能迁移到空 Hook 链，不能恢复途中悄悄添加扩展。

## 5. 真实结果、恢复与取消

一次逻辑操作保存 `original_input`、`final_input`、`raw_output` 和 `effective_output`，以及 before / after 进度与诊断。真实模型响应或工具结果先持久化，再运行 after。工具结果的原始记录与给模型的投影视图分别进入会话管理。

| 中断位置 | 恢复行为 |
| --- | --- |
| before 拒绝 | 工具返回结构化拒绝信息供模型调整；底层操作未执行。模型或生命周期拒绝终止当前执行。 |
| before / after 扩展失败 | 可安全重算的未完成扩展可以续跑；已完成扩展不再执行。未声明可重算的扩展进入 `HookRecoveryRequired`。 |
| 真实结果已经保存，after 失败 | 继续处理 `raw_output`，不会为了后处理重新调用工具或模型。 |
| 工具执行中断，结果未知 | 普通工具返回“结果未知”的反馈，要求先核对实际状态；不会盲目重放。明确声明可恢复的委派等能力按各自恢复契约继续。 |
| 模型已发出但响应未知 | 默认停止；显式允许重试后才重新请求，可能产生额外计费。已保存结果的模型调用不需要重试。 |
| 检查点保存失败 | 抛出 `ToolPersistenceError`，不把持久化故障伪装成普通工具业务错误。 |

异常入口为 `HookError`，具体包括 `HookFailed`、`HookRejected` 和 `HookRecoveryRequired`。`engine.resume(retry_unknown_models=True)` 明确授权重试结果未知的模型请求；默认 `False`。API 恢复请求采用同名布尔字段，未确认时返回 409。这不授权重复结果未知的工具副作用。

取消会传递到当前 Hook 与底层任务，清理有时间限制。进程崩溃不能保证 finally 已运行；外部补偿和 exactly-once 执行不属于这个机制的保证。

## 6. 当前边界

此版本是运行内核与可信开发 SDK。以下能力仍在后续阶段：

- Hook 包上传 / Git 导入、依赖安装、团队管理页面，以及 Agent 草稿中的可视化绑定。当前已有可信扩展的 API 绑定与发布清单。
- 隔离 Hook Worker，以及面向不受信任代码的文件 / 网络 / 内存限制。
- 完整 ContextBlock 授权和来源清单、任意可选消息改写、暂停 / 短路 / around 执行。
- 跨机器租约、幂等外部事务、大载荷不可变存储引用与完整审计页面。

现有检查点由宿主保存完整操作载荷；API 宿主沿用加密存储和脱敏边界。纯 Python 使用者须自行提供相应存储保护，不应将示例的内存列表当作生产检查点服务。

## 7. 验证记录

2026-10-08 执行 `PYTHON_DOTENV_DISABLED=1 bash deploy/check.sh`：302 个后端测试、12 个前端测试通过，Python / Vue 格式检查、TypeScript 检查和生产构建通过。数据库测试使用临时 SQLite，没有连接或迁移用户的远程 PostgreSQL；模型使用确定性替身，工具集成包含本地 MCP 服务。

回归覆盖：只读载荷、有序补丁与参数复验、保护字段、目的 / 子任务匹配、取消与超时、真实结果先保存、after 失败不重复计费或执行、恢复期间用户补充、未知模型请求显式重试、发布版本固定、代码变化拦截，以及 Hook 附加输入触发的有界压缩和最终预算校验。
