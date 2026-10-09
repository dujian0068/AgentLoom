"""System file tools reached only through ToolRuntime's request/reply boundary."""

from .. import sandbox
from ..tool_contracts import ToolDefinition, bindings
from ..workspace import storage_call

S = {"type": "string"}
B = {"type": "boolean"}
PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000}
HASH = {"type": "string", "pattern": "^([a-f0-9]{64})?$"}


def register(registry, snapshot, provider):
    def add(name, description, properties, required=(), *, method=None, read_only=False):
        async def handler(context, args):
            def execute():
                store = provider.for_instance(context.instance)
                return getattr(store, method or name)(**args)

            result = await storage_call(execute)
            if isinstance(result, dict) and "file" in result and context.instance != "main":
                result = {**result, "file": "subagents/" + context.instance + "/" + result["file"]}
            return result

        registry.register(
            ToolDefinition(
                name="workspace_" + name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
                handler=handler,
                before_plan=read_only,
                resume_inflight=read_only,
                evidence_fields=("path",) if "path" in properties else (),
                implementation_id="workspace-posix-tools/v1:" + name,
                tool_id="workspace_" + name,
            )
        )

    add(
        "list",
        "列出当前会话工作区目录；只接受相对路径，结果受数量限制。",
        {
            "path": PATH,
            "limit": LIMIT,
            "include_hidden": B,
            "recursive": B,
        },
        read_only=True,
    )
    add("stat", "查看文件或目录类型、大小、内容哈希。", {"path": PATH}, ["path"], read_only=True)
    add(
        "glob",
        "按文件路径通配符查找文件，如 **/*.py；结果返回相对路径。",
        {
            "pattern": S,
            "path": PATH,
            "limit": LIMIT,
            "include_hidden": B,
        },
        ["pattern"],
        read_only=True,
    )
    add(
        "grep",
        "搜索文件内容，返回文件、行号和匹配行。默认字面匹配；literal=false 使用有执行时限的正则。",
        {
            "pattern": S,
            "path": PATH,
            "glob": S,
            "literal": B,
            "case_sensitive": B,
            "limit": LIMIT,
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            "include_hidden": B,
        },
        ["pattern"],
        read_only=True,
    )
    add(
        "read",
        "按行读取当前会话文件，offset 从1开始；返回 sha256、行号、截断标记。",
        {
            "path": PATH,
            "offset": {"type": "integer", "minimum": 1},
            "limit": LIMIT,
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000},
        },
        ["path"],
        read_only=True,
    )
    add(
        "write",
        "原子写入当前会话文件。expected_sha256 传已读哈希避免覆盖他人修改，空串表示只新建。",
        {
            "path": PATH,
            "content": S,
            "expected_sha256": HASH,
        },
        ["path", "content"],
    )
    add(
        "edit",
        "精确替换文件中的旧文本；多处匹配默认拒绝，replace_all=true才替换全部。可用哈希防止过期修改。",
        {
            "path": PATH,
            "old_text": S,
            "new_text": S,
            "replace_all": B,
            "expected_sha256": HASH,
        },
        ["path", "old_text", "new_text"],
    )
    add("mkdir", "在当前会话工作区建立目录。", {"path": PATH}, ["path"])
    add(
        "delete",
        "删除当前会话的单个普通文件；不递归删除目录。可传已读哈希。",
        {
            "path": PATH,
            "expected_sha256": HASH,
        },
        ["path"],
    )

    async def command(context, args):
        store = await storage_call(provider.for_instance, context.instance)
        return await sandbox.command(
            store,
            bindings(snapshot, context.config, "skills"),
            args["command"],
            **({"timeout": args["timeout"]} if "timeout" in args else {}),
        )

    registry.register(
        ToolDefinition(
            name="workspace_command",
            description="在独立容器执行 shell，当前会话目录挂载到 /workspace，已绑定技能位于 /skills/<id>。无网络和平台密钥；文件保留，进程不跨调用保留。",
            parameters={
                "type": "object",
                "properties": {
                    "command": S,
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=command,
            timeout=None,
            implementation_id="workspace-posix-tools/v1:command",
            tool_id="workspace_command",
        )
    )
