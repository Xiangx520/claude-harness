# Docker 沙箱与服务器对接

Taffy 的终端界面、模型请求、任务调度和审批仍在本地运行。`bash`、`read_file`、`write_file`、`edit_file`、`glob` 共用 Docker 内的 `/workspace`，主代理、子代理、队友和后台 shell 都走同一个执行入口。

CLI 默认 `SANDBOX_MODE=docker`，在开始会话前创建沙箱。Docker 缺失、镜像未构建、SSH 连接失败或复制失败时启动报错；不会退回本机 shell。程序化创建 `HarnessConfig(...)` 默认仍为 `disabled`，需要显式传入 `sandbox_mode="docker"`。旧行为可通过 `.env` 中的 `SANDBOX_MODE=disabled` 恢复。启动界面不再打印命令帮助和沙箱状态，需要时使用 `/sandbox status` 查询。

## 当前本机的使用方式

本机已经配置 `taffy-aliyun` SSH 别名和 Docker context，专用密钥保存在 `%USERPROFILE%\.ssh\taffy_aliyun_ed25519`，主机指纹已通过阿里云控制台输出核对。Docker CLI 位于 `%USERPROFILE%\.taffy\bin\docker.exe`，不依赖本机 Docker daemon。

全局 `%USERPROFILE%\.taffy\.env` 和当前仓库 `.env` 的沙箱项已切换到远端配置，模型和认证项保留。修改前的环境配置备份为对应目录的 `.env.before-sandbox`，SSH 配置备份为 `%USERPROFILE%\.ssh\config.before-taffy`。

在希望操作的项目目录运行 `taffy`，再输入 `/sandbox status`。应看到 `context=taffy-aliyun`、`network=none`、`sync=automatic`。当前限额为 0.5 CPU、512 MiB 内存、64 个进程、128 MiB 工作区和 120 秒命令时限。模型在远程副本执行，但文件修改会自动回到本地原项目，不需要手动导出。

真实远程容器测试检查 UID 1000、只读根文件系统、资源限额、断网、密钥和 Docker socket 不可见、文件工具和 shell 共用副本、自动回写、双向同步、冲突处理、路径越界拒绝、超时、可选导出以及容器删除。测试不发送模型对话请求。

## 本地准备

安装可运行 Linux 容器的 Docker。然后在本仓库根目录执行：

```powershell
docker --context default build -t taffy-sandbox:1 sandbox
```

全局 `%USERPROFILE%\.taffy\.env` 或当前项目 `.env` 加入：

```dotenv
SANDBOX_MODE=docker
SANDBOX_IMAGE=taffy-sandbox:1
SANDBOX_DOCKER_CONTEXT=default
SANDBOX_CPUS=1
SANDBOX_MEMORY_MB=1024
SANDBOX_PIDS=128
SANDBOX_TIMEOUT_SECONDS=120
SANDBOX_WORKSPACE_MB=512
SANDBOX_TRANSFER_MB=128
```

主程序仍使用原有 `ANTHROPIC_API_KEY` 和 `MODEL_ID` 配置。它们不会传入容器。镜像提供 Python 3.13、Git、ripgrep 和 GNU coreutils；项目依赖需要预装到自定义镜像。运行容器默认断网，因此不能在会话中下载依赖。默认镜像并未安装本项目的 `anthropic`、`python-dotenv` 和 `pyyaml`，若要在沙箱内运行本项目测试，需要先在自定义镜像中安装它们。

## 项目文件与自动同步

启动时复制当前项目的普通文件。跳过符号链接、Windows junction，以及 `.git`、`.env*`、`.ssh`、`.aws`、常见私钥文件、本地依赖目录和 Taffy 的运行状态。排除规则是文件名规则，无法识别任意源文件内写死的密钥；上传项目副本前应把这类内容移出项目。Git 历史不会上传，模型不能创建或移除本机 Git worktree；已绑定的 worktree 按其 cwd 复制为独立沙箱，不与主工作区实时同步。

文件写入、编辑及 shell 命令结束后，Taffy 自动把项目变化应用到本机原项目，包括普通文件的新增、修改、删除、重命名和空目录。即使命令退出码非零或超时，已产生的正常文件变化也会同步。每次工具调用前检查本地编辑；需要时合并并上传到容器，因此你可以继续使用本地编辑器。这里是工具调用边界上的同步，不是后台实时文件监视。

同步比较上一次双方一致的版本、本地版本和容器版本。只有一方修改时自动采用该修改；双方修改成相同内容时直接接受；双方独立修改不同文件时自动合并。双方对同一路径做了不同修改（包括一方删除、另一方编辑）时暂停本次同步及后续模型操作，不覆盖本地文件。远程未同步的变化自动保存到该工作区的 `.sandbox_conflicts/<会话>/<编号>/remote/`，删除和目录变化记录在 `changes.json`。这不是逐行文本合并。

正常回写直接更新项目。`.sandbox_history/<会话>/<编号>/before/` 只保留被改写或删除文件的旧版本，`changes.json` 记录新增和删除，便于恢复；不会再保存一份完整结果等待你搬运。这些历史记录不会上传到沙箱，暂不自动清理。

宿主生成的工具结果和历史归档通过专用内部入口复制到沙箱，便于上下文压缩后读取；这些运行状态不参与项目回写。任务、记忆、技能加载、MCP 及审批属于宿主功能，MCP 的远端操作继续使用原有权限策略；这不是对整个 Taffy 进程的隔离。

交互命令：

```text
/sandbox
/sandbox status
/sandbox sync
/sandbox resolve local
/sandbox resolve remote
/sandbox export
```

普通任务不需要这些命令。`sync` 可以主动同步；发生冲突时，`resolve local` 保留冲突路径的本地版本并上传，`resolve remote` 采用冲突路径的远程版本并回写；其他不冲突的变化按正常规则合并。两种选择都保留远程冲突记录。`export` 仅按需把远程项目快照保存到本机项目下 `.sandbox_exports/<随机 ID>/`，不影响日常自动同步。管理命令须等待后台任务与队友退出。

**正常退出不需要导出。** 退出时再次收集项目变化，然后删除本次会话的容器；冲突成果先保存到本地。如果服务器不可达或无法安全收集快照，保留尚未取回成果的容器并报告名字，避免丢失工作。断网期间已同步到本地的文件不受影响。强制终止进程或服务器重启仍可能丢失尚未收集的 tmpfs 内容。

同步和导出在写入前检查路径越界、符号链接、硬链接、设备文件、不安全 Windows 文件名、大小限额和大小写冲突；不会覆盖本地链接指向的外部文件。删除目录只移除空目录，保留排除的凭据和运行状态。文件用临时文件原子替换；一批文件不构成文件系统事务，磁盘或连接故障可能造成部分应用，失败时保留恢复记录。普通权限修改在 POSIX 下同步，Windows 不传播 Linux 可执行位。

主代理、子代理、队友和后台 shell 的沙箱操作使用同一锁串行执行，防止回写互相竞态；因此长命令会暂时阻塞其他沙箱工具。同步需要读取完整的受限项目快照，较大项目或较慢 SSH 会增加延迟。内存上限同时覆盖工作区与 `/tmp` 的内存使用，应大于预期文件和进程用量。`SANDBOX_TRANSFER_MB` 控制普通文件和快照传输大小；排除的依赖及状态目录不会进入返回快照。

容器以 UID/GID 1000 运行，根文件系统只读，移除 Linux capabilities，禁止提权，默认 Docker seccomp 策略保持开启；不挂载宿主目录、Docker socket、SSH agent 或密钥，不开放端口。CPU、内存、进程数、tmpfs 容量与单条命令时间均有限制。命令超时由容器内 GNU `timeout` 执行，Docker 客户端断开时尝试删除本次容器；SSH 完全不可达时无法保证远端即时清理，会显示残留容器名。服务器恢复后按名字检查和清理本次会话的容器。

## 阿里云服务器：先检查，再连接

当前已确认的服务器为 Alibaba Cloud Linux 3、x86_64，Docker Engine 26.1.3，内存约 1.8 GiB，磁盘剩余约 24 GiB。现有 Docker 可进入镜像构建和连接验证阶段，不需要重复安装。先用 [aliyun.env.example](aliyun.env.example) 中的较小限额：0.5 CPU、512 MiB 内存、64 个进程、128 MiB 工作区和 64 MiB 文件传输额度。此配置是初始值，不保证所有项目都能在该资源内构建；先运行一个会话再按实际用量调整。

服务器对接需要本机 Docker CLI 和 OpenSSH 客户端，以及可由本机访问的 SSH 地址、端口和密钥认证。若 Docker CLI 未加入 PATH，可通过 `SANDBOX_DOCKER_EXECUTABLE` 指定完整路径，例如 `C:/Users/your-name/.taffy/bin/docker.exe`。使用阿里云控制台远程连接时，可以先在服务器构建镜像和执行容器测试，然后再完成本地连接。

在阿里云控制台远程连接中运行以下只读命令，将输出提供给维护者。Docker 未安装或权限不足的报错也保留：

```bash
cat /etc/os-release
uname -m
id
free -h
df -h /
docker version
docker info --format '{{.OSType}} {{.Architecture}} {{.SecurityOptions}}'
```

确认系统后准备 Linux Docker Engine 和 SSH 密钥认证。阿里云控制台能连接服务器并不代表本机能通过 SSH 访问：还需要服务器公网 IP / SSH 域名、SSH 端口、用户名，以及本机可使用的认证方式。私钥内容和密码不用发到聊天里。SSH 用户需要运行 Docker 的权限；Docker 权限本身意味着较高的服务器权限，应使用专用于沙箱的主机或账户部署，优先考虑 rootless Docker。

对接时在本机安装 Docker CLI 与 OpenSSH 客户端；使用远端 Docker 不需要本机 Docker daemon。下面的 `USER`、`SERVER`、`22` 是占位符，待实际信息确认后替换：

```powershell
ssh -p 22 USER@SERVER
docker context create taffy-aliyun --docker "host=ssh://USER@SERVER:22"
docker --context taffy-aliyun info
docker --context taffy-aliyun build --network=host --build-arg DEBIAN_MIRROR=mirrors.aliyun.com -t taffy-sandbox:1 sandbox
```

首次 SSH 连接先核对主机指纹，并确认使用密钥认证可以正常连接。远端构建只发送 `sandbox/` 的 Dockerfile，不上传本地配置。然后设置：

```dotenv
SANDBOX_MODE=docker
SANDBOX_DOCKER_CONTEXT=taffy-aliyun
SANDBOX_IMAGE=taffy-sandbox:1
```

工作区通过 Docker exec 的 stdin/stdout tar 流传输，由容器内的非 root 用户在 tmpfs 中解包，兼容 Docker 26 的只读根文件系统；不会把 Windows 路径作为服务器 bind mount。模型看到的仍是 Linux `/workspace`。网络隔离只限制模型执行的容器，宿主模型 API 请求、SSH 通道与镜像构建不受该限制。

上述 `--network=host` 只作用于镜像构建的软件包下载步骤。每次实际创建沙箱容器时，项目代码仍强制使用 `--network none`。

如果 APT 下载明显慢于服务器上的 curl HTTP/2 下载，可把 `build_aliyun.py` 和 Dockerfile 上传到服务器，在它们所在目录执行 `python3 build_aliyun.py < Dockerfile`。此辅助程序只在构建期间监听 `127.0.0.1` 的随机端口，通过 curl 的 HTTPS / HTTP2 通道下载 Debian 文件；APT 继续检查仓库签名和软件包哈希。构建结束后关闭监听，最终镜像中的软件源恢复为 Debian HTTPS 源。辅助程序要求服务器具有 Python 3.6+、支持 HTTP2 的 curl 和 Docker。

此方案通过 SSH 使用服务器，不需要配置公网 HTTP 沙箱 API、HTTPS 证书或开放 Docker 的 TCP 端口。域名可作为 SSH 主机名使用，也可保留给以后的网站界面。[Docker 官方 SSH 连接说明](https://docs.docker.com/engine/security/protect-access/)。

## 验证

普通回归和沙箱边界测试不需要 Docker：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

镜像构建成功后，在本机执行真实容器验证；使用阿里云时保留对应 context 环境变量：

```powershell
$env:TAFFY_DOCKER_INTEGRATION = "1"
$env:SANDBOX_DOCKER_CONTEXT = "taffy-aliyun"
$env:SANDBOX_DOCKER_EXECUTABLE = "$env:USERPROFILE/.taffy/bin/docker.exe"
uv run python -m unittest discover -s tests -p test_sandbox.py -v
Remove-Item Env:TAFFY_DOCKER_INTEGRATION
```

真实验证检查非 root 身份、断网、密钥与 Docker socket 不可见、shell / 文件工具共享副本、本地自动更新、本地编辑上传、二进制文件、删除重命名、空目录、冲突保存与选择、越界拒绝、超时、可选导出与容器清理。没有 Docker 时该项明确跳过，普通测试通过不等于已完成服务器验证。
