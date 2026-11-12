# 项目工具补缺与发布验收（2026-11-12）

本记录接续 2026-11-09 的两层接口上线。代码 `ec10f4527ecdf6c98152cea2540a9201e9f3615a` 已推送 `origin/master` 并于 2026-11-12 20:04:26（Asia/Shanghai）部署。下列离线、生产模型、实际渠道验收分开记录。

## 实现与删除

| 分支 | 本轮结果 | 退役及保留边界 |
| --- | --- | --- |
| DS 搜索成功/鉴权失败/限流/空结果/坏 JSON/慢流 | 普通 Bot 与投研共用 `POST /v1/tools/search`，已有密钥、AuthorityHTTP、DataHub；归档原始 JSON，解析和登记分别回执 | 删除 Bing/DDG HTML 提取和 provider 选择，无自动换源、重试或新搜索框架；来源原文仍受公网逐跳校验 |
| 固定日报/简报/旧报告筛选 | 删除注册、回调、筛选与可写配置；旧已存提案拒绝执行 | 保留历史 YAML 可读、手动完整报告、现有 worker、数据采集时钟；不迁移或重放旧报告 |
| 邮件 PDF/其他附件成功、生成失败、归档失败 | 保存实际 MIME 解码字节及固定哈希；run 关联私有附件 ID，可跨会话查询本人同会话报告 | owner/chat、路径、symlink/hardlink、哈希校验；隐藏目录不入公共索引；无历史附件不猜、不重发，无解析不暗中 OCR |
| 监控列表/参考持仓/保存配置 | 明确业务工具直接接原函数及 ProposalStore | 删除对应 Bot slash 字符串往返；即时命令与业务工具共函数；保留回测旧执行及既有权限/确认 |
| 保存配置的 Git 范围 | `git commit --only -- config/config.yaml` | 不夹带其他已暂存文件；配置未变不误提交其他文件；真实 Git 失败仍返回失败 |
| 相对时间/消息重试/无效平台时间 | `schedule.after_seconds` 以平台消息时间或既有持久 turn.created 计算，保留秒/微秒 | 不引入新调度器、不向当前处理时间顺延；`list` 仅 state，prefix 仅 catalog |

生产 urllib3 为 1.26.5，无 read1；使用原 requests 流接口的单字节块并要求 identity 编码，以便滴流期间持续检查取消/截止时间，无需更换依赖。连接/空闲读取沿用最多 20 秒 timeout；已在阻塞中的一次网络读取仍受该 timeout 控制，不将软截止描述成即时中断。

## 离线证据

各组可能重叠，不相加；原始失败保留。mock 外部 API/渠道不代表真实收件。

- G05：旧版本精确反例 11 failed，修后 11 passed；集中 541 passed，最终配置/cron 小改另有 129 passed。`.tmp/g05_retirement_20261109/receipt.json` 固定源码和各 XML 哈希。
- 管理入口：11-09 的 Node 24.19 补验已覆盖原环境错误，按用例去重 223 passed；11-12 复核结果。新增 Git 提交范围的真实隔离仓库红绿验证 2 failed→6 passed，保留他人暂存及真实锁失败。`.tmp/structured_management/`。
- 附件：原 328 项中 2 个旧成功 fixture 把 PDF 模拟为 None，与新 partial 语义冲突；修正 fixture 后 11-12 最新 169 passed，含两个入口、权限绕过、哈希变更、归档/SMTP失败和 validation。`.tmp/email_attachment_acceptance_20261112/`。
- DS 最终流读取、API/归档与失败边界：24 passed，`.tmp/gaps_release_20261112/search-final.xml`。共享源抓取、投研搜索、触发器、真实会话时间及 validation 组合首轮 202 passed / 7 failed / 2 errors：1 项旧调度提示断言更新，另 8 项由 PATH Node 22.17 导致；切到已有 Node 24.19 后这 9 项全部通过。原始 root-tests.xml/log 与 trigger-recheck.xml/log 保留，不声称测量测试覆盖率。

## 仍需区分的边界

- 新附件私有读取不等于已把历史 HTML 邮件公共资料策略全部重做；历史正文继续沿用原可见范围。
- SGX 源站超时、Ke 缓存过期和既有原件缺解析属于数据可用性，不因代码回归通过而消失；不隐式跑优化器、付费 OCR 或重发历史报告。
- SMTP sent 只证明 SMTP 接受；真实收件要独立验证收件箱。PDF 失败但 HTML 已发送时报告 partial，SMTP 子回执仍 sent；归档失败不改写发送事实。
- 本轮未恢复旧任务或改变已有触发器定义；所有上线/模型调用/新报告证据应注明实际代码版本。


## 发布与生产验收

- 本地工作目录 `E:\CodeBases\trade_eyes_keeper`，分支 `master`；代码提交 ec10f452 推送后 `origin/master` 与本地相同，ahead/behind 为 0/0。48 个提交文件；业务源码新增 965、删除 1432（净 -467），含业务函数移动。其他 SGX/XBorder、抓取器、文档及忽略规则改动未纳入本次代码提交。
- 生产进程 1015222 → 1057714；20:04:26 切换后 ready/fresh/transport_ready。源码清单 283 文件逐一验证，聚合 SHA256 `3af7efa30aeef88bdae28c649691fc2fdc5f8cd19125506d7f9eda97fccda4ac`；228 份保护配置/持仓文件未变。21 个安装路径含源码删除及三份说明，旧报告 cron 为 0，临时通知抑制 drop-in 已清除，正式进程没有跳过通知。切换前后旧触发器/历史动作未变。
- 用冻结发布源码在服务器隔离运行 871 项，0 failed / 0 errors / 0 skipped；外部网络被阻断。这是回归证据，不代替下列真实模型/渠道证明。切换备份及 SQLite 快照在服务器 `data/deployments/project_gaps_20261112_ec10f452/backup`。

### DS 搜索、原文与机器人可见性

- 真实 DS Search 请求一次，约 0.619 秒；返回 6 条，原始 JSON 19061 字节，解析完成并登记，材料 `material:4e578153ef5ab530940f5481bc8e2d0551d8ce22952909c642ad26f43ab66d50`。
- 初次原文调用被验收脚本的 URL fragment 字符串比较拦截，未出网；只修验收脚本为已有 canonical_url 比较，没有改产品/重发搜索。随后 IFRS 官方实施支持 PDF 一次下载成功，456234 字节、18 页本地解析完成且无 OCR，材料 `material:6f185e98de510c295b2c02bd8946d6831f4f7a63a1651fc48a148f1c809950c4`。这是官方实施支持材料，不是准则正文。
- 新会话关闭联网工具后，真实模型通过 data_find/data_read 重读上述已存原件并引用位置；当前能力/状态查询实际返回 54 项可见工具（报告 3、持仓 8、触发器 1、投研 14、资料 12、管理 16）和已部署版本。
- 以上使用生产源码、真实 DS API、隔离会话及捕获回复；不声称本轮重新覆盖真实飞书入站、SDK事件与模型选触发工具整条路径。

### 新触发器、完整日报与收件

| 验收项 | 实际结果 |
| --- | --- |
| 创建入口 | 使用线上 TriggerTools，为既有验收用户/会话新建 #16「部署验收ec10f452」，after_seconds=180；直接工具调用，不冒充飞书入站 |
| 基准及到期 | 20:07:44.178518 → 20:10:44.178518 +08:00，精确 180 秒 |
| 实际执行 | 20:10:44.280194 启动，晚约 0.101676 秒；20:15:26.869917 完成，耗时约 282.59 秒 |
| 唯一执行与范围 | 一个 action，一个 run `20f3ced646864a658c33b91311c28719`，trigger done、run succeeded；完整配置 26 标的，26 last_close、0 unavailable，quote_issues 为空 |
| 邮件 | SMTP 接受；按实际 Message-ID 搜索 Gmail，唯一邮件 `1a1cfaf48d31ded1` 位于 INBOX，主题「触发日报 #16 · 股票日报 - 2026-11-12」 |
| 飞书 | 价格与基本面、事件速览、收益曲线三个分片均有平台 message ID 和成功回执 |
| PDF | 实际收到「日报.pdf」108285 字节，与服务器 MIME 解码归档长度及 SHA256 `e3594a41d710897835ca87970e08f8a794d0717388a580417a9c6005916dbfdb` 完全一致 |
| 附件可见性 | report_get 返回私有 report_artifact ID，合法读取完成哈希校验，其他 owner 被拒绝；无既有正文解析，明确 existing_text_parse_unavailable，不虚称能读 PDF 正文 |

所有启用渠道（email、feishu）已实际发送；Telegram 当前未启用，未改渠道配置。没有重放历史报告。日志中部分分红外部 API 空响应/取数失败仍保留，不将 26 只行情就绪扩展成所有财务字段齐全；此前 Ke/解析等缺口也未由本轮验收自动消除。

本次说明更新仅同步这三份文档，不重启服务或变更运行源码清单；生产运行代码仍为 ec10f452。原始回执在本地 `.tmp/gaps_release_20261112/`：progress/cutover/postflight、post_search/post_sources/post_reread/capabilities、report_acceptance/report_status/mail_receipt/gmail_metadata/gmail_receipt；服务器部署目录保留运行及模型证据。邮件原始 MIME 仅留在本地忽略目录，不提交收件人信息或凭据。
