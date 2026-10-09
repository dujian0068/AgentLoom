# 织点 AgentLoom v0.2 迭代记录

> **历史记录。** 保留原阶段的能力、日期和测试数字，不作为当前实现清单。2026-10-09 核对后的现状见[总体技术设计](technical-design.md)，当前需求见[产品需求](requirements-v0.2.md)。

日期：2026-09-30

本轮目标：将集中式 MVP 拆分为可维护模块，并补齐运行失败、事件连接、模型异常与数据库升级处理。

## 目录

| 文件或目录                                    | 职责                                                          |
| --------------------------------------------- | ------------------------------------------------------------- |
| `apps/api/agentloom/app.py`                   | 应用组装、生命周期、公共中间件。                              |
| `routes/`                                     | 账号、空间、资源、模型、技能、工具、知识库、Agent、运行 API。 |
| `services/`                                   | 发布快照、运行执行、工具构建。                                |
| `migrations/`                                 | 编号 SQL 和自动升级。                                         |
| `packages/runtime/agentloom_runtime/`         | engine、planning、context、completion、provider。             |
| `apps/web/src/components/`                    | AgentEditor、PublishedAgentView。                             |
| `apps/web/src/composables/useRunWorkspace.ts` | 会话、运行、SSE、恢复。                                       |
| `apps/web/src/domain/`                        | 类型、标签、历史对话重建。                                    |

## 可靠性

- 任务初始化异常可正确失败退出；状态与结束事件原子提交；并发事件序号不冲突。
- `Last-Event-ID`/`after` 续接，前端重复事件去重、有限重连、旧连接清理。
- 历史恢复保留用户每次补充信息。
- 模型临时错误最多重试两次；401 与读取超时不重试；响应及向量校验。
- 原数据库无损采用版本迁移，后续追加迁移文件。

## 验证

- 37 项后端、5 项前端测试；Ruff 静态与格式检查，Prettier，TypeScript，Vue 生产构建。
- 浏览器使用 `work/qa` 隔离数据及替身模型验证保存、发布、Plan 和恢复。
- 正式服务保持 8766，首次账号由用户创建，QA 不注入正式环境。

## 限制

- 单机单进程；模型调用和 embedding 的真实供应商联调仍待 Key。
- Docker 未安装，容器托管工具/Skill 脚本未做实机验证。
- 私有 Git、任务队列、全面 Skill 宿主工具兼容、完整历史知识索引版本仍待后续迭代。

此前实现与限制详见 [README](../README.md) 和 [implementation-v0.1.md](implementation-v0.1.md)。

## 2026-10-02 · 事件总线与工具处理器解耦

- Loop 统一发送 `tool.execute` 请求并等待 `ToolOutcome`，去掉具体工具的分支执行。
- EventBus 实现请求/响应、关联 ID、通知订阅、取消和超时清理。
- ToolRegistry/ToolRuntime 管理工具定义、绑定权限、JSON Schema 与执行/恢复策略。
- `handlers` 分别实现工作区、Skill、MCP、RAG、计划和子 Agent；`create_engine` 统一组装。
- 保留发布快照、加密检查点与旧子 Agent 恢复状态；新增工具通过注册接口扩展。
- 新增 [examples/runtime-tools/word_count.py](../examples/runtime-tools/word_count.py)；说明见 [runtime-event-bus.md](runtime-event-bus.md)。

### 验证

- 84 项后端默认 SQLite 测试、5 项前端测试、Ruff、格式检查、TypeScript 与 Vue 构建通过。
- 包含 14 项事件总线、10 项工具运行时和 14 项 PostgreSQL 测试设施报错保护测试。
- 远程 PostgreSQL 的 Plan、子 Agent 和恢复 3 项复测在连接阶段失败：本机 15432 隧道入口未监听。
- 因此没有重启现有 8766 服务；隧道恢复后仍需完成远程复测并重启加载新后端代码。
- 没有修改数据库结构、正式数据或本地 `.env`。
