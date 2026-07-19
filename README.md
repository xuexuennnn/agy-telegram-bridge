# Antigravity CLI for Telegram

> **中文**：把官方 Antigravity CLI 接入 Telegram，在手机上获得接近 Hermes Agent 的聊天体验：普通文字多轮对话、`/new`、`/stop`、持续输入状态、适合移动端的格式化回复，以及生成文件自动回传。
>
> **English**: Use the official Antigravity CLI from Telegram with a Hermes-inspired chat experience: plain-text multi-turn conversations, `/new`, `/stop`, typing feedback, mobile-friendly responses, and automatic delivery of generated files.

这个项目的重点是让已经安装并登录的 `agy` CLI 变成一个实用的 Telegram 助手。它参考了 Hermes 的部分交互逻辑，但不依赖 Hermes 才能完成核心聊天功能。Hermes 状态检查、CPA 凭证导入和项目维修属于可选的运维扩展，不是项目的主要定位。

The core chat path turns an existing, authenticated `agy` installation into a practical Telegram assistant. It borrows selected interaction patterns from Hermes without requiring Hermes for normal Antigravity conversations. Hermes status checks, CPA credential imports, and managed-project repair are optional operations features rather than the main product.

## 主要体验

- 直接发送文字即可和 Antigravity 对话，不必每次输入命令。
- 自动延续当前会话；使用 `/new` 开启新对话，使用 `/stop` 中止正在运行的任务。
- 执行期间持续显示 Telegram typing 状态，并对超时、取消和失败给出简洁反馈。
- 清理 CLI 的工具旁白和重复过程信息，将结果转成适合手机阅读的 Telegram HTML。
- 长回复按 Telegram 的 UTF-16 限制安全拆分，格式化失败时自动回退为纯文本。
- 将 AGY 新生成或修改的文档、表格、图片、音频、视频和压缩包作为 Telegram 原生附件发回。
- 每个 AGY 任务强制运行在 Bubblewrap 沙箱中，默认看不到主机 home 和其他数据目录。

## Interaction highlights

- Send ordinary text to chat with Antigravity; commands are optional for the normal conversation flow.
- Continue the active conversation automatically, start fresh with `/new`, and interrupt work with `/stop`.
- Keep Telegram typing feedback active while AGY runs, with concise timeout, cancellation, and failure messages.
- Remove CLI narration and repetitive tool chatter, then render the useful result as mobile-friendly Telegram HTML.
- Split long replies by Telegram's UTF-16 limits and fall back to bounded plain text when formatting is rejected.
- Return newly generated documents, spreadsheets, images, audio, video, and archives as native Telegram attachments.
- Run every AGY task inside a mandatory Bubblewrap sandbox that hides the host home and unrelated data roots by default.

## 架构与安全边界

Telegram 更新先通过“私聊 + 数字用户 ID”双重校验。命令在普通 Python 控制面处理；每个 AGY 任务都必须进入内部 Bubblewrap 沙箱。沙箱从空根目录开始，只读暴露明确列出的系统可执行文件、动态加载库以及 DNS/TLS/NSS 所需材料；`/var`、`/opt`、`/srv`、任意 home 和其他主机数据根默认不可见。它另行提供隔离的可写状态，并将已有官方登录令牌只读挂载。外层 systemd 用户服务刻意不使用文件系统 namespace 指令，因为这些指令会阻止必需的非特权嵌套 Bubblewrap 用户 namespace。AGY 子进程以外的主机访问由 Bot 代码和单用户 allowlist 控制，而不是由 systemd 文件系统 namespace 控制。高风险操作使用绑定用户与聊天的随机 nonce、五分钟 TTL 和一次性消费。凭证上传先删除 Telegram 消息，再执行大小限制、严格 JSON 解析、原子写入、回滚与故障隔离。

主要防御目标是恶意消息、提示注入、恶意仓库树、链接/挂载逃逸、Git hook/config 执行、秘密输出、并发修改及失控子进程。它不是多租户 Bot，也不是通用远程 shell。

## 命令与可选运维功能

核心聊天功能使用 `/ask`、`/agy` 或普通文字；`/new` 开启新会话，`/stop` 终止当前任务，`/agy_login` 显示本地登录说明。Bot 不转发登录 URL、不接收授权码，也不代用户接受第三方条款。

如果同时安装并配置 Hermes 与 CPA，还可以使用以下附加能力：

- `/status`、`/restart`：检查或经确认操作配置的 Hermes Gateway。
- `/project_status`：检查本地账号池、模型和 CPA 服务状态。
- `/project`、`/project_repair`：只读分析或经确认维修一个明确配置的本地项目；默认关闭维修。
- 上传受支持的 Codex、CPA/C2API 或 Sub2 JSON：通过严格校验和原子事务导入本地 CPA 账号池。

### 手机友好回复与生成文件

聊天、普通 AGY 任务和项目分析/维修统一要求默认用简洁中文、结论优先、短标题与项目符号，不展示工具调用旁白或内部推理。Bot 使用非重叠 tokenizer 先转义原始 HTML，再将 1–3 级 Markdown 标题、粗体、行内代码和项目符号转换为 Telegram HTML；代码内容不会再次解析为粗体。最终 HTML 按不超过 3500 个 UTF-16 code unit 拆分，每块都保持实体完整和标签平衡。HTML 投递失败时会尝试有界纯文本回退，且不会阻止已经冻结的产物上传。

普通聊天和 `/agy` 共用一个专用可写工作区。AGY 成功结束后，Bot 会比较执行前后的安全文件快照，把新增或改变的受支持文件作为 Telegram 原生文档发送；失败、超时或停止时不发送。项目只读分析和维修不会自动返回仓库文件。

每次回复最多发送 10 个文件，每个文件最多 45 MiB。支持：`.doc`、`.docx`、`.rtf`、`.odt`、`.xls`、`.xlsx`、`.csv`、`.ppt`、`.pptx`、`.pdf`、`.txt`、`.md`、`.json`、`.yaml`、`.yml`、`.xml`、`.png`、`.jpg`、`.jpeg`、`.webp`、`.gif`、`.mp3`、`.m4a`、`.ogg`、`.wav`、`.mp4`、`.mov`、`.webm`、`.zip`、`.tar`、`.gz`、`.7z`。请求 Word 文件时，提示会明确要求创建结构有效的 `.docx`，而不是给纯文本改扩展名。

产物扫描拒绝不受信任的根目录和子目录、符号链接、硬链接、跨文件系统条目、错误所有者、组/全局可写文件、空文件、越界路径及不支持格式；目录或条目预算耗尽会使整次快照失败并关闭该次自动返回，不会采用部分结果。Bot 在任务仍持有聊天锁时，以 `O_DIRECTORY|O_NOFOLLOW` 打开工作区并逐级使用相对 dirfd 安全遍历，比较复制前后的设备、inode、类型/模式、所有者、链接数、大小、mtime 和 ctime，只读取快照声明的准确字节数。验证后的内容被复制到 Bot 私有状态目录中的 0600 临时文件，随后才释放聊天锁；Telegram 只读取这份稳定私有副本，绝不读取实时工作区描述符。该机制不能证明文件内容本身无恶意，也不能替代文档格式解析、杀毒或内容审查；Telegram/文件系统/内核层仍可能存在本项目无法消除的风险。

## Optional operations features

The normal chat flow uses `/ask`, `/agy`, or plain text. `/new` starts a fresh conversation, `/stop` interrupts the current task, and `/agy_login` displays local login instructions without carrying OAuth URLs or authorization codes through Telegram.

When Hermes and CPA are also installed and configured, the bot can expose additional administrator-only commands for checking the local services, confirming a Hermes Gateway action, inspecting a configured Git project, performing an explicitly confirmed repair, and importing supported credential bundles through strict parsing and atomic transactions. These features are optional and fail closed when their paths or switches are not configured.

## Security model

- Single administrator, private chats only, with an exact numeric user-ID allowlist.
- Mandatory per-task Bubblewrap isolation built from an empty root and explicit runtime mounts.
- No OAuth URL forwarding: `/agy_login` only instructs the operator to run the official `agy` CLI in a trusted terminal.
- Loopback-only CPA endpoint validation; public or credential-bearing endpoint URLs are rejected.
- Bounded subprocess, HTTP, Telegram, and artifact output with deterministic secret redaction.
- Symlink, hardlink, nested-mount, traversal, ownership, permission, race, and cancellation defenses around project and artifact access.
- Risky host actions use random one-time nonces bound to the requesting user and chat, with a five-minute expiry.

### Quick start

1. Install Python 3.11+, Bubblewrap, the official AGY CLI, and any optional Hermes/CPA components you intend to use.
2. Copy `.env.example` to `$HOME/.config/hermes-rescue-bot/rescue.env`, keep it mode `0600`, and fill in the required values locally.
3. Install Python dependencies in a virtual environment.
4. Install the included systemd user unit and start it only after reviewing the configuration.
5. Run the test and verification commands listed below before every deployment.

No real credentials, server addresses, OAuth client identifiers, production paths, or deployment-specific project names belong in this repository.

## 前置条件与配置

需要 Python 3.11+、Telegram Bot token、管理员数字用户 ID、Bubblewrap，以及按需安装的 Hermes Agent、CLIProxyAPI 和官方 `agy` CLI。先通过 BotFather 创建 Bot；向可信的 ID 查询工具获取自己的数字 ID，写入 `RESCUE_ALLOWED_USER_ID`。不要把 Bot 加入群组，也不要把 token 或 ID 提交到 Git。

环境文件的固定位置是 `$HOME/.config/hermes-rescue-bot/rescue.env`。`CPA_BASE_URL` 只接受 loopback。聊天工作区、会话标记、AGY 隔离状态、默认 CPA 凭证目录和配置都位于 `$HOME/.local/state/hermes-rescue-bot/`；程序不会在源码或安装目录创建运行状态。

Antigravity CLI 不随本项目分发。必须由用户在可信服务器终端直接运行官方 `agy`，自行阅读并接受 Google 的适用条款；缺少 CLI 或现有登录令牌时，Bot 启动或相关功能会明确不可用，且不会向 Telegram 暴露本地路径或秘密。本项目与 Google 无关联。

## 安装与 systemd 用户服务

```sh
install -d "$HOME/.local/share/hermes-rescue-bot" "$HOME/.config/hermes-rescue-bot" "$HOME/.local/state/hermes-rescue-bot"
git archive HEAD | tar -x -C "$HOME/.local/share/hermes-rescue-bot"
install -m 0600 .env.example "$HOME/.config/hermes-rescue-bot/rescue.env"
${EDITOR:-vi} "$HOME/.config/hermes-rescue-bot/rescue.env"
cd "$HOME/.local/share/hermes-rescue-bot"
python -m venv .venv
.venv/bin/pip install -r requirements.txt
install -Dm644 systemd/hermes-rescue-bot.service "$HOME/.config/systemd/user/hermes-rescue-bot.service"
systemctl --user daemon-reload
systemctl --user enable --now hermes-rescue-bot.service
```

必须先编辑 mode 0600 的环境文件并填写 token、允许用户 ID 等必需值，再启动服务。上面的 `git archive` 不会把 `.git` 部署进应用；也可使用带 `--exclude=.git` 的 rsync/install 流程，不要盲目 `cp -a .`。

这是用户服务，内核参数、内核模块和控制组保护必须由主机策略提供。

更新前停止服务，并分别备份 `$HOME/.config/hermes-rescue-bot/rescue.env` 与 `$HOME/.local/state/hermes-rescue-bot/`；替换无 `.git` 的应用文件、重新安装 requirements、运行测试后再启动。卸载时停止并禁用服务，删除 unit 和 `$HOME/.local/share/hermes-rescue-bot`；只有确认聊天会话、AGY 状态及 CPA 数据均不再需要后，才删除 `$HOME/.local/state/hermes-rescue-bot/` 和环境文件。运行状态无需也不应复制回源码或安装树。

## 通用项目适配器

设置 `RESCUE_PROJECT_REPO` 为明确、规范化的绝对 Git 仓库路径；可选设置安全过滤后的 `RESCUE_PROJECT_NAME` 和 `.service` 结尾的 `RESCUE_PROJECT_SERVICE`。仓库树会检查符号链接、硬链接、特殊文件、嵌套挂载和所有权；Git hooks、分页器、外部 diff 与可执行配置被禁用。

外层 systemd 服务不建立只读 home；项目读写边界由每次 AGY 调用时强制创建的内部 Bubblewrap 沙箱实施。`project-read` 只读暴露配置的项目；`project-repair` 只将该项目暴露为可写，并仅在项目存在且通过校验时额外只读挂载 `.venv` 及其受信任 Python runtime。要启用维修，设置 `RESCUE_ENABLE_PROJECT_REPAIR=1`；维修 allowlist 只能包含 `RESCUE_PROJECT_REPO` 的精确项目路径。每次维修仍须按钮确认。

## CPA 凭证警告

Telegram 不是理想的凭证传输通道。只有在理解风险时才上传；Bot 会尽力先删除原消息，但删除失败时必须人工删除。导入成功不代表额度、刷新能力或每个账号均已验证。任何异常都应立即停止 CPA、轮换秘密并人工检查隔离/回滚结果。

## 排障、测试与限制

若启动失败，检查允许 ID、可执行文件、Bubblewrap、官方 AGY 登录令牌、状态目录权限和环境文件。项目命令显示关闭时，确认仓库路径是绝对路径且树满足安全检查。CPA 失败时确认配置文件权限和 loopback 地址。

```sh
python -m unittest discover -s tests -q
python -m py_compile bot.py rescue_core.py tests/test_*.py
python -m pip check
scripts/verify-unit.sh
git diff --check
```

`scripts/verify-unit.sh` 只在 `/tmp` 建立并清理伪安装树和替换后的临时 unit，不写真实 home，也不启动、停止或重载服务；需要主机提供 `systemd-analyze`。CI 在该命令可用时运行它。

AGY 的受控任务仍共享主机网络，且 AGY 进程必然可读取其只读登录令牌，因此不能把不可信仓库、提示或第三方 CLI 视为零风险。诊断沙箱不共享网络。为保证非特权嵌套 Bubblewrap 可用，外层 systemd 用户服务不使用 `ProtectSystem`、`ProtectHome`、路径绑定或其他文件系统 namespace 指令；这意味着 Bot 控制面自身的主机访问边界依赖代码中的固定路径、校验和单用户 allowlist。所有 AGY 任务仍强制使用内部 Bubblewrap 的最小系统 runtime 挂载、namespace、凭证和进程边界。服务重启依赖用户级 systemd 权限；第三方 CLI、服务和条款由各自维护者负责。本项目与 Telegram、OpenAI、Google、Hermes Agent 或 CLIProxyAPI 的维护者均无隶属或背书关系。
