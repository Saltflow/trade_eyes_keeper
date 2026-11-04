# Bot 业务数据读取覆盖清单（2026-11-04）

本表区分生产元数据盘点、本地离线读取验证和真实 Pi 调用。文件存在、索引命中、
正文可读、业务数据完整、机器人完成真实调用是不同结论；未取得对应证据时不合并宣称。
本文记录开发验收状态，不代表代码已经部署，也不保证生产全部历史文件均可解析。

## 生产元数据基线

证据：`.tmp/business_formats_inventory_20261104/remote_evidence.json`，
采集时间 `2026-11-04T12:43:04.926299+00:00`。采集脚本只读取文件元数据，未读取业务正文、
打开数据库、重建索引、调用来源/模型或发送通知。基线文件属于本地验收产物，不随本文发布。

声明范围为生产 `reports/`、`output/`、`outputs/`、`cache/`、`data/`。
扫描检查 68,269 个目录项，范围内完成，元数据访问错误为 0；排除私有账户、会话、
凭据样文件、部署/代码包、隐藏文件与符号链接等 749 项。
其中 `data/runtime/` 仅盘点 `reports/` 和 `trigger_snapshots/`，未盘点私有数据库。

| 格式 | 范围内文件数 | 证据能够证明的内容 |
| --- | ---: | --- |
| JSON | 31,161 | 格式后缀与大小；不等于 schema 或内容已验证 |
| CSV | 5,792 | 同上 |
| Markdown | 2,233 | 同上 |
| HTML | 692 | 其中邮件归档 256；按路径/名称识别的报告 HTML 360，可重叠 |
| TXT | 600 | 仅后缀与大小，未做全文解析 |
| JSONL | 283 | 未逐行验证 |
| YAML | 261 | 未做安全解析或业务 schema 验证 |
| PDF | 165 | 未读取文本，不能推断含文本层或可提取 |
| SQLite3 | 127 | 仅按后缀计数，从未打开，不能据此赋予 Bot 查询权限 |
| XML | 7 | 业务语义未确认 |
| XLS | 2 | 业务语义未确认，未读取单元格 |

生产未在上述范围观测到 XLSX、Parquet/Arrow 等文件；这不是未来格式支持的承诺。
生产依赖检查：`pandas 2.3.3`、`openpyxl 3.1.5` 可导入；`pyarrow` 不可导入；
`xlrd` 未检查。依赖声明或本地安装不等于生产依赖已就绪。

按目录，`data/` 识别 41,220 份、`cache/` 101 份、`reports/` 2 份 Markdown；
生产 `output/` 和 `outputs/` 在此次采集时不存在。
没有发现名称/目录含 weekly、week_report 或“周报”的文件，但未读正文，
因此不能据此判断历史内容是否按周汇总。

## 报告和索引的业务边界

- 当前完整报告执行入口只有 `reports.daily`、`reports.brief`；托管 `optimize` 预留，
  不执行；没有托管 `reminder` 报告。个人监控提醒是独立业务。
- `scheduler.daily_report_frequency=weekly` 是日报的发送频次，默认周五、可配置星期，
  不是独立周报 worker。旧 fundamental 周报及专用历史查询入口已删除；公开历史导出仍可经通用数据工具读取。
- 日报/简报任务保存在 `data/runtime/feishu_assistant.sqlite3` 的 `execution_runs`，
  运行文件在 `data/runtime/reports/<run_id>/`；结果与逐渠道投递收据关联。
- `data/email_archive/` 是 HTML 副本，生成时间早于实际发送；副本存在不证明已投递。
  日报 PDF 经临时目录编译后作为字节交付，不能假定有持久 PDF 档案。
- `data/runtime/trigger_snapshots/last_daily.json`、`last_brief.json` 是最新数据快照，
  不是历史报告库；快照生成不等于该次投递成功，也不保证全部字段同样新鲜。
- 搜参每市场运行保存在 `data/optimizer/runs/<run_id>/`，候选 `manifest.yaml`
  与活动指针 `data/optimizer/latest_strategy.yaml` 分开。搜索完成不代表已激活。
  现有搜参通知没有与完整报告相同的持久逐渠道回执合同。
- 本轮 DatasetCatalog 增加公开邮件/报告归档与 `output/outputs` 业务根，
  `data_find` 会增量登记符合规则的现有产物，不生成新报告。
  `runtime/` 和私有账户路径仍排除；快照、执行记录通过认证适配器读取。
  原有 `read_dataset_text` 仍只读取已索引 TXT/MD；新增 `data_read`
  才承担多格式读取、脱敏与分页。不能把开放公开归档理解为开放任意私有路径。

源码依据：`src/core/report_execution.py`、`src/task_runtime/execution.py`、
`src/triggers/snapshots.py`、`src/notification/email_notifier.py`、
`src/notification/daily_pdf.py`、`src/search/artifacts.py`、
`src/search/run_diagnostics.py`、`src/notification/manager.py`、
`src/data/dataset_catalog.py` 与 `main.py`。

## 本地验收记录

| 能力 | 已实际验证的范围 | 尚未证明的内容 |
| --- | --- | --- |
| `query_stock_fundamentals` 的规范化文件名 | `BRK.B` 落盘为 `BRK-B.*`、`BRK-B`、数字代码；全索引和指定 dataset 两种查询；披露日、价格、财报条数；已有数据不触发补采 | 生产每只股票数据完整性、实时行情正确性、生产 Bot 已部署此修复 |
| 英文代码参考清单 | 大写英文代码 JSON 仅元数据命中时，明确返回 `reference_metadata_only`，不假装有行情财报 | 不证明参考清单含可计算的财报 |
| 共享补采兼容性 | 既有单股票补采及失败冷却测试继续通过，外部来源使用 fixture 替代 | 未调用真实来源，不能声称真实补采成功 |
| 通用文本/结构化 reader | JSON、JSONL、CSV、TSV、YAML、XML、TXT、MD、HTML 的本地文件发现与读取；HTML去除收件人注释、脚本和外链图片；分页、筛选、聚合、超限显式返回 | 生产所有文件的schema/编码/业务完整性；HTML存在不证明邮件送达 |
| 二进制表格 reader | 真实生成的 Parquet、Feather、Arrow、XLSX 文件读取；XLSX工作表枚举；合成的合法BIFF2 XLS工作簿通过显式 `xlrd` 引擎读取 | 生产 `pyarrow` 尚未就绪；生产2份XLS正文及其业务语义仍未经读取验收 |
| SQLite reader | 只读连接、表枚举、私有表/私有字段拒绝、禁止任意SQL | 元数据盘点中的127份数据库未经打开，不能证明都可读或都有权限 |
| DataHub 已登记材料 | 已注册TXT正文读取，不启动新解析；格式与可用性在读取时明确返回 | 未证明扫描版PDF的OCR或新PDF解析；不能把原件存在当作文本已提取 |
| 归档发现与隔离 | 报告/邮件归档增量登记、日期过滤、更新；私有路径排除、回调分页和公开/认证数据脱敏 | 生产全部历史档案无遗漏；跨会话私人研究读取不因公开数据能力而开放 |
| Bot 入口与权限 | personal/triggers/research/完整四种能力范围都有 `data_find/data_read`；每轮携带数据读取指南；合法 `as_of` 转发且只读问答不补采；个人数据绑定当前sender/chat；report_run按 `config_snapshot._trigger_context` 隔离且不泄露配置 | 生产部署、所有私人研究附件及复杂个人账户业务的全面验收 |
| 真实 Pi 桥流程（模型响应模拟） | 实际Node24/Pi子进程，4轮模型响应、3次工具调用、2次检索：发现公开派息、读取快照、创建一次完整日报规则；临时SQLite中规则归属当前会话，未直接执行日报 | 未调用真实模型、未读取真实生产快照、未创建线上规则、未验证真实投递 |
| 历史周报文档发现 | `reports/` 中已有weekly Markdown可发现并读取；旧 fundamental 公开导出与 `fundamental_data` 公开衍生数据可登记，私人研究来源排除 | 没有增加周报生成器；旧混合研究/会话SQLite归属未确认，保持关闭且显式报覆盖缺口 |

### 读取边界与预览证据

需要整文档解析的JSON/YAML/XML/HTML、Excel及已有材料解析JSON限制8 MiB；
CSV、JSONL、TXT/MD、SQLite和Arrow系列使用行/批次读取，并非承诺任意大文件可全量进入上下文。
每次最多扫描100,000行，返回页限制1–100条及24,000字符；超限返回截断/覆盖缺口，
扫描上限后的空结果不能证明业务记录不存在。JSON集合可通过 `collection` 路径分页，
HTML表头与行关系保留；不能把JSON外层对象的一条记录当作其中业务集合只有一行。

公开派息表可以保留仅有payment_date的行供读取，但下一次派息触发器需要稳定的
`code + ex_date` 身份；缺少ex_date时不能仅凭一条支付日期创建可执行事件或宣称券商已到账。
快照没有足够证据时等待数据，不把缺数据解释为没有未来派息。

本地真实历史预览：`.tmp/bot_data_preview_20261104/preview.html` 与 `evidence.json`，
数据取自已有 `2026-11-02` 完整快照和日报正文。预览验证23个股票行、2条公开派息行、
176条HTML正文行；这是历史全池展示，未刷新生产数据、未生成新的投资结论、未发送消息。
归档正文仍标记 `delivery_status=unknown`，预览时间不改变原始数据时点。

上述定向命令：

```powershell
New-Item -ItemType Directory -Force .tmp/bot_data_coverage_20261104 | Out-Null
python -X utf8 -m pytest tests/interactive/test_assistant_research.py -k 'stock_query or stock_backfill' -q -o addopts= --basetemp=.tmp/bot_data_coverage_20261104/research_tests_recheck
ruff check src/interactive/assistant/research.py tests/interactive/test_assistant_research.py
```

本轮结果：9 passed，91 deselected；Ruff 通过。
本地 JUnit 证据：`.tmp/bot_data_coverage_20261104/research_tests.xml`。
第一次执行因 `--basetemp` 父目录未创建而发生 setup 错误；创建后复跑得到以上通过结果，
没有把环境错误计为业务失败或成功。

最终冻结源码组合验收为 **95 passed、1 skipped**，证据
`.tmp/bot_data_final_verified_20261104.xml`。范围含通用reader、目录索引、Bot适配器、
真实Pi桥（模拟模型）、公开派息生命周期和来源/事实合同；包含最终XLS与历史周报用例。
跳过项因Windows没有创建符号链接的权限，不代表该环境已实测该分支。
早期61 passed/1 skipped及扫描上限定向复验是中间记录，不与最终总数相加。

其他独立回归：主机兼容44 passed、触发器/行情92 passed、事件/工具/核心组合163 passed、
验证目录36 passed（`.tmp/bot_data_validation_20261104.xml`）、Node桥18 passed、
PIT文件名修复9 passed。各范围存在交叉，不计算“累计通过数”；
这些离线结果也不替代生产部署、真实来源访问或真实渠道投递验收。
本轮将解析依赖加入项目声明；部署前不能把依赖声明当作生产安装完成。
上述测试使用本地临时文件，未读取生产业务正文、调用模型或发送通知。

Bot 入口及真实 Pi 桥命令：

```powershell
$env:FEISHU_PI_TEST_NODE='C:/Users/one/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
python -X utf8 -m pytest tests/interactive/test_assistant_business_data.py -q -o addopts= --basetemp=.tmp/bot_data_coverage_20261104/service_tests_recheck
ruff check tests/interactive/test_assistant_business_data.py
```

结果：11 passed（104.24秒）；本地证据
`.tmp/bot_data_coverage_20261104/service_tests.xml`。
真实桥审计记录 `backend=pi, model_rounds=4, tool_calls=3, retrieval_attempts=2`。
默认Node22.17不满足测试夹具要求的22.19，成功复跑显式使用本地Node24.19；
未联网安装运行时。模型响应使用模拟值，出站消息由Mock接收，规则只写临时数据库。
