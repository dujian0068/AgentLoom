# 织点 AgentLoom · 运行时事件总线

实现说明 · 2026-10-07。模型与策略注入、检查点版本和完整示例见 [Runtime 模块化实现 v0.1](runtime-modularity-v0.1.md)。

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
| `engine.py`         | 模型循环、调用注入的压缩/完成策略、通用调用进度、结果反馈。                   |
| `event_bus.py`      | 异步 request/reply、唯一关联 ID、通知订阅、取消和超时清理。            |
| `tool_contracts.py` | 请求、结果、工具定义与可信执行上下文。                                 |
| `tool_runtime.py`   | 工具注册表、模型可见定义、绑定权限、JSON Schema 校验、恢复与执行策略。 |
| `handlers/`         | 具体执行实现；新增工具不需要给 Engine 增加 `if`/`elif`。               |
| `runtime.py`        | 组装入口，通过 `create_engine` 注入模块、预算和工具注册表。            |

- `ToolRequest` 包含 `call_id`、`name`、`arguments` 和 `uncertain`。
- 请求 `Event` 包含 `topic`、`correlation_id`、`payload` 和进程内可信 `context`；执行上下文只给请求处理器。
- 平台构造的 ToolContext 提供深只读 config、实例/工作区，以及 plan、children、citations、invocation 受限接口；不直接暴露 frame/state。模型只能提供工具参数，不能替换上下文。
- `execution_services.py` 把受限接口适配到 format=1 检查点，宿主仍持有运行状态和子任务执行回调；这不是可直接跨进程传输的对象。
- `ToolOutcome` 包含 `value`、`status` 和证据参数。

## 注册与扩展

每个工具由 `ToolDefinition` 声明：`name`、`description`、JSON Schema、async handler。需要恢复的自定义工具还须声明 `implementation_id`，例如 `"example-tool/v1"`；未版本化工具只能用于新 Run，恢复时会明确拒绝。工具目录及版本纳入检查点兼容校验。

`available(config, instance)` 决定模型是否可见以及实际调用时是否授权。

`before_plan`、`resume_inflight`、`timeout`、`evidence_fields` 属于运行策略元数据。

Loop 不根据工具名称判断具体执行方式。

默认处理器集中在 `handlers/__init__.py` 组装。

应用可以传 `create_engine(..., configure_tools=register)` 追加受信任的后端工具；传 `registry=ToolRegistry()` 则使用完整替代注册表，不自动安装内置工具。configure_tools 在冻结前执行；ToolRuntime 组装时冻结注册表，运行期间继续 register 会报错。

完整例子见 [examples/runtime-tools/word_count.py](../examples/runtime-tools/word_count.py)。

这是后端扩展接口；已有面向外部开发者的 MCP SDK 和页面绑定流程继续使用。

## 请求和通知

- `request` 是单处理器请求/响应：每个 `topic` 只能注册一个执行处理器；重复注册和未知 `topic` 明确报错。`register()` 返回释放句柄，必须等该 topic 的在途请求完成清理后才能释放；旧句柄不能移除后来的新注册。
- 工具调用使用统一 `topic=tool.execute`，由注册表选择具体处理器。
- `subscribe` 是观察通知：同一通知可有多个订阅者，用于日志、指标等；观察者不能替代请求返回值。`publish` 仅接受 JSON 形状的数据，先复制并递归冻结字典/列表；对象引用和循环数据被拒绝，观察者拿不到 ToolContext。
- 总线通知 `request.started/completed/failed/cancelled/timed_out` 只携带原 `topic`、关联 ID 和显式提供的标量 observation_metadata，不复制工具内容或执行上下文。手工 publish 的内容仍须由调用方控制必要字段和脱敏。
- 已有 `tool.started/completed/failed` 等执行记录继续写入数据库，并增加 `request_id` 和 `call_id`，页面和 API 保持兼容。

## 主子任务和恢复

每次运行拥有独立总线；每个请求使用独立异步任务，因此子 Agent 可以在父请求等待期间继续发起工具请求，不受单 worker 队列阻塞。

任务取消或超时会向处理器传播取消，并等待协作式清理与终态通知。外部 MCP 服务已经发生的操作不能保证回滚。

ToolRuntime 关闭自有总线；若接入共享总线，只调用 `cancel_topic("tool.execute")` 清理自己的请求并释放注册，不关闭其他 topic。cancel_topic 不阻止新请求，宿主需先停止接单；处理器或观察者不能在自身调用栈内关闭总线或取消 topic，以免等待自己。每个 Engine 只允许一个主执行；调用方须 `await engine.close()`，通常放在 finally 中。关闭先取消并等待活动执行，再释放工具与模块资源，关闭后拒绝执行。

请求 ID、调用进度和处理器私有状态进入原有加密检查点。恢复时，普通工具的未知结果不会自动重放；子 Agent 处理器可根据保存的实例 ID 恢复原子任务。

兼容旧版检查点中的 `pending.child_instance`，加载时转换到通用 `handler_state`。

## 当前实现范围

这是单进程内的 `asyncio` 请求/响应总线。PostgreSQL 保存执行记录和检查点，不充当工具执行消息队列。

本轮没有引入 Redis、RabbitMQ 或跨服务器 worker，也不提供分布式 exactly-once 保证。

后续要跨进程执行，需要把可信上下文改为可解析的引用，并增加持久消息、租约和幂等协议；工具处理器与 Loop 的分离边界可以继续沿用。
