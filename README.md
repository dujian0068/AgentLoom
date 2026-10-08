# 织点 · AgentLoom

团队低代码 Agent 平台，Vue 3 + TypeScript 前端，Python + FastAPI 后端。v0.2 已实现真实服务端存储与运行 API，并拆分管理接口、运行服务及前端配置/运行模块，旧静态演示保留在 `apps/web-demo`。

总体分层、模块职责、执行链路和后续拆分见 [架构设计 v0.3](docs/architecture-v0.3.md)。该文档区分当前实现与目标边界，目录规划按可运行模块逐步落地。

## 文档

完整阅读入口：[文档目录](docs/README.md)。需求与设计已整理为 Markdown，配图使用仓库内相对路径，可直接在 GitHub 阅读。

| 文档                                                          | 内容                                             |
| ------------------------------------------------------------- | ------------------------------------------------ |
| [产品需求 v0.2](docs/requirements-v0.2.md)                    | 平台范围、资源管理、发布与运行、验收标准         |
| [总体架构 v0.3](docs/architecture-v0.3.md)                    | Runtime 核心、分层、模块职责与执行链路           |
| [Hooks 设计 v0.1](docs/hooks-design-v0.1.md)                  | 模型与工具出入参、扩展注册、管道、隔离与恢复     |
| [Hooks 使用说明 v0.1](docs/hooks-runtime-v0.1.md)             | 已实现挂点、Python SDK、发布绑定与失败恢复       |
| [上下文管理设计 v0.1](docs/context-management-design-v0.1.md) | 上下文装配、预算、压缩、Skill/RAG 与主子任务隔离 |
| [运行时事件总线](docs/runtime-event-bus.md)                   | 工具请求与结果契约、注册、取消和恢复             |
| [目录职责规划](docs/directory-plan.md)                        | 目录边界、依赖方向与后续模块拆分                 |

## 本地启动

需要 Python 3.11+ 和 Node.js 22.12+（本轮使用 Node 24.21）。使用 PostgreSQL 时，先按下节配置项目根目录的 `.env` 并保持 SSH 隧道连接。在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
npm --prefix apps/web ci
npm --prefix apps/web run build
./deploy/run.sh
```

访问 http://localhost:8766 。首次打开由你自行创建空间管理员账号；没有默认密码，也不会预置供应商 Key。

1. 在模型连接中添加 DeepSeek / OpenAI，填写 Base URL、实际模型 ID 与 Key。
2. 导入 Skill、接入 MCP、上传知识文档。
3. 配置 Agent，保存并发布，再进入发布应用调试。
4. 在空间成员页生成 API Key，调用同一发布版本。

Vue 开发模式：后端正常启动后，执行 `npm --prefix apps/web run dev`，访问 http://127.0.0.1:5173 。使用 `localhost` 启动 Vite 时设置 `AGENT_LOOM_DEV_ORIGIN=http://localhost:5173`。

## PostgreSQL 与 SSH 隧道

项目根目录的 `.env` 配置数据库连接。首次配置可参考 `.env.example`；已有 `.env` 时保留原文件，仅调整需要修改的变量。数据库密码在本机填写，`.env` 已被 Git 忽略。

```dotenv
DB_BACKEND=postgresql
DB_HOST=127.0.0.1
DB_PORT=15432
DB_NAME=agentloom
DB_USER=agentloom
DB_PASSWORD=
```

这里的 `127.0.0.1:15432` 是**运行 Python 后端的本机 SSH 隧道入口**。SSH 将连接转发到远程 PostgreSQL，`DB_HOST` 无需填写远程数据库地址。后端运行期间须保持隧道；转发目标由已有 SSH 配置决定。

Python 启动时读取项目 `.env`，已设置的系统环境变量优先，文件中的密码按字面值读取。`DB_BACKEND=postgresql` 时，缺少密码、隧道中断或连接失败都会报错，不会回退到 SQLite。修改连接配置后重启后端。可选项 `DB_SSLMODE` 默认 `prefer`，`DB_CONNECT_TIMEOUT` 默认 5 秒。

启动时自动执行 PostgreSQL 的编号迁移，数据库账号需具有目标 schema 的建表、建索引与读写权限。现有 SQLite 数据不会自动导入 PostgreSQL，原 `data/agentloom.db` 会保留。离线开发可显式设置 `DB_BACKEND=sqlite`；未配置 `DB_BACKEND` 和 `DB_HOST` 时也使用 SQLite。

PostgreSQL 关键词检索使用 GIN 全文索引，支持中文二元词及 SQL 标识符；暂不依赖 pgvector。向量仍以 JSON 文本存入 `TEXT` 字段，相似度在 Python 中计算，适用于当前小规模知识库。接入 PostgreSQL 后仍以单进程启动，任务调度尚未改为多进程或分布式服务。

可执行 `.venv/bin/python deploy/check_db.py` 做只读连接检查。

连接、部署和备份细节见 [PostgreSQL 使用说明](docs/database-postgresql.md)。

## 实现范围

- 账号登录、空间邀请码注册、基础成员访问；管理员管理模型密钥与 API Key。
- 模型连接：DeepSeek / OpenAI 的 Chat Completions 工具调用接口；可配置模型 ID 与连接测试。模型必须实际支持该接口和工具调用。
- Skill：完整目录上传、公开 HTTPS Git 导入、frontmatter 校验、附件读取、按需加载。
- 工具：外部 MCP Streamable HTTP / SSE，工具发现与测试；Python 托管包上传/Git、Docker 构建、stdio MCP 调用。
- 知识库：MD/TXT/SQL 上传、分块、文件引用、关键词 + 向量融合检索；独立 Embedding Hooks、固定索引版本、文件导入恢复和新索引重建。
- Agent：主 Agent 的 ReAct/自动 Plan，内嵌子 Agent 配置；子 Agent 不选模型或执行模式，动态处理委派任务。
- 发布：不可变配置版本；草稿不允许运行。网页和 API 同一引擎，支持会话、SSE 事件、取消、超时、产物下载。
- Hooks：模型、工具、上下文、主/子任务共用版本化 HookManager；可信 Python 扩展可校验或修改允许的输入输出、阻止执行。真实结果先保存，后处理失败恢复不重复底层调用。上传隔离 Worker 和可视化管理尚待实现。

## Agent 执行循环

采用类似 Codex 的任务执行方式，自行实现运行时，不声称复制 Codex 的内部实现。

两种策略共用“模型决策 → 工具执行 → 反馈结果 → 继续决策”的循环。ReAct 按当前结果行动；Plan 必须先通过 `update_plan` 创建结构化计划，随即自动执行。计划可以在运行中添加、调整或取消步骤，状态为 pending / in_progress / completed / cancelled。计划由模型维护，不再逐条执行预先生成的固定列表。子 Agent 共用循环、独立上下文和资源绑定，继承主模型，不配置独立模式。

工具调用经过统一事件总线：`Loop → tool.execute 请求 → EventBus → 注册处理器 → ToolOutcome → Loop`。Loop 只保存调用进度、发送请求和接收结果，不根据工具名称判断文件、Skill、MCP 或 RAG 的执行方式。工具定义、绑定权限、参数校验、超时和恢复策略由工具运行时与各处理器负责。请求使用关联 ID 匹配结果，取消时会等待处理器清理；子 Agent 可以在父请求等待期间继续提交自己的工具请求。

当前总线在单进程内通过 asyncio 实现，PostgreSQL 继续保存执行记录与加密检查点。请求/响应与通知订阅分开：执行请求只交给一个处理器，日志等观察者可以订阅通知，观察者失败不替代工具结果。架构与扩展接口见 [运行时事件总线](docs/runtime-event-bus.md)。

模型返回候选回答后，运行时发起完成检查，依据任务目标、计划和工具记录判断 complete / continue / blocked。未完成且可继续时反馈具体缺项，继续执行；存在真实阻碍时进入 needs_input，用户补充信息后继续。检查由同一模型执行，是额外质量控制，不是保证正确的外部裁判；代码测试、文件读回或其他验证仍需要真实工具证据。

新配置默认在有效输入预算达到 80% 时主动压缩，也可配置用户轮数或行动次数触发；按完整工具调用组保留原任务、当前计划、摘要和最近记录。当前采用保守 Token 估算；未配置策略的旧发布版本保留约 60000 字符策略。原始消息与模型可见视图分开保存，Hook 的临时附加资料不会自动写入永久历史。技能按需读取。网页执行记录展示计划更新、工具失败、完成检查、压缩和子任务结果，不显示内部思维链。

执行检查点加密保存在当前数据库的 checkpoints 表，包括主/子任务上下文、计划和工具进度；任务文件保存在原工作区。失败、取消、重启中断或等待补充的任务可从“最近任务”选择，再点“继续任务”；恢复仍使用原发布版本。已完成的工具不会自动重跑，执行中中断而结果不明的外部操作会标为未知，要求先观察实际状态，避免盲目重放。仍然不是跨外部服务的 exactly-once 保证。

API：`GET /api/v1/agents/{id}/runs` 查看自己的任务，`POST /api/v1/runs/{id}/resume` 恢复，JSON 为 `{"input":"可选补充信息","stream":true}`。恢复只允许原运行用户，沿用空间鉴权、会话并发和运行限额。没有检查点的历史任务需要重新发起；完成任务不能恢复。恢复保留原任务的上下文；在它之后发起的其他任务不会自动混入该检查点。

模型请求已经发出但未收到结果时，任务返回 `requires_model_retry=true`。核对供应商状态后，网页勾选允许重试，或恢复 API 显式传 `retry_unknown_models: true`；否则返回 409，不重发请求。已经保存真实响应、仅 after Hook 失败的任务不需要这个授权，恢复继续后处理。

## v0.2 工程与可靠性

后端入口 `apps/api/agentloom/app.py` 只负责组装；业务接口在 `routes/`，运行、发布快照与构建逻辑在 `services/`。运行循环位于 `packages/runtime/agentloom_runtime/engine.py`，计划维护、上下文压缩和完成检查分别在 `planning.py`、`context.py`、`completion.py`。前端 `components/AgentEditor.vue` 管理开发配置，`PublishedAgentView.vue` 展示发布应用，`composables/useRunWorkspace.ts` 管理运行状态和事件连接。

运行时通过 `runtime.create_engine()` 组装：`event_bus.py` 管理异步请求和结果，`tool_contracts.py` 声明统一契约，`tool_runtime.py` 提供工具注册和统一校验，`handlers/` 分别实现文件、技能、知识检索、MCP、计划和子任务。增加受信任的后端工具只需注册 `ToolDefinition`，无需修改 Engine 分支；示例见 [word_count.py](examples/runtime-tools/word_count.py)。已有页面 MCP 接入流程和工具 SDK 保持可用。

SQLite 与 PostgreSQL 使用各自的编号 SQL 迁移，通过 `schema_migrations` 记录版本，启动时在事务中执行。已有 v0.1 SQLite 数据库会保留原表与数据并补齐记录及索引；后续结构变更通过新的迁移文件完成。PostgreSQL 迁移文件在 `apps/api/agentloom/migrations/postgresql/`。升级前按下文备份数据库与本地文件。

任务状态和对应结束事件在同一事务提交；事件序号支持并发写入。初始化失败也会结束任务并清理运行句柄。SSE 支持 `after` / `Last-Event-ID` 续接；前端按序号去重、有限次数重连并显示连接问题，切换任务会关闭旧连接。恢复历史对话时保留每次补充信息。

模型请求对 429、502、503、504 和连接失败最多重试两次，采用有界退避；401 等配置错误不重试。读取超时不自动重试，避免重复请求。一次引擎模型调用可能包含多次 HTTP 尝试，供应商计费取决于实际处理情况。响应结构、工具参数和向量维度校验失败会给出明确错误，不向前端返回供应商错误正文或 Key。

## 运行条件与当前限制

模型未配置真实 Key 时，不提供假模型回答。测试中使用替身模型验证执行逻辑；实际供应商调用需要配置后验证。

Skill 脚本与托管工具需要可用 Docker 引擎。本机未安装 Docker，因此容器构建与脚本执行未做实机验证。不会在宿主机执行上传脚本。上传内容仍会保存，页面显示缺少环境；Docker 就绪后可以构建。脚本默认使用 `python:3.12-slim`，无网络，只挂载该任务工作区与绑定技能。依赖额外软件的 Skill 需要后续定制镜像。托管工具入口是 `tool.py`，可附 `requirements.txt`，运行时默认无网络。

Docker Compose 仅运行平台服务，**默认不挂载宿主 Docker socket**。如需托管工具/脚本，当前推荐在有 Docker 的宿主机原生启动后端；后续拆分独立执行服务。Compose 镜像本次未实机构建。

Git 导入首版仅支持公开 HTTPS 仓库，不支持私有凭据。每次 Skill 导入一个技能目录，文件夹最多 30MB、500 文件；知识文档单文件最多 5MB。

知识库索引在请求中执行，文件导入及各批次向量化都有持久化记录。失败后可按原导入 ID 恢复，已保存的模型结果不会重复调用；结果未知时需要明确允许重试。模型和 Embedding Hooks 在创建知识库时固定，文档入库与查询使用同一处理链。修改模型、维度或 Hook 时，通过 API 创建新知识库索引，完成后在 Agent 草稿选择它并重新发布；旧索引和发布版本继续保留。接口与扩展示例见 [Embedding Hooks 与知识库索引](docs/embedding-hooks-v0.1.md)。

发布记录固定文档 ID 集合；新上传文件须重新发布后加入。删除文档后历史运行也不能再检索它；文件删除采用逻辑删除。现阶段在 SQLite 或 PostgreSQL 的文本字段中保存向量，并由 Python 在小规模数据上计算相似度，后续可替换向量库。

第一版是单进程单机版本；每空间最多并行 4 个运行，每次执行 10 分钟超时，模型调用上限 96 次、每实例最多 64 次行动、最多 8 个委派任务；压缩和完成检查计入模型调用预算。恢复任务会重置本次执行预算，累计调用仍记录。子任务顺序执行，无递归创建；暂未实现分布式队列或完整 Skill 宿主工具兼容。

网页使用 cookie 会话，API 使用有效期 90 天的空间 Key。服务器通过 HTTPS 反向代理部署时设置 `AGENT_LOOM_SECURE_COOKIE=1`；同源校验用请求 Host 和 Origin，需要代理保留正确 Host。

## 目录

```text
apps/web/                         Vue 正式前端
apps/api/agentloom/               app 组装、routes 接口、services 服务、migrations 迁移
packages/runtime/agentloom_runtime/  模型适配、执行引擎、MCP、容器执行
packages/tool-sdk/                Python MCP 工具 SDK
packages/contracts/              导出的 OpenAPI 契约
examples/runtime-tools/           通过注册表扩展运行时工具的示例
examples/hooks/                   无需 Key 的可信 Hooks 接入示例
examples/skills/report-helper/    可上传的完整技能目录
examples/tools/text-utils/        可托管的 Python MCP 工具
apps/web-demo/                   旧交互演示
docs/                            需求、目录与验证记录
deploy/                          本地启动脚本、Dockerfile、Compose
tests/                           后端集成测试、临时 UI QA 服务
data/                            运行数据（Git 忽略）
work/                            临时验证数据（Git 忽略）
```

SQLite 模式备份需包含 `data/agentloom.db`；PostgreSQL 模式需单独备份远程数据库。两种模式都必须备份本机的 `data/encryption.key`、`data/assets/`、`data/runs/`，数据库不会代替这些文件的存储。加密密钥文件权限为 0600；丢失后无法解密已保存的供应商 Key 和任务检查点。数据库与文件应保留对应的同一批次备份。

## 工具开发

可安装 SDK：`.venv/bin/pip install -e packages/tool-sdk`。使用 `ToolServer` 的 `@server.tool()` 声明函数，`server.serve()` 启动 stdio MCP。外部部署也可以直接使用官方 `mcp.server.fastmcp.FastMCP` 并选择 HTTP 传输。示例目录可以直接上传。

## 验证

```sh
.venv/bin/pip install -r requirements-dev.txt
./deploy/check.sh
```

检查脚本依次执行 Python 静态检查与格式检查、后端测试、前端格式检查与测试、TypeScript 检查及 Vue 生产构建。修正格式可执行 `.venv/bin/ruff format apps/api packages tests examples` 与 `npm --prefix apps/web run format`。

事件总线改造后，默认 SQLite 回归已通过 84 项后端测试，前端 5 项测试与构建通过。新增验证覆盖请求关联、通知隔离、嵌套调用、取消与超时清理、注册工具扩展、主子资源隔离和旧检查点恢复。PostgreSQL 测试设施的连接失败会隐藏连接参数与底层异常，另有渲染测试验证不暴露凭据。此前数据库接入轮次的 13 项 PostgreSQL 核心验证通过；本轮 3 项远程复测在连接阶段被阻塞（127.0.0.1:15432 SSH 隧道未监听），不能视为已通过。此前浏览器验证使用独立 QA 数据与替身模型，覆盖保存、发布、自动 Plan、等待补充和恢复；不写入正式账号或模型连接。

默认 pytest 会强制使用临时 SQLite 数据库，即使 `.env` 已配置 PostgreSQL，也不会访问正式数据库的数据表。需要验证真实 PostgreSQL 时保持 SSH 隧道，显式执行：

```sh
.venv/bin/pytest -q --postgres
```

该选项使用 `.env` 中的 PostgreSQL 连接，在每个集成测试中创建独立的随机 schema，测试结束后自动删除该 schema；数据库账号需有创建 schema 的权限。测试文件与密钥也使用临时目录。也可执行 `./deploy/check.sh --postgres`，连同前端一并检查。

测试包含真实 MCP HTTP 服务发现与调用；模型与 embedding 使用可控替身验证执行及混合检索，不使用实际供应商费用。

API 文档：http://localhost:8766/api/docs 。
需求文档：[产品需求 v0.2](docs/requirements-v0.2.md)；本轮记录：[实现记录 v0.2](docs/implementation-v0.2.md)。
