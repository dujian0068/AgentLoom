"""Render the ContextManager target module diagram using only the stdlib."""
from html import escape
from pathlib import Path
OUT = Path(__file__).parent
W, H = 1580, 1770
parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title description">
<title id="title">AgentLoom 上下文管理目标架构</title>
<desc id="description">ContextManager 是 Agent Runtime 的内部模块，包含记录与来源、调用视图装配、Token 预算、分段摘要、激活资源跟踪及主子 Agent 作用域六个逻辑组件。Loop 准备上下文，模型网关运行 Hook 后再次校验预算。工具通过事件总线与 ToolRuntime 执行；实际结果与有效视图分开保存。检查点关联源记录版本、摘要和激活资源引用。压缩仅替换视图，保留执行事实。完整 ContextManager 与 Hooks 是待实现设计。</desc>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto-start-reverse"><path d="M0 0 L7 4 L0 8" fill="none" stroke="#8396ad" stroke-width="1.5"/></marker></defs>
<style>text{{font-family:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC",sans-serif}}</style><rect width="{W}" height="{H}" fill="#fff"/>''']
def rect(x,y,w,h,fill='#fff',stroke='#d7e1ec',r=12,dash=False):
    dashed=' stroke-dasharray="7 5"' if dash else ''
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dashed}/>')
def text(x,y,value,size=20,fill='#576d85',weight=400,anchor='start'):
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>')
def arrow(x1,y1,x2,y2,both=False):
    start=' marker-start="url(#arrow)"' if both else ''
    parts.append(f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="#8396ad" stroke-width="1.6" marker-end="url(#arrow)"{start}/>')
def card(x,y,w,h,title,english,detail='',color='#405aa2',tint='#fff',title_size=22):
    rect(x,y,w,h,tint)
    text(x+16,y+30,title,title_size,color,650)
    text(x+16,y+58,english,18,color)
    if detail: text(x+16,y+h-17,detail,17)
text(40,42,'AGENTLOOM  /  CONTEXT MANAGEMENT',16,'#2862b3',650)
text(40,91,'织点 · 上下文管理',38,'#20344b',650)
text(40,126,'把运行资料组织为每次模型调用需要的上下文，并保留可恢复的执行事实。',20)
rect(1270,50,270,44,'#fff7e3','#d9bf8c',22)
text(1405,80,'目标架构 v0.1 · 待实现',20,'#8a601e',600,'middle')
for x,title,name,detail in [(40,'发布策略与资源目录','ContextPolicy / Skill Catalog','固定发布修订 · 按需加载正文与附件'),(550,'用户输入与会话历史','User Input / History','当前任务 · 用户补充 · 已授权历史'),(1060,'工具、知识与子任务结果','Tool / RAG / Task Results','片段与定位 · 产物引用 · 执行状态')]:
    card(x,154,480,104,title,name,detail,color='#277c78',tint='#f0f9f6')
arrow(790,267,790,292)
text(812,287,'授权输入 · 选择后进入调用视图',17)
rect(30,304,1520,1160,'#f7f9fe','#8e9dcc')
text(50,343,'Agent Runtime · 内核内部的逻辑模块',26,'#405aa2',650)
text(1529,342,'统一 Loop 调度 · 组件可替换',18,'#536ba4',anchor='end')
rect(50,371,1065,564,'#f0f4ff','#7792c9')
text(70,411,'ContextManager · 上下文管理',28,'#405aa2',650)
text(1094,410,'下列六个组件均待实现',18,'#6d7fa2',anchor='end')
modules=[('记录与来源','ContextRecordStore','消息记录 · 原始来源 · 受保护引用'),('调用视图装配','ContextAssembler','按 purpose 选择 · 去重 · 保留边界'),('Token 预算','BudgetPolicy','完整请求计量 · 输出预留 · 硬上限'),('分段摘要','CompactionPolicy','摘要候选 · 校验 · 版本化提交'),('激活资源','ResourceTracker','Skill 修订 · 引用 · 生命周期'),('主子作用域','InstanceScope','独立上下文 · 明确委派与结果出口')]
for i,(title,name,detail) in enumerate(modules):
    card(70+(i%3)*345,446+(i//3)*129,330,110,title,name,detail)
card(70,718,503,92,'已发布上下文策略','ContextPolicy · 固定版本 / 预算 / 选择规则',color='#277c78',tint='#f0f9f6')
card(592,718,503,92,'只读上下文快照','ContextSnapshot · 源记录版本 / 摘要 / 引用',color='#405aa2',tint='#e8edff')
text(70,850,'prepare 生成调用工作副本；结果按 operation_id 与源版本去重追加。',20,'#405aa2',550)
text(70,889,'压缩替换视图，保留原始执行事实；主、子 Agent 分别管理上下文。',20)
rect(1140,371,390,564,'#fff7e3','#c89943')
text(1160,411,'HookManager',27,'#8a601e',650)
text(1160,445,'统一扩展接口 · 待实现',19,'#8a601e')
for y,title,english in [(480,'上下文装配与压缩','Context lifecycle'),(584,'模型调用前后','model.chat.before / after'),(688,'工具执行前后','tool.before / after')]:
    card(1160,y,350,86,title,english,color='#8a601e',title_size=21)
text(1160,825,'类型化补丁 → 校验 → 有效视图',19,'#8a601e',550)
text(1160,862,'扩展不直接修改 frame 或源记录',18)
text(1160,899,'模型与工具边界共用此管理器',18)
arrow(1119,655,1135,655,both=True)
text(50,981,'模型调用路径',24,'#405aa2',650)
text(252,980,'根据调用目的准备输入；Hook 修改后重新校验完整请求。',19)
for x,w,title,name,detail in [(50,220,'统一执行循环','Loop','action / completion 等'),(300,270,'准备上下文','ContextManager.prepare','版本化选择与预算'),(600,230,'调用工作副本','PreparedContext','messages / tools / refs'),(860,385,'模型网关','ModelGateway','model.before → 必需块 / 预算校验'),(1275,255,'模型提供方','Provider Adapter','实际请求与实际响应')]:
    card(x,1004,w,113,title,name,detail)
for a,b in [(270,300),(570,600),(830,860),(1245,1275)]: arrow(a+5,1059,b-7,1059)
text(879,1146,'共用 HookManager；逻辑调用记录最终输入与有效输出',17,'#8a601e')
text(50,1190,'工具结果回流',24,'#926528',650)
text(252,1189,'Loop 发出请求，能力执行层完成工具调用，再提交上下文记录。',19)
for x,w,title,name,detail in [(50,290,'请求总线','EventBus','发送请求 · 等待结果'),(370,330,'工具执行层','ToolRuntime → Handler','MCP / 文件 / RAG / 子任务'),(730,400,'实际结果与有效视图','ActualResult / EffectiveView','分别保存 · 保留状态与来源'),(1160,370,'统一提交','ContextManager · append outcome','去重提交 · 校验版本与消息边界')]:
    card(x,1213,w,113,title,name,detail,color='#926528',tint='#fffdf7')
for a,b in [(340,370),(700,730),(1130,1160)]: arrow(a+5,1268,b-7,1268)
text(386,1356,'共用 HookManager；执行完成后仍须保留真实执行状态',17,'#8a601e')
text(50,1405,'职责边界',21,'#405aa2',650)
text(185,1405,'ContextManager 装配资料；工具执行、RAG 检索、长期 Memory 写入由各自能力模块负责。',20)
rect(40,1490,1500,108,'#f0f9f6','#8bbcb1')
text(60,1528,'检查点与恢复',25,'#277c78',650)
text(287,1528,'依赖版本集合  /  已校验摘要  /  激活资源引用  /  提交阶段  /  Hook 进度',21,'#277c78')
text(60,1572,'压缩失败保留旧视图；恢复后复用已保存的实际结果，不因上下文或后置 Hook 失败重放外部操作。',20)
rect(40,1624,1500,96,'#fff','#bcc9dc',dash=True)
text(60,1661,'实现状态',22,'#526982',650)
text(200,1661,'当前已有 frame.messages 与 context.py 字符阈值压缩；完整 ContextManager 与 Hooks 尚待实现。',20)
text(60,1698,'图中六个组件是 Runtime 内部的职责划分，不要求拆成六个进程或独立服务。',19)
text(40,1751,'Context Management v0.1 · AgentLoom · 与独立上下文管理设计文档配套',17,'#77889c')
parts.append('</svg>')
(OUT/'context-management.svg').write_text('\n'.join(parts),encoding='utf-8')
