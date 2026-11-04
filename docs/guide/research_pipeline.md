# 一次运行完整年报研究

入口是 `scripts/financial_pipeline.py`；API为 `src.research_pipeline.engine`。历史批次runner只作档案，不能拿来启动新任务。完整边界见[编排设计](../design/research_pipeline_orchestration.md)。

## 准备与离线检查

在仓库根目录运行。Python和依赖沿用项目requirements；模型密钥放环境变量或现有 `config/.env`，不要粘进命令、源码或日志。SEC采集需要已有 `SEC_USER_AGENT` 联系身份。

默认共享缓存为仓库 `data/fundamental_data/shared_research`，可用`--store-root`改到有足够持久磁盘的目录。同机任务必须共用它才能跨进程去重与共享20路。不同机器不会自动同步。

无`--paid`是离线计划：只读登记旧正式产物及已有材料，不查询目录、行情、余额，不解析新文件，不调用模型。输出目录必须全新：

```powershell
python -B -X utf8 scripts/financial_pipeline.py run `
  --company a_share:600348 --company hk:00883 --company us:XOM `
  --years 7 --output data/fundamental_data/research_runs/preview_001
```

公司代码必须带市场。可用`--start-year 2019 --end-year 2025`代替默认七年。`--profile a_share:600348=extractives`可明确行业合同；无可靠行业映射时只跑公共块并记录扩展未定，不猜银行/工程/能源。

`--cache-root`可重复提供历史正式批次根；默认检查现有fundamental_data档案。导入不执行旧脚本、不改旧文件、不把历史费用扣到本次预算。不能确认来源身份的记录列待核。

## 明确授权后执行

下面是语法示例，美元金额必须由调用方明确授权，不是工具默认赠送的额度：

```powershell
python -B -X utf8 scripts/financial_pipeline.py run `
  --company a_share:600348 --company a_share:600546 `
  --years 7 --budget-limit-usd 20 --paid --background `
  --focus "先划经济可比组，再比较资本投入、普通股现金和估值条件" `
  --output data/fundamental_data/research_runs/energy_001
```

`--background`返回run ID，Windows隐藏运行，Linux独立session。没有自动重启/定时触发。前台调用去掉该参数。`--concurrency`为1—20，仍受同库全局20和预算约束，不等于保证20实际在途。

真实执行会检查官方年报目录及行情；只有新版本才下载。年报原件、修订及对应年度分析有缺口时继续其他对象。源站限流/封禁不清状态、不绕过。已有有效缓存仍可用，但“本轮未确认最新”会显示。

可显式传入已有材料清单与行情：

```powershell
python -B -X utf8 scripts/financial_pipeline.py run `
  --company us:XOM --source-manifest "<绝对路径>/annuals.json" `
  --market-snapshot "<绝对路径>/market.json" `
  --budget-limit-usd 6 --paid --output "<全新输出目录>"
```

`annuals.json`包含`documents`数组，每项明确`instrument`、`issuer_id`、`fiscal_year`、`document_id`、`local_path`或官方`url`、`sha256`、`content_type`、`kind`、真实期间及披露时间。不能根据文件名猜身份。`kind`为annual/amendment等明确类型；部分修订与原报共同使用。

可在同一清单的`applicability`数组保留明确的年度适用性证据：`instrument`、`fiscal_year`、`status`（`prelisting`或`not_yet_due`）、`listing_date`或`period_end`，以及`evidence.source_url/record/sha256`。`record`须含同一证券及相同日期，SHA用`src.research_pipeline.common.fingerprint(record)`生成，来源须是该市场官方站点。若已结束财年但尚未到官方披露期限，还须在声明和record中同时提供`filing_due_date`。这些是调用方提供、哈希绑定的官方记录，本轮不会独立补查上市日或推定法定期限；证据不全继续显示缺口。实际找到的完整年报优先，不被“不适用”声明隐藏。

行情JSON可为数组或`quotes`数组；每项明确`instrument`、`price`、`date`/`quote_time`、`currency`。Markdown快照接受“代码/日期/价格/币种”列。价格必须原币、未复权，有源日期；无汇率/ADR证据就限制相关估值。

`--issuer-map`接受“市场证券ID→已确认发行人ID”的JSON映射。没有证据时不要合并A/H或ADR。`--assumptions`可提供明确假设文件；它和`--focus`变化只影响最终研究。

## 调查建模与重新研究

新研究在底稿交接后默认先运行 Agent 调查、补证及 Python 建模，再进行原三轮写作。行业、业务分部、驱动和公式由 Agent 根据资料决定；完整合同与行业验收案例见[调查建模设计](../design/research_investigation.md)。提取阶段的 profile 不限制研究阶段能够查看的全文或业务字段。

运行主机需提前准备 Docker 和可信的本地 Python 镜像。仓库提供只有标准库的镜像定义，以下命令由管理员显式构建，使用 stdin 避免发送仓库构建上下文：

```powershell
Get-Content config/research_model.Dockerfile | docker build -t trade-eyes-research:local -
```

构建会获取基础镜像；研究运行不会拉取镜像。可用 RESEARCH_MODEL_IMAGE 指定事先准备的镜像。容器无网络且不挂载源码或凭据，默认单次计算最多 120 秒、1 GiB 内存、2 CPU；实际镜像 ID 和输入输出哈希留档。缺 Docker / 镜像时保留 unavailable 和条件结论，不在宿主执行模型。

已有有效报告继续按原版本复用。要明确重新调查，在新输出目录的 run 命令加 --reinvestigate；仍须 --paid 和已授权预算。它只建立新的研究身份，复用已有原件、对齐、摘要及横评，保留旧报告。Python 对应 reinvestigate=True，Bot 的 prepare 参数也同名并进入既有确认预览。

delivery/investigation 包含冻结来源、模型及计算产物；handoff.md 为完整研究交接。审阅可触发一次预算内建模返工，产生新版本和新图片；旧版本不改。初始调查和返工分别预留三轮 / 最后一轮写作费用。达到预算、工具或时间上限保存 partial，不把执行 complete 当成独立财务验证。

## 查进度、日志、取消与续接

```powershell
python -B -X utf8 scripts/financial_pipeline.py status --source "<本次运行目录>"
python -B -X utf8 scripts/financial_pipeline.py logs --source "<本次运行目录>" --follow
python -B -X utf8 scripts/financial_pipeline.py artifacts --source "<本次运行目录>"
python -B -X utf8 scripts/financial_pipeline.py cancel --source "<本次运行目录>"
python -B -X utf8 scripts/financial_pipeline.py resume --source "<本次运行目录>" --paid
```

也可用`--run-id ID --store-root <共享目录>`定位。`resume --foreground`等待完成；默认后台启动。续接不接受新预算/输入覆盖，旧成功、失败和unknown均不再发送。取消停止新派发，活着的在途请求不会假装已杀掉；主机异常恢复先暂停，需显式resume。

真实前台执行也由冻结源码的独立宿主启动worker；CLI立即输出任务ID，随后跟随持久化事件。后台模式立即返回。宿主能独立发现worker死亡，不自动重启；宿主或整机同时离线仍需外部OS监督。

重点查看phase、最后网络活动、waiting_balance/预算/源站原因、gaps与artifacts。`index.html`是自动更新的进度页，`delivery/index.html`是可核查交付；MD是完整原文，不是摘要的摘要。CLI进度不是公开网页服务。远程由已有Feishu/Pi任务工具和文件交付取得产物，不把服务器路径冒充远程链接。

## Python与Feishu/Pi

```python
from decimal import Decimal
from pathlib import Path
from src.research_pipeline.models import ResearchSpec
from src.research_pipeline.engine import start_research, get_status

spec = ResearchSpec(
    companies=("a_share:600348", "hk:00883", "us:XOM"),
    output=Path("<全新运行目录>"),
    store_root=Path("<同机共享持久目录>"),
    budget_limit_usd=Decimal("30"),  # 调用方明确授权；所有阶段含维修共享
    paid=True,
)
run = start_research(spec, background=True)
status = get_status(run["id"], store_root=spec.store_root)
```

同一包提供`plan_research`、阻塞`run_research`、`cancel_research`与`resume_research`。用户界面和agent不用另写执行逻辑。

历史报告缺少可证明的旧行情/研究重点/假设身份时，先作为`legacy_reference`交付并显示待核，不直接重新收费。需要通过新的迁移登记补齐可验证身份；不能修改旧计划或猜测它等同本次研究。

Feishu Pi提供有权限的任务列表、状态、事件、日志尾部、产物和维修工具。新研究通过既有提案确认明确预算。CLI没有origin不会给任何聊天发消息。只有原chat/原用户可查任务或确认候选。

## 自动诊断、候选维修与确认

错误先落盘并主动反馈。Pi可读该任务脱敏证据，在隔离候选中修改允许的执行/解析/呈现代码，宿主验证后发送候选diff和测试回执。它不能更改财务prompt/schema、预算、原文或自己的治理权限。

维修最多每事故2轮，每轮10分钟/12次模型交互，费用包含在总预算默认10%份额中（`--repair-fraction`可调）。修好也要等待你确认具体候选；候选/基线/回执变化后旧确认失效。确认后新版本只继续未发工作，不重发旧失败。

生产候选测试需要预先准备的本地Docker镜像，配置`RESEARCH_REPAIR_IMAGE`；工具禁网络、只读候选，不pull镜像，不自动安装依赖，不在缺沙箱时直接运行任意本机候选。缺少镜像/运行时显示test_unavailable，不能称测试通过或应用。

Pi还需要worker的`PATH`可找到Node >=22.19，以及现有`tools/feishu-pi`锁定依赖。环境准备沿用[飞书指南](feishu_telegram_setup.md)，本轮没有安装软件或更改系统PATH。当前本机默认Node为22.17；桥接补验使用已有Node 24，不代表当前默认运行环境已切换。

## 预算口径（2026-11-01核价）

Flash每百万tokens：非峰缓存命中US$0.003、未命中US$0.15、输出US$0.60；高峰各翻倍。周末全天非峰；输出已经包含reasoning。来源：[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)。实际执行会重新核价并保存来源，不能用这段文字当永久费率。

同时冻结官方人民币价，按两种公开费率核验账户资金的可比比率；当前为0.15，程序不把它当汇率。节假日使用`config/research_tariff_calendar.json`的官方通知，目前收录2026年。新年份的高峰窗口若缺已核验日历，暂停新请求并显示`pricing_holiday_calendar_unverified_no_new_POST`；核验当年官方日历并保留旧快照、建立明确的新执行版本后才续接，不能改旧freeze伪装通过。非峰窗口与周末不依赖这个附加日历。

| 类似当前能源样本的场景 | 非峰费用粗估 | 建议硬上限，含10%维修份额 |
|---|---:|---:|
| 年报/组合/研究条件不变，缓存全命中 | 0模型费用 | 可0预算复用 |
| 上游已有，只更新行情做三轮 | 约US$0.34 | US$2 |
| 第一阶段已有，完成7家后续 | 约US$1.37 | US$5 |
| 一家公司七年从原件开始 | 约US$2—5 | US$6 |
| 七家公司49份原件从零开始 | 约US$16—26 | US$30 |

历史7家44份年报：第一阶段已知US$12.6142＋4笔usage未知，后续21次US$1.3707，已知合计US$13.9849。第一阶段缓存命中约79.4%，摘要阶段才约1.115%。相同已知tokens输入全未命中约US$23.1089，仍另有4笔未知；它不是所有块成功后的成本上限。

硬上限是止损线，不保证所有公司完成。港美原文长度、修订、行业块和输出量不同；准备阶段区分预估区间/未知/派发预留。缓存历史消费不再扣本次授权；账户人民币扣减是账户范围观察，不与usage美元冒充同一账单。预留不是实际消费，缺usage不填0。首次模型调用前检查资金；全缓存命中不要求模型密钥。

## 验收与部署边界

查看运行目录中的manifest、原响应、usage、冻结文件和故障时间线。任务结束/JSON通过/输入保留，不等于字段齐全或财务结论准确。源站补充、图表和通知失败应该局部降级；授权、账本、原请求和核心代码完整性失败应停止新付费。

若SQLite本身不可写，另查运行目录的`emergency/ledger_unavailable.json`及stderr。宿主仅在冻结任务身份和原会话归属可验证时发送一次紧急告警；不能落盘保存去重记录时只写脱敏stderr，不猜通知目标或自动恢复。

Windows离线回归、三市场目录免费验证、真实模型、Linux/Pi线上、Docker候选验证分别列结果。没有新付费授权不跑真实模型；没有发布操作不称服务器已部署。

`delivery/accounting.json`按真实attempt列当前研究的usage、公司/阶段和未知项；历史复用费用单列，不再次占授权。`delivery/hashes.json`核验实际交付文件。原文、失败响应、修订前版本和审计资料保留；新版对齐保存新路径，不能通过改旧hash把损坏文件变成成功。

## 服务器实机补验

2026-11-01补验使用服务器独立目录，没有替换现有Bot服务或发布生产版本。服务器现有Node 22.22.1满足桥接要求；离线维修镜像为`trade-eyes-repair-acceptance:20261101-offline-v3`，镜像ID和真实隔离测试回执见[实机补验记录](../development/research_pipeline_live_acceptance_20261101.md)。运行时仍需由调用环境明确设置`RESEARCH_REPAIR_IMAGE`，本轮没有修改服务的全局环境变量。

补验修复了POSIX候选文件0600导致非root容器无法读取、维修/模型轮次日志字段冲突，以及冻结包漏带实际维修测试的问题。只对隔离公开候选目录设755/644，专用结果挂载使用sticky目录；凭据、原代码和权威宿主回执不在可写容器挂载内。冻结包包含允许的维修/validation测试及公开规则依赖，不从实时私密配置补齐测试。

复核没有输出新chart块时，仍保留先前实际PNG；不同阶段图按路径并列，不静默删证据。制图事件显示图数量及问题。Linux优先使用已有Noto Sans CJK SC/WenQuanYi回退，原图与后续离线修复图分开。已有研究缓存不会因图像呈现修复自动重新收费。

人民币授权不能给每个验收run重新发一遍额度。本轮按官方双币种费率核验授权可比值；更换执行版本时，先确认旧进程退出且在途为零、STOP封住旧run，再扣除全部旧usage、未知责任及在途占用，只分配剩余额度。该验收控制不等于CLI已有任意跨run预算池；正式CLI的授权单位仍为一个run。
