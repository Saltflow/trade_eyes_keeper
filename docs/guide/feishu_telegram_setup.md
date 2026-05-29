# 飞书 & Telegram 消息推送配置

## 飞书 Bot

### 1. 创建机器人

- 打开飞书，进入目标群聊
- 群设置 → 群机器人 → 添加机器人 → **自定义机器人**
- 设置名称（如"股票量化助手"），点击添加
- **复制 Webhook URL**：`https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx`

> 安全建议：在飞书机器人设置中开启 **IP 白名单** 或 **签名校验**，防止 webhook 泄露后被滥用。

### 2. 设置环境变量

```bash
# config/.env
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx
```

### 3. 启用

```yaml
# config/config.yaml
notification:
  feishu:
    enabled: true
    msg_type: "interactive"   # 交互卡片（推荐）或 "text"（纯文本）
```

### 4. API 参考

- 文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- 限流：100 次/分钟，5 次/秒（避免整点/半点发送）
- 消息体上限：20 KB
- 卡片模板编辑器：https://open.feishu.cn/cardkit

---

## Telegram Bot

### 1. 创建机器人

- Telegram 搜索 `@BotFather`，发送 `/newbot`
- 输入机器人名称和 username（如 `stock_quant_bot`）
- **记录 Bot Token**：`123456789:ABCdefGhijklmnOpQrsTuvWxyz`

### 2. 获取 Chat ID

**方式一：通过 API**
```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```
给 Bot 发一条消息后执行，从返回 JSON 中找 `"chat":{"id":-100123456}`。

**方式二：用 @getidsbot**
搜索 `@getidsbot`，拉它进群或私聊，它会回复 chat_id。

### 3. 设置环境变量

```bash
# config/.env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhijklmnOpQrsTuvWxyz
TELEGRAM_CHAT_ID=-100123456789
```

### 4. 启用

```yaml
# config/config.yaml
notification:
  telegram:
    enabled: true
    parse_mode: "HTML"
```

### 5. API 参考

- 文档：https://core.telegram.org/bots/api#sendmessage
- 单条消息上限：4096 字符（系统自动分片）
- 支持的 HTML 标签：`<b>` `<i>` `<u>` `<s>` `<code>` `<pre>` `<a>`

---

## 验证

```bash
# 部署通知会发送到所有 enabled 频道
python ci_cd_deploy.py

# 或手动触发
python main.py --once          # 完整日报
python main.py --brief          # 早盘简报
python main.py --brief afternoon_snapshot  # 收盘简报
```

## 排错

| 现象 | 原因 | 解决 |
|------|------|------|
| 飞书无消息 | webhook URL 不对 | 检查 `.env` 中是否有多余空格 |
| 飞书 code=19022 | IP 不在白名单 | 在飞书后台添加服务器 IP |
| 飞书 code=19024 | 关键字未匹配 | 添加关键字或关闭安全校验 |
| Telegram 无消息 | chat_id 不对 | 重新获取 chat_id |
| Telegram 403 | bot_token 错误 | 重新在 @BotFather 获取 |
