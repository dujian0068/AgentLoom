# 织点 · AgentLoom

团队低代码 Agent 平台。Vue 3 + TypeScript 提供管理与发布应用，Python + FastAPI 承载服务，以可插拔 Agent Runtime 为核心。集中管理模型、Skill、MCP 工具与知识文档，配置并发布 Agent 后，通过网页或 API 使用。

当前为开发测试阶段，采用单进程模块化单体；支持 PostgreSQL 和隔离测试用 SQLite。最新文档按 `9941e31` 代码核对，更新于 2026-10-09。

## 文档

推荐通过网页阅读：在项目根执行 `npm ci`、`npm run docs:dev`，打开 http://127.0.0.1:8767 。文档站提供固定目录、中文搜索、章节定位和连续翻页。正文仍为 Markdown，GitHub 入口是[文档目录](docs/README.md)；构建与部署见[文档站说明](docs/documentation-site.md)。

| 文档                                              | 内容                                                     |
| ------------------------------------------------- | -------------------------------------------------------- |
| [总体技术设计](docs/technical-design.md)          | 当前模块架构、需求落实情况、主链路、可插拔边界与演进计划 |
| [Runtime 详细设计](docs/runtime-design.md)        | Loop、模型、策略、事件总线、Hooks、上下文、工具与恢复    |
| [平台、API 与数据设计](docs/platform-api-data.md) | 前后端、权限、发布、资源、数据库、完整接口索引           |
| [部署与运维](docs/deployment-operations.md)       | 启动、环境变量、数据库、共享卷、容器、备份与排障         |
| [产品需求](docs/requirements-v0.2.md)             | 已确认需求、验收标准与当前实现差距                       |
| [源码目录与依赖](docs/directory-plan.md)          | 文件职责、扩展入口与尚待拆分的边界                       |

[![AgentLoom 当前实现架构](docs/diagrams/current-system-architecture.svg)](docs/diagrams/current-system-architecture.svg)

## 本地启动

需要 Python 3.11+、Node.js 22.12+。在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
npm --prefix apps/web ci
npm --prefix apps/web run build
./deploy/run.sh
```

访问 http://localhost:8766 ，首次自行创建空间管理员；没有默认密码或预置供应商 Key。API 文档位于 http://localhost:8766/api/docs 。

1. 添加 DeepSeek / OpenAI 模型连接，获取模型目录、选择模型并测试。
2. 导入 Skill、接入 MCP、上传知识文本。
3. 配置 Agent 的 Prompt、模式、资源与子模板，保存并发布。
4. 进入发布应用发起任务；API Key 可在空间成员页创建。

Vue 开发模式执行 `npm --prefix apps/web run dev`，默认 http://127.0.0.1:5173 。以 `localhost` 访问 Vite 时，后端需设置 `AGENT_LOOM_DEV_ORIGIN=http://localhost:5173`。

### PostgreSQL 与 SSH 隧道

在项目根 `.env` 中配置；已有文件不要覆盖。完整模板见 [.env.example](.env.example)，密码仅在本机填写。

```dotenv
DB_BACKEND=postgresql
DB_HOST=127.0.0.1
DB_PORT=15432
DB_NAME=agentloom
DB_USER=agentloom
DB_PASSWORD=
```

`127.0.0.1:15432` 是 **Python 后端所在机器的 SSH 隧道入口**。运行期间保持隧道。指定 PostgreSQL 后，连接失败会报错，不回退 SQLite；已有 SQLite 数据不会自动迁移。可运行 `.venv/bin/python deploy/check_db.py` 做只读连接检查。

应用启动自动执行编号迁移。开发离线模式可显式设置 `DB_BACKEND=sqlite`；未配置 backend 和 DB_HOST 时也使用 SQLite。启动、迁移、Docker 网络与备份细节见[部署运维](docs/deployment-operations.md)。

## 核心能力

- **统一 Loop**：模型决策 → 工具请求 → 反馈 → 继续；ReAct 与自动 Plan 共用内核。Plan 使用 `update_plan` 动态维护步骤；候选回答经过完成检查。
- **事件总线**：Loop 不按文件/MCP/Skill 等工具名称实现功能。单处理器响应执行请求，多个 Observer 可订阅通知；当前总线在进程内。
- **子 Agent**：内嵌模板，独立 Prompt、资源与上下文，按需创建，继承主模型；不单选模式，当前顺序执行且不递归。
- **连续上下文**：每个新提问建立 Run，同 Session 接上历史；原始记录与模型视图分开，默认有效输入预算达到 80% 主动压缩，也可配置轮数条件。
- **Hooks**：模型、工具、上下文、压缩、任务和 Embedding 的版本化扩展；保存真实结果后再执行后处理，恢复不重复已保存的底层调用。
- **资源执行**：Skill 文件夹/公开 HTTPS Git、外部 MCP HTTP/SSE、Docker 托管 Python MCP、MD/TXT/SQL 混合检索。
- **系统工具**：list/stat/glob/grep/read/write/edit/mkdir/delete/command；工作区按空间、Agent、Session 隔离，支持共享 POSIX 卷。
- **发布与恢复**：发布后运行，网页/API 共用版本；SSE 事件、取消、超时、加密检查点、原 Run 恢复和产物下载。

同会话的新提问继承已提交主实例历史；恢复则继续原 Run 的检查点。结果未知的外部动作不会无条件重放；未知模型响应重试需要 `retry_unknown_models`。SSE 当前发送执行事件和最终回答，没有模型 token 级流式输出。

## 当前限制

当前 Run 调度为**单进程**，共享 PostgreSQL/文件不等于支持集群。每空间最多并行 4 个运行，每次执行 10 分钟；每次执行/恢复默认共享 96 次聊天模型调用预算，每实例最多 64 个行动模型轮次。每 Run 最多 8 次子任务委派，委派数不因恢复重置。详细计数与恢复规则见[Runtime 设计](docs/runtime-design.md)。

Hook 仅支持可信 Python 注册，上传/Git Hook、隔离 Hook Worker 与管理页面尚未实现。长期 MemoryService 尚未实现。Skill 导入不代表所有宿主专属工具都已兼容；模型列表也不保证包含精确上下文元数据。

系统命令、Skill 脚本和托管工具需要 Docker。命令沙箱要求非 root、匹配 UID/GID、本地 Docker socket 和预加载镜像；没有宿主命令回退。仓库默认 Compose 只运行平台，不挂 Docker socket，镜像默认 root，不能直接作为当前系统命令执行环境。本地已有替身测试，Docker/NFS/gVisor 仍需实机联调。

知识库当前使用 SQL 保存原文、分块及 JSON 向量，Python 计算向量相似度；PostgreSQL 提供 GIN 关键词检索，没有 pgvector。当前文档检索不是可用路径访问的文件式 Wiki。

发布固定配置，但资源删除、外部端点变化和凭据轮换仍有边界。聊天连接更换 provider/base URL 应新建模型资源再发布，避免已发布快照的旧地址配上新 Key；详见[平台设计](docs/platform-api-data.md)。

## 开发与验证

```sh
.venv/bin/pip install -r requirements-dev.txt
AGENT_LOOM_TEST_DATA="$(mktemp -d)"
PYTHON_DOTENV_DISABLED=1 DB_BACKEND=sqlite \
  AGENT_LOOM_DATA="$AGENT_LOOM_TEST_DATA" \
  AGENT_LOOM_WORKSPACE_BACKEND=local \
  AGENT_LOOM_WORKSPACE_ROOT="$AGENT_LOOM_TEST_DATA/workspaces" \
  bash deploy/check.sh
```

默认测试强制使用临时 SQLite，不访问正式数据库表。脚本执行 Python 静态/格式检查与测试、前端格式/测试、TypeScript 校验及 Vue 构建。`--postgres` 会访问配置的 PostgreSQL 并创建临时 schema，只在明确选择数据库联调时使用。

`9941e31` 基线最近一次隔离验证记录为 **540 项 Python、12 项前端测试通过，静态/格式检查、TypeScript 和构建通过**。这不是本次文档更新重新运行的结果，也不代表真实模型、Docker、共享卷和远程数据库均已验收。历史验证记录按原日期保留。

工具 SDK：`.venv/bin/pip install -e packages/tool-sdk`，通过 `ToolServer` 声明函数并启动 stdio MCP。示例见 [托管工具](examples/tools/text-utils)、[运行时工具](examples/runtime-tools/word_count.py)、[Hooks](examples/hooks)、[Skill](examples/skills/report-helper)。

## 数据与备份

SQL 保存业务状态、事件、检查点和知识；磁盘保存 `data/encryption.key`、`data/assets`、旧 `data/runs` 与当前工作区。默认工作区是 `data/workspaces`，共享模式使用配置的卷。数据库、加密根密钥、资源包与工作区必须成套备份，不能只备份数据库。主会话工作区会被后续 Run 修改，旧 Run 的产物入口不等于不可变文件归档。

目录职责和扩展入口见[源码目录](docs/directory-plan.md)；部署前按[运维说明](docs/deployment-operations.md)核对实际环境。
