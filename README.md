# Muse WeChat Bridge

基于腾讯 iLink ClawBot 协议的微信消息桥接工具，用于连接微信私聊与 Muse 等外部任务执行者。

本项目负责登录、消息收发、媒体传输和本地队列管理。**AI 任务执行和定时调度需要由外部系统提供**，安装本项目不会自动创建调度任务或启动 AI 执行者。

## 功能与边界

- 仅接收本次扫码绑定用户的私聊，过滤群聊及其他用户消息。
- 接收文字，以及图片、文件、语音、视频附件；支持发送文字、图片和文件。
- 使用本地收发队列，支持消息去重、发送进度恢复、有限重试和延迟补发。
- 支持 Windows、Linux 和 macOS 的文件锁及运行数据权限控制。
- 当前为单账号实现，未提供多账号管理或 MCP Server。

图片识别、文件解析、语音转写、视频理解和任务执行均取决于外部执行者。桥接脚本只负责传输与存储，不内置这些能力。

## 风险提示

账号和消息处理由微信的安全风控系统决定，请仅将该能力用于合法、正当的用途，尊重用户同意和隐私，并避免垃圾信息、滥用自动化、欺诈、规避平台控制或其他被禁止的行为。

如微信调整风控策略，账号可能被限制登录或收发；使用前请自行评估风险，重要账号建议先用小号验证。

## 快速开始

### 给 Muse 的部署提示词

把下面整段复制发给你的 Muse，它会带着你走完安装、扫码登录和调度配置：

```text
请帮我部署 wechat-bridge（微信 ↔ Muse 双向桥），让它能收微信消息、执行任务并回微信。

【项目】https://github.com/yayamahardika/muse-wechat-bridge
（如果是 fork 或你自己的仓库地址，换成实际地址）

【你要做的事，按顺序来】

1. 获取代码：把仓库克隆到本地（或下载解压），记下项目根目录的绝对路径。

2. 安装依赖：按 README「快速开始 → 1. 安装依赖」操作，建议用虚拟环境；
   后续命令里的 `python` 都指装好依赖的那个解释器。

3. 扫码登录：运行 `python bridge.py login`，把生成的 state/qr.png 展示给我看；
   我扫码后，你运行 `python bridge.py wait-login` 等待确认（微信若要求验证码，
   你问我要）；最后 `python bridge.py status` 确认 logged_in 为 true。

4. 创建调度任务：在 Muse 里创建一个 cron 任务：
   - 名称：wechat-bridge-tick，每分钟运行一次；
   - 任务指令：直接使用本 README《执行者提示词》一节的全文，
     把里面的工作目录换成第 1 步记下的实际项目路径。
   如果你用的不是 Muse：用系统 cron / systemd timer / Windows 任务计划程序，
   每分钟依次运行 `poll` →（你作为执行者处理 inbox 并用 queue_reply() 入队）→ `send`
   也行，具体规则同样参考《执行者提示词》。

5. 验证：任务建好后，用绑定微信给机器人发一条消息，一分钟内应收到你的回复；
   再运行 `python bridge.py status`，确认 last_tick 在持续更新。

【纪律】不要把 state/ 下的会话凭据写入聊天记录或代码仓库（.gitignore 已配好，
不要动它）；所有"今天 / 昨天 / 几点"的判断一律按 Asia/Shanghai 来。
```


### 1. 安装依赖

准备 Python 3 和 pip，在项目根目录执行。建议使用虚拟环境。

**Windows（PowerShell）：**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux / macOS：**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

依赖包括 `qrcode`、`pillow` 和 `pycryptodome`。后文统一使用 `python` 表示已安装依赖的解释器；未激活虚拟环境时，请替换为上面的虚拟环境解释器路径。

运行账户需要具备设置运行目录和文件权限的能力；Windows 使用当前用户与 SYSTEM 的专用 ACL。

### 2. 扫码登录

```sh
python bridge.py login
```

打开生成的 `state/qr.png`，使用微信扫码，然后等待确认：

```sh
python bridge.py wait-login
python bridge.py status
```

`wait-login` 默认等待 180 秒。如微信要求验证码，可执行：

```sh
python bridge.py wait-login --verify-code CODE
```

将 `CODE` 替换为微信显示的验证码。二维码过期后，重新执行 `login`。

### 3. 拉取消息

用绑定用户向机器人发送一条消息，然后执行：

```sh
python bridge.py poll
```

新消息写入 `inbox/`，媒体附件下载到 `media/<日期>/`。此步骤只接收消息；要生成任务回复，需要接入外部执行者，见下文。

### 4. 手动发送图片或文件

手动发送需要先通过 `poll` 收到绑定用户的消息，以取得回复所需的 `context_token`。

```sh
python bridge.py send-image "path/to/image.png" --text "图片说明"
python bridge.py send-file "path/to/report.pdf" --text "文件说明"
python bridge.py send --typing
```

将示例路径替换为实际文件路径。**`send-image` 和 `send-file` 只将消息加入队列，`send` 才执行发送**；`--text` 为可选说明文字，`--typing` 用于在发送期间显示正在输入状态。

## 工作流程与执行者接入

推荐由外部调度器周期性启动任务执行者，每次按以下顺序处理：

```text
外部调度器 → poll → inbox → 外部执行者 → queue_reply() → outbox → send → 微信
```

1. 执行 `python bridge.py poll`，拉取消息并持久化到 `inbox/`。
2. 执行者筛选属于当前账号、状态为 `new` 的记录，使用 `mark` 标记为 `processing`。
3. 根据消息文字及附件执行任务，通过 `bridge.queue_reply()` 将结果加入 `outbox/`。
4. 回复入队成功后，将原消息标记为 `done`；任务执行失败时标记为 `failed` 并记录原因。
5. 执行 `python bridge.py send`，将待发送回复发回微信。

`mark` 的 `<key>` 为 `inbox/<key>.json` 的文件名去掉 `.json` 后的部分：

```sh
python bridge.py mark MESSAGE_KEY processing
python bridge.py mark MESSAGE_KEY done
python bridge.py mark MESSAGE_KEY failed --note "失败原因"
```

将 `MESSAGE_KEY` 替换为实际消息键。`done` 表示执行者已完成处理，发送结果另由 outbox 状态记录。

外部 Python 执行者可按以下方式入队回复。示例中的 `record` 为选中的 inbox 记录，`message_key` 为该记录的文件名键，`result_text` 为执行结果：

```python
import bridge

bridge.ensure_dirs()
bridge.queue_reply(
    reply_to=message_key,
    to_user_id=record["from_user_id"],
    context_token=record["context_token"],
    text=result_text,
)
```

发送媒体时，可额外传入 `media={"kind": "image", "path": "/absolute/path/image.png"}`，或使用 `kind="file"`。同一个 `reply_to` 已存在时不会重复入队；`queue_reply()` 遇到锁冲突会抛出异常，调用者应稍后重试。不要直接改写队列文件。

### Muse 调度示例

在 Muse 环境中，可由名为 `wechat-bridge-tick` 的 cron 任务每分钟唤醒一次 agent run，在同一次 run 内完成拉取、执行、回复入队和发送。该调度任务需在 Muse 中另行配置，不属于本仓库的 Python 命令。

此部署方式由 cron 唤醒 Muse，收发通过 iLink HTTP 接口完成。约一分钟的调度间隔会带来消息处理延迟；微信消息不会自动出现在 Muse App 聊天中。执行者的历史上下文、长任务分段回传和异常通知策略也需由外部系统实现。

### 执行者提示词（Muse 调度任务用）

下面是一段可直接用作 Muse cron 调度任务指令的提示词（任务名示例：`wechat-bridge-tick`，
每分钟唤醒一次 agent run）。它来自实际部署中踩过的坑：时区换算、收件人字段取错、
无事时的静默纪律，以及"重要事项才主动推送"的用户授权。

```text
微信桥 tick（工作目录为本项目根目录，用已安装依赖的 python 解释器）。
你是本轮的执行者，有完整工具调用能力。

【时间纪律】所有"今天 / 昨天 / 几点"的判断一律以 Asia/Shanghai（UTC+8）为准。
inbox 记录里的 local_time 字段已经是北京时间（如 "2026-10-12 07:53"），直接用它；
不要对 create_time_ms 自行换算——它是 UTC 毫秒，裸读小时数会差 8 小时。

【流程】按顺序执行：
1. `python bridge.py poll` 拉取新消息（附件自动下载到 media/）。
   输出 NO_SESSION 表示尚未扫码登录；NEED_LOGIN 表示会话失效。
2. `python bridge.py send --typing` 发送 outbox 中待发的回复。
3. 处理 inbox/ 下 status 为 new 的消息（按文件名排序）：
   a. 先 `python bridge.py mark <key> processing`。
   b. 同一批次（batch 字段相同）的多条消息合并为一个任务；不同批次分别处理。
   c. 看 inbox 记录的 attachments：
      - 有图片：用视觉能力看图内容，再按图回复；
      - 有文件：按类型读取内容，再处理；
      - 只有语音 / 视频且无文字：回复"这条通道暂时没有配置这类内容处理，
        请先用文字描述"，标记 done。
   d. 文字任务必须真正用工具执行（上网、读文件、计算等），不许编造结果；
      需要用户补充信息或授权时，在回复里明确说明要他做什么；
      预计超过 3 分钟的长任务，先入队一条"收到，正在处理…"并发送，再继续执行。
   e. 用 bridge.queue_reply() 写回复：to_user_id 取 inbox 记录的 from_user_id
      （注意：是 from_user_id，不是 to_user_id），context_token 取 inbox 记录的
      context_token；需要交付文件 / 图片时传入 media={"kind": "image"|"file",
      "path": "/绝对路径"}（25MB 以内）。微信回复简洁，默认不超 500 字，不刷屏。
   f. `python bridge.py mark <key> done`；失败则 mark failed 并在 note 写清原因。
   g. 再执行一次 `python bridge.py send --typing`，把本轮回复发出去。

【主动推送】仅在用户明确授权后启用（授权示例："重要事项才主动找我"）。
允许主动说的情形只有三类：① 桥本身出故障需要用户处理；② 用户的任务失败，
需要他介入；③ 用户明确交代要盯的事项有了动静。
不允许：日常播报、重复提醒、营销式内容。安全类事项（账号异动）走主聊天报备，
不在微信重复，避免双通道轰炸。
发送方式：`python bridge.py send-proactive "正文" --kind <类别> --cooldown <秒>`，
正文单条、200 字以内，说清什么事和要用户做什么；同一 kind 靠 cooldown 去重，
不要绕过。无事发生时不要输出任何面向用户的文字。

【纪律】
- 常规轮询无新消息、或仅做了收发动作时，最终不要输出任何面向用户的文字，保持静默。
- 任务结果只发往微信，不在当前聊天里复述。
- 只有两种情况才在当前聊天输出一句话：① poll 报告 NO_SESSION 或 NEED_LOGIN
  （提醒用户重新扫码登录）；② 同一问题连续失败需要人工介入。其他一律静默。
- 绝不把 bot_token 等凭据写入回复或聊天；status 命令已做脱敏。
- 失败时如实说明原因和下一步，不许用"收到，我记下了"代替实际执行，
  也不许把"等待处理"写成"已完成"。
```

## 命令参考

所有命令均以 `python bridge.py` 为前缀，可通过 `--help` 或 `<命令> --help` 查看参数。

| 命令 | 用途 | 可选参数与默认值 |
| --- | --- | --- |
| `login` | 获取登录二维码 | 无 |
| `wait-login` | 等待扫码确认 | `--timeout 180`、`--verify-code CODE` |
| `poll` | 拉取消息并下载媒体 | 无 |
| `send` | 发送 outbox 中的待发送回复 | `--typing` |
| `send-file <path>` | 将本地文件加入发送队列 | `--text TEXT` |
| `send-image <path>` | 将本地图片加入发送队列 | `--text TEXT` |
| `send-proactive <text>` | 使用最近接收消息的上下文，将文字加入发送队列 | `--kind general`、`--cooldown 3600`（秒） |
| `mark <key> <status>` | 标记 inbox 消息状态 | `--note TEXT`；状态为 `new`、`processing`、`done` 或 `failed` |
| `cleanup` | 清理旧收发记录与媒体 | `--days 30`，须为正整数 |
| `serve` | 在前台循环拉取和发送，用于调试 | `--every 60`（每轮结束后等待的秒数） |
| `status` | 查看登录、队列、游标和暂停状态 | 无 |

`send-proactive` 按 `kind` 记录入队冷却时间，也需要已有 `context_token`；入队后仍需执行 `send`。它不能绕过服务端对上下文有效性的限制。

`serve` 不执行 AI 任务，仅循环调用 `poll` 和 `send`。按 `Ctrl+C` 退出。由于拉取请求可能等待响应，实际循环周期包含请求耗时与 `--every` 指定的等待时间。

## 项目结构与运行数据

```text
wechat-bridge/
├── bridge.py          # 登录、消息队列与命令行入口
├── media.py           # 媒体加解密、上传下载与输入状态
├── runtime_io.py      # 私有文件权限与原子写入
├── requirements.txt   # Python 依赖
├── tests/             # 离线回归检查
├── state/             # 会话、二维码、游标、锁、日志与延迟队列
├── inbox/             # 接收消息及任务处理状态
├── outbox/            # 待发送回复及发送进度
├── media/             # 按日期保存的接收媒体
└── history.jsonl      # 本地收发历史
```

运行数据在使用时创建，均已被 `.gitignore` 忽略。会话凭据位于 `state/session.json`，日志位于 `state/bridge.log`，最近一次收发活动时间位于 `state/last_tick.txt`。

## 状态检查与故障恢复

```sh
python bridge.py status
```

命令输出脱敏后的会话信息、inbox 状态计数、待发送回复数、最近 tick 时间及是否暂停。`last_tick` 表示桥接收发活动时间，不代表 AI 任务执行成功或调度器健康。

| 现象或输出 | 处理方式 |
| --- | --- |
| `NO_SESSION` / `NEED_LOGIN` | 执行 `login` 和 `wait-login` 重新扫码；token 或绑定用户缺失时拒绝收发 |
| `NO_CONTEXT_TOKEN` | 先让绑定用户发送消息，再执行 `poll` 获取上下文 |
| `PAUSED_1H` / `skipped: paused` | 账号触发 `-14` 暂停，等待一小时后恢复；暂停不消耗发送重试次数 |
| `skipped: locked` | 另一个操作持有文件锁，本次跳过，稍后重试 |
| 待发送回复持续积压 | 检查是否调用 `send`、日志中的发送错误、账号暂停状态及外部调度器 |
| 每分钟调度时，`last_tick` 超过约三分钟未更新 | 检查调度任务是否仍在运行，同时检查会话、暂停状态和日志 |

在 Muse 部署中，可用外部调度工具 `cron.status` 检查 `wechat-bridge-tick`；停止时通过 `cron.update` 禁用该任务，或通过 `cron.remove` 移除任务。

重新登录时会尝试通过 `local_token_list` 复用旧 token。切换账号会重置游标与暂停状态，旧消息和回复保留账号归属，避免跨账号收发。

## 数据安全与可靠性

### 本地存储与并发

- 会话凭据以**明文 JSON** 保存在本地，不要分享或强制提交运行数据。
- POSIX 运行目录权限为 `0700`，凭据、队列和历史文件权限为 `0600`；Windows 为运行目录和敏感文件设置仅当前用户与 SYSTEM 可访问的 ACL。权限设置失败时报告错误，不降级为公开文件。
- 登录、拉取、发送、队列写入和清理共用 `state/bridge.lock`。Linux / macOS 使用 `flock`，Windows 使用字节范围锁。
- 外部执行者应调用 `queue_reply()` 写回复，并自行管理任务领取与执行恢复；桥接文件锁不覆盖整个 AI 任务执行过程。

### 重试与恢复

- 消息持久化成功后才保存下一页游标；重复拉取按消息与账号去重。
- 媒体与文字分别保存 `client_id`、上传结果及发送进度，重试时跳过已确认成功的部分。
- 网络超时导致服务端是否收到消息未知时，重试复用原 ID；最终去重仍取决于服务端，**不能保证严格只投递一次**。
- 旧格式 outbox 在首次发送时补齐进度；升级前已发生的部分成功无法追溯。
- 发送失败累计三次后，在后续 `send` 处理中转入延迟队列。下次收到该用户消息时，第一条延迟回复恢复为待发送状态，再由 `send` 尝试发送。
- 延迟队列写入失败时保留原 outbox。`cleanup` 保留未完成任务、未发送回复及其引用的媒体；它不负责清理日志、会话和 `history.jsonl`。

### 网络与媒体限制

- API、二维码图片及媒体 URL 仅接受 HTTPS 的 `weixin.qq.com` 及其子域，重定向也会校验；鉴权请求拒绝跨主机重定向。其他 CDN 域名需审查后显式扩展信任范围。
- 媒体使用 AES-128-ECB 加解密。上传和下载的明文上限均为 **25 MiB（25 × 1024 × 1024 字节）**；下载允许 AES 填充产生的额外 16 字节。

## 离线回归检查

安装依赖后，在项目根目录执行：

```sh
python -B -m unittest discover -s tests -v
```

检查使用临时目录与模拟 HTTP 响应，覆盖中断恢复、重试、用户隔离、文件锁、权限及实际 AES 加解密，不登录微信或发送真实消息。Windows 下，运行账户需要具备设置临时目录 ACL 的权限。

## 协议依据与许可证

代码中的协议参考为 `tencent/openclaw-weixin` 的 `docs/protocol_zh_CN.md` 及微信开放文档中的 Clawbot 相关接口；媒体字段映射参考 `DONGH4O/wechat-ilink-bridge`。

本项目采用 [MIT 许可证](LICENSE)。
