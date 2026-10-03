# AgentLoom 目录职责与依赖规划

日期：2026-10-02

总体方案：以 Agent Runtime 为核心的模块化单体，管理控制面与运行服务分别承担配置和实例生命周期。逻辑分六层，当前仍部署一个 Python 服务；完整架构见 [architecture-v0.3.md](architecture-v0.3.md)。

以下明确区分现有目录和后续目标，不预先创建大量空模块。

## `apps/web/`

- **现有**：Vue 3 + TypeScript 管理工作台及发布应用；`AgentEditor`、`PublishedAgentView`、`useRunWorkspace` 已拆分。
- **目标**：`features/agents`、`resources`、`knowledge`、`runs`、`space`、`auth` 按功能组织；`App.vue` 只保留页面壳。
- **边界**：仅通过平台 HTTP/SSE 访问业务，不持有供应商密钥或直接连接数据库。

## `apps/api/agentloom/`

| 文件或目录                | 职责与状态                                                          |
| ------------------------- | ------------------------------------------------------------------- |
| `app.py`                  | FastAPI 组装、生命周期、公共中间件与异常映射。                      |
| `routes/`                 | 请求响应、输入校验、认证上下文、SSE 和下载。                        |
| `services/`               | 完整业务用例；优先收拢 RunService 与 ReleaseService，再补资源服务。 |
| `domain/`（目标）         | Agent/发布版本/资源/运行状态与业务规则。                            |
| `repositories/`（目标）   | 按 Agent、Resource、Run、Knowledge 等领域收口数据操作。             |
| `infrastructure/`（目标） | 数据库、文件、SecretStore、EventStore、CheckpointStore 具体实现。   |

当前 `store.py`、`database.py`、`assets.py`、`security.py` 仍在包根；路由仍包含部分业务 SQL。

`knowledge.py` 当前同时包含文档入库与检索，目标拆成 IngestionService 与 Retriever。

## `packages/runtime/agentloom_runtime/`

| 文件或目录                                     | 职责与状态                                                                                                                                    |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `runtime.py`                                   | 根据发布快照和依赖接口组装运行时。                                                                                                            |
| `engine.py`                                    | 统一执行循环、模型与工具消息、调用进度、预算、继续和完成。                                                                                    |
| `context.py` / `planning.py` / `completion.py` | 上下文、计划与完成策略。                                                                                                                      |
| `hooks/`（目标）                               | HookManager、阶段上下文、HookResult、注册顺序、超时/异常和恢复策略；模型、工具、上下文和生命周期扩展。当前只有观察通知，完整 Hooks 尚未实现。 |
| `event_bus.py`                                 | 工具请求/响应、关联 ID、取消/超时、观察通知。                                                                                                 |
| `tool_contracts.py` / `tool_runtime.py`        | 工具契约、注册表、权限/参数/策略校验。                                                                                                        |
| `handlers/`                                    | 工作区、Skill、MCP、RAG 查询、计划更新、子任务委派。                                                                                          |
| `ports.py`（目标）                             | ModelGateway、CheckpointStore、EventSink、ChildTaskRunner 等接口。                                                                            |
| `adapters/`（目标）                            | provider、MCPClient、Sandbox 等实现，逐步从当前文件平移。                                                                                     |

**边界**：Runtime 接收已发布配置，不承担登录、资源导入、草稿 CRUD 或文档入库。

## `packages/contracts/`

- **现有**：导出的 OpenAPI。
- **目标**：统一 AgentConfig、ChildConfig、RuntimeSpec、ToolRequest/Outcome、RunEvent、Checkpoint 等版本化契约，减少手写类型漂移。

## `packages/tool-sdk/`

独立的 Python MCP 工具开发 SDK；函数声明、参数 schema、MCP 服务启动。保持不依赖平台 API 实现。

## `examples/`

可上传 Skill、托管 MCP 工具、直接注册运行时工具的最小示例。

## `docs/` / `deploy/` / `tests/`

需求、架构、验证记录；启动、配置和部署脚本；模块与端到端行为测试。

## 依赖与数据流

- API 路由调用应用服务；业务事务和任务生命周期由应用服务统一处理。
- 应用组装入口依赖 Runtime；Runtime 不反向导入 `apps/api`。
- Loop 通过 ModelGateway 调模型，通过 EventBus 请求工具；具体能力由处理器实现。
- 数据访问经过仓储或存储接口；接口由使用方定义，具体实现由组装入口注入。
- Hooks 参与执行并可在允许阶段修改/拦截；EventBus 传递请求与响应，Observer 消费通知。修改后仍须执行核心授权与参数校验。
- 工具执行请求和运行记录/SSE 是两条通道；页面消费事件不控制实际工具执行。

## 运行数据

当前 PostgreSQL 保存账号、资源配置、发布、会话、运行、事件、加密检查点及知识原文/分块/向量。

本地 `data/assets` 保存导入包，`data/runs` 保存工作区与产物，`data/encryption.key` 保存加密根密钥。

SQLite 可用于隔离测试和显式离线模式。数据库与本地文件需要对应备份。

## Hooks 详细设计

详见 [hooks-design-v0.1.md](hooks-design-v0.1.md)。目标 `hooks/` 包内区分 HookPointRegistry、HookRegistry、HookManager、HookPipeline 和 HookExecutor；模型网关与工具分发使用同一套机制。用户 SDK 和隔离 Worker 尚待实现。
