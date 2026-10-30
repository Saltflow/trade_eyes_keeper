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

在飞书开发者后台创建企业自建应用并启用机器人，配置消息接收事件 `im.message.receive_v1`。
私聊需要应用身份权限 `im:message.p2p_msg:readonly`，群聊 @ 需要
`im:message.group_at_msg:readonly`，回复需要 `im:message:send_as_bot`。
在事件订阅方式中选择“使用长连接接收事件”，发布应用并把机器人加入需要的聊天。

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

私聊完全没有回复时，先核对聊天对象是否与 `FEISHU_APP_ID` 对应的应用机器人一致，
再检查开发者后台的已发布权限、接收事件和长连接订阅方式。SDK 有出站连接并不证明
消息正在投递给本进程。服务日志中的 `Feishu message routed` 显示接收后的分流原因；
`Feishu message rejected: code=...` 表示平台拒绝回复；
`Feishu message delivered` 表示发送 API 已接受，仍需实际客户端确认收件。
通用服务日志不记录自然语言正文或令牌；助手正文脱敏后保存在受限的 SQLite 中。
助手 SQLite 的 receipts 仅统计进入助手的消息，
旧命令与提前忽略的事件不会产生该收据，不能仅凭 receipts 为 0 认定平台未投递。

## DeepSeek 自然语言助手

2026-10-29修复候选（未部署）：需要资料的研究问答强制走 Pi 自动工具循环，模型请求继续使用 thinking；单股票查询结果只作为模型输入，不直接拼模板回复。自然语言估值问题不再返回旧研究退役提示。资料不足时继续查询或说明已查范围，目录和旧会话不冒充本轮证据；没有实际检索就直接作答会被拦截并要求补查一次。

启用助手前需准备 Node >=22.19，并在 `tools/feishu-pi` 执行 `npm ci --ignore-scripts --no-audit --no-fund`。`harness_backend` 控制普通问答的后端，研究问题始终使用 Pi，缺少依赖时不自动降级直答。只读检索不需要确认提案；配置变更和脚本/回测仍需明确确认。持仓、提醒的业务回执保持原合同。


飞书私聊可以直接提问，群聊需要明确 @ 当前机器人。助手复用 `DEEPSEEK_API_KEY`
和 `llm.base_url`，单独使用 `interactive.feishu.assistant.model`（默认
`deepseek-flash`），启用 DeepSeek `high` 思考模式；服务端最多执行 16 轮模型请求、
48 次工具调用和 300 秒对话预算。对话调用次数和历史记录不占用公告提取的每轮额度。
在配置样例的 `interactive.feishu.assistant` 中启用功能并重启服务。

示例：

- `现在日报什么时候发送？`：查询实际配置后解释。
- `把普通日报改成每周五发送`：展示原值、新值、影响范围及生效方式。
- `日报只在有策略信号时发送`：机器人预览并保存一条声明式规则；可再说“改成所有条件都满足”来切换 any/all。
- `早盘简报标题含年报才发`：机器人按报告数据路径和 contains 运算符生成规则；手动 `/brief` 仍按原行为发送。
- `把 A 股市场的搜索预算调整为 10000`：按市场 Solver 配置生成提案。
- `用当前策略验证 600036 在 2023-01-01 到 2025-12-31 的表现`：展示策略、参数、数据补齐范围与脚本。
- `生成 Python，把上述回测的窗口结果绘图`：生成脚本预览，在隔离容器中运行。

确认时必须由发起人在原聊天发送 `确认 <12位编号>`，例如
`确认 a1b2c3d4e5f6`。群聊的确认、取消和查询同样需要 @ 机器人。提案默认
15 分钟有效，完整预览和脚本附件送达后才能确认。配置或批准的源码变化后需要
重新预览；一条确认只执行一次。多个配置文件分别生成提案，确认一项后若另一项
依赖的版本变化，会要求重新生成预览。

`取消 <编号>` 可以取消提案、排队任务或停止执行；`进度 <编号>` 查询阶段；
`获取 <编号>` 重新发送预览或已完成结果文件。文件发送失败不会重新执行任务。
`清空对话` 只清理当前发送者的对话上下文，保留提案和任务记录。

现有斜线命令继续按原逻辑直接执行。自然语言中引用 `/config` 等命令不会执行该命令。
凭证、部署、聊天权限和容器权限不属于自然语言配置工具开放范围。

日报和每个简报可分别配置声明式触发规则，不需要改代码或增加固定事件判断。规则对象为
`{mode: any|all, conditions: [...]}`；多个条件按 mode 组合。每条 condition 支持
`path`、`operator`、`value`，path 从 `dividend_events`、`placements`、`announcements`、
`signal_scan` 或 `alerts` 等报告数据根开始，支持 `*` 通配段；运算符有 `exists`、
`non_empty`、`contains`、`equals`、`gt`、`gte`、`lt`、`lte`。例如策略信号非空可设为
`{mode: any, conditions: [{path: signal_scan.alerts, operator: non_empty}]}`；
年报公告可用 `announcements.*.*.title` 配合 `{operator: contains, value: 年度报告}`。空对象保持原有发送行为。
用户通过机器人修改时先预览再确认。规则只筛选定时日报/简报，不改变手动 `/daily`、
`/brief` 和既有独立告警行为。

脚本附件和结果下载需要应用具有文件上传权限 `im:resource`，并继续具备
`im:message:send_as_bot`；在飞书开发者后台发布权限变更。若附件权限未生效，
提案保持 `preview_failed`，不能以不完整的预览启动脚本。

研究工作保存在部署项目的 `data/feishu_research/<编号>`；提案、去重和状态位于
`data/runtime/feishu_assistant.sqlite3`。配置变更通过 ConfigStore 保存，调度会刷新
运行中任务，策略及其它运行设置在下次任务生效。研究配置与代码在提案时冻结，
确认后队列任务继续使用该快照。

新研究区间至少为 24 个自然月，若项目合同要求更长则按合同执行。已有部署中的
旧策略方案会在修改预算等独立字段时保留；修改或切换评估方案时会重新检查窗口
长度、留出验证与重叠隔离。需要的预热行情及财报历史可能早于研究起点，均会列在预览中。

补数由主机上的现有数据准备服务执行，复用供应商共享限流。只补充批准的标的、
时间范围及必要基准，无法取得的真实数据会明确报告。容器始终离线，无法读取
主机 `.env`、机器人凭证或 Docker socket。回测结果沿用 raw 价格、公司行动、
分红税费与基准合同；正式策略验收仍需按配置全量运行，研究任务不自动激活策略。

部署及 Docker 构建步骤见 [部署说明](../deployment.md#飞书研究助手部署)。

### 项目资料与问题排查

助手每轮自动检索部署主机的项目文档并加载仓库模块概览，可继续列目录、关键词搜索、
按行读取源码和测试，或用 Python AST 查看函数定义位置。它会沿函数名继续查调用者，
按需要多轮查询，无需用户逐步告诉它下一条工具指令。
回答项目规则时引用 `文档路径:L起始行-L结束行`。自动文档检索使用 `knowledge.py`
列出的说明文档，进一步查代码使用下述仓库只读范围；两者均拒绝范围外路径、符号
链接、凭证和部署记录。当前配置与执行结果仍以对应工具为准，不能把旧文档中的
默认值当成运行状态。
公告栏筛选与时效说明见 [公告说明](announcements.md)。

从此版本起，`data/runtime/feishu_assistant.sqlite3` 的 `conversation_turns` 保存
每轮的问题、回复、状态、错误、时间和关联消息 ID；`conversation_events` 保存检索
片段及来源哈希、模型返回、工具参数/结果、耗时和飞书发送结果。凭证先脱敏，正文
最长 16000 字符，单事件 JSON 最长 8000 字符、每轮最多 100 项；超限有截断标记。
通用服务日志用 `Assistant turn started/finished: trace=...` 关联同一轮，API 失败回复
也带排查编号。可以在主机上通过 SQLite 的 `conversation_turns.id` 与
`conversation_events.turn_id` 联查，不需要用户重新整理聊天记录。

留档默认 30 天，可由部署管理员调整 `conversation_retention_days`（1–365）；在
启动及接收新消息时清理到期会话，提案、任务和执行审计不随之删除。数据库正文属于
聊天记录，应按部署主机的访问权限和备份策略保管，不开放给其他发送者的模型工具。
重启将未完成问答标为 interrupted，不自动重发或重新执行；有效期内成功问答可恢复
同一聊天和发送者的上下文。`清空对话` 阻断旧上下文恢复，但排查留档按期限保留。
这项记录不追溯补齐旧版本漏记的问答，也不改变斜线命令原有日志方式。

### 仓库理解与 Pi 多轮回答

仓库工具覆盖 `src/`、`scripts/`、`tests/`、项目说明、配置样例和配置加载代码、
主入口以及 `tools/feishu-pi` 桥接源码；不读取 `.env`、真实 YAML 配置、运行数据、
聊天数据库、历史操作记录或 Git 内部文件。实际公告开关、日期窗口和提取设置通过
`read_configuration` 的 `announcements` 前缀查询，返回 `writable:true`，
未配置的值与代码默认值分别标明。七项公告业务参数支持“提案→预览→确认→保存”，
下次任务读取，无需登录主机。日期窗口为1–3650天，LLM成功提取计数限制为0–1000，
PDF体积配置为0.1–100 MiB；计数限制按单次公告丰富调用计算，不是全报告请求总额。
如发送“把普通公告回看窗口改为8天”，助手先展示原值/新值，只有回复确认编号后保存。
这些日期窗口用于筛选数据，不等同于公告正文或问答记录的留档期限。

`harness_backend: pi` 使用真实 Pi Agent 核心控制多轮；所有工具和模型请求通过
stdio 回到 Python，由已有权限与参数校验分发。Pi 不接收密钥，也没有 shell、文件
写入、插件自动加载或直接执行工具。`native` 保留同一工具集合与预算的 Python 循环，
管理员可以显式切换；Pi 出错不会自动重跑另一套流程。

模型可搜索、读代码、再查调用位置，直到形成答复或用完预算。每轮限制工具次数、
模型轮数、上下文字符数及时间；旧工具内容按需压缩，最后一轮留给无工具总结。
Pi 子进程由定时器在时间预算到期时清理，HTTP调用也使用剩余时间限制连接/读取；
网络超时是连接/读取超时，不能把它理解为硬实时的整轮完成保证。
资料按真实行号传入模型，引用必须落在本轮读取范围；不合格会自动要求修正一次，
仍不合格则返回明确错误并保留轨迹。此检查验证引用范围，不能代替对内容含义的判断。

仓库扫描默认最多5000文件、64MiB，单文件1MiB；搜索有时间和结果数量上限。
`truncated:true` 表示未扫描完整，应缩小目录再查，不能据此声称不存在某功能。
文档或代码与当前配置不同，以实际配置和生效时机为准；回测及配置修改仍须确认编号。
# 统一数据目录入口

助手支持跨数据集查询文件、覆盖范围、财报原文及文字版；详见 [统一数据集目录](dataset_catalog.md)。消息包含六位股票代码时，服务端会在模型回答前自动查索引中的所有数据集，无需用户先找 `dataset_id`；该查询和结果也进入本轮排查日志。需要外部当前信息时可搜索公开网页并返回链接；搜索摘要应与本地核验数据区分。网页搜索要求部署主机允许访问外部 HTTPS；若主机没有可用出网路由，机器人会明确报告联网失败，不会用模型记忆补造数据。索引必须检查分页总数和时间；当前默认时点库的结果不代表服务器全部数据。文档缺失状态可查询，下载与实际研究执行分别管理。

明确要求读取、获取或分析 A 股完整年报/半年报时，助手会从巨潮资讯查找指定或最新一期公告、下载原 PDF 并登记到统一数据目录。文字层 PDF 保留逐页标记；扫描 PDF 用 DeepSeek Flash 视觉接口分批识读，失败页明确列为缺口。单份文件上限为 50 MiB、300 页。机器人回复解析结果及页码依据，不自动发送 PDF；可用返回的 `text_file_id` 调用 `read_dataset_text` 继续分页读取全文。助手不接受模型或用户工具参数提供的任意下载 URL。

若没有可用行情与时点财报，助手会按单一代码调用项目现有的点时补齐服务，使用共享供应商限流和数据验证，将结果写入 `data/point_in_time/assistant_ad_hoc` 并增量登记到统一索引。同一代码30分钟内不会重复发起补数，每小时最多自动补6个代码。指定 `dataset_id` 的查询保持只读。
