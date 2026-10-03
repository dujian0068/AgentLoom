# 织点 · AgentLoom — PostgreSQL 使用说明

## 连接方式

当前连接入口为 `DB_HOST=127.0.0.1`、`DB_PORT=15432`，数据库名和用户名均为 `agentloom`。

这个地址是 Python 后端所在本机的 SSH 本地端口转发入口。SSH 会将请求送到远程服务器上的 PostgreSQL；数据库本身不需要运行在本机。

已有 SSH 隧道保持连接即可，后端不会替你创建、重连或管理 SSH 隧道。SSH 断开后，数据库查询会失败；恢复隧道后，连接池会丢弃已失效的连接并在后续请求中重新建立连接，不会自动重放失败的写入。

## 配置

在项目根目录的 `.env` 中配置：

```dotenv
DB_BACKEND=postgresql
DB_HOST=127.0.0.1
DB_PORT=15432
DB_NAME=agentloom
DB_USER=agentloom
DB_PASSWORD=
```

`DB_PASSWORD` 必须由你在本地填入实际数据库密码，不需要发送到聊天。首次配置可参考 `.env.example`；已有 `.env` 时不要覆盖整个文件。该文件已加入 Git 忽略项，应仅允许需要运行平台的账号读取。

Python 启动时从项目根目录加载 `.env`，不覆盖已存在的系统环境变量；密码中的美元符号等字符不做变量插值。修改数据库配置后重启后端。

可选配置：

| 配置                   | 说明                                            |
| ---------------------- | ----------------------------------------------- |
| `DB_SSLMODE=prefer`    | PostgreSQL TLS 模式；按实际数据库要求设置。     |
| `DB_CONNECT_TIMEOUT=5` | 连接建立超时秒数，范围 1 到 60。                |
| `DB_SCHEMA=public`     | 业务表所在的 schema；自定义 schema 需预先存在。 |

显式选择 `postgresql` 后，缺少密码或连接错误都会报错，不会悄悄切换 SQLite。

离线开发时可显式设置 `DB_BACKEND=sqlite`。未设置 `DB_BACKEND` 但设置 `DB_HOST` 时，默认选择 PostgreSQL；两者均未设置时，默认选择 SQLite。

## 启动与迁移

可先执行以下命令做只读连接检查，不打印密码、不建表：

```sh
.venv/bin/python deploy/check_db.py
```

安装 `requirements.lock.txt` 后，仍从项目根目录执行：

```sh
./deploy/run.sh
```

默认页面为 [http://localhost:8766/](http://localhost:8766/)。

PostgreSQL 迁移位于 `apps/api/agentloom/migrations/postgresql/`，启动时根据 `schema_migrations` 表按编号执行，迁移在事务中提交。数据库账号需具有目标 schema 的建表、建索引和读写权限。

切换数据库仅改变平台的存储后端，不会自动把原有 SQLite 账号、Agent、发布版本和运行记录导入 PostgreSQL。原 `data/agentloom.db` 会保留；如需导入，须另外执行经过校验的数据迁移。

在 PyCharm 中运行时，Python 后端会自行读取项目 `.env`；如果 Run Configuration 中已填 `DB_*` 变量，其值优先于 `.env`。隧道也必须运行在该 Python 进程所在的机器上。

以后把应用放到服务器或容器时，需要重新确认 `DB_HOST` 和 `DB_PORT`。`127.0.0.1` 总是指应用所在主机或容器自身，不会自动指向开发电脑当前的隧道。

## 检索实现

知识库支持 Markdown、TXT、SQL 文本。中文按现有二元词逻辑处理，英文和 SQL 标识符生成检索词。

PostgreSQL 使用普通 `chunk_fts` 表与基于 `to_tsvector('simple', terms)` 的 GIN 索引，查询用参数化 `to_tsquery` 和 `ts_rank_cd` 排序。不同查询词采用 OR；SQL 标识符的下划线按相邻词处理，保留标识符查询语义。查询仍按空间、允许访问的文档集合和删除状态过滤。

无需安装 pgvector 扩展。Embedding 向量仍保存为 TEXT 字段中的 JSON，Python 计算相似度并与关键词结果融合。知识库较大时，需要另行规划数据库端向量检索和异步索引任务。

并发上传会按知识库加事务锁，读取最新 revision 后递增，文档、分块、检索词和资源状态一起提交；外部向量化请求在事务外完成。

## 运行限制

- 当前仍是单进程平台，启动时不要添加多个 Uvicorn worker。PostgreSQL 事务锁保护发布版本、任务事件序号、知识库版本等并发写入，但应用的执行任务句柄、取消和服务重启恢复仍依赖单进程状态，尚未提供分布式队列。
- SSH 或数据库短暂故障会产生明确错误。连接池不会自动重试事务写入，避免在写入是否成功未知时重复创建记录。任务结束状态是特例：后台保留待收尾结果，连接恢复后幂等提交状态和结束事件；不会重新执行模型或工具。进程重启后则使用原有检查点恢复机制。
- 当前仍有同步数据库调用，跨地域 SSH 隧道的延迟会影响接口和任务响应；建议正式部署时让应用靠近数据库。

## 备份与恢复

使用 PostgreSQL 后，需要同时备份远程数据库和本机数据目录：

| 备份对象              | 内容或要求                                                                            |
| --------------------- | ------------------------------------------------------------------------------------- |
| 远程数据库            | 账号、空间、Agent、资源元数据、发布版本、文档文本、索引、运行记录、事件与加密检查点。 |
| `data/encryption.key` | 解密供应商 Key 和检查点必需的本机密钥，权限保持 `0600`。                              |
| `data/assets/`        | 上传的 Skill、工具和相关文件。                                                        |
| `data/runs/`          | 任务工作区与产物。                                                                    |

上传文件和解密材料不会因接入 PostgreSQL 自动复制到远程服务器。丢失 `encryption.key` 后，仅恢复数据库也不能解密原有 Key 和检查点；仅恢复数据库也不能还原任务工作区。应保留数据库与文件相互对应的备份批次。

仍使用 SQLite 时，额外备份 `data/agentloom.db`。自定义 `AGENT_LOOM_DATA` 时，以上文件位于你设置的数据目录中。

## 测试隔离

默认执行以下命令时，会强制选择临时 SQLite 数据库，不因 `.env` 中的 PostgreSQL 配置访问正式业务表：

```sh
.venv/bin/pytest -q
```

需要真实数据库验证时执行：

```sh
.venv/bin/pytest -q --postgres
```

测试沿用 `.env` 的数据库连接，为各集成测试创建名称随机的独立 schema；正常结束或测试失败后自动删除该 schema。测试账号、模型配置和文档只进入临时 schema，文件与密钥使用临时目录。数据库账号需要创建 schema 的权限。

测试进程被强制终止或隧道在清理阶段中断时，自动清理可能无法完成，可按测试日志核对遗留测试 schema 后再处理。

完整检查入口为 `./deploy/check.sh`；附加 `--postgres` 可同时执行 PostgreSQL 集成测试和前端检查。

测试中的模型和 Embedding 使用替身，不产生真实模型供应商费用；真实 PostgreSQL 与 MCP 通信仍会执行。

## 本轮验证结果

正式服务 `/api/health` 返回 `database=postgresql`，`schema_migrations` 为 1、2，`public` 中已创建 15 张表。原本地 SQLite 账号、Agent、资源和任务数量均为 0，因此本次无需业务数据搬迁，原数据库文件保留。

46 项默认后端回归、5 项前端测试、TypeScript 与生产构建通过。13 项新增及核心 PostgreSQL 验证通过，覆盖参数绑定、事务回滚、时间精度、并发发布、并发文档版本、缺失配置、断线错误、收尾重试、结果不明的提交、中文/SQL 检索、混合检索、Plan 与任务恢复。模型响应为替身；数据库为真实远程 PostgreSQL。

补充：原有 37 项回归在 PostgreSQL 模式下也全部通过；专项 13 项另行通过，其中包含重复验证的核心流程。临时测试 schema 已自动清理。
