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
- 重复实例：poll/send 带文件锁，cron tick 之间不会互踩。
- 重新扫码：`session.json` 失效时 tick 会报告 NEED_LOGIN，在 Muse 主聊天里
  通知；届时重新 `login` + `wait-login` 即可（local_token_list 会尝试复用）。
- 停止服务：`cron.update` 把 `wechat-bridge-tick` 设为 disabled，或
  `cron.remove wechat-bridge-tick`。

## 限制（实话）

- 轮询约 1 分钟一次（cron 最小实用粒度），微信消息**不会**出现在 Muse App
  聊天里，桥只做任务执行与回微信。
- 图片/语音/视频/文件暂不处理，会明确回复告知。
- 执行者默认没有完整历史对话上下文，只拿到本条任务文字。
- 超长任务可能超出单次 run 预算，会先确认再分段回传。
