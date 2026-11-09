# 两层项目工具发布及线上验收（2026-11-09）

状态：代码已提交、推送并部署；真实业务验收进行中。不能把服务就绪或历史回执当成新版本完整投递通过。

## 发布范围与身份

- 代码提交：`52eaaa0c2ed0f5de87bc5afa06eaffca19eec94a`，`origin/master` 推送后已通过 ls-remote 核对，同步差为 0/0。
- 提交 65 个文件，生产安装 23 个业务源码；不携带 SGX 预研、`.gitignore`、`src/core/data_fetcher.py` 的已有修改。共享项目文档只暂存本轮追加内容，工作区原字节保留。
- 生产全量受管理源码由 281 个变为 284 个（新增 3 个源码文件），与该 Git 提交逐文件完全一致；清单外差异 0。
- 源码总摘要：`bef7fdd7ae11b3273cf349d982336c2876a72be7f70f7576e325f511dfe89e64`。
- 部署 ID：`20261109T210951444986+0800-bef7fdd7-b1bc1a22b2c1`；上线北京时间 21:09:51。

## 验证与切换

- 发布前[离线回归](offline_regression_20261109.md)：全仓首轮加修复路径专项，去重 5611 passed / 19 环境 skipped，离线范围无未解决失败。保留首轮失败与复验边界。
- 服务器隔离 Git 候选目录中，18 组目标共 **377 passed / 0 skipped / 0 errors / 0 failures**；测试禁止外部连接，不用生产凭据或通知目标作为测试配置。
- 切换前核验源文件、空闲任务/模型队列，备份原文件、Git index、release manifest，并在停服后备份执行/持仓 SQLite。
- 原子替换 23 文件，Git 从既有旧基线安全对齐新提交，不 reset 无关工作区；失败路径可恢复原文件/manifest/index/ref 并启动旧服务。
- 新宿主 PID `1012579`、飞书接收进程 `1012587`；ready/fresh/transport_ready、build verified 均通过，NRestarts=0。
- 228 个配置/参考持仓/优化器保护文件在切换前后哈希不变，触发历史未改写；完整监控池 26 个标的，固定 daily/brief 调度保持关闭，旧事件未重放。
- 临时 systemd drop-in 已清除。新进程继承的 `SKIP_STARTUP_NOTIFICATION=true` 仅抑制启动通知；`SKIP_NOTIFICATIONS`、`SKIP_EMAIL`、`SKIP_FEISHU`、`SKIP_TELEGRAM` 均未设置。
- 21:15:16 独立只读复核：Git/manifest/284 源码一致，无新增 failed/partial/unknown 或重复 job+request 组。接收器存活与 transport_ready 不替代真实入站验收。

## 真实业务验收

真实模型/资料查询已完成分项验收；新建一次性触发器 → 完整日报 → 启用渠道回执 → Gmail Message-ID 落箱尚未完成。

- 当前生产启用 email、feishu，telegram 禁用；不为验收修改开关。已连接 Gmail 与生产唯一收件人仅做哈希匹配，结果一致，地址不写入报告。
- Windows 飞书客户端已运行，可访问性文本可读取，但截图多次返回 `window capture timed out` / `FrameArrived timed out`，输入点击返回 `coordinate input geometry is unavailable`，未代发消息、未改写用户草稿。
- 已请用户通过现有飞书会话发送“上线验收52eaaa0”一次性 3 分钟后完整日报请求，以验证真实认证入站；内部工具调用不能替代这一步。
- 首条真实入站原文只有 `请创建一次性触发器，名称“上线验收52eaaa0`，与会话保存文本一致，缺时间及后半段要求。入站鉴权、处理与回复成功，Bot 要求补充时间；没有 create 调用、触发器或报告任务。此项是验收输入不完整，不能记为邮件失败或全链路通过。已请用户重新完整发送。
- 该轮模型两次在 `trigger_manage list` 中误传仅适用于 `catalog` 的 `prefix`，被现有参数校验拒绝，随后恢复正常查询；不是创建或落库失败。保留原始错误，不绕过参数校验。
- 第二条真实入站完整，`trigger_manage create` 成功创建 #11（rev1、once、daily），Bot 回复已发送。计划北京时间 21:30:00，完整 26 标的、email/feishu；入站时间 21:27:07.924，模型将“3 分钟后”取整至分钟，实际相差约 172 秒。如实保留该时间精度，不改计划或重放。等待正常调度与实际收件结果。

### 项目工具可见性

使用生产真实模型及已部署工具实现，隔离验收对话状态并捕获回复；这部分不替代飞书真实入站。未提交报告、触发器、补采、OCR 或研究任务，未发送聊天或邮件。

| 路径 | 结果与边界 |
| --- | --- |
| 能力和状态 | 51/51 工具可见，六组数量 3/1/7/14/12/14；部署版本及服务状态正确 |
| 历史邮件 | 发现 262 个归档，读取两份正文；没有匹配的授权投递回执，未把正文存在当成邮件送达 |
| 已有财报全文 | 002281 的 2022 年已有全文可检索、读取并引用来源文本行；没有 PDF 页码映射则不猜页码 |
| 结构化投研 | 600519 的 1455 行行情和 44 个财务期间可查询，返回 dataset/file 标识及覆盖边界 |
| 600519 财报原文 | 本次未取得；存在缺 URL/PDF 的记录，分页检索不完整，不能推断全库不存在 |
| 已有 CICT 原件 | authority 可解析到原件，但没有现存文本解析；如实返回缺解析，未自动补跑 |
| 联网搜索、新原文留档及跨会话回读 | 尚未通过：默认 DuckDuckGo 三次查询超时；DNS 成功，TCP 443 超时、尚未到 TLS，未取得链接或归档，不用直接抓取冒充搜索成功 |

生产剩余磁盘约 9.65 GiB，低于已有 10 GiB 采集保留阈值；本次搜索先在网络阶段失败。未降低阈值或删除历史资料。其他现有 provider 的只读连通诊断另行记录。

### 线上搜索默认值修复

现有底层 `public_web.search_url` 默认 Bing，而 Bot `web_search.search_web` 单独默认 DuckDuckGo。显式调用既有 Bing 在生产成功取得 104,950 字节搜索页、完成解析与登记并提取 5 个链接；这只证明显式 provider 可用，不算当时 Bot 默认路径通过。

最小补丁仅将 Bot 默认值统一为 `bing`，保留显式 DuckDuckGo 支持；没有新增 fallback、重试、provider 路由机制或放宽公共抓取规则。配套测试使用对应 Bing HTML，并保留显式 DuckDuckGo 和失败不重试断言。独立审查确认身份、authority、归档及模型参数边界不变，`tests/validation/` 7 passed。部署及默认 Bot 闭环验收结果待补充；不在 #11 报告运行时停服。

## 原始证据

本地 `.tmp/two_layer_release_20261109/`：`scope.json`、`commit.json`、`staged.patch`、`request.json`、`prepare.json`、`cutover.json`、`mail_target.json`、`net_preflight/`、`net_postflight/`；真实业务证据会保存在 `visibility/` 与 `mail_acceptance/`。`.tmp` 为本地证据，不受 Git 跟踪。

生产备份及服务器测试回执：`data/deployments/two_layer_20261109_52eaaa0c/`。保护文件只保存摘要，不将配置凭据或收件人正文写入发布说明。
