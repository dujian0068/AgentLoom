# 部署与运维

代码核对基线：`9941e31`，日期：2026-10-09。本文按仓库现有实现编写。
命令为供部署者执行的示例；编写本文没有连接数据库、读取本地 `.env` 或调整生产配置。

> **当前只支持一个活动 API 进程。不要配置多个 Uvicorn worker，也不要让多个节点同时运行同一套业务数据。**
> PostgreSQL、共享工作区和文件锁已经接入，但持久任务队列、Worker 租约、fencing 与跨节点取消尚未实现。
> `db.init()` 会把数据库中所有 `queued/running` 任务标记为中断或失败；第二个实例启动会影响第一个实例的任务。

## 1. 部署对象与现状

| 对象                 | 当前部署位置与职责                                                |
| -------------------- | ----------------------------------------------------------------- |
| Vue 3 / TypeScript   | `apps/web`；构建为 `apps/web/dist`，由 FastAPI 提供静态页面       |
| Python / FastAPI     | `apps/api`；管理接口、鉴权、发布版本、运行 API、SSE 和任务调度    |
| AgentRuntime         | `packages/runtime`；在 API 进程内运行 Loop、Hooks、模型与工具调用 |
| PostgreSQL 或 SQLite | 保存业务记录、会话、加密检查点、知识索引与工作区绑定              |
| 平台数据目录         | `AGENT_LOOM_DATA`；密钥、导入资源及旧任务文件                     |
| 会话工作区           | 本机 POSIX 目录或已挂载的共享 POSIX 卷；保存任务文件              |
| Docker daemon        | 系统命令沙箱和托管 MCP 工具的执行依赖，各有独立调用路径           |

`TASKS`、`BUILDS`、登录尝试记录和待补写结束状态均在进程内存中。
数据库事务锁能保护部分记录的并发写入，不能替代运行进程的归属管理。
共享文件也不会自动分发 Skill、工具镜像、Hook Python 代码或平台密钥。

## 2. 开发环境与首次启动

从项目根目录操作。当前 Dockerfile 使用 Python 3.12；本地文档要求 Python 3.11+、Node.js 22.12+。
Python 依赖固定在 `requirements.lock.txt`，前端安装使用 `apps/web/package-lock.json`。
Git 导入功能另需可执行的 `git`；仅运行前端和远程 MCP 不需要本机 Docker。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
npm --prefix apps/web ci
npm --prefix apps/web run build
./deploy/run.sh
```

默认页面为 `http://127.0.0.1:8766`。
首次访问由使用者创建空间管理员，没有预设管理员密码或供应商 Key。
在模型、Skill、工具及知识库准备完成后配置 Agent，发布后再通过页面或 API 运行。

`deploy/run.sh` 的行为需要留意：

- 只有 `.venv/bin/python` 不存在时才安装 Python 依赖。
- 只有 `apps/web/dist` 不存在时才安装并构建前端。
- 修改依赖或前端代码后，应显式重新安装或构建，不能依靠脚本自动更新旧产物。
- `AGENT_LOOM_HOST`、`AGENT_LOOM_PORT` 由 shell 读取，默认分别为 `127.0.0.1`、`8766`。
- 脚本不 `source .env`；把上述启动地址变量仅写进 `.env` 不会改变其 shell 参数。

Vue 开发模式使用另一个终端：

```sh
npm --prefix apps/web run dev
```

Vite 默认监听 `127.0.0.1:5173`，将 `/api` 代理到 `http://127.0.0.1:8766`。
后端端口变更时，需要相应调整开发代理。
后端默认允许的开发 Origin 为 `http://127.0.0.1:5173`；使用其他地址时设置 `AGENT_LOOM_DEV_ORIGIN`。
开发热更新与生产进程分开使用，不要用开发重载进程承载长任务。

## 3. PostgreSQL 与 SSH 隧道

首次配置参考根目录 `.env.example`，已有 `.env` 时不要覆盖整份文件。
以下只展示字段，密码应由部署者在本地填写：

```dotenv
DB_BACKEND=postgresql
DB_HOST=127.0.0.1
DB_PORT=15432
DB_NAME=agentloom
DB_USER=agentloom
DB_PASSWORD=
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT=5
DB_SCHEMA=public
```

Python 通过 `python-dotenv` 读取项目根目录 `.env`，系统环境变量优先，密码不做变量插值。
修改配置后重启后端。不要把密码写入命令行参数、Git、截图或诊断工单。

`127.0.0.1:15432` 指运行 Python 的机器上的隧道入口。
部署者已有 SSH 配置时，示例转发形式如下；`db-tunnel` 是需要自行配置的 SSH 别名：

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:15432:127.0.0.1:5432 db-tunnel
```

转发目标 `127.0.0.1:5432` 从 SSH 服务端所在主机解释，需按真实拓扑调整。
AgentLoom 不创建、不守护、不自动重连 SSH 隧道；后端运行期间需保持连接。
在容器内运行时，容器自己的 `127.0.0.1` 不会指向宿主机隧道。
不要为解决地址问题直接把数据库或隧道暴露到公网。

配置约束与默认值：

| 字段或行为              | 当前实现                                                                  |
| ----------------------- | ------------------------------------------------------------------------- |
| 后端选择                | 显式 `DB_BACKEND` 优先；否则有 `DB_HOST` 用 PostgreSQL，没有则用 SQLite   |
| PostgreSQL 必填项       | `DB_HOST`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`                            |
| `DB_PORT`               | 默认 5432，范围 1–65535；示例 SSH 入口显式使用 15432                      |
| `DB_CONNECT_TIMEOUT`    | 默认 5 秒，范围 1–60 秒                                                   |
| `DB_SSLMODE`            | 默认 `prefer`，应按真实数据库 TLS 要求配置                                |
| `DB_SCHEMA`             | 默认 `public`；自定义 schema 必须事先存在，名称使用小写字母、数字、下划线 |
| 连接池                  | 最小 1、最大 8；等待连接 10 秒，最多 32 个等待者                          |
| PostgreSQL 语句与锁等待 | `statement_timeout=30000`、`lock_timeout=30000` 毫秒                      |

显式选择 PostgreSQL 后，缺配置或断连会报错，不回退到 SQLite。
连接池会淘汰失效连接，但不自动重放结果未知的事务写入。
同步数据库访问仍存在，远程隧道延迟会影响接口及任务响应。

部署者需要检查连接时可手动执行：

```sh
.venv/bin/python deploy/check_db.py
```

该脚本会读取应用数据库配置并实际连接，查询数据库、用户、schema 和版本；不执行业务迁移。
它不是离线检查，不应在普通文档校验或默认测试中顺带执行。
数据库层面为只读查询，但导入存储模块仍可能创建配置的数据目录。

## 4. 单进程生产启动

当前较直接的部署路径是：Linux 服务账号运行 Python，预构建 Vue，由本机反向代理终止 HTTPS。
为平台数据、会话工作区和资源文件准备稳定的路径与权限；系统命令功能要求非 root 服务账号。
只保留一个活动服务实例；升级采用停止旧实例、备份、更新、启动新实例的顺序。

```sh
npm --prefix apps/web ci
npm --prefix apps/web run build
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/uvicorn agentloom.app:app --app-dir apps/api \
  --host 127.0.0.1 --port 8766 --workers 1
```

使用服务管理器时，工作目录设为项目根目录，启动命令使用虚拟环境中的绝对路径。
环境变量、SSH 隧道和文件系统挂载必须在应用启动前准备好。
进程退出需要给取消及沙箱清理留出时间；强制终止后检查持久执行标记，再恢复任务。
不要使用“先启动新实例、再停旧实例”的滚动升级方式连接当前同一业务库。

反向代理需支持 SSE，保留事件流并合理设置缓冲和超时。
使用 HTTPS 时将 `AGENT_LOOM_SECURE_COOKIE=1`，避免沿用本地 HTTP 示例的 Cookie 配置。
健康检查入口是 `/api/health`；接口可用不能证明模型、MCP、共享卷或 Docker 全部可用。

可信 Hook 通过部署启动代码显式注册，注册代码和依赖版本需要与已发布 manifest 对应。
改动 Hook 代码、版本或配置后，旧发布版本可能拒绝运行；保留原实现或重新发布 Agent。
不要把用户上传的 Hook 文件直接导入 API 进程。详见 [Hooks 使用说明](hooks-runtime-v0.1.md)。

## 5. 现有 Dockerfile 与 Compose 的边界

`deploy/Dockerfile` 先用 Node 构建前端，再用 Python 3.12 镜像启动 Uvicorn。
`deploy/compose.yaml` 只将 `127.0.0.1:8766` 映射到容器，并把 `../data` 挂载到 `/app/data`。
这是基础 Web/API 容器示例，当前配置存在以下限制：

| 项目         | 仓库文件中的实际情况                                                                 |
| ------------ | ------------------------------------------------------------------------------------ |
| 服务用户     | Dockerfile 没有 `USER`，默认 root；系统命令沙箱会拒绝 root 服务进程                  |
| Docker 访问  | 没有安装 Docker CLI，也没有挂载 Docker Unix socket                                   |
| Git 导入     | Dockerfile 没有显式安装 Git CLI                                                      |
| 数据库环境   | Compose 没有 `env_file`，也没有传入 `DB_*`；宿主项目 `.env` 不会自动成为容器应用配置 |
| 共享工作区   | 没有配置 shared_posix 卷、卷标记或一致的宿主路径                                     |
| HTTPS Cookie | 示例固定 `AGENT_LOOM_SECURE_COOKIE=0`，需要部署覆盖                                  |
| SSH 隧道     | 没有隧道服务，也没有解决容器与宿主机回环地址差异                                     |

因此，直接执行现有 Compose 不能据此宣布 PostgreSQL、Git 导入、托管工具或系统命令沙箱已就绪。
在未注入数据库配置的情况下，应用会使用挂载数据目录内的 SQLite。

如坚持容器化 API，需要另行构建部署变体：安装必要 CLI、使用非 root UID/GID、注入数据库环境，
并让 API 传给 Docker 的绝对宿主路径在 daemon 所在主机真实存在。
容器中 `/app/data/...` 与宿主机 `.../data/...` 仅内容对应并不够；Docker bind mount 使用 daemon 看到的路径。
可选择在 API 容器内外使用一致的挂载绝对路径，但必须逐项验证会话目录与 Skill 目录。
Docker socket 是部署控制权限，不应通过平台配置交给模型，也不挂入任务容器。
这些部署变体尚未包含在当前 Compose 中，本文不宣称已完成实机验证。

## 6. 会话工作区与共享 POSIX 存储

默认配置：

```dotenv
AGENT_LOOM_WORKSPACE_BACKEND=local
# 默认根目录为 AGENT_LOOM_DATA/workspaces
# AGENT_LOOM_WORKSPACE_ROOT=/absolute/local/workspaces
```

新任务的工作区绑定保存在 `run_workspaces` 中，主工作区按空间、Agent、会话连续使用：

```text
<workspace-root>/
  .agentloom-volume                         # shared_posix 卷标记
  .agentloom-locks/<session-scope-hash>/     # 锁与持久执行/隔离标记
  spaces/<space>/agents/<agent>/sessions/<session>/
    main/                                  # 同一会话不同提问共享
    runs/<run>/children/<instance>/         # 每次运行的子 Agent 隔离目录
```

子 Agent 不会挂载主工作区或其他子 Agent 的目录。
没有 `run_workspaces` 绑定的旧运行仍解析到 `AGENT_LOOM_DATA/runs/<run>`，不会自动搬迁。
产物下载读取当前工作区文件，不是不可变历史归档；同会话后续任务可能修改主目录文件。

共享卷示例：

```dotenv
AGENT_LOOM_WORKSPACE_BACKEND=shared_posix
AGENT_LOOM_WORKSPACE_ROOT=/mnt/agentloom-workspaces
AGENT_LOOM_WORKSPACE_VOLUME_ID=agentloom-team-volume-v1
```

先由管理员挂载 NFS/NAS，再确认真实挂载来源，然后创建卷根下的 `.agentloom-volume` 普通文本文件。
文件内容为固定卷标识；不能有符号链接或额外硬链接，大小不超过 1024 字节。
应用不创建缺失共享根或卷标记，校验失败时不回落到本地空目录。
卷标记只确认配置身份，不证明路径确实是 NFS，也不是存储真实性认证。

同一会话绑定原 backend 和 volume_id，不能通过修改配置无声切换存储卷。
local 模式的卷身份包含绝对根路径哈希，随意更改本机路径会使原绑定无法解析。
shared_posix 允许在相同卷身份下使用不同节点挂载路径，但仍不提供跨节点运行调度。

写入和系统命令使用 `flock` advisory lock；锁目录在沙箱挂载之外。
所有写入者必须遵循相同锁规则，外部编辑器或其他程序直接改文件不会被 advisory lock 自动阻止。
参与挂载的服务身份要有一致 UID/GID 和目录权限，不能用全局可写权限替代权限规划。
共享的是会话文件卷，不要把多个 Docker daemon 的内部数据目录当作本项目工作区共享。

默认只读检查，不读取 `.env`，不连接数据库：

```sh
.venv/bin/python deploy/check_workspace.py \
  --backend shared_posix --root /mnt/agentloom-workspaces \
  --volume-id agentloom-team-volume-v1
```

显式执行本机读写、原子替换和独立进程锁探测：

```sh
.venv/bin/python deploy/check_workspace.py \
  --backend shared_posix --root /mnt/agentloom-workspaces \
  --volume-id agentloom-team-volume-v1 --probe
```

`--probe` 只使用随机 `.agentloom-probe-*` 目录，清理已知探测文件；遇到未知内容会保留现场。
输出 `scope=single_node`、`advisory_lock_local=true` 只代表本机检查结果。
真实跨节点文件可见性、锁互斥、原子替换和容器挂载，需要独立的存储联调；当前平台仍不能多实例运行。
NFS 底层 I/O 阻塞时，Python 取消不能强制结束内核调用，应用预算不是严格的存储 I/O 截止时间。

## 7. 系统命令沙箱与隔离恢复

系统文件工具通过 `WorkspaceStorage` 操作目录；执行 shell 命令使用一次性 Docker 容器。
`workspace_command` 和 Skill 脚本的命令执行不会在 Docker 缺失时回退到宿主机 shell。

```dotenv
AGENT_LOOM_SANDBOX_IMAGE=python:3.12-slim
# 可选，要求管理员已在 Docker 中安装并配置 runsc
# AGENT_LOOM_SANDBOX_RUNTIME=runsc
# 默认本机 Unix socket；不支持远程 TCP Docker daemon
# AGENT_LOOM_DOCKER_HOST=unix:///var/run/docker.sock
```

服务进程必须以非 root UID/GID 运行，任务容器使用相同 UID/GID。
预先准备可信镜像；执行时带 `--pull never`，不自动拉取缺失镜像。
若 Skill 需要特定依赖，应事先制作包含依赖的镜像；命令沙箱无网络，不能依靠运行时联网安装。
gVisor 是可选 runtime 配置，不会被项目自动安装，真实系统调用兼容性仍需验证。

当前系统命令沙箱约束：

- 只挂载当前实例目录到 `/workspace`，授权 Skill 目录以只读方式挂到 `/skills/<id>`。
- 不挂入平台数据库、密钥、共享根、其他会话目录或 Docker socket。
- 关闭网络，容器根文件系统只读，使用 32 MiB 可写 `/tmp`。
- 内存 256 MiB、CPU 1、进程数 64，移除 capabilities，启用 `no-new-privileges`。
- 固定工作目录 `/workspace`，命令使用 `umask 077`。
- 默认 60 秒，可选 1–120 秒；累计输出超过 256 KiB 停止，结果仅保留最后 12000 字符。

命令启动前，锁目录写入并同步 `<lock-hash>.lock.quarantine` 持久标记。
正常结束后确认容器已清理才删除标记；进程崩溃、启动结果未知或清理失败会保留标记。
锁释放不等于容器已停止，标记没有自动过期时间，重启服务也不会自动解除隔离。
后续写入和命令执行会被阻止；保留读取能力不表示允许继续执行。

管理员恢复流程：

1. 暂停对应会话的新任务与恢复请求，保留标记、运行记录和日志。
2. 找到标记中的 `container_name`，在原执行主机的 Docker daemon 核对容器。
3. 确认旧 Worker、Docker CLI、未完成启动请求都不会再创建执行者；处理仍存在的容器。
4. 检查命令留下的文件及业务副作用，确定任务下一步，不盲目重跑原命令。
5. 在任务仍停用、锁协调完成后，仅解除该作用域的确切隔离标记，再恢复任务。

标记不完整或原执行主机无法确认时应继续保持隔离；单次 `No such container` 不能证明未知启动请求不会稍后生效。
不要删除锁文件、整棵锁目录或其他会话的标记。当前没有模型可调用的解除隔离工具，也没有自动恢复管理页面。
详情见 [系统工具与共享工作区](system-tools-shared-workspace-v0.1.md)。

## 8. 发布资源、Skill 与 MCP 分发

Agent 发布快照固定配置、资源引用、Hook 链等运行契约，运行恢复沿用原发布版本。
会话共享卷只解决任务文件位置，不自动迁移下面这些执行依赖：

| 资源      | 运维需要保留或分发的内容                                                   |
| --------- | -------------------------------------------------------------------------- |
| 模型连接  | 数据库元信息及对应解密密钥；供应商可用性、模型权限另行验证                 |
| Skill     | `data/assets` 中完整目录及快照引用的路径；沙箱 daemon 也须看见同一宿主路径 |
| 外部 MCP  | endpoint、凭据、可达性与已发布工具契约；远程服务自身单独运维               |
| 托管 MCP  | 上传源码、依赖和构建镜像；镜像位于 Docker daemon，不在共享工作区           |
| 可信 Hook | 部署注册代码、依赖及固定版本/哈希；不可替换为上传代码热导入                |
| 知识库    | 文档、分块、索引签名、Embedding 操作与导入状态；索引升级走重建流程         |

托管 MCP 使用独立的 `mcp_tools.py` 构建及 stdio 容器流程。
它不是系统命令沙箱：没有会话挂载和相同的 quarantine 管理，也不使用 `AGENT_LOOM_DOCKER_HOST` 这一专用选择参数。
托管工具构建会运行依赖安装，默认限时 180 秒；构建阶段与运行阶段的网络需求不同。
不要把系统命令沙箱的清理、UID 或 `--pull never` 保证直接套用到托管 MCP。

## 9. 配置默认值与运行限制

| 项目               | 当前默认值或限制                                                | 配置位置                                  |
| ------------------ | --------------------------------------------------------------- | ----------------------------------------- |
| 平台数据目录       | 项目根 `data/`                                                  | `AGENT_LOOM_DATA`                         |
| 单空间活动任务     | 最多 4 个；同会话不并发运行                                     | API 当前固定限制                          |
| 单次运行/恢复执行  | 600 秒                                                          | 运行服务当前固定限制                      |
| Loop 预算          | 本次执行共 96 次模型调用，每实例 64 次循环迭代                  | Runtime `ExecutionLimits`，非现有环境变量 |
| 子任务实例         | 单次运行最多 8 个，不递归委派                                   | Runtime 当前限制                          |
| 模型 HTTP 请求     | 90 秒；有限重试连接/特定响应，读取超时不盲重试                  | Provider 适配器                           |
| 模型未知窗口回退值 | 32768；输出预留 4096，安全余量 1024                             | 模型配置；不是供应商真实上限声明          |
| 主动压缩           | 有效输入预算 80% 触发，目标 60%                                 | Agent `context_policy`                    |
| 压缩附加触发       | 用户轮数、模型行动数默认关闭；每次压缩操作默认最多 4 次摘要调用 | Agent `context_policy`                    |
| 文件工具单文件     | 最大 2 MiB；路径深度最多 32                                     | POSIX 存储当前常量                        |
| 搜索扫描           | 最多 1000 文件、20 MiB；目录遍历最多 5000 条目                  | POSIX 存储当前常量                        |
| 写锁等待           | 5 秒                                                            | POSIX 存储当前常量                        |
| 资源上传包         | 最多 500 文件、总计 30 MiB                                      | 资源上传服务                              |

有效输入预算为窗口减去输出预留和安全余量，压缩阈值作用于该预算。
当前默认估算器基于 UTF-8 JSON 字节和额外开销，不等于供应商 tokenizer 精确计数。
压缩和完成检查计入模型调用预算；恢复重置本次预算，累计调用记录仍保留。
旧发布版本缺少新策略时仍可能使用字符压缩策略；修改草稿不会改变已发布版本。
文件/沙箱限制多数是代码常量或 SDK 配置，不要创造未实现的 `.env` 开关。

## 10. 数据迁移、备份与还原

启动时在事务中执行尚未应用的编号迁移，版本记录在 `schema_migrations`。
SQLite 与 PostgreSQL 各有对应 SQL，数据库账号需具备目标 schema 建表、建索引及读写权限。
当前文件版本：

| 编号 | 主要内容                                                        |
| ---- | --------------------------------------------------------------- |
| 001  | 账号、空间、资源、Agent、发布版本、文档、分块、会话、运行与事件 |
| 002  | 检查点及查询索引                                                |
| 003  | 连续会话原始消息和模型可见视图                                  |
| 004  | Embedding 操作及知识文件导入的持久记录                          |
| 005  | 新运行的会话工作区绑定                                          |

切换数据库不搬迁 SQLite 数据，迁移 `005` 也不移动旧任务文件。
当前没有自动降级迁移；升级失败后先保留现场，使用匹配备份恢复，不能只回滚代码猜测兼容性。

备份需要覆盖同一批次的数据库与文件：

| 对象                             | 还原要求                                                            |
| -------------------------------- | ------------------------------------------------------------------- |
| PostgreSQL 数据库/schema         | 包括迁移记录、发布快照、运行、上下文、知识索引与 `run_workspaces`   |
| SQLite 数据库                    | 停止写入后备份或使用一致性备份方式；不能只复制活跃 WAL 模式的主文件 |
| `AGENT_LOOM_DATA/encryption.key` | 解密模型 Key、检查点及会话记录所需；保留 `0600`，不可重新生成替代   |
| `AGENT_LOOM_DATA/assets/`        | Skill、托管工具源码及相关附件，保留原路径关系                       |
| `AGENT_LOOM_DATA/runs/`          | 尚未迁移的旧运行文件与产物                                          |
| local/shared_posix 工作区根      | 新会话文件、子任务文件、卷标记、锁目录及持久执行/隔离标记           |
| 部署配置与代码                   | `.env` 的受控副本、相应代码版本、可信 Hook 注册及依赖               |
| Docker 镜像                      | 系统沙箱可信镜像、托管 MCP 构建镜像或可重现的构建材料               |

备份前停止新任务并让现有任务及文件写入收敛；数据库快照与文件快照不能各自随意取不同时间点。
对于遗留容器，先核对实际状态，不能为得到“干净备份”直接抹掉隔离标记。
仅恢复 PostgreSQL 不会恢复本机资源文件；丢失 `encryption.key` 时，原加密数据无法用新密钥读取。

还原时先保持应用停止，恢复匹配数据库、密钥、资源和工作区，再核对卷身份与文件权限。
shared_posix 保留原 volume_id；local 模式需考虑绝对路径构成卷身份，旧资源还可能保存绝对文件路径。
只启动一个实例验证迁移版本、登录、发布读取、文件读取及选定测试任务。
恢复未知模型请求前确认是否允许再次计费；恢复命令前确认旧容器与文件副作用已处理。

## 11. 故障排查与日志

| 现象                         | 先核对什么                                                        |
| ---------------------------- | ----------------------------------------------------------------- |
| PostgreSQL 连接失败          | 隧道进程、Python 所在主机/容器地址、必填环境变量、schema 权限     |
| 数据库恢复后任务仍失败       | 失败写入不会自动重放；检查原运行检查点和可恢复状态                |
| 服务重启后任务中断           | 当前启动恢复规则的预期行为；不要启动第二个实例试图接管            |
| 共享卷不可用/标识不匹配      | 实际挂载、普通卷标记内容、volume_id、路径权限；不创建本地替代目录 |
| 写锁超时                     | 同作用域写入者、未结束命令、锁目录及存储状态；不要删除锁文件解锁  |
| 工作区执行状态未知           | 按 quarantine 流程核对原容器及未完成启动请求                      |
| Docker 缺失或 root 拒绝      | 服务身份、Docker CLI、本机 socket、预拉镜像与 UID/GID             |
| 托管工具 environment_missing | 检查托管 MCP 自身构建环境；共享工作区配置不会解决镜像构建         |
| Hook/模块版本不兼容          | 对照发布 manifest 和检查点恢复原代码/配置，不替换旧链强行恢复     |
| 模型结果未知，恢复返回冲突   | 先核对供应商状态；明确授权后才使用 `retry_unknown_models`         |
| 前端仍是旧页面               | 显式重新构建 `apps/web/dist`，检查代理与静态文件版本              |
| SSE 无更新                   | 代理缓冲/超时、后端任务状态、是否误启动多进程、事件序号           |

Uvicorn 输出、工具构建日志、事件 payload、模型/工具返回及工作区文件都可能包含业务敏感内容。
当前对正在使用的部分凭据做脱敏，不等于所有日志字段都经过内容级脱敏。
账号密码哈希、供应商密钥加密和检查点加密不代表数据库中的所有业务字段都是密文。
运维日志中保留 run/session/operation ID、阶段、错误类型即可完成多数定位；避免复制完整上下文或 `.env`。
备份、日志导出、数据库查询结果和共享文件按业务数据控制访问与保留时间。

## 12. 测试隔离与验证范围

默认测试选择临时 SQLite；为避免导入阶段创建真实数据目录或读取本地配置，使用独立临时根：

```sh
.venv/bin/pip install -r requirements-dev.txt
AGENT_LOOM_TEST_DATA="$(mktemp -d)"
PYTHON_DOTENV_DISABLED=1 DB_BACKEND=sqlite \
  AGENT_LOOM_DATA="$AGENT_LOOM_TEST_DATA" \
  AGENT_LOOM_WORKSPACE_BACKEND=local \
  AGENT_LOOM_WORKSPACE_ROOT="$AGENT_LOOM_TEST_DATA/workspaces" \
  bash deploy/check.sh
```

此入口包括 Python lint/格式/测试、Vue 格式/测试、类型检查和生产构建。
MCP 集成替身需要绑定本机测试端口；这与访问真实供应商或业务数据库不同。
检查结束后由执行者确认临时目录内容，再按测试数据保留策略清理。
普通回归不要添加 `--postgres`：该选项会使用实际数据库配置创建和删除测试 schema，需要另行授权和准备。

基线 `9941e31` 对应的历史验证记录为 **540 项 Python 测试、12 项 Vue 测试通过**。
这是截至 2026-10-09 的已有记录，不是本次编写文档重新执行的结果。
真实远程 NFS 跨节点、真实 Docker/gVisor 与目标服务器容器部署仍需实机验证。
自动化替身测试、本机 `--probe` 或静态配置审查，均不能替代这些联调，也不能证明集群部署已支持。
