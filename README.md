# WeChat Bridge（Muse <-> 微信）

腾讯 iLink ClawBot 官方协议的桥接实现（协议依据：tencent/openclaw-weixin
`docs/protocol_zh_CN.md` + 微信开放文档 Clawbot 相关接口）。

## 机制（微信消息如何唤醒 Muse 执行任务）

没有常驻 daemon（云主机会不定时重建，常驻进程不可靠）：

1. **cron 每分钟唤醒一次 agent run**（任务 `wechat-bridge-tick`）。
2. run 内先执行 `bridge.py poll`：经 `POST /ilink/bot/getupdates` 拉取新消息，
   只收**本次扫码绑定用户**的私聊（过滤群聊与其他用户），写入 `inbox/`。
3. 有新文字任务，**同一次 run 里直接调用 Muse 工具实际执行**（上网、读文件、
   计算等），结果写 `outbox/`。
4. `bridge.py send` 经 `POST /ilink/bot/sendmessage` 把回复发回微信
   （携带原消息的 `context_token`，按消息 ID 去重，失败有限重试）。

所以"唤醒"Muse 的是 cron 调度；收发走官方 iLink HTTP 接口。

## 操作

- `python3 bridge.py login` — 获取登录二维码（`state/qr.png`）
- `python3 bridge.py wait-login [--verify-code CODE]` — 等待扫码确认
- `python3 bridge.py poll` / `send [--typing]` / `status` — 拉取（含媒体下载）/ 发送 / 状态（凭据脱敏）
- `python3 bridge.py send-file <path> [--text]` / `send-image <path> [--text]` — 发文件/图片
- `python3 bridge.py mark <key> <new|processing|done|failed>` — 标记消息状态
- `python3 bridge.py cleanup [--days 30]` — 清理旧收发记录与媒体

生产收发由 cron tick 驱动；`serve` 仅用于前台调试。

## 媒体能力（对标 wechat-ilink-bridge 的字段映射）

- 收：图片/文件/语音/视频自动下载到 `media/<日期>/`，AES-128-ECB 解密；
  图片我可直接看懂内容，文件按类型读取；语音/视频暂不支持，会明确告知。
- 发：任务产出的文件/图片可加密上传后以媒体消息发出（25MB 以内）；
  发送时可带 typing 正在输入状态。
- 发送失败 3 次后进入延迟队列，下次收到该用户消息时自动补发第一条。
- 单账号（多账号未做）；无 MCP server（本桥的执行者是 cron tick，不需要）。

## 状态与恢复

- `bridge.py status`：登录态、inbox/outbox 计数、last_tick、是否暂停。
- `state/bridge.log`：运行日志；`state/last_tick.txt`：最近一次 tick。
- 健康检查：last_tick 超过 ~3 分钟未更新 → cron 可能停了，用 `cron.status`
  查 `wechat-bridge-tick`。
- 重复实例：poll/send、登录、队列写入与清理共用 `state/bridge.lock`，冲突时跳过；
  `queue_reply()` 冲突时抛出异常，调用者应稍后重试。支持 Linux/macOS 的 `flock`
  和 Windows 的字节范围锁。外部执行者写回复应调用 `queue_reply()`，不要直接改写队列文件。
- 重新扫码：`session.json` 失效时 tick 会报告 NEED_LOGIN，在 Muse 主聊天里
  通知；届时重新 `login` + `wait-login` 即可（local_token_list 会尝试复用）。
  token 或绑定用户缺失时拒绝收发；切换账号时重置游标和暂停状态，并保留旧记录的账号归属。
- 停止服务：`cron.update` 把 `wechat-bridge-tick` 设为 disabled，或
  `cron.remove wechat-bridge-tick`。

## 限制（实话）

- 轮询约 1 分钟一次（cron 最小实用粒度），微信消息**不会**出现在 Muse App
  聊天里，桥只做任务执行与回微信。
- 桥负责媒体收发；媒体内容的理解与任务执行仍需外部执行者支持。
- 执行者默认没有完整历史对话上下文，只拿到本条任务文字。
- 超长任务可能超出单次 run 预算，会先确认再分段回传。

## 数据安全与重试

- 本地会话凭据以明文 JSON 保存。POSIX 运行目录使用 `0700`、凭据和队列文件使用
  `0600`；Windows 为运行目录和敏感文件设置仅当前用户与 SYSTEM 可访问的 ACL。
  对话历史也限制读取权限。无法设置权限时报告错误，不降级为公开文件。
- 运行目录、消息、媒体和历史均被 Git 忽略；不要手工强制提交这些文件。
- 消息持久化成功后才保存下一页游标；重复拉取按消息与账号去重。
- 发送按媒体/文字分别保存 `client_id`、上传结果与成功进度，重试跳过已确认成功的分段。
  网络超时导致服务端是否收到未知时，会复用原 ID；最终去重仍取决于服务端，不能保证严格只投递一次。
  旧格式 outbox 首次发送时补齐进度，升级前已发生的部分成功无法追溯。
- `-14` 会暂停账号所有鉴权请求一小时；暂停不消耗发送重试次数。
- API、二维码图片与媒体 URL 仅接受 `https://weixin.qq.com` 及其子域，重定向也校验；
  鉴权请求拒绝跨主机重定向。其他 CDN 域名需先审查并显式扩展信任范围。
- 媒体下载和上传的明文上限均为 25MB，下载允许 AES 填充产生的额外 16 字节。
- 延迟队列写入失败时保留原 outbox；清理保留未完成任务、未发送回复及其引用的媒体。

## 离线回归检查

安装 `requirements.txt` 后运行：

```sh
python -B -m unittest discover -s tests -v
```

检查使用临时目录与模拟 HTTP 响应，覆盖中断恢复、重试、用户隔离、文件锁、权限及实际
AES 加解密，不登录微信或发送真实消息。Windows 下需要运行账户具备设置其临时目录 ACL 的权限。
