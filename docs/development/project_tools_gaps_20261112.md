# 项目工具补缺与发布验收（2026-11-12）

本记录接续 2026-11-09 的两层接口上线。11-12 19:48（Asia/Shanghai）生产只读核对仍为 `8d6b95ab`，服务/飞书接收线程就绪；本轮候选尚未发布。以下测试是离线证据，生产切换与真实模型/通知验收另外记录。

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
