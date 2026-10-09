"""Generate the editable, dependency-free SVG for the abstract module map."""
from pathlib import Path
from html import escape

OUT = Path(__file__).parent
W, H = 1580, 2046
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title description">
<title id="title">AgentLoom 抽象模块架构图</title>
<desc id="description">以 Agent Runtime 为核心的目标模块架构：内核包含 Loop、模型网关、上下文、计划、子任务、完成检查、恢复与统一 HookManager。Hooks 包含挂点契约、扩展注册、有序管道和执行器；模型与工具边界共用 Hooks，用户扩展由隔离 Worker 执行。管理控制面发布配置与扩展绑定；事件总线负责请求和结果，SSE 负责观测。可信 Hooks 已实现；扩展管理和隔离 Worker 仍待实现，图中为目标职责。</desc>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto-start-reverse"><path d="M0 0 L7 4 L0 8" fill="none" stroke="#8696aa" stroke-width="1.5"/></marker></defs>
<style>text{{font-family:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC",sans-serif}}.title{{font-weight:650;fill:#20344b}}.body{{fill:#576d85}}.small{{fill:#667b91}}</style>
<rect width="{W}" height="{H}" fill="#fff"/>
''']


def rect(x, y, w, h, fill, stroke='#d7e1ec', radius=12, dash=False, sw=1.3):
    dashed = ' stroke-dasharray="7 5"' if dash else ''
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dashed}/>')


def text(x, y, value, size=22, fill=None, weight=None, anchor='start', css='body'):
    attrs = f' fill="{fill}"' if fill else ''
    attrs += f' font-weight="{weight}"' if weight else ''
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" class="{css}"{attrs}>{escape(value)}</text>')


def line(x1, y1, x2, y2, both=False):
    start = ' marker-start="url(#arrow)"' if both else ''
    parts.append(f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="#8696aa" stroke-width="1.5" marker-end="url(#arrow)"{start}/>')


def group(y, height, idx, title, note, color, bg):
    rect(40, y, 1190, height, bg, color, sw=1.5)
    rect(59, y+15, 42, 32, color, color, 7)
    text(80, y+38, idx, 17, '#ffffff', 650, 'middle', '')
    text(116, y+39, title, 25, color, 650, css='')
    if note:
        text(1208, y+38, note, 18, color, anchor='end', css='')


def card(x, y, width, height, title, subtitle='', color='#2d4564', tint='#fff', size=22, subsize=18):
    rect(x, y, width, height, tint)
    text(x+16, y+(25 if height <= 66 else 29), title, size, color, 600, css='')
    if subtitle:
        text(x+16, y+height-(10 if height <= 70 else 16), subtitle, subsize)


def panel(x, y, width, height, title, note, color, bg):
    rect(x, y, width, height, bg, color, sw=1.6)
    text(x+20, y+39, title, 26, color, 650, css='')
    if note:
        text(x+width-20, y+38, note, 18, color, anchor='end', css='')


text(40, 42, 'AGENTLOOM  /  RUNTIME-CENTERED ARCHITECTURE', 16, '#2862b3', 650, css='')
text(40, 93, '织点 · 以 Agent Runtime 为核心', 38, css='title')
text(40, 127, '目标模块架构 · Hooks 属于内核 · 管理配置、承载运行与执行能力分别组织', 20)
rect(1350, 53, 190, 42, '#f1f5fa', '#d7e1ec', 21)
text(1445, 81, '架构 v0.3', 21, '#466383', 600, 'middle', '')

# Control plane feeds a published configuration into the runtime factory.
panel(40, 150, 300, 1348, '管理控制面', '', '#277c78', '#f0f9f6')
text(60, 219, '资源、配置与发布', 19, '#4f7b75', css='')
for y, title, subtitle in [(240, '空间与成员', '成员 / 角色 / API Key'), (322, 'Agent 配置', '主配置 / 子任务模板'), (404, '发布管理', '依赖校验 / 版本快照')]:
    card(60, y, 260, 68, title, subtitle, color='#277c78', subsize=17)
rect(60, 499, 260, 80, '#dff1e9', '#a3cec0')
text(76, 529, '已发布运行配置', 22, '#22645f', 600, css='')
text(76, 561, '配置 + 资源修订 + Hooks', 17)
for y, title, subtitle in [(622, '模型连接', '模型 / 地址 / 凭据引用'), (704, 'Skill 管理', '目录导入 / 包版本'), (786, '工具服务管理', 'MCP 接入 / 托管构建'), (868, '知识库管理', '上传 / 分块 / 索引修订'), (950, 'Hooks 扩展管理', '导入 / 测试 / 版本 / 绑定')]:
    card(60, y, 260, 68, title, subtitle, color='#277c78', subsize=17)
text(60, 1100, '管理端的产出', 23, '#277c78', 600, css='')
for i, row in enumerate(['经过校验的发布配置', '可使用的资源版本', '受控的扩展配置']):
    text(60, 1138+i*31, row, 20)
text(60, 1355, '内核可独立复用', 23, '#277c78', 600, css='')
text(60, 1394, '网页 / API / 脚本入口', 19)
text(60, 1426, '共用 Runtime 公共接口', 19)

# Hosting services surround rather than define the kernel.
panel(380, 150, 1160, 184, '接入与运行服务', '承载 Runtime 实例', '#3565a6', '#f4f8ff')
for x, title, subtitle in [(400, '接入适配', '页面 / HTTP API / SSE / 产物'), (780, '会话与运行', '用户任务 / 发布版本 / 运行状态'), (1160, '调度与控制', '启动 / 配额 / 停止 / 恢复')]:
    card(x, 219, 360, 87, title, subtitle, color='#3565a6', subsize=18)
line(960, 343, 960, 365)
text(979, 360, '启动 / 控制运行实例', 17)

# Core: HookManager is a first-class lifecycle extension module.
panel(380, 376, 1160, 770, 'Agent Runtime · 核心执行内核', '统一 Loop · 可独立使用', '#506cba', '#f4f5ff')
card(400, 451, 230, 100, '运行时组装', '配置与依赖注入', color='#405aa2')
card(650, 451, 538, 100, '统一 Loop', '模型决策 → 行动请求 → 结果反馈 → 继续 / 完成', color='#405aa2', tint='#e8edff', subsize=18)
card(1208, 451, 312, 100, '模型网关端口', '出入参 / before / after', color='#405aa2', subsize=17)
line(636, 500, 643, 500)
line(1193, 500, 1202, 500, both=True)
# Published configuration enters the factory; not every resource manager calls Loop.
parts.append('<path d="M320 539 H357 V515 H393" fill="none" stroke="#699e92" stroke-width="1.6" marker-end="url(#arrow)"/>')
for x, title, subtitle in zip([400, 684, 968, 1252], ['上下文管理', '计划管理', '子任务管理', '完成检查'], ['消息 / 摘要 / 压缩', '生成 / 更新 / 步骤状态', '继承模型 / 委派与汇总', '继续 / 完成 / 待补充']):
    card(x, 572, 268, 88, title, subtitle, color='#405aa2', subsize=17)
card(400, 678, 552, 82, '检查点与恢复', '对话 / 计划 / 工具边界 / 子实例 / Hook 阶段', color='#405aa2', subsize=18)
card(968, 678, 552, 82, '执行策略', '调用预算 / 超时 / 取消 / 委派限额', color='#405aa2', subsize=18)
rect(400, 784, 1120, 313, '#fff7e3', '#c89943', 12, sw=1.8)
text(420, 819, 'HookManager · 生命周期扩展', 26, '#8a601e', 650, css='')
text(1498, 818, '可信 Python 已实现 · Worker 待建', 18, '#8a601e', anchor='end', css='')
for x, label in [(420, '模型调用前后'), (696, '工具执行前后'), (972, '上下文与压缩'), (1248, '任务与子任务')]:
    text(x, 856, label, 21, '#826427', 600, css='')
for x, title, subtitle in [(420, '挂点契约', 'HookPointRegistry'), (692, '扩展注册与绑定', 'HookRegistry'), (964, '有序处理管道', 'Manager 内部管道'), (1236, '扩展执行器', 'HookExecutor')]:
    card(x, 879, 254, 83, title, subtitle, color='#8a601e', size=21, subsize=18)
text(420, 993, '执行：HookManager 的有序管道 → HookExecutor；注册表提供契约与版本绑定。', 19)
text(420, 1027, '入参 → before → 授权与校验 → 实际调用 → 保存真实结果 → after → 有效输出', 19)
text(420, 1070, '可信 Python 已实现 / 隔离 Worker 待建；按阶段修改或拦截，真实执行事实独立保存。', 19)
text(400, 1126, '模型网关与工具分发器共用 HookManager；阶段、版本与进度进入检查点，支持恢复。', 18)
line(888, 1154, 888, 1184)
line(1012, 1184, 1012, 1154)
text(915, 1174, '请求 / 结果', 17)

# Pluggable capabilities are called through one request/reply channel.
panel(380, 1192, 1160, 306, '可插拔能力执行', '统一工具契约 · 实现可替换', '#926528', '#fef9ef')
for x, title, subtitle in [(400, 'EventBus · 请求总线', '路由 / 关联 ID / 等待与回传'), (784, 'ToolRuntime · 调用治理', '授权 / 校验 / 前后 Hook / 超时'), (1168, '工具注册表', '工具定义 / Schema / 处理器')]:
    card(x, 1256, 352, 83, title, subtitle, color='#8a601e', subsize=17)
line(759, 1297, 777, 1297)
line(1143, 1297, 1161, 1297, both=True)
text(400, 1374, '能力处理器', 19, '#8a601e', 600, css='')
handlers = [('文件与工作区', '读写 / 产物'), ('Skill 加载', '指令 / 附件'), ('MCP 工具', '调用 / 结果'), ('知识检索', '召回 / 引用'), ('计划更新', '步骤与状态'), ('任务委派', '子任务入口')]
for i, (title, subtitle) in enumerate(handlers):
    card(400+i*189, 1389, 172, 75, title, subtitle, color='#8a601e', size=19, subsize=18)
text(400, 1484, '分发器调用同一 HookManager；最终授权与参数校验始终由执行层保证。', 18)
line(960, 1506, 960, 1526)

# Concrete infrastructure implements ports used by all upper modules.
panel(40, 1536, 1500, 238, '基础设施与适配器', '上层通过端口访问；部署仍采用模块化单体', '#667685', '#f5f7f9')
infra = [('业务数据仓储', 'PostgreSQL / 测试存储'), ('文件存储', '资源包 / 工作区 / 产物'), ('凭据存储', '加密 / 解析 / 轮换'), ('索引适配', '关键词 / 向量检索'), ('模型供应商适配', 'DeepSeek / OpenAI'), ('MCP 协议适配', 'HTTP / SSE / stdio'), ('隔离执行环境', '脚本沙箱 / Hook Worker'), ('执行状态存储', '事件 / 检查点 / Hook 状态')]
for i, (title, subtitle) in enumerate(infra):
    card(60+(i%4)*370, 1600+(i//4)*82, 350, 70, title, subtitle, color='#526273', subsize=18)
rect(40, 1800, 1500, 78, '#f8fafc', '#cbd7e5', dash=True)
text(60, 1846, '贯穿能力', 24, '#50677f', 600, css='')
text(240, 1846, '空间权限  /  公共契约  /  资源版本  /  追踪与审计  /  凭据保护', 23)

rect(40, 1906, 735, 90, '#fff9ed', '#e2cfaa')
text(60, 1939, '执行扩展与工具通道', 21, '#8a601e', 650, css='')
text(60, 1973, 'Hooks 参与节点处理；EventBus 传递请求和结果。', 21)
rect(797, 1906, 743, 90, '#eef7fb', '#bdd7e5')
text(817, 1939, '观测通道', 21, '#376e91', 650, css='')
text(817, 1973, '运行记录 → 事件存储 → SSE → 页面 / 观察者', 21)
text(40, 2034, '目标架构 · 2026-10-09：可信 Hooks 与 Python SDK 已实现；扩展管理与隔离 Worker 待建。', 18)
parts.append('</svg>')
svg = '\n'.join(parts)
(OUT / 'abstract-module-architecture.svg').write_text(svg, encoding='utf-8')
# A vector detail view of the same core, for reviewing Hooks without the surrounding platform.
import re
focus = re.sub(r'width="1580" height="2046" viewBox="0 0 1580 2046"', 'width="1168" height="778" viewBox="376 372 1168 778"', svg, count=1)
(OUT / 'agent-runtime-hooks.svg').write_text(focus, encoding='utf-8')
print('Generated runtime-centered architecture and Hooks detail SVGs')
