# Taffy Harness

基于 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的 `s15_integrated_harness/code.py`，按功能拆分为类，并集成 `s17_goal_loop` 的目标循环。保留任务状态流转、锁、重试策略、工具权限和上下文压缩顺序。

## 安装与运行

使用 Python 3.13 或更新版本。在项目根目录进行一次安装：

CLI 现在默认启用 Docker 沙箱。首次运行前需要构建沙箱镜像并配置本地或服务器 Docker context，见[沙箱与阿里云对接说明](sandbox/README.md)。本机尚未安装 Docker 时，启用沙箱的启动会报错；若要继续使用原来的本机执行方式，可以显式设置 `SANDBOX_MODE=disabled`。

```cmd
uv sync
uv tool install --python 3.13 --editable "E:\Xiangx\Pycharm\AgentProjects\claude-harness-main"
uv tool update-shell
```

重新打开 CMD，在任意项目目录执行：

```cmd
cd /d E:\Projects\demo
taffy
```

Taffy 使用启动时的当前目录作为项目根，显示粉色 `>>Taffy`。沙箱模式下文件和 shell 工具在隔离副本 `/workspace` 中执行，新增、修改和删除会自动同步到本地原项目；本地编辑在下一次工具调用前同步到沙箱，无需手动搬运文件或退出前导出。任务、记忆和运行日志保存在本机项目目录。并发修改同一文件时保留双方版本并提示冲突，用 `/sandbox resolve local` 或 `/sandbox resolve remote` 选择冲突版本；其他互不冲突的改动仍会合并。`/sandbox export` 只用于可选快照。无需进入 harness 仓库或激活虚拟环境。

交互启动时，会在后台服务启动前显示一次 Taffy 的像素头像，根据终端大小选择合适的尺寸。头像资源随包安装，从 `harness/assets/startup_avatar.ansi.json` 读取，不依赖当前工作目录。输出重定向、终端空间不足、字符编码不支持或设置 `NO_COLOR` / `TERM=dumb` 时跳过头像。Windows 控制台会先启用 ANSI 颜色支持。

可编辑安装直接使用当前仓库的源码，修改 Python 文件后重新运行 `taffy` 即可生效；源码仓库需要保留在安装时的路径。修改依赖或入口配置后重新执行安装命令。`uv tool` 使用独立的工具环境；`uv.lock` 固定本仓库 `uv sync` 的依赖版本。

### 全局配置与项目覆盖

全局配置位于 `%USERPROFILE%\.taffy\.env`。填写 `ANTHROPIC_API_KEY`、`MODEL_ID`，以及可选的 `ANTHROPIC_BASE_URL`、`FALLBACK_MODEL_ID`。

如果仓库中已有配置好的 `.env`，首次设置时可以在仓库目录用下面的 CMD 命令复制配置；已有全局文件不会被覆盖：

```cmd
if not exist "%USERPROFILE%\.taffy" mkdir "%USERPROFILE%\.taffy"
if exist ".env" if not exist "%USERPROFILE%\.taffy\.env" copy ".env" "%USERPROFILE%\.taffy\.env"
```

某个项目需要不同模型或接口时，在该项目的当前目录放置 `.env`。同名配置项的优先级是：**当前目录 `.env` > 进程环境变量 > 全局 `.env`**。文件按 UTF-8 读取，支持 UTF-8 BOM。不会搜索父目录或源码目录中的 `.env`。

缺少模型 ID 或认证信息时，会提示缺失项及配置文件位置，返回非零退出码，不输出配置值。配置文件不会包含在构建出的安装包中。

### 原有启动方式

在仓库目录仍可使用：

```cmd
uv run python src/code.py
uv run python -m harness
uv run taffy
```

这三种方式和全局 `taffy` 共用同一个入口。PyCharm 可继续使用 `.venv\Scripts\python.exe` 运行 `src/code.py`；工作目录设置为希望 Taffy 操作的项目目录。

## Goal Loop：按完成条件自动续跑

启动 `taffy` 后，在交互提示符中输入：

```text
/goal 修复当前项目，直到 python -m unittest discover -s tests -v 退出码为 0；不要修改测试来绕过失败
```

设置目标会立即开始执行，无需再输入“开始”。每个会话只有一个目标；再次输入 `/goal <新条件>` 会替换旧目标并立即执行。条件最多 4000 个字符，应明确最终状态、验证方式和约束。

主模型准备结束当前轮次时，`GoalController` 使用独立的、没有工具权限的模型调用检查目标。评估器读取目标条件及当前会话中的工具调用、命令结果和后台通知；条件未满足时，把缺失证据反馈到同一个主循环，自动开始下一轮。没有设置目标时，仍按原有方式结束轮次，不发起目标评估请求。

| 命令 | 行为 |
| --- | --- |
| `/goal <完成条件>` | 设置或替换目标，并立即开始工作 |
| `/goal` | 查看目标、耗时、评估次数、主模型输入/输出 token 消耗及最近原因 |
| `/goal clear` | 清除目标，停止后续目标续跑 |

清除目标也支持 `stop`、`off`、`reset`、`none`、`cancel`。查看和清除是本地命令，不调用模型。目标仍处于 active 状态时，普通输入会继续参与该目标；达到上限或评估出错后，可查看 `/goal`，补充信息或输入“继续”，也可替换或清除目标。

后台 shell 或队友仍在工作、或结果尚未送入会话时，目标检查返回 `defer`，保留目标并暂缓评估。既有异步事件循环会在收到结果后唤醒主代理。目标执行期间，shell 工具结果会包含 `exit_code`，后台通知也会携带完整输出，便于评估器检查。原有 shell 和 MCP 审批规则继续生效：后台唤醒的轮次无法交互审批，需要新审批的工具仍会被拒绝，可在交互提示符补充输入后由主线程继续。

可在全局或项目 `.env` 中设置：

```dotenv
# 默认使用 MODEL_ID；可指定更小的评估模型，共用现有 API 客户端和接口
GOAL_EVALUATOR_MODEL_ID=your-evaluator-model-id
# 可选：限制每次输入或事件唤醒的主循环轮数，含工具轮次和目标续跑
MAX_TURNS=20
# 每次输入或事件唤醒最多接受的目标续跑次数，默认 8
GOAL_STOP_HOOK_BLOCK_CAP=8
```

`MAX_TURNS` 默认不设上限，连续目标续跑默认最多 8 次。达到任一上限、主模型恢复失败或评估器返回异常/无效 JSON 时，会显示原因并保留未完成目标；不会标记为完成。评估器确认目标满足时返回 `achieved`；确认已无法达成时返回 `failed`，两者都会结束该目标。设置的整数上限必须大于 0。

目标条件存放在会话中，每轮动态注入系统提示词，并在上下文压缩时保留。评估器只依据当前可见会话判断，验证证据被裁剪后需要主模型重新展示。`runtime.goal.events` 保存 `goal_status` 事件，`GoalController.restore(evaluator, events)` 可供宿主恢复未完成的条件，计时、评估次数和 token 基线重新开始；CLI 会持久化主会话，并通过 `/session resume <id>` 显式恢复完整目标状态和用量基线。

实现来源：[s17_goal_loop/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s17_goal_loop/code.py)。本项目将示例的异步评估接口适配为现有同步主循环，并复用后台任务与团队消息机制。

## 子 agent：同步任务与后台队友

`task(description)` 创建独立对话，同步执行子任务并返回最后的文本；主 agent 等待返回后继续。`spawn_teammate(name, role, prompt, task_id?, require_plan?)` 启动后台线程，立即返回；队友通过消息总线回传结果，完成当前执行后进入 idle，等待消息或领取下一项任务。主 agent 在创建队友后结束当前轮次，由团队事件唤醒；它仍然可以在其他轮次直接读写代码、执行验证。

两种入口共用 `AgentWorker` 的模型与工具循环，但保持各自的工具权限和任务目录规则。临时 subagent 不继承主 agent 的历史，也不能再创建 agent；队友保留自己的对话、任务分配、计划审批和关闭协议。模型结束当前执行不代表任务板任务已完成，队友仍需调用 `complete_task`。

```dotenv
# 后台队友数量上限，含 working、idle、waiting_approval、stopping
MAX_TEAMMATES=3
# 每次 task 或队友唤醒最多执行的模型轮数；idle 等待不计入
WORKER_MAX_TURNS=30
```

两个参数必须是正整数，可在全局、进程或项目配置中覆盖。队友达到数量上限时立即拒绝新建请求，不认领任务、不创建线程；应复用已有队友或关闭空闲队友。同步 `task` 不占后台队友名额。线程启动失败或退出时释放名额，未完成任务退回任务板。达到执行轮数上限、模型输出被截断或执行异常时明确报告未完成；不会将中间文字作为成功摘要，截断响应中的工具调用不会执行。

所有子 agent 在请求模型前执行上下文预算和历史压缩，保留当前任务、最新协调消息以及工具调用与结果的配对。超过现有 30,000 字符阈值的工具输出、最终文本和团队消息正文保存到 `.task_outputs/tool-results`，传递预览与完整内容路径，可通过文件工具按需读取。worker 产物使用独立执行 ID 命名，避免相同工具调用 ID 覆盖其他会话的结果；归档历史保存到 `.transcripts`。上下文阈值沿用序列化字符估算，不是精确 token 预算。

本次提供数量、轮数和上下文保护，不提供任务排队、全局 token 预算、执行中暂停恢复或强制取消。关闭队友仍是现有的协作式关闭，后台 shell 仍遵守现有审批限制。

## 功能拆分

| 文件 | 类 | 职责 |
| --- | --- | --- |
| `src/code.py` | — | 启动入口 |
| `src/harness/runtime.py` | `HarnessRuntime` | 创建对象、连接依赖、启动 CLI |
| `src/harness/config.py` | `HarnessConfig` | 环境变量、工作目录和原有运行参数 |
| `src/harness/cli.py` | `HarnessCLI` | 输入、退出和交互会话 |
| `src/harness/console.py` | `ConsoleBroker` | 串行输入与后台终端输出 |
| `src/harness/models.py` | `Task`、`ProtocolState`、`CronJob` | 原始数据记录 |
| `src/harness/tasks.py` | `TaskSystem` | 任务持久化、依赖、领取和分配租约 |
| `src/harness/worktrees.py` | `WorktreeManager` | 任务绑定的 Git 工作树 |
| `src/harness/skills.py` | `SkillLoader` | 扫描和加载 `skills/*/SKILL.md` |
| `src/harness/prompts.py` | `PromptBuilder` | 动态系统提示词 |
| `src/harness/tools.py` | `BasicTools` | 文件、shell、进程清理和 TODO |
| `src/harness/sandbox.py` | `DockerSandbox` | 本地或 SSH Docker context、资源隔离、工具执行与自动同步 |
| `src/harness/sandbox_sync.py` | `WorkspaceSync` | 双向版本比较、冲突保存、历史备份与原项目回写 |
| `src/harness/bus.py` | `MessageBus` | 文件邮箱和消息等待 |
| `src/harness/team.py` | `TeamManager` | 队友线程、计划审批和关闭协议 |
| `src/harness/hooks.py` | `HookManager` | 权限、日志和生命周期 hook |
| `src/harness/subagents.py` | `SubagentRunner` | 同步临时子代理适配器 |
| `src/harness/worker.py` | `AgentWorker`、`WorkerResult` | 共享子 agent 循环、独立对话、上下文预算及结束状态 |
| `src/harness/context.py` | `ContextManager` | 工具结果预算、压缩和记忆上下文 |
| `src/harness/memory.py` | `MemorySystem` | 记忆存储、召回、提取和整理 |
| `src/harness/recovery.py` | `RecoveryManager`、`RecoveryState` | 重试、备用模型和恢复状态 |
| `src/harness/background.py` | `BackgroundTaskManager` | 后台 shell 与结果通知 |
| `src/harness/scheduler.py` | `CronScheduler` | 定时任务、持久化和交付恢复 |
| `src/harness/mcp.py` | `MCPManager`、`MCPClient` | 原有 `docs` / `deploy` MCP 模拟服务 |
| `src/harness/registry.py` | `ToolRegistry` | 内置工具定义、绑定和 MCP 工具合并 |
| `src/harness/agent.py` | `AgentLoop` | 模型与工具循环、异步事件处理 |
| `src/harness/goal.py` | `GoalController`、`PromptGoalEvaluator` | 会话目标、独立完成评估、停止或续跑决策 |

每个功能类持有自己的状态，跨功能调用通过 `HarnessRuntime` 显式连接。原来的全局字典、计数器和锁现在属于对应对象；创建两个 runtime 时，它们的内存状态独立。文件数据仍按工作目录共享。

导入包不会创建 API 客户端或启动服务。环境读取和客户端创建在构造 `HarnessRuntime()` 时执行；沙箱预检、调度线程和 shell 清理在运行 CLI 时启动。原有审批流程继续生效；启用沙箱后，文件与 shell 工具改为操作隔离副本，宿主 worktree 创建和移除会被拒绝。

安装本包后，可以在代码中使用：

```python
from harness import HarnessRuntime

runtime = HarnessRuntime()
task = runtime.tasks.create_task("Inspect project", "Read the existing files")
print(runtime.tasks.run_list_tasks())
runtime.run()
```

## 记忆模块来源

移植文件原先动态导入缺失的 `s09_memory/code.py`。现在将上游同一仓库中的记忆存储、召回、提取、整理函数封装为 `MemorySystem`，复用主程序的配置和客户端。

来源：[s09_memory/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/main/s09_memory/code.py)，Git blob 为 `f9baf803e5f6bf4758faa8a0420f6405e1606ec2`。本地原始 s15 文件与取回的上游文件内容一致，Git blob 为 `e9549b2d09ec424dfb9c4ba84d8a1bef17f8e75a`。上游 MIT 许可保留在 `LICENSE`。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

测试保留了原有 33 项功能回归场景，并增加全局配置、项目覆盖、配置校验、UTF-8 BOM、入口导入与旧脚本兼容测试。它们使用模拟模型客户端和临时工作目录，覆盖任务依赖与并发领取、分配释放、团队协议和线程生命周期、文件边界、shell、权限 hook、技能、MCP、记忆回滚、上下文压缩、重试、定时任务恢复、后台结果、代理循环、CLI 和真实的临时 Git 工作树。

Goal Loop 测试另外覆盖独立评估调用、自动续跑、成功与失败、无效评估响应、循环上限、后台结果唤醒、队友等待、目标命令、状态恢复、压缩后保留目标和配置校验，不发送真实 API 请求。

沙箱测试覆盖凭据排除、复制大小限制、路径边界、失败时禁止本机执行、文件工具分发、超时、清理、归档回读、自动双向同步、二进制文件、删除和重命名、冲突处理、历史备份以及路径和链接校验。真实 Docker 测试需构建镜像并设置 `TAFFY_DOCKER_INTEGRATION=1`；缺少 Docker 时跳过，服务器连接验证单独执行。

最初进行类拆分时另对比了原始 s15 与 s09 的 174 个函数，去除对象引用、`self` 参数和 docstring 缩进差异后，AST 一致。全局命令启动验证不发送模型请求；真实 API 对话需要有效的配置。

### Skill 状态与派发

`load_skill(name, offset=0, limit=12000)` 为当前 agent 启用技能并保存原文。每次请求均在历史之外重新注入，因此压缩不会删除启用状态。
可选技能的指导预算为字符数，默认最多 12000，且不超过 `CONTEXT_LIMIT` 的四分之一；可通过 `config.SKILL_GUIDANCE_BUDGET` 调整。
可选技能预算先保留其重载入口，再按启用顺序附加正文。截断部分通过 offset/limit 分页加载；预算不足以容纳入口时拒绝请求。
此预算不代表整个 API 请求的 token 限额。

`task(description="实现功能", required_skills=["agent-builder"])` 和 `spawn_teammate(..., required_skills=["agent-builder"])` 显式绑定必要技能。
缺失技能会阻止派发。每个子代理独立保存状态；可选技能通过自身的 load_skill 按需加载，不会继承主代理的启用列表。
持久队友在后续工作中保留自身技能；当前状态仅在进程生命周期内保存。

技能不能授予工具权限，操作仍受现有 Hook、工作区限制、计划审批和异步 shell 限制约束。可验证行为通过回归测试检查。

必要技能在派发时直接注册到子 agent 的上下文状态中，每次模型请求均完整注入原始正文，不受上述技能指导字符预算限制，也不要求子 agent 主动调用 load_skill。
技能指导预算及分页重载仅适用于按需加载的可选技能。对话压缩不改变已注册的必要技能。


## Worker 心跳租约与工具幂等

队友在模型调用前后、工具执行边界和空闲等待期间续租，默认
`WORKER_LEASE_SECONDS=300` 秒。独立监控线程每秒检查租约，不依赖主代理的
模型请求完成。租约过期后释放未完成任务、通知 Lead，并使旧 worker 的工具
执行和任务状态更新失效；旧线程晚退出也不能释放同名新 worker 的任务。
任务会回到 `pending`，由 Lead 或其他队友接手，不自动创建替代线程或恢复对话。
租约是进度检测，不会由独立心跳线程替卡住的模型调用无限续期。

已开始执行的工具持有租约保护，监控器不会在工具运行中交接任务；返回后续租。
这避免旧工具和新 worker 同时修改工作区，但永久卡死的工具仍需宿主终止或
工具自身超时，不能靠 Python 强行杀线程。任务租约同时写入 `.tasks`；重启后
只回收已过期的带租约任务，未到期任务等待期限结束。旧格式无租约任务不自动回收。
此机制要求同一项目只运行一个 Harness 宿主，不提供跨宿主分布式租约。

工具调用日志保存在 `.tasks/tool_calls.sqlite3`，不上传沙箱。主代理、普通子代理
和队友的常规工具调用都会先记录调用意图，再保存完整结果；同一执行中的相同
工具调用 ID 重放已有结果。同一个 ID 携带不同工具或参数会返回
`IDEMPOTENCY_CONFLICT`，不会执行。

工具还支持可选字符串参数 `idempotency_key`，由执行层移除，不传给实际工具。
在同一 Task 内重试同一个逻辑操作时复用该键，新 worker 即使获得不同的模型
调用 ID，也能读取原结果；新的操作必须使用新键。无任务绑定时键仅在当前
worker/主代理执行范围内有效。不要按参数相同自动去重：同一个命令在不同时间
可能是有意再次执行。后台 shell 的日志只覆盖“启动”这个动作，不代表后台作业
已经完成，也不会恢复丢失的后台线程。

如果调用还在执行，或进程在产生副作用后、保存结果前退出，日志保留 `started`，
重复调用返回 `TOOL_OUTCOME_UNKNOWN`，禁止直接重放。Agent 必须检查文件或外部
系统状态，再决定是否发起新的操作；不要通过更换键盲目重试。这是本地调用去重，
不保证外部系统 exactly-once，订单等操作仍需要服务端幂等支持。无需步骤检查点。


### 主会话恢复与故障降级

主会话检查点保存在工作区 `.sessions/sessions.sqlite3`，自动保留当前和上一份快照。默认启动新会话；同一工作区只允许一个 CLI 宿主运行。

- `/session list`：列出已保存会话。
- `/session status`：显示当前会话 ID、暂停状态和断点阶段。
- `/session resume <id>`：加载指定会话并展示恢复报告；有活跃子代理或后台任务时不能切换。
- `/session continue`：继续未完成轮次，或将已完成的恢复会话切回可接受新请求的状态。

恢复后，定时任务和团队消息可以排队，但等待继续期间不会触发模型和工具。恢复包括历史、当前请求、主执行 ID、目标及其 token 基线、技能原文、todos、累计 token 和循环限制。动态提示词、连接和线程会重新构建。子代理与后台进程不重启，已保存产物保留，任务归属按现有租约回收；恢复报告会标出中断任务。

工具完成结果从 `.tasks/tool_calls.sqlite3` 回填；结果未知的调用返回 `TOOL_OUTCOME_UNKNOWN`，需要核查实际效果，不会盲目重放。此机制不提供外部系统的 exactly-once 事务保证。模型响应、每个工具结果及事件接收均经过检查点；模型响应保存前崩溃可能重新请求模型，但尚不会执行该响应中的工具。

网络、超时、429 和可重试服务异常采用有限退避；连续过载达到阈值或主模型预算耗尽后，自动切到 `FALLBACK_MODEL_ID`。备用模型重试预算也有限，同一用户轮次保持备用模型，新请求重新从主模型开始。SDK 自动重试关闭，避免叠加请求。认证、权限、普通参数错误直接暂停。未完成轮次恢复时不会重置预算；预算耗尽后需排查故障并启动新请求/会话，而不是通过重启绕过限制。

上下文超限只尝试一次反应式压缩。摘要服务失败时保留当前请求和最近完整工具轮次，目标及技能仍由系统提示词注入；仍超预算则暂停。快照损坏时回退上一份有效快照并报告；版本或工作区不兼容时拒绝恢复。持久化失败会停止下一次模型/工具执行，错误会说明检查点不可用，原数据保留。旧版本没有保存的完整历史无法恢复。
