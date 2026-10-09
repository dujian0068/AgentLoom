# AgentLoom Runtime 模块化实现 v0.1

> **模块化第一阶段记录。** 下文的动机与示例保留；后续已新增 ContextManager、Hooks、WorkspaceProvider 和兼容校验。完整现状以[Runtime 详细设计](runtime-design.md)为准（代码 `9941e31`，2026-10-09）。

2026-10-07 · 第一轮模块化接口与使用说明

本轮将模型调用、上下文压缩、完成判断和 ReAct / Plan 行为拆成可注入模块，并收窄工具处理器、事件观察者可以接触的状态。本页保留第一轮接口和当时验证结果。

> **后续实现更新：** 已增加可注入 ContextManager、连续会话来源、可复用视图和预算 / 轮数主动压缩，见[上下文实现 v0.2](runtime-context-v0.2.md)。新保存并发布的配置默认使用 80% 策略；缺少策略的旧发布版本保留字符压缩。2026-10-08 已接入统一可信 HookManager，见[Hooks 使用说明](hooks-runtime-v0.1.md)。当前 Token 大小采用保守估算，Memory 与完整类型化来源仍待实现。

## 1. 已实现的模块与契约

[契约定义](../packages/runtime/agentloom_runtime/module_contracts.py)使用 Python Protocol；受信任的后端实现满足方法签名即可替换，无需继承默认类。

| 模块              | 契约                                                               | 默认实现与边界                                                                                                                                                                          |
| ----------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ModelGateway      | `invoke(ModelRequest) -> dict`                                     | [ProviderModelGateway](../packages/runtime/agentloom_runtime/model_gateway.py) 调用 provider 并解析模型凭据；Engine 不再持有 provider 或模型解密回调。                                  |
| CompactionPolicy  | `compact(ContextInput, ModelCall) -> CompactionResult \| None`     | [BudgetCompactionPolicy / CharacterCompactionPolicy](../packages/runtime/agentloom_runtime/context.py)：新发布策略使用预算 / 轮数，旧无策略版本保留 60,000 字符触发与 16,000 近期目标。 |
| ContextManager    | `create / restore / append / queue_input / drain_inputs / prepare` | [JournalContextManager](../packages/runtime/agentloom_runtime/context_manager.py) 管理来源、可见视图、摘要快照和计数；通过 `context_manager=` 注入。                                    |
| CompletionPolicy  | `review(CompletionInput, ModelCall) -> CompletionDecision`         | [EvidenceCompletionPolicy](../packages/runtime/agentloom_runtime/completion.py) 返回 complete / continue / blocked；默认完成检查仍用模型，不能替代实际工具证据。                        |
| ExecutionStrategy | `instructions`、`candidate_feedback`、`authorize_tool`             | [ReactStrategy / PlanStrategy](../packages/runtime/agentloom_runtime/execution_strategy.py) 提供指令、交付前反馈与工具准入；子 Agent 不单独配置模式。                                   |
| ExecutionLimits   | `max_model_calls`、`max_iterations`                                | 正整数，默认 96 次模型调用、64 次 Loop；摘要和完成检查同样消耗模型调用预算。                                                                                                            |
| ToolRegistry      | `register`、`resolve`、`definitions`、`validate`                   | [ToolRegistry](../packages/runtime/agentloom_runtime/tool_runtime.py) 可整体替换，也可在组装时追加工具；组装后冻结注册。                                                                |

`ModelRequest` 包含 messages、tools、purpose、instance 和 options。当前 purpose 使用 `action`、`compaction`、`verification`；目标文档中的 `completion` 尚未替换现有 `verification`。网关返回模型消息字典，例如 role、content、tool_calls。所有网关响应都经宿主统一规范化与校验，空响应、畸形调用、重复 call_id 不进入消息或 pending 状态；自定义网关同样遵守此规则。

ContextInput 包含当前任务、计划、消息与实例；CompletionInput 增加执行证据和候选结果。宿主传递工作副本，模块返回候选结果，由 Engine 提交消息与检查点。压缩和完成模块请求模型时使用宿主提供的 ModelCall，使调用仍经过统一预算和模型网关。宿主固定辅助调用的 purpose 和 instance，并拒绝它们携带行动工具；压缩返回值提交前还会校验原始系统消息及完整工具调用/结果关联。模块不应访问 Engine、frame 或数据库的内部对象。

这些接口用于受信任的进程内 Python 扩展，并不是不可信代码的安全沙箱。工作副本与只读配置限制正常接口使用中的意外修改，不承诺阻止 Python 反射或恶意代码。

## 2. 组装与替换

[create_engine](../packages/runtime/agentloom_runtime/runtime.py)保留应用现有 snapshot、workspace、emit、decrypt、search、checkpoint、save 参数，新增关键字注入点：

```python
create_engine(
    snapshot, workspace, emit, decrypt, search,
    checkpoint=None, save=None, configure_tools=None,
    *, model_gateway=None, compaction_policy=None,
    completion_policy=None, strategy=None, limits=None, registry=None,
    context_manager=None, hooks=None, hook_scope=None, workspace_provider=None,
)
```

未传入的模块使用默认实现。默认执行策略按发布配置中的 `mode` 选择。snapshot 与传入检查点由工厂复制；凭据解析与知识检索回调保留在默认适配器和处理器的组装边界。

- `configure_tools(registry)`：默认内置注册完成后追加工具。
- `registry=...`：整体替换内置工具目录，宿主负责注册所需能力；此时 configure_tools 只扩展传入的目录。
- ToolRuntime 组装后不能再 register；运行中更换模块或目录不属于本接口的支持范围。

以下完整示例不访问真实模型、数据库或外部工具。在项目根目录、安装项目依赖后，将代码保存为 `/tmp/agentloom-runtime-demo.py`，运行 `PYTHONPATH=packages/runtime .venv/bin/python /tmp/agentloom-runtime-demo.py`。

```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from agentloom_runtime.context import CharacterCompactionPolicy
from agentloom_runtime.module_contracts import ExecutionLimits, ModelRequest
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_runtime import ToolRegistry


class DemoGateway:
    module_id = "demo-gateway/v1"

    async def invoke(self, request: ModelRequest) -> dict:
        if request.purpose == "verification":
            content = '{"decision":"complete","reason":"演示回答已生成"}'
        else:
            content = "这次回答来自注入的模型网关。"
        return {"role": "assistant", "content": content}


async def main():
    snapshot = {
        "config": {
            "prompt": "直接回答用户。", "mode": "react",
            "skills": [], "tools": [], "wiki": [], "subs": [],
        },
        "model_obj": {"model_id": "demo"},
        "skills": [], "tools": [], "wiki": [],
    }
    with TemporaryDirectory() as directory:
        engine = create_engine(
            snapshot, Path(directory),
            emit=lambda kind, payload: None,
            decrypt=lambda value: value,
            search=lambda *args: [],
            model_gateway=DemoGateway(),
            compaction_policy=CharacterCompactionPolicy(
                context_chars=12000, keep_recent_chars=4000,
            ),
            limits=ExecutionLimits(max_model_calls=8, max_iterations=4),
            registry=ToolRegistry(),
        )
        try:
            print(await engine.execute("介绍这次模块注入。"))
        finally:
            await engine.close()


asyncio.run(main())
```

预期输出：`这次回答来自注入的模型网关。` 空注册表用于证明模型/策略组合可独立运行；本例不覆盖真实供应商、工具执行或 Token 压缩。

## 3. 工具能接触哪些状态

[ToolContext](../packages/runtime/agentloom_runtime/tool_contracts.py)不再直接公开 frame、全局 state 或通用 run_child 回调。它提供实例标识、工作区、深只读 config、观察出口，以及有限能力：

| 接口                 | 允许操作                                                       |
| -------------------- | -------------------------------------------------------------- |
| `plan.read/update`   | 读取只读计划，或经校验更新计划。                               |
| `children.run`       | 按已绑定的子 Agent 配置执行或恢复任务。                        |
| `citations.record`   | 记录引用元数据。                                               |
| `invocation.get/set` | 操作当前工具调用的私有恢复状态，读取和写入均与调用方数据分离。 |

[execution_services.py](../packages/runtime/agentloom_runtime/execution_services.py)仍把这些接口适配到宿主的 format=1 状态。它内部持有 frame/state，关键保存失败会还原本地变更并上抛 ToolPersistenceError；此错误不能被当作普通工具失败后继续消费调用。子任务仍复用 Engine.loop，当前顺序执行、最多 8 次委派，不递归委派。

因此当前完成的是处理器访问边界的收窄，尚未实现独立 StateStore、PlanManager 或 ChildTaskManager 的完整持久化模块。

## 4. 通知与生命周期

[EventBus](runtime-event-bus.md)将执行请求与观察通知分开：请求处理器获得可信 ToolContext；观察者仅得到 JSON 深冻结快照，不获得执行上下文。自动 request 通知包含 topic、关联 ID 及显式标量元数据，工具参数和结果不会自动复制过去。手工 publish 仍由发布方选择内容并脱敏。

`register(topic, handler)` 返回释放句柄，须在该 topic 的在途请求及通知清理结束后调用。`cancel_topic(topic)` 只清理指定 topic，不关闭共享总线，也不阻止新请求；宿主先停止接单再清理。旧释放句柄不会移除后来新注册的处理器。

每个 Engine 同时只接受一个主 execute；关闭后禁止继续执行。运行方必须在 finally 中等待 `engine.close()`；关闭会先取消并等待活动执行，再由 ToolRuntime 清理自有总线，或仅清理共享总线中的工具 topic，随后 RuntimeModules 按执行策略、完成、上下文、压缩、模型、Hooks 的顺序调用模块可选的异步 `aclose()`，同一个对象不会重复关闭。带客户端、文件句柄等资源的注入模块应实现 aclose；默认按单个 Engine 所有权组装，不应把待自动关闭的同一模块对象随意共享给多个 Engine。

模块关闭时的异常会向宿主报告。取消依赖处理器的协作式清理，不能撤销外部服务已经发生的副作用。统一生命周期 HookManager 已通过同一组契约接入模型、工具、上下文与运行边界；观察订阅仍保持独立。

## 5. 模块版本与恢复

[RuntimeModules](../packages/runtime/agentloom_runtime/modules.py)保存 model、compaction、completion、strategy、context、hooks 和 limits 的绑定。每个模块必须声明非空 `module_id`，约定携带兼容版本，如 `character-handoff/v1`。影响恢复的配置通过可选 `checkpoint_config()` 返回 JSON 数据；未提供该方法时配置按空对象记录，不能把关键配置藏在未声明的对象字段里。默认 ProviderModelGateway 记录 provider、base_url、model_id，不记录 secret，允许在相同模型绑定下轮换凭据。

检查点新增 `modules` 绑定，并纳入工具目录的描述、Schema、策略字段和 implementation_id；顶层格式仍为 `format=1`。新检查点恢复要求模块 ID、声明配置、运行预算与工具绑定一致；不匹配则拒绝恢复，不能默默换算法继续旧任务。没有 modules 字段的旧检查点仅允许内置模块、默认字符预算、默认 ExecutionLimits 和带内置版本标识的工具恢复；改用自定义模块、预算或工具须显式迁移。修改算法且不兼容时升级 module_id，不复用旧 ID。

恢复要求每个工具声明带版本的 `implementation_id`，例如 `ToolDefinition(..., implementation_id="example-tool/v1")`。未声明版本的自定义工具可用于新 Run，但不能据其检查点直接恢复。工厂通过 `registry.implementation_namespace("builtin-tools/v1")` 为内置注册批次生成版本标识，该命名空间不覆盖后来追加的自定义工具。工具实现或授权判断发生不兼容变化时，开发者须升级标识；仅改函数内容而复用旧标识不会被内容哈希自动识别。

当前机制是模块恢复兼容检查，不是完整资源发布系统，也没有自动迁移任意自定义模块私有状态。模型及工具绑定已有兼容检查，Skill 包等完整资源修订和发布存储仍受现有能力限制。恢复时 Engine 会重置本次执行调用/Loop 预算；跨提问的压缩计数基线现已独立保存在上下文检查点与合规会话视图，不复用这些执行预算计数。

## 6. 验证与后续工作

本页示例已在项目 .venv 中运行，输出与预期一致；使用注入的确定性网关，无需供应商或数据库。接口替换、策略输入隔离、检查点兼容、观察者不可修改执行状态、注册释放和共享总线关闭属于本轮验证重点；最终执行结果以对应测试命令为准，不把接口存在等同于真实外部集成验证。

本轮验证结果：后端 129 项测试、前端 5 项测试、项目检查脚本指定范围的 Ruff 检查与格式校验、前端格式检查、类型检查和生产构建均通过。后端使用临时 SQLite 与模型替身，不连接用户 PostgreSQL；运行时设置 `PYTHON_DOTENV_DISABLED=1`，避免加载本地凭据。新增模块及恢复边界的主要测试见 [test_runtime_modules.py](../tests/test_runtime_modules.py)。

下一阶段沿已建立端口推进：

1. ContextManager、80% / 轮数配置与恢复基线的第一版已在 v0.2 落地；继续补齐精确来源映射、按用途装配、资源修订与持久压缩节流。
2. 将 format=1 宿主状态适配逐步替换为版本化 StateStore、PlanManager 和 ChildTaskManager 契约。
3. 统一 HookManager 的可信 Python 机制已完成；继续建设隔离 Worker 与团队可视化管理。
4. 补齐资源版本、长期 Memory 及各模块真实供应商/环境验证；跨进程调度仍需单独设计持久协议。
