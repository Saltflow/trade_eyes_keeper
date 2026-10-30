# 飞书私聊无回复排查（2026-09-28）

用户反馈私聊机器人完全没有回复。本轮不能沿用 2026-09-24 的服务就绪结果
作为账号端问答可用的结论，真实消息收发仍需实际验证。

## 已核实的事实

- 排查开始时 `trade-eyes.service` 仍为 MainPID 389100、NRestarts 0，
  本地心跳 fresh/ready，飞书 SDK 子进程有一条已建立的出站 443 连接。
- 飞书与助手开关均开启，`allowed_chat_ids: ['*']` 保持不变。
  主进程初始环境和部署 `.env` 均未设置 `SKIP_FEISHU`、`SKIP_NOTIFICATIONS`。
- 上次发布的 19 个文件 SHA 与接受清单一致。
- 助手 SQLite 的 receipts、actions、audit 均为 0。日志只找到启动记录，
  不能据此区分平台未投递和接收后的提前忽略，也不能认定 DeepSeek 调用失败。
- 官方权限查询返回 HTTP 200 / code 0，应用身份的
  `im:message.p2p_msg:readonly`、`im:message:readonly`、
  `im:message:send_as_bot`、`im:resource` 均已授权。
- 已上线应用版本 1.0.3 包含 `im.message.receive_v1`。v6 查询没有返回当前
  `subscription_type`，因此尚不能认定它使用长连接，也不能认定缺少事件订阅。
- 官方机器人信息接口返回的显示名称为“试用clawbot”。本主机只发现本服务这一套
  明显相关接收器；其他主机是否存在同应用客户端尚未验证。
- 开发者后台需要登录，已向用户请求登录并确认实际私聊的机器人名称。

只读证据位于 `temp_test_cache/feishu_no_reply_20260928/`：
`initial_snapshot.json`、`feishu_scopes.json`、`event_subscription.json`、
`bot_identity_and_peers.json`。证据没有导出凭证或聊天正文，没有发送测试消息。

## 诊断改动与验证

- `feishu_handler.py` 为每个分流结果记录固定原因，包括禁用、无文本、群聊未 @、
  权限/去重/限流、缺发送者、旧命令及助手入队；日志不包含自然语言正文或消息标识。
- `feishu_app.py` 记录发送被抑制、平台拒绝的数字错误码及发送成功。
  不记录平台响应正文、令牌或消息内容。
- 新增回归验证忽略与正常入队可区分，以及平台错误码可见而私密正文不可见。
- 本地相关飞书/助手路由测试与完整 `tests/validation/`：**69 passed**。
  两个源码文件和相应测试的 Ruff 检查通过。

两文件已于北京时间 09:20 独立备份发布并重启一次。
新 MainPID 428497、NRestarts 0，心跳 fresh/ready，助手初始化成功；
8 个配置、凭证和状态保护文件哈希保持一致，没有修改飞书平台或发送测试消息。
回执为 `temp_test_cache/feishu_no_reply_20260928/diagnostic_deployment.json`，备份位于
`/root/trade_eyes_keeper/data/deployments/feishu_diagnostics_20260928T012017Z_c72b53e8/backup`。
当前改动补齐观测能力，不代表无回复的根因已经消除。
09:22 的最后快照仍为 receipts 0，没有分流或发送事件；近五天官方 SDK 的连接、
接收和处理错误类别计数也均为 0。对应回执为 `metadata_2026-09-28T01-22-00.json`
和 `sdk_error_categories.json`。未启动持续监听或轮询。

## 登录后台后确认的根因与修复

用户完成登录后，20:06 在“试用clawbot”的事件配置页确认：订阅方式仍为
“将事件发送至开发者服务器”，目标是旧主机的 `:1933/feishu/events` HTTP 回调。
当前部署已经移除 HTTP 接收入口，使用官方 SDK 长连接。因此，SDK 连接正常并不会
让仍配置为 HTTP 投递的消息进入当前助手。此前权限和事件名称检查均未覆盖这项设置。

已将同一应用的事件订阅切换为“使用长连接接收事件”，平台验证显示“连接成功”。
保存后平台要求发布版本才能生效，已于 2026-09-28 20:08 发布 **1.0.4**，
页面显示“已发布”“当前修改均已发布”。发布差异只有事件订阅方式，权限变更为“暂无”，
应用可用范围和外部访问设置保持原样。本阶段没有重新启动部署服务。
20:10:07 的官方只读 API 同样确认线上版本 1.0.4、无待审核版本，保留消息接收事件；
对应回执为 `event_subscription_2026-09-28T12-10-07.json`。同秒 metadata 快照
仍没有新消息，需要用户在发布后重新发送一条私聊问题。

浏览器截图证据：

- `temp_test_cache/feishu_no_reply_20260928/event_mode_before.png`
- `temp_test_cache/feishu_no_reply_20260928/event_mode_published.png`

发布后用户已确认“收到了 能正常回复”。20:24 只读检查也发现 4 条助手收据、
4 次助手入队和 4 次成功发送。私聊收发链路已验证；切换前未进入助手的旧消息
不能视为自动重放。后续公告栏问答质量和留档缺失另行修复。

参考：[飞书接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、
[事件订阅方式](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)。
