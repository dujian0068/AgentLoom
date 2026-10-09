# AgentLoom 源码目录与依赖

更新：2026-10-09；核对基线 `9941e31`。本文列出实际目录和扩展入口，后续建议单独标注。完整关系见[总体技术设计](technical-design.md)。

```text
agent-loom/
├── apps/
│   ├── web/src/                       Vue + TypeScript 正式工作台
│   ├── api/agentloom/                 FastAPI、应用服务、存储适配和迁移
│   └── web-demo/                      历史静态演示
├── packages/
│   ├── runtime/agentloom_runtime/     Agent 核心和能力端口/适配器
│   ├── tool-sdk/                      Python MCP 工具开发 SDK
│   └── contracts/openapi.json         导出的 API 契约
├── examples/                         运行时工具、Hooks、Skill、托管工具示例
├── deploy/                           启动、验证、Docker 与卷探测
├── tests/                            后端及集成测试、测试辅助服务
├── docs/                             当前设计、专题、历史记录与 SVG
├── data/                             本地运行数据，Git 忽略
└── work/                             临时验证资料，Git 忽略
```

## 1. 前端

| 路径                                                                        | 当前职责                                                |
| --------------------------------------------------------------------------- | ------------------------------------------------------- |
| [App.vue](../apps/web/src/App.vue)                                          | 页面壳、登录与资源管理等操作；还没有全部按 feature 拆分 |
| [AgentEditor.vue](../apps/web/src/components/AgentEditor.vue)               | 草稿配置、主/子模板、资源和压缩策略                     |
| [PublishedAgentView.vue](../apps/web/src/components/PublishedAgentView.vue) | 已发布应用、对话、计划、引用和产物                      |
| [useRunWorkspace.ts](../apps/web/src/composables/useRunWorkspace.ts)        | Run 选择、事件连接、取消与恢复                          |
| [api.ts](../apps/web/src/api.ts)                                            | HTTP 与 SSE 客户端                                      |
| [domain](../apps/web/src/domain)                                            | 类型、对话投影、模型目录、压缩配置及显示辅助            |
| [前端测试](../apps/web/tests)                                               | 对话、目录与策略等行为验证                              |

前端只访问平台接口，不直接连接数据库或调用模型供应商。已保存的 Key 不回传；用户新输入的 Key 会临时保留在表单并提交给 API，不写入浏览器持久存储。后续可将 `App.vue` 中的模型、知识、工具、空间等页面按功能拆出；当前并不存在 `features/` 目录。

## 2. 平台后端

| 路径                                                                                                       | 当前职责                                                                                                 |
| ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| [app.py](../apps/api/agentloom/app.py)                                                                     | 应用、生命周期、中间件与路由组装                                                                         |
| [routes](../apps/api/agentloom/routes)                                                                     | auth、spaces、models、agents、skills、tools、knowledge、resources、runs；HTTP 校验与响应，部分仍包含 SQL |
| [services/agents.py](../apps/api/agentloom/services/agents.py)                                             | Agent 配置验证、发布快照和资源绑定                                                                       |
| [services/runs.py](../apps/api/agentloom/services/runs.py)                                                 | 执行与 Runtime 装配、状态/事件收尾、恢复辅助校验；准入、创建及恢复请求主要在 routes/runs.py              |
| [services/session_history.py](../apps/api/agentloom/services/session_history.py)                           | 连续消息、历史前缀与模型视图存取                                                                         |
| [services/model_catalog.py](../apps/api/agentloom/services/model_catalog.py)                               | 模型目录与元信息解析                                                                                     |
| [services/hooks.py](../apps/api/agentloom/services/hooks.py)                                               | 平台可信 Hook 注册和发布绑定                                                                             |
| [services/embedding.py](../apps/api/agentloom/services/embedding.py)                                       | Embedding 调用、Hook 链与持久操作                                                                        |
| [services/knowledge_imports.py](../apps/api/agentloom/services/knowledge_imports.py)                       | 导入任务、批次恢复、索引重建                                                                             |
| [services/knowledge_search.py](../apps/api/agentloom/services/knowledge_search.py)                         | 可恢复查询向量化与检索调用                                                                               |
| [knowledge.py](../apps/api/agentloom/knowledge.py)                                                         | 分块、关键词/向量检索及相关数据操作；并非所有知识逻辑都已拆走                                            |
| [services/workspaces.py](../apps/api/agentloom/services/workspaces.py)                                     | Run 存储绑定和工作区 Provider 的平台适配                                                                 |
| [services/tools.py](../apps/api/agentloom/services/tools.py)                                               | 托管工具构建等服务                                                                                       |
| [assets.py](../apps/api/agentloom/assets.py)                                                               | 上传/Git 包处理、技能元信息与路径校验                                                                    |
| [security.py](../apps/api/agentloom/security.py)、[dependencies.py](../apps/api/agentloom/dependencies.py) | 密码/令牌、加解密、认证与空间访问上下文                                                                  |
| [database.py](../apps/api/agentloom/database.py)、[store.py](../apps/api/agentloom/store.py)               | SQLite/PostgreSQL、事务、迁移、表数据/事件/检查点操作                                                    |
| [schema.py](../apps/api/agentloom/schema.py)、[state.py](../apps/api/agentloom/state.py)                   | 请求契约和平台运行状态                                                                                   |
| [migrations](../apps/api/agentloom/migrations)                                                             | SQLite 001–005 与 postgresql 下对应迁移                                                                  |

当前没有独立的 `domain/`、`repositories/`、`infrastructure/` 包，也没有名为 `RunService`、`ReleaseService` 的完整服务类集合。它们是后续收口方向，不应按架构图名称寻找不存在的代码。

## 3. Agent Runtime

| 模块                                                                                                                                                                                                                                   | 职责                                                                |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| [runtime.py](../packages/runtime/agentloom_runtime/runtime.py)                                                                                                                                                                         | `create_engine` 组装与检查点兼容解析                                |
| [module_contracts.py](../packages/runtime/agentloom_runtime/module_contracts.py)、[modules.py](../packages/runtime/agentloom_runtime/modules.py)                                                                                       | 模型、策略、上下文、压缩、完成检查端口，模块绑定和关闭              |
| [engine.py](../packages/runtime/agentloom_runtime/engine.py)                                                                                                                                                                           | 统一 Loop、主/子实例执行状态、进度与持久化边界                      |
| [execution_strategy.py](../packages/runtime/agentloom_runtime/execution_strategy.py)、[planning.py](../packages/runtime/agentloom_runtime/planning.py)、[completion.py](../packages/runtime/agentloom_runtime/completion.py)           | ReAct/Plan 策略、计划工具、完成判断                                 |
| [model_gateway.py](../packages/runtime/agentloom_runtime/model_gateway.py)、[provider.py](../packages/runtime/agentloom_runtime/provider.py)                                                                                           | 默认模型网关与供应商协议/HTTP 适配                                  |
| [context_manager.py](../packages/runtime/agentloom_runtime/context_manager.py)、[context.py](../packages/runtime/agentloom_runtime/context.py)、[budget.py](../packages/runtime/agentloom_runtime/budget.py)                           | 消息管理、压缩算法、预算及 Token 估算                               |
| [context_validation.py](../packages/runtime/agentloom_runtime/context_validation.py)、[hooked_context.py](../packages/runtime/agentloom_runtime/hooked_context.py)                                                                     | 上下文合法性和 Hook 装饰边界                                        |
| [hooks](../packages/runtime/agentloom_runtime/hooks)                                                                                                                                                                                   | 契约、注册表、指纹、Manager、Executor、durable operation            |
| [model_hooks.py](../packages/runtime/agentloom_runtime/model_hooks.py)、[lifecycle_hooks.py](../packages/runtime/agentloom_runtime/lifecycle_hooks.py)、[embedding_hooks.py](../packages/runtime/agentloom_runtime/embedding_hooks.py) | 各类边界接入及校验                                                  |
| [event_bus.py](../packages/runtime/agentloom_runtime/event_bus.py)、[observation.py](../packages/runtime/agentloom_runtime/observation.py)                                                                                             | 请求/响应、通知与观察者                                             |
| [tool_contracts.py](../packages/runtime/agentloom_runtime/tool_contracts.py)、[tool_runtime.py](../packages/runtime/agentloom_runtime/tool_runtime.py)                                                                                 | 工具定义、注册、统一执行、授权/参数/策略验证                        |
| [execution_services.py](../packages/runtime/agentloom_runtime/execution_services.py)                                                                                                                                                   | Handler 使用的能力服务和委派适配                                    |
| [handlers](../packages/runtime/agentloom_runtime/handlers)                                                                                                                                                                             | filesystem、workspace、skills、knowledge、mcp、planning、delegation |
| [workspace.py](../packages/runtime/agentloom_runtime/workspace.py)、[workspace_store.py](../packages/runtime/agentloom_runtime/workspace_store.py)                                                                                     | 工作区 Provider、namespace、POSIX 文件操作和锁                      |
| [sandbox.py](../packages/runtime/agentloom_runtime/sandbox.py)、[mcp_tools.py](../packages/runtime/agentloom_runtime/mcp_tools.py)                                                                                                     | Docker 沙箱、外部/托管 MCP 调用                                     |

接口实际在 `module_contracts.py` 等使用方模块，不存在统一 `ports.py`；适配器仍分布在上述文件，尚未统一移动到 `adapters/`。文件移动本身不等于解耦，先保持依赖边界和可替换契约。

## 4. 扩展入口与依赖方向

- 新模型或执行策略：实现相应 Protocol，通过 `create_engine` 注入，声明版本化 `module_id` 和可序列化配置。
- 新原生工具：注册 `ToolDefinition`，实现受限 Handler；不在 Engine 添加工具名分支。见 [word_count 示例](../examples/runtime-tools/word_count.py)。
- 外部工具：走标准 MCP 和[工具 SDK](../packages/tool-sdk)；不要把 MCP 服务管理塞进 Loop。
- 新 Hook：可信代码注册 `HookDefinition` 并绑定，遵守挂点补丁范围与恢复策略；见[Hooks 示例](../examples/hooks)。
- 新工作区后端：保持 Provider/Store 的逻辑 scope、卷身份、文件安全及恢复语义；不能仅替换路径拼接。
- 新持久化实现：由 API 组装时注入回调；下一步可收口为 CheckpointStore/Repository，Runtime 不反向导入平台包。

执行请求和观测通知分离。Hooks 可以在允许阶段修改数据，Observer 只观察。最终授权、参数校验和真实结果保存必须在执行路径中完成。

## 5. 部署与数据位置

[deploy](../deploy) 包含单进程启动、检查脚本、Dockerfile/Compose、数据库只读检查与工作区探测。当前容器配置不等于分布式执行部署。

| 存储                       | 内容                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------------ |
| PostgreSQL / SQLite        | 用户/空间、资源、发布、Run/事件、检查点、会话消息/视图、知识正文/分块/向量、Embedding 操作、工作区绑定 |
| `data/assets`              | 导入的 Skill 和托管工具包                                                                              |
| `data/workspaces` 或共享卷 | 当前按空间/Agent/Session 隔离的主工作区与子实例文件                                                    |
| `data/runs`                | 历史 Run 工作区兼容目录                                                                                |
| `data/encryption.key`      | 根加密密钥；需与 SQL 和文件备份对应                                                                    |

环境变量、卷布局、迁移表清单和备份流程见[部署运维](deployment-operations.md)。
