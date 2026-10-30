# 飞书 / Telegram Bot 配置

HTTP/HTTPS health server 已整体下线。使用飞书/TG Bot 更灵活、更安全，接收命令只需服务器主动连接平台，无需公网回调或管理端口。

`notification` 控制日报、简报、告警和部署通知外发；`interactive` 控制管理命令接收。两个开关分别配置。凭证写在 `config/.env`。

## 飞书

### 仅接收推送

在群设置中添加自定义机器人，将其 Webhook 填入 `FEISHU_WEBHOOK_URL`，启用：

```yaml
notification:
  feishu:
    enabled: true
    msg_type: interactive
```

配置方法见[飞书自定义机器人官方文档](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。这种机器人只发消息，管理命令需要下面的自建应用。

### 接收管理命令

在飞书开发者后台创建企业自建应用并启用机器人，配置消息接收事件 `im.message.receive_v1`，授予相应消息接收权限及 `im:message:send_as_bot`。在事件订阅方式中选择“使用长连接接收事件”，发布应用并把机器人加入需要的聊天。

```dotenv
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
```

```yaml
interactive:
  feishu:
    enabled: true
    allowed_chat_ids: ["oc_your_chat_id"]
    rate_limit_per_minute: 10
```

可以显式使用 `allowed_chat_ids: ["*"]` 沿用当前群成员范围；此时能够向应用发消息的聊天都可调用管理命令。空列表仍表示没有授权聊天，不会放行。

本项目使用[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/README.md)的长连接接口，随 `requirements.txt` 安装。此模式不需要旧 HTTP 回调的 verification token、加密密钥或请求 URL；事件由使用应用凭证建立的连接接收。具体平台设置见[官方事件订阅说明](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)。

## Telegram

通过 `@BotFather` 创建 Bot，取得 token。给 Bot 发消息后，通过官方 `getUpdates` API 获取目标 `chat.id`；群聊通常使用负数 ID，YAML 中仍写为字符串。

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_notification_chat_id
# 可选代理
TELEGRAM_PROXY=
```

```yaml
notification:
  telegram:
    enabled: true
    parse_mode: HTML
interactive:
  telegram:
    enabled: true
    allowed_chat_ids: ["your_management_chat_id"]
    rate_limit_per_minute: 10
    polling_interval: 2
```

管理白名单不会继承 `TELEGRAM_CHAT_ID`；该变量仅决定推送目标。Telegram 管理必须填写非空 ID 列表，不支持通配符。若以前配置了 webhook，应在切换前通过官方 `deleteWebhook` 清除；官方 API 的 webhook 与 `getUpdates` 互斥。[Telegram 官方接收 API](https://core.telegram.org/bots/api#getupdates)

## 启动和命令

```bash
python main.py --service       # 常驻调度 + Bot
python main.py --interactive   # 仅 Bot，/schedule 无运行中调度器可修改
python main.py --status        # 本地状态，不向 Bot 发消息
```

两端使用同一个命令分发器，例如 `/help`、`/list`、`/add 600036 00883`、`/remove 600036`、`/daily`、`/brief`、`/schedule`、`/optimize`、`/config`。完整参数以 `/help` 为准。耗时回测在后台执行，每个 Bot 同时接受一个回测；接收器保留事件去重和持续限流状态。

启用的 Bot 缺少凭证或合法白名单时会明确报错；停用的 Bot 不启动连接。修改服务开关、凭证或白名单后重启服务。`SKIP_NOTIFICATIONS` 和渠道 `SKIP_*` 会跳过外发及回复，但管理命令仍执行；停用命令接收须关闭对应 `interactive` 开关。

本地 `--status` 只说明服务与接收线程状态。账号端权限、平台连接及消息送达需在实际环境用 `/help` 验证；自动化测试不会发送真实消息或修改平台设置。
