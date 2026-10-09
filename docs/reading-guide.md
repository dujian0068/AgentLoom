# 阅读指南

按你现在要解决的问题选择入口。标为“当前实现”的文档以实际代码为依据；目标设计和历史记录放在后面，避免把规划中的能力当成已经交付。

如果第一次接触项目，先读[总体技术设计](technical-design.md)，再进入 Runtime、平台接口或部署方向。[文档目录](README.md)保留全部资料和架构图。

<a id="architecture"></a>

## 了解架构

| 想了解的问题                                              | 从这里开始                                  |
| --------------------------------------------------------- | ------------------------------------------- |
| 平台要解决什么问题，哪些需求已实现？                      | [产品需求与实现状态](requirements-v0.2.md)  |
| 管理端、Agent Runtime、工具和存储如何分工？               | [总体技术设计](technical-design.md)         |
| Loop 怎样执行，Plan / ReAct 有何差别，子 Agent 如何协作？ | [Runtime 详细设计](runtime-design.md)       |
| 一次会话中的多轮输入、上下文压缩和中断恢复如何关联？      | [上下文与会话记录](runtime-context-v0.2.md) |
| 每个模块实际对应哪些源码？                                | [源码目录与依赖](directory-plan.md)         |

建议顺序：总体技术设计 → Runtime 详细设计 → 源码目录。先掌握边界，再查具体接口。

<a id="extensions"></a>

## 开发与扩展

| 想做的事情                                           | 对应文档                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------- |
| 替换模型网关、执行策略、完成判断或上下文模块         | [Runtime 详细设计](runtime-design.md)                               |
| 注册原生工具，理解事件请求与观察通知                 | [运行时事件总线](runtime-event-bus.md)                              |
| 在模型或工具调用前后读取数据、修改允许字段、阻止执行 | [Hooks 运行时使用说明](hooks-runtime-v0.1.md)                       |
| 调整上下文预算、80% 压缩阈值与会话历史继承           | [上下文与会话记录](runtime-context-v0.2.md)                         |
| 接入读写文件、list / grep 等系统工具和共享工作区     | [系统工具、共享工作区与沙箱](system-tools-shared-workspace-v0.1.md) |
| 扩展文本向量化、知识库索引与查询处理                 | [Embedding Hooks](embedding-hooks-v0.1.md)                          |
| 导入 Skill，接入外部 MCP 或托管工具                  | [平台资源与接口](platform-api-data.md)                              |

扩展前先确认当前契约和恢复要求。已有 Hook SDK 面向部署时注册的可信 Python 代码；上传 / Git 导入 Hook 与隔离 Worker 属于后续目标。

<a id="deployment"></a>

## 部署与排障

从[部署与运维](deployment-operations.md)开始，按开发启动、生产启动、共享存储、备份和故障排查逐项阅读。

- PostgreSQL、SSH 隧道与迁移细节：[数据库使用说明](database-postgresql.md)。文末首次接入验证属于历史记录。
- 文件写在哪里、会话怎样隔离、沙箱异常如何处理：[系统工具与共享工作区](system-tools-shared-workspace-v0.1.md)。
- 任务为什么不能直接重跑、检查点怎样恢复：[Runtime 详细设计](runtime-design.md)。

当前运行调度要求一个活动 API 进程。已接入 PostgreSQL 和共享工作区，并不代表已实现多节点任务调度。

<a id="api"></a>

## API 与页面接入

主要入口是[管理平台、运行 API 与数据模型](platform-api-data.md)，包含鉴权、空间资源、发布版本、Session / Run、REST 索引、SSE 和错误约定。

- 配置 Agent 后先发布，再通过页面或 API 使用：[产品需求](requirements-v0.2.md)。
- 创建任务、接收执行事件、取消和恢复：[平台 API](platform-api-data.md)。
- 理解恢复后继续执行的边界：[Runtime 详细设计](runtime-design.md)。
- 定位 Vue 页面、请求客户端和后端路由：[源码目录](directory-plan.md)。

服务启动后的交互式接口文档位于 `/api/docs`，OpenAPI 位于 `/api/openapi.json`。这两个地址属于业务服务，不是文档站自身的路由。

## 术语与检索

| 中文说法                         | 文档中的常用名称                     |
| -------------------------------- | ------------------------------------ |
| 运行时、模型循环、自动计划       | Runtime、Loop、ReAct、Plan           |
| 会话、一轮提问、执行实例         | Session、Run、instance               |
| 上下文、历史、摘要、压缩         | ContextManager、CompactionPolicy     |
| 钩子、前置 / 后置处理            | Hook、Hooks、HookManager             |
| 中断、恢复、检查点               | checkpoint、resume、可恢复操作       |
| 子 Agent、子任务、委派           | subagent、delegate_task              |
| 文件隔离、共享存储、沙箱         | Workspace、shared_posix、NFS、Docker |
| 知识库检索、文本向量化、长期记忆 | RAG、Embedding、Memory               |

长期记忆 Memory 与上传型知识库 RAG 是不同模块；独立 MemoryService 尚未实现。相关目标保留在[上下文管理设计](context-management-design-v0.1.md)。

## 目标与历史如何阅读

[总体架构 v0.3](architecture-v0.3.md)、[统一 Hooks 设计](hooks-design-v0.1.md)、[上下文管理设计](context-management-design-v0.1.md)说明后续方向和设计背景，使用前对照当前实现文档。

早期需求、演示验证、实现迭代和[模块化第一阶段记录](runtime-modularity-v0.1.md)用于理解演进过程，其中的测试数量、限制和接口示例应按各自日期阅读。不要仅凭文件名中的版本号判断哪篇代表当前全貌。
