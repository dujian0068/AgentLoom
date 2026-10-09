# 系统工具、共享工作区与沙箱 v0.1

> **专题参考。** 当前跨模块架构与边界见[总体技术设计](technical-design.md)、[Runtime 详细设计](runtime-design.md)、[平台/API/数据](platform-api-data.md)和[部署运维](deployment-operations.md)，统一核对基线为 `9941e31`（2026-10-09）。本文保留专题契约和阶段验证。

实现说明与调研记录 · 2026-10-09。

AgentLoom 的文件能力通过统一工具事件总线执行，并由独立工作区模块管理。新任务以空间、应用、会话为文件作用域；同一会话的后续提问可以继续使用之前生成的文件。部署共享存储时，选择 `shared_posix` 后端并将 NFS/NAS 挂载到每个工作节点。

**本轮完成的是可配置的共享 POSIX 存储适配、工具、安全路径边界和沙箱集成。没有自动采购、挂载或部署 NFS，也没有将现有单进程调度改为分布式队列。**

## 1. 对比 Claude Code 与 Codex

Claude Code 的官方工具参考列出了 `Read`、`Write`、`Edit`、`Bash`、`Glob`、`Grep` 等能力。工具可用性受版本、平台和设置影响；当前文档特别说明 `Glob`、`Grep` 在 macOS、Linux、WSL 默认不提供，不能将其理解为所有环境均以独立工具开放。本文比较能力，不要求完全复制工具名称。[Claude Code 工具参考](https://code.claude.com/docs/en/tools-reference)

Codex 对比基于本地源码版本 `a933dd77dbe101d7bd746ea3c7d1f8174eca4a05`：该版本通过 Shell 使用 `rg`、`rg --files` 等命令搜索，通过 `exec_command` / `write_stdin` 执行或继续命令，并提供 `apply_patch`、`view_image`。这是一份特定版本的源码观察，不代表所有 Codex 产品形态的固定工具集。[Shell 契约](https://github.com/openai/codex/blob/a933dd77dbe101d7bd746ea3c7d1f8174eca4a05/codex-rs/core/src/tools/handlers/shell_spec.rs)、[Patch 契约](https://github.com/openai/codex/blob/a933dd77dbe101d7bd746ea3c7d1f8174eca4a05/codex-rs/core/src/tools/handlers/apply_patch_spec.rs)、[图片工具](https://github.com/openai/codex/blob/a933dd77dbe101d7bd746ea3c7d1f8174eca4a05/codex-rs/core/src/tools/handlers/view_image_spec.rs)

| 能力                         | Claude Code / Codex 的相关能力 | AgentLoom 本轮实现                                        |
| ---------------------------- | ------------------------------ | --------------------------------------------------------- |
| 列目录、文件信息             | 文件搜索或 Shell               | `workspace_list`、`workspace_stat`                        |
| 按文件名搜索                 | Glob 或 Shell 文件搜索         | `workspace_glob`                                          |
| 按内容搜索                   | Grep 或 Shell `rg`             | `workspace_grep`，路径、行号、匹配及截断信息              |
| 读文本                       | Read 或 Shell                  | `workspace_read`，按行范围读取并返回 SHA-256              |
| 写文件                       | Write、Patch 或 Shell          | `workspace_write`，原子替换及可选版本条件                 |
| 局部编辑                     | Edit / `apply_patch`           | `workspace_edit`，精确文本替换，拒绝不明确的多处匹配      |
| 新建目录、删除文件           | Shell / Patch                  | `workspace_mkdir`、`workspace_delete`；不递归删除目录     |
| 执行命令                     | Bash / `exec_command`          | `workspace_command`，一次性 Docker 沙箱；不要求绑定 Skill |
| 持续进程、终端交互           | 后台任务 / `write_stdin`       | 尚未实现，需要进程会话、归属节点、输出游标及终止协议      |
| 图片、Notebook、语义代码查询 | 多模态读取、Notebook、LSP 等   | 尚未实现专用系统工具                                      |
| 原生多文件 Patch             | `apply_patch`                  | 尚未实现原生 Patch 格式；当前精确编辑覆盖常规文本修改     |
| 网络搜索、网页读取           | 相应网络工具                   | 沿用可绑定的外部 MCP；本轮不扩大沙箱网络权限              |

Skill 加载、MCP 调用、知识检索、计划维护与子 Agent 委派已有独立处理器。本轮补充基础文件与命令能力，没有把这些能力塞进 Loop。

## 2. 模块与事件链路

```text
Loop
  → EventBus.request("tool.execute", ToolRequest)
  → ToolRuntime：工具解析、权限、参数校验、Hooks、调用记录
  → 文件 / 命令 Handler
  → WorkspaceProvider：可信作用域解析
  → WorkspaceStorage：可替换存储接口
  → PosixWorkspaceStore：本地目录或当前节点上的共享挂载
  → ToolOutcome → Loop → 下一轮模型决策
```

写操作是一条有唯一执行者的请求/响应消息。`request.started/completed/failed` 等通知可以交给多个观察者；观察者不能重复执行写入，也不能替代请求结果。这沿用[运行时事件总线](runtime-event-bus.md)的单处理器契约。

| 模块                         | 责任                                                        |
| ---------------------------- | ----------------------------------------------------------- |
| `engine.py`                  | 保存通用调用进度、发送请求、接收结果；不判断具体文件操作    |
| `tool_runtime.py`            | 注册、校验、授权、工具 Hooks 和未知结果恢复边界             |
| `handlers/filesystem.py`     | 模型可见工具定义及参数到存储方法的适配                      |
| `workspace.py`               | `WorkspaceStorage` 接口、`WorkspaceProvider` 和异步调用适配 |
| `workspace_store.py`         | 有界读写、遍历、正则搜索、原子替换、版本条件、共享锁        |
| `sandbox.py`                 | 命令进程隔离、单作用域挂载、资源限制和清理                  |
| API `services/workspaces.py` | 从已鉴权的运行记录解析作用域，固定存储卷，列出和下载产物    |
| `run_workspaces`             | 保存每个新运行的逻辑存储绑定；由编号迁移 `005` 建立         |

替换存储实现通过 `WorkspaceProvider(..., store_factory=...)` 注入，不需要修改 Loop。文件工具仍经过已有的 `tool.before` / `tool.after` Hooks；Hook 不能替换宿主提供的空间、应用、会话或根目录。

## 3. 为什么选择共享 POSIX 文件系统

| 方案                 | 对当前需求的适配                                                            | 本轮选择                                                     |
| -------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------ |
| NFSv4.1 / 托管 NAS   | 多节点挂载同一文件树，适配现有读写、目录、原子替换与 Shell 工作目录         | 首选；由部署环境提供挂载                                     |
| CephFS               | 提供 POSIX 文件系统及独立元数据服务；适合已经维护 Ceph 的团队               | 可作为同一适配器的挂载来源，不为测试阶段新建 Ceph 集群       |
| S3 / S3 兼容对象存储 | 对象与版本管理适合交付产物、备份、归档；不是本接口直接调用的 POSIX 文件目录 | 后续可添加 ArtifactStore；本轮不经 FUSE 把对象存储当工作目录 |
| 节点本地目录         | 测试方便，但其他节点看不到同一文件                                          | 默认开发后端，不能当成共享部署                               |

这是根据当前文件操作与命令执行需求做出的选型。[CephFS 官方说明](https://docs.ceph.com/en/latest/cephfs/)将其定义为 POSIX 文件系统；[S3 官方说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)说明对象、存储桶与版本管理语义。对象存储适配属于后续模块，不宣称它和 POSIX 的文件锁、目录及修改语义等价。

NFS 部署要验证实际客户端、服务端与挂载参数支持跨节点锁。以 EFS 为例，官方说明支持 NFSv4 文件锁，采用 close-to-open 一致性，锁为 advisory lock；未遵守同一锁协议的外部写入仍然可能并发发生。[EFS 一致性与文件锁](https://docs.aws.amazon.com/en_en/efs/latest/ug/features.html)

## 4. 作用域和文件布局

```text
<共享挂载根>/
  .agentloom-volume
  .agentloom-locks/<session-scope-hash>/...
  spaces/<space_id>/agents/<agent_id>/sessions/<session_id>/
    main/                                同会话主 Agent 文件
    runs/<run_id>/children/<instance>/    该轮子 Agent 文件
```

- 空间、应用、会话、运行 ID 从数据库中已授权的运行记录取得。模型参数只接受相对文件路径。
- 同一会话的不同提问共享 `main/`，不同空间、应用和会话使用不同目录。
- 子 Agent 与主 Agent 的可挂载目录分开；子实例还包含 Run ID，避免下一轮 `sub-1` 继承上一轮同名实例的文件。
- 主 Agent 的 `subagents` 顶层路径保留给产物 API 的展示命名，不能用文件工具创建。
- 下载 API 将本轮子任务文件显示为 `subagents/<instance>/<path>`。这种展示不等于把子目录挂入主 Agent 的沙箱。
- 已发布 Agent 仍是空间共享资源；运行状态、事件、文件、取消和恢复只允许发起用户访问，避免通过工具结果事件绕过文件权限。

主工作区是会话的**当前文件视图**。旧 Run 的产物入口也会看到当前主工作区内容，不代表不可变的历史产物快照。若需要文件审计历史，应另行实现 ArtifactStore 或文件版本存档。

新运行在入队事务内固定 `backend`、`volume_id`、布局版本与作用域。共享卷标识不包含绝对挂载路径，因此节点 A 使用 `/mnt/loom`、节点 B 使用 `/srv/loom` 时，仍可解析同一逻辑工作区；两处必须实际挂载同一个卷。一个会话不能中途切换卷。

旧运行没有 `run_workspaces` 记录时仍使用 `data/runs/<run_id>`，恢复沿用旧工具清单和检查点契约；不自动搬移旧文件，也不把旧检查点静默升级成新作用域。

## 5. 文件操作与并发边界

文件路径由 `dir_fd` 逐级打开，使用 `O_NOFOLLOW` 拒绝符号链接；文件打开后再次检查实际文件类型。绝对路径、`..`、NUL、反斜线、非法组件、硬链接和特殊文件不进入文本读写路径。新建目录权限为 `0700`，文件为 `0600`，不再使用 `0777/0666`。

写入先创建同目录临时文件，保存内容后原子替换目标。`write/edit/delete` 支持 `expected_sha256`：指定摘要时仅修改仍匹配的版本，空字符串表示目标必须不存在。摘要不匹配会失败，模型应重新读取后判断，而不是覆盖别人的新内容。编辑默认要求旧文本恰好匹配一次，明确设置 `replace_all` 才替换全部匹配。

平台写工具与命令执行使用作用域共享锁；锁文件位于沙箱挂载范围外，锁标识按逻辑作用域生成，跨节点不同挂载路径仍一致。共享文件系统必须实际支持这些 POSIX advisory locks。宿主管理员或其他不遵守协议的客户端写入不在该保证内。

默认文件上限为 2 MB；文本读取按行返回，遍历、搜索、匹配输出均有限额，并显式标记截断。正则匹配使用带超时的 `regex` 实现，防止一个表达式长时间阻塞执行。下载 API 同样最多返回 2 MB；不会在安全检查后用 `FileResponse` 重新打开用户路径。

已经记录成功结果的工具恢复时复用结果；结果未知的写操作不自动重放。共享存储与原子替换不构成跨数据库、文件系统、模型和外部工具的 exactly-once 事务。

## 6. 沙箱与共享存储如何配合

共享存储保存工作区文件。沙箱进程在当前 Worker 上运行，容器销毁后工作区文件继续保留。

每次命令只将已授权的当前工作区挂入 `/workspace`，绑定的 Skill 目录挂入只读 `/skills/<skill_id>`。平台数据库、密钥、其他会话目录、整个共享根目录和 Docker socket 不会作为任务容器挂载。容器内固定从 `/workspace` 执行。

当前实现：无网络、只读容器根文件系统、可写临时 `/tmp`、删除 capabilities、`no-new-privileges`、CPU/内存/进程数限制；平台和沙箱使用匹配的非 root UID/GID，命令设置 `umask 077`。命令默认 60 秒，允许 1–120 秒，限制输出量，取消和超时需要等待容器清理。启动命令前保存持久执行标记；只有确认容器已清理才移除。Worker 崩溃或无法确认清理时，后续写入和命令执行被阻止，避免遗留容器与恢复任务并发修改文件，需管理员核对后解除隔离。

隔离标记保存在对应锁目录的 `.quarantine` 文件中。恢复操作由管理员完成，不提供模型可调用的“解除隔离”工具：

1. 暂停该工作区的新任务和恢复请求，保留标记、运行记录及日志。
2. 检查标记中的 `container_name`，在原执行 Worker 的 Docker daemon 上核对该容器；若 Worker 归属不明确，应核对所有可能节点。标记不完整时结合日志调查，不能猜测可以清理。
3. 确认原 Worker、Docker CLI 和未完成的启动请求已停止产生新容器；停止并清理仍存在的目标容器。取消或杀死 CLI 后单次返回 `No such container`，不能证明此前未知的启动请求不会稍后生效，因此实现不会据此自动移除标记。
4. 只有确认没有遗留或待启动的执行者后，管理员在保持任务停用并协调对应文件锁的情况下，解除该工作区的确切隔离标记，再恢复任务。不要删除锁文件、其他作用域标记或整个工作区目录。

仅删除标记、等待标记过期或直接重试命令均不是安全的恢复流程。

Docker bind mount 绑定的是 Docker daemon 所在节点的路径，因此本实现限定本机 Unix socket。Docker 官方建议显式 `--mount`，源目录不存在时默认报错，可避免 `-v` 自动创建缺失目录。[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

`AGENT_LOOM_SANDBOX_RUNTIME=runsc` 可以选用已安装并配置到 Docker 的 gVisor runtime；本项目不会自动安装它。普通容器和 gVisor 的隔离强度、系统调用兼容性不同，部署时需验证实际 Skill。[gVisor Docker 配置](https://gvisor.dev/docs/user_guide/quick_start/docker/)

不要把多个节点的 Docker `data-root` 指向同一个 NFS 目录。Docker 官方要求各 daemon 使用独立目录；这里共享的仅为任务工作区。[Docker daemon 数据目录](https://docs.docker.com/engine/daemon)

## 7. 部署配置

本地开发默认：

```dotenv
AGENT_LOOM_WORKSPACE_BACKEND=local
# 不填写时使用 AGENT_LOOM_DATA/workspaces
# AGENT_LOOM_WORKSPACE_ROOT=/absolute/local/workspaces
```

共享部署示例：

```dotenv
AGENT_LOOM_WORKSPACE_BACKEND=shared_posix
AGENT_LOOM_WORKSPACE_ROOT=/mnt/agentloom-workspaces
AGENT_LOOM_WORKSPACE_VOLUME_ID=agentloom-team-volume-v1
AGENT_LOOM_SANDBOX_IMAGE=python:3.12-slim
# AGENT_LOOM_SANDBOX_RUNTIME=runsc
# AGENT_LOOM_DOCKER_HOST=unix:///var/run/docker.sock
```

1. 由服务器管理员创建并挂载共享文件系统，确保 API/Worker 与本机 Docker daemon 都可访问该挂载路径。
2. **确认已挂载共享卷后**，在卷根创建 `.agentloom-volume` 普通文本文件，内容恰为配置的卷标识。不同节点读取同一个文件。应用不会自动创建卷标记或缺失的共享根。
3. 各节点使用一致的非 root 服务 UID/GID，并确保该身份可创建会话目录和锁目录。NFS 身份/权限配置仍由共享存储管理，不用开放全局写权限解决权限问题。[EFS UID/GID 与文件权限](https://docs.aws.amazon.com/efs/latest/ug/user-and-group-permissions.html)
4. 提前准备可信沙箱镜像。运行时使用 `--pull never`，不会在执行用户命令时自动下载镜像。
5. 启动升级后的平台以执行编号迁移 `005`。数据库检查点、`run_workspaces` 绑定和共享文件要纳入一致的备份流程；本地开发也要备份 `data/workspaces/`，旧 `data/runs/` 继续保留。
6. 上线前在两台实际节点验证文件可见性、锁互斥、原子替换及容器只挂载指定子目录，再接入生产流量。

部署检查入口为 `deploy/check_workspace.py`，仅读取显式环境配置，不读取项目 `.env` 或连接数据库。默认校验存储卷配置；可选探测使用独立临时命名空间验证文件操作。检查脚本不能替代两台真实节点的文件锁和容器联调。

```sh
# 默认只读，检查已有共享根及卷标记。
.venv/bin/python deploy/check_workspace.py \
  --root /mnt/agentloom-workspaces --volume-id agentloom-team-volume-v1

# 显式允许在随机临时目录读写、原子替换及检查本机 advisory lock；完成后清理。
.venv/bin/python deploy/check_workspace.py \
  --root /mnt/agentloom-workspaces --volume-id agentloom-team-volume-v1 --probe

# 本地目录只读检查，不宣称已连接共享卷。
.venv/bin/python deploy/check_workspace.py --backend local --root /absolute/local/workspaces
```

也可事先显式导出 `AGENT_LOOM_WORKSPACE_BACKEND`、`AGENT_LOOM_WORKSPACE_ROOT`、`AGENT_LOOM_WORKSPACE_VOLUME_ID`。脚本不会为省略的根目录选择默认路径，也不会创建缺失的挂载根。输出中的 `single_node` 和 `advisory_lock_local` 只代表本机检查结果，不证明目录实际来自 NFS，也不证明另一台节点能够正确互斥。

共享模式在创建任务、恢复任务、产物读取和实例文件访问前检查根目录及卷标记；缺失或不匹配时停止，不默认回落到本地目录。已持有的宿主挂载若被有特权管理员强制卸载或替换，仍需要部署侧协调与停机保护，不能仅依靠应用层检查承诺任意挂载变更下的原子隔离。

NFS 使用 hard 挂载时，网络断开可能使内核文件 I/O 长时间等待；Linux 的 NFS 手册说明 hard 模式会持续重试请求。[NFS 挂载选项](https://man7.org/linux/man-pages/man5/nfs.5.html) 应用将文件操作放入线程并等待必要清理，但 Python 取消无法强制终止阻塞的内核系统调用，因此文件大小、正则匹配和应用调用预算不等于严格的文件 I/O 超时。挂载策略、失联节点处置和运维恢复需要在真实环境验证；检查脚本也有相同的底层阻塞边界。

## 8. 当前验证范围和后续模块

自动化测试覆盖新系统工具、路径与链接拒绝、版本冲突、范围隔离、同会话跨提问文件连续性、卷标记失败关闭、不同节点路径下的卷标识、旧运行兼容，以及 API 用户权限。沙箱有 Docker 参数与清理流程的替身测试；当前开发环境未完成真实 Docker/gVisor 和远程 NFS 多节点联调。

事件总线和运行任务注册表仍在单进程内。若下一轮需要真正的跨节点任务转移，还应实现持久队列、Worker 租约与 fencing、命令进程归属、统一取消、节点失联恢复和安全的发布资源分发；共享文件适配不能代替这些模块。Skill 资源、平台密钥和数据库连接也有各自的分发与授权要求，不因工作区共享而自动复制。

后续可独立增加 ArtifactStore（不可变产物及归档）、PTY/ProcessService、图片输入、原生 Patch、存储配额和生命周期清理。这些模块沿用相同的工具事件总线和可信作用域边界。
