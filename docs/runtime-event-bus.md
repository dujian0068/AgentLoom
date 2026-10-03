# 织点 AgentLoom · 运行时事件总线

## 执行路径

1. 模型返回 `tool_calls`。
2. Loop 保存调用进度，提交 `tool.execute` 请求。
3. EventBus 按 `correlation_id` 管理请求和等待中的 Future。
4. ToolRuntime 从 ToolRegistry 取得处理器、校验绑定权限和参数。
5. 独立 Handler 执行文件 / Skill / MCP / RAG / 计划 / 子 Agent。
6. ToolOutcome 返回同一请求。
7. Loop 将统一结果放回模型上下文，继续下一轮。

## 职责边界

| 文件或目录          | 职责                                                                   |
| ------------------- | ---------------------------------------------------------------------- |
| `engine.py`         | 模型循环、上下文、完成检查、通用调用进度、结果反馈。                   |
| `event_bus.py`      | 异步 request/reply、唯一关联 ID、通知订阅、取消和超时清理。            |
| `tool_contracts.py` | 请求、结果、工具定义与可信执行上下文。                                 |
| `tool_runtime.py`   | 工具注册表、模型可见定义、绑定权限、JSON Schema 校验、恢复与执行策略。 |
| `handlers/`         | 具体执行实现；新增工具不需要给 Engine 增加 `if`/`elif`。               |
| `runtime.py`        | 应用组装入口，通过 `create_engine` 注入注册后的工具运行时。            |

- `ToolRequest` 包含 `call_id`、`name`、`arguments` 和 `uncertain`。
- 总线 `Event` 包含 `topic`、`correlation_id`、`payload` 和进程内可信 `context`。
- `context` 由平台构造，绑定当前 Agent、实例、检查点和工作区；模型只能提供参数，不能替换上下文。
- `ToolOutcome` 包含 `value`、`status` 和证据参数。

## 注册与扩展

每个工具由 `ToolDefinition` 声明：`name`、`description`、JSON Schema、async handler。

`available(config, instance)` 决定模型是否可见以及实际调用时是否授权。

`before_plan`、`resume_inflight`、`timeout`、`evidence_fields` 属于运行策略元数据。

Loop 不根据工具名称判断具体执行方式。

默认处理器集中在 `handlers/__init__.py` 组装。

应用可以传 `create_engine(..., configure_tools=register)` 追加受信任的后端工具。

完整例子见 [examples/runtime-tools/word_count.py](../examples/runtime-tools/word_count.py)。

这是后端扩展接口；已有面向外部开发者的 MCP SDK 和页面绑定流程继续使用。

## 请求和通知

- `request` 是单处理器请求/响应：每个 `topic` 只能注册一个执行处理器；重复注册和未知 `topic` 明确报错。
- 工具调用使用统一 `topic=tool.execute`，由注册表选择具体处理器。
- `subscribe` 是观察通知：同一通知可有多个订阅者，用于日志、指标等；观察者不能替代请求返回值。
- 总线通知 `request.started/completed/failed/cancelled/timed_out` 只携带原 `topic` 与关联 ID，不复制工具内容。
- 已有 `tool.started/completed/failed` 等执行记录继续写入数据库，并增加 `request_id` 和 `call_id`，页面和 API 保持兼容。

## 主子任务和恢复

每次运行拥有独立总线；每个请求使用独立异步任务，因此子 Agent 可以在父请求等待期间继续发起工具请求，不受单 worker 队列阻塞。

任务取消或超时会向处理器传播取消，并等待协作式清理。外部 MCP 服务已经发生的操作不能保证回滚。

请求 ID、调用进度和处理器私有状态进入原有加密检查点。恢复时，普通工具的未知结果不会自动重放；子 Agent 处理器可根据保存的实例 ID 恢复原子任务。

兼容旧版检查点中的 `pending.child_instance`，加载时转换到通用 `handler_state`。

## 当前实现范围

这是单进程内的 `asyncio` 请求/响应总线。PostgreSQL 保存执行记录和检查点，不充当工具执行消息队列。

本轮没有引入 Redis、RabbitMQ 或跨服务器 worker，也不提供分布式 exactly-once 保证。

后续要跨进程执行，需要把可信上下文改为可解析的引用，并增加持久消息、租约和幂等协议；工具处理器与 Loop 的分离边界可以继续沿用。
