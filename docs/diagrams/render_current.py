"""Render the code-audited architecture using only Python's standard library."""

from html import escape
from pathlib import Path

OUT = Path(__file__).parent / "current-system-architecture.svg"
W, H = 1440, 1450
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc">
<title id="title">AgentLoom 当前实现架构</title>
<desc id="desc">2026-10-09，对照代码9941e31。单进程模块化单体：Vue与API接入应用服务，发布配置交给Runtime组装。统一Loop通过模型网关决策，通过进程内EventBus请求工具处理器；Hooks覆盖执行边界，SQL保存事件、历史与检查点，文件保存在隔离本地或共享POSIX工作区。MemoryService、隔离Hook Worker和分布式调度待实现。</desc>
<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="none" stroke="#6c809a" stroke-width="1.5"/></marker></defs>
<style>text{{font-family:"PingFang SC","Noto Sans CJK SC",sans-serif}} .title{{font-weight:650}} </style>
<rect width="{W}" height="{H}" fill="#fff"/>
''']


def rect(x, y, w, h, fill, stroke="#cfdae6", dashed=False):
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')


def text(x, y, value, size=19, color="#4c6078", weight=400):
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(value)}</text>')


def arrow(x1, y1, x2, y2):
    parts.append(f'<path d="M{x1} {y1} L{x2} {y2}" stroke="#6c809a" stroke-width="1.6" fill="none" marker-end="url(#arrow)"/>')


def card(x, y, w, h, title, lines=(), color="#2c568f", fill="#fff"):
    rect(x, y, w, h, fill)
    text(x + 16, y + 30, title, 22, color, 650)
    for i, line in enumerate(lines):
        text(x + 16, y + 59 + i * 26, line, 18)


def panel(y, h, title, detail, color, fill):
    rect(36, y, 1368, h, fill, color)
    text(56, y + 35, title, 25, color, 650)
    text(56, y + 65, detail, 18)


text(36, 42, "AGENTLOOM  /  CURRENT IMPLEMENTATION", 17, "#2c568f", 650)
text(36, 88, "织点 · 以 Agent Runtime 为核心", 36, "#21344c", 650)
text(36, 123, "代码基线 9941e31 · 2026-10-09 · 实线为当前模块与调用边界；底部虚线为后续建设", 19)

panel(149, 173, "平台接入与应用服务", "配置与发布 / 运行准入 / 记录与展示；当前一个 FastAPI 进程", "#277a70", "#f0f8f5")
card(56, 232, 312, 70, "Vue 工作台 / 发布应用", ["配置 / 发布应用 / 执行记录"], "#277a70")
card(386, 232, 312, 70, "HTTP API / SSE", ["认证、空间与个人运行权限"], "#277a70")
card(716, 232, 312, 70, "资源与发布", ["模型 / Skill / MCP / 知识 / Hooks"], "#277a70")
card(1046, 232, 338, 70, "会话与 Run 服务", ["历史、工作区绑定、启动与恢复"], "#277a70")
arrow(718, 328, 718, 349)

panel(358, 399, "Agent Runtime · 核心执行内核", "runtime.create_engine 组装 · Protocol / 模块身份 / 配置注入 · Runtime 不反向依赖平台 API", "#5d63ac", "#f6f5fc")
card(56, 442, 256, 92, "统一 Loop", ["决策 → 行动 → 反馈", "继续 / 完成 · engine.py"], "#5d63ac", "#ebeafa")
card(330, 442, 310, 92, "ModelGateway", ["Chat Completions / 统一响应", "DeepSeek / OpenAI"], "#5d63ac")
card(658, 442, 326, 92, "ContextManager", ["连续历史 / 模型视图 / 预算", "80% 与轮数触发主动压缩"], "#5d63ac")
card(1002, 442, 382, 92, "策略与执行状态", ["ReAct / 自动 Plan / 完成检查", "子实例 / 调用预算 / 检查点"], "#5d63ac")
rect(56, 554, 1328, 141, "#fff8e8", "#c9a65a")
text(74, 587, "HookManager · 已实现的可信 Python 扩展", 24, "#876321", 650)
text(74, 620, "挂点契约 / 注册与绑定 / 有序执行 / 超时与恢复 · 模型、工具、上下文、任务；Embedding 使用独立操作记录", 19)
text(74, 655, "保存有效输入 → 实际调用 → 保存真实结果 → 后置 Hook → 保存有效输出", 21, "#876321")
text(74, 681, "无 Hook 也保留调用阶段；代码与契约版本进入检查点；上传隔离 Worker 待实现", 18)
text(56, 733, "可替换：模型网关 / 上下文 / 压缩 / 完成检查 / 执行策略 / Hooks；工具细节留在执行层。", 19, "#5d63ac")
arrow(718, 764, 718, 788)

panel(797, 106, "进程内 EventBus → ToolRuntime", "单处理器执行请求；关联结果返回 Loop；通知支持多个 Observer。当前不是跨机器消息队列。", "#2f718e", "#eef7fb")
text(56, 890, "ToolDefinition / Registry → Hook → 权限、参数与策略校验 → Handler → 真实结果与有效观察", 20, "#2f718e")
arrow(718, 910, 718, 933)

panel(942, 196, "能力适配与执行", "由宿主提供可信资源范围；模型不能指定空间、存储根目录或平台凭据", "#9a722c", "#fffbf2")
card(56, 1025, 254, 92, "文件与沙箱", ["10 个系统工具 / Docker", "本地或共享 POSIX 工作区"], "#9a722c")
card(326, 1025, 254, 92, "Skill", ["目录 / 公开 HTTPS Git", "按需加载 / 只读技能挂载"], "#9a722c")
card(596, 1025, 254, 92, "MCP", ["外部 HTTP / SSE", "托管 Python stdio"], "#9a722c")
card(866, 1025, 254, 92, "知识检索", ["SQL 原文 / 分块 / 向量", "RAG / Embedding Hooks"], "#9a722c")
card(1136, 1025, 248, 92, "计划与委派", ["update_plan / 子任务", "继承模型 / 独立上下文"], "#9a722c")

rect(36, 1164, 669, 124, "#f5f8fc")
text(56, 1197, "持久化：SQL 与文件分别保存", 23, "#375b86", 650)
text(56, 1228, "PostgreSQL / SQLite：配置、发布、Run、事件、历史、检查点", 18)
text(56, 1258, "文件：导入包、根密钥、会话与子实例工作区、隔离标记", 18)
rect(723, 1164, 681, 124, "#f5f8fc")
text(743, 1197, "观测与当前部署边界", 23, "#375b86", 650)
text(743, 1228, "保存事件 → SSE 续接 / 页面；无 token 级流式输出", 18)
text(743, 1258, "Run 句柄在内存；共享文件不等于支持多 Worker 或集群", 18)

rect(36, 1313, 1368, 91, "#fff", "#acb7c6", dashed=True)
text(56, 1346, "后续建设 · 尚未实现", 23, "#66758b", 650)
text(56, 1380, "持久调度 / 租约与 fencing   ·   隔离 Hook Worker   ·   MemoryService   ·   完整资源来源与撤销过滤", 20)
text(36, 1431, "设计与代码索引：docs/technical-design.md · runtime-design.md · platform-api-data.md · deployment-operations.md", 17, "#6d7f94")
parts.append("</svg>")
OUT.write_text("\n".join(parts), encoding="utf-8")
print(f"Generated {OUT.name}")
