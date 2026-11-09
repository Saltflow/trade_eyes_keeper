# 两层项目工具发布及线上验收（2026-11-09）

状态：代码已提交、推送并部署；真实飞书入站至邮件收件链路通过，搜索及原文留档回读闭环通过。报告数据仍有明确缺口，不能判全部数据验收通过。

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

真实模型/资料查询已完成分项验收；新建一次性触发器 → 全监控池日报 → 启用渠道回执 → Gmail Message-ID 落箱已完成。报告数据完整性与投递结果分开判定。

- 当前生产启用 email、feishu，telegram 禁用；不为验收修改开关。已连接 Gmail 与生产唯一收件人仅做哈希匹配，结果一致，地址不写入报告。
- Windows 飞书客户端已运行，可访问性文本可读取，但截图多次返回 `window capture timed out` / `FrameArrived timed out`，输入点击返回 `coordinate input geometry is unavailable`，未代发消息、未改写用户草稿。
- 已请用户通过现有飞书会话发送“上线验收52eaaa0”一次性 3 分钟后完整日报请求，以验证真实认证入站；内部工具调用不能替代这一步。
- 首条真实入站原文只有 `请创建一次性触发器，名称“上线验收52eaaa0`，与会话保存文本一致，缺时间及后半段要求。入站鉴权、处理与回复成功，Bot 要求补充时间；没有 create 调用、触发器或报告任务。此项是验收输入不完整，不能记为邮件失败或全链路通过。已请用户重新完整发送。
- 该轮模型两次在 `trigger_manage list` 中误传仅适用于 `catalog` 的 `prefix`，被现有参数校验拒绝，随后恢复正常查询；不是创建或落库失败。保留原始错误，不绕过参数校验。
- 第二条真实入站完整，`trigger_manage create` 成功创建 #11（rev1、once、daily），Bot 回复已发送。计划北京时间 21:30:00，完整 26 标的、email/feishu；入站时间 21:27:07.924，模型将“3 分钟后”取整至分钟，实际相差约 172 秒。如实保留该时间精度，不改计划或重放。最终调度与实际收件结果见下文。

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
| 联网搜索、新原文留档及跨会话回读 | 初验默认 DuckDuckGo 三次 TCP 超时；修复为现有 Bing 后，真实模型默认搜索、官方原件留档及新会话回读通过，原始失败保留，详见后文 |

生产剩余磁盘约 9.65 GiB，低于已有 10 GiB 采集保留阈值；本次搜索先在网络阶段失败。未降低阈值或删除历史资料。其他现有 provider 的只读连通诊断另行记录。

### 线上搜索默认值修复

现有底层 `public_web.search_url` 默认 Bing，而 Bot `web_search.search_web` 单独默认 DuckDuckGo。显式调用既有 Bing 在生产成功取得 104,950 字节搜索页、完成解析与登记并提取 5 个链接；这只证明显式 provider 可用，不算当时 Bot 默认路径通过。

最小补丁仅将 Bot 默认值统一为 `bing`，保留显式 DuckDuckGo 支持；没有新增 fallback、重试、provider 路由机制或放宽公共抓取规则。配套测试使用对应 Bing HTML，并保留显式 DuckDuckGo 和失败不重试断言。独立审查确认身份、authority、归档及模型参数边界不变，`tests/validation/` 7 passed。部署及默认 Bot 闭环结果见下文；切换安排在 #11 报告结束之后。

- 最小修复提交 `8d6b95abda97767930fc69e88a91e7a25bd2aaf8` 已推送，远端 SHA 核对且 ahead/behind 为 0/0。专项 200 passed，加独立 validation 7 passed；最初缺 coverage 插件的环境错误单独保留，不声称测量覆盖率。
- 显式 Bing 搜索页跨进程回读通过；实际搜索返回的 IFRS、PwC 来源抓取两次在 DNS 阶段报 EAI_AGAIN，稍后同轮系统解析已恢复，未发现 hostname 提取错误。失败记录保留，不等同原文闭环通过。
- #11 于北京时间 21:30:00.067 自然到期生成唯一 daily run `37b6746ffee144578972a91f133ee12b`，request/owner/chat 和 26 标的范围一致；21:33 仍处于 `daily_stock_data` 阶段，worker 日志增长，没有投递回执。
- 21:35 附近 SSH 观测及隔离候选准备连接超时，未执行切换；只读恢复探测已成功。当时候选准备结果未明，先核对目录/进程，再恢复准备；该连接超时不能直接推断日报失败。

### 触发器 #11 的最终投递与数据结果

- run `37b6746ffee144578972a91f133ee12b` 于 21:34:25.848 结束，耗时约 265.77 秒；只产生一个 action、一个 run，无重复投递任务，触发器已结束。
- email `sent / smtp_accepted`；feishu `sent`，3 段回执均成功。全部当前启用渠道已完成；telegram 原本禁用，不计失败，也未为验收启用。
- Gmail 通过精确 RFC822 Message-ID 查询命中唯一邮件，`INBOX` 标签及 Google Received 头确认北京时间 **21:34:30** 收件，主题《触发日报 #11 · 股票日报 - 2026-11-09》，PDF 附件 113,509 字节。不是仅靠 SMTP 接受或发件箱推断。
- 实际收件 HTML 包含 26 个完整监控池标的；当前归档与保存时固定 SHA256 相同。真实 owner/chat 的 ReportTools 查询取得同一 run 和归档 `file_e349a5044d4020a3a67a7bac`，其他 owner 查询拒绝。归档额外有一行元数据注释，因此不得直接把归档文件 SHA 与 MIME 正文 SHA 当成同一种字节证据。
- 剥离归档首行 132 字节元数据后，28,682 字节正文与 Gmail MIME 解码正文逐字节哈希一致：`8236a976383a3e59119e9d644151277967a8044398524f00c542c2351e595135`。触发器说明段保留，没有通过去掉业务内容来凑匹配。
- 数据结果保留 `partial / data_not_ready`：24 个最近收盘、2 个 unavailable；C38U.SI、G3B.SI 报价获取失败，两行仍在正文，显示缺口而非伪造数据。Ke 曲线缓存 2026-11-01 已超过 7 天，按已有规则显示不可用；没有为验收隐式跑优化器。投递成功不等于行情/策略数据全项通过。
- 静态 worker 日志进一步定位：两个 SGX 标的均为 `api.sgx.com` 读取超时（30 秒），未发现 403 或证券代码无效证据；另 C38U.SI 公告支路的 `www.sgx.com` 配置请求发生临时 DNS 错误。行情与公告失败分开记录，没有为排查重跑报价或补发报告。

### 搜索修复部署回执

- SSH 恢复后，独立诊断确认候选目录不存在、无验证进程；因此才重新准备候选。服务器隔离测试 **207 passed / 0 skipped / 0 errors / 0 failures**。
- 待 #11 结束后，于北京时间 **21:42:25** 部署 `8d6b95ab`，只替换一个业务源文件；新 PID `1015222`，ready/fresh/transport_ready 与 build verified 均通过。
- 新源码总摘要 `5f224269a71075eb7142b3ee33628e68783f0ccc296f94f633819921b599a45c`；284 份受管理源码全部匹配，228 份保护文件未变，26 标的及调度配置不变，触发历史在切换前后未变。
- #11 的真实邮件来自 `52eaaa0`；不得把它写成修复后版本生成的报告。`8d6b95ab` 的变化仅搜索默认值，另做该路径真实模型验收。
- 21:43:27 独立复核：源码/manifest/保护文件全部相符，NRestarts=0，无新增 failed/partial/unknown、重复任务或服务日志错误信号；通知总开关、email/feishu 开关均未被跳过变量屏蔽，临时 drop-in 已清除。

### 修复后的真实模型留档闭环

- 在 `8d6b95ab` / PID `1015222` 上，真实模型通过原 schema 调用 `search_web`，没有传入或覆盖 provider；实际走默认 Bing，返回 IFRS 18 官方链接。
- 模型从本次真实搜索结果选择两个官方 URL，显式 `refresh=true` 获取原件，分别保存 595,744 / 124,080 字节，解析 complete、目录 registered。没有直接指定猜测 URL 来冒充搜索闭环。
- 另一个进程及新会话用 `data_find` / `data_read` 重新发现并读取已归档材料；该轮禁止来源搜索/抓取工具，模型 API 仍正常联网，不宣称整个进程零网络。两段真实模型调用分别 completed；不是依赖上一轮对话记忆，也没有重新下载、OCR 或后台研究任务。
- 新会话两次 find 均找到 `material:05a4aaeff5abb768ee4ba7aa2ccb102e0c879de037a7cbf30617e8349aa5ef7f` 与 `material:14cfa7332182095abb86860bdbd240852dda21bf423bcd12d39540e998fe24c0`；6 次 read 返回非空正文，来源 SHA 与首次抓取一致，正文 SHA 在分页间稳定。回读只调用 find/read，没有来源联网工具调用。
- 这部分是生产真实模型与工具、隔离验收会话及捕获回复；没有通过飞书发送这两次查询，不将它写成第二次真实飞书入站验收。真实飞书入站的证据是前述 #11。
- 原始 DuckDuckGo 超时、IFRS/PwC 的临时 DNS 失败均保留；后续成功不抹除失败，也不承诺所有外部站点可达。资料存在不代表所有财务事实已核证。

## 原始证据

本地 `.tmp/two_layer_release_20261109/`：`scope.json`、`commit.json`、`staged.patch`、`request.json`、`prepare.json`、`cutover.json`、`mail_target.json`、`net_preflight/`、`net_postflight/`；真实业务证据保存在 `visibility/` 与 `mail_acceptance/`。搜索修复发布证据另存 `.tmp/web_search_release_20261109/`，含首次 SSH 准备失败记录、207 项服务器测试及最终部署核验。`.tmp` 为本地证据，不受 Git 跟踪。

生产备份及服务器测试回执：`data/deployments/two_layer_20261109_52eaaa0c/`。保护文件只保存摘要，不将配置凭据或收件人正文写入发布说明。
