# 财报流程统一入口

2026-10-29核对：正式推荐入口为 `scripts/financial_pipeline.py`。第一、二阶段业务实现保持原合同。该入口负责提取、整合、摘要与第三阶段文件交接；横评使用 `run_peer_research.py`，后续固定三轮研究见[正式研究指南](research_report.md)。这些是显式命令，不会因生成交接包自动执行。完整关系见[架构图](../architecture_overview.md#财报研究链路放大图)。

## 阶段与参数

| 命令 | `--source` | 其他输入 | `--output` | 调用模型 |
|---|---|---|---|---|
| `extract` | 一份PDF | `--company`、`--year`、可重复的`--profile` | 新单年运行目录 | 默认预检；显式`--paid`及预算才调用 |
| `context` | 一个或多个运行目录 | 可选`--baseline` | 新第二阶段目录 | 否 |
| `summary-prepare` | 第二阶段目录 | `--metadata`、预算，可重复`--company` | 新冻结摘要计划目录 | 否 |
| `summary-run` | 上一步冻结计划目录 | 必须显式`--paid` | 写入该计划预定的新结果位置 | 是；每公司一次，无恢复/重试 |
| `stage3-input` | 已有摘要运行目录，或单个摘要文件 | `--metadata`，可重复`--company` | 新交接目录 | 否 |
| `stage3-read` | 交接目录 | 无 | 完整正文输出到stdout | 否 |

共同使用`--source`表示本步输入，`--output`表示新输出目录；预算统一为`--budget-limit-usd`。输出目录必须新建，不能覆盖旧运行。`summary-run`执行既有冻结计划，因此不再提供另一个output或预算覆盖参数。

## 启动命令

以下为PowerShell示例。`<年报PDF绝对路径>`、`<预算美元>`需要替换；新增输出目录名不能已存在。以下命令仅展示用法，不代表新的付费授权。

第一阶段：单年预检，不发送请求：

```powershell
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py extract --source "<年报PDF绝对路径>" --company 600039 --year 2025 --profile construction --output E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_600039_2025
```

实际提取须在同一条命令加`--paid --budget-limit-usd <预算美元>`，并使用另一个全新输出目录。预检目录不能直接作为已完成提取输入；应含实际生成的data.json后再整合。`--block`原样传给冻结提取器，不改变分块方案。

2026-10-30起，`--block`只选择本次实际执行的任务；财报前的`frozen_job_ownership`始终包含所选profile的完整归属合同。同一PDF、公司、年度、profile顺序和合同版本下，单块请求与全量计划中的同一块请求字节一致。模型输入中的执行块标记保留在财报之后的`[block:名称]`任务尾部，前置归属目录不随执行选择收缩。

`call_plan.json`及最终`summary.json`中的`execution_plan.ownership_blocks`记录完整合同块，`execution_plan.execution_blocks`记录本次选择。预算、`max_calls`和冻结执行块仍只计所选任务，单块不会变成全量调用；replay继续读取历史请求，不回写旧计划或旧结果。该变更已离线验证，未测量服务商实际缓存命中提升。

统一入口生成的单年运行布局：

```text
new_600039_2025/
  sources.json                    # 参数明确声明公司、年份和PDF路径
  stage1/600039_2025/             # 原提取器完整输出，业务实现不变
```

第二阶段：直接使用现有三家公司三年资料，离线运行原整合器：

```powershell
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py context --source E:/CodeBases/trade_eyes_keeper/data/fundamental_data/three_companies_fixed4_20261024_115306 --output E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_context
```

也可传多个由统一入口创建的单年运行目录：`context --source <运行目录1> <运行目录2> ... --output <新目录>`。命令会在输出目录旁生成`<新目录名>_inputs`，仅按各自sources.json复制data.json、原阅读正文与已有失败raw附件，再调用原整合器；不重提取、不修复、不猜公司或年份。同公司同年份重复输入报错，不覆盖。该inputs目录是保留的正式输入适配产物，可通过`--metadata`交给后续步骤；不是临时运行脚本。

`context --baseline <旧第二阶段目录>`可选，仅用于已有对比功能。单个输入运行必须有sources.json和stage1目录；不会从PDF名、文件夹名或不明report_period猜年份。原data.json中的身份未定仍保持未定，清单声明身份单独保留。

摘要准备：仅生成冻结请求和离线预算，每公司一次摘要，没有恢复任务：

```powershell
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py summary-prepare --source E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_context --metadata E:/CodeBases/trade_eyes_keeper/data/fundamental_data/three_companies_fixed4_20261024_115306 --output E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_summary --budget-limit-usd 2
```

`--company 600039`可筛选一家；不指定时处理第二阶段目录中的全部公司。`--appendix <已有附录.md>`可重复，但必须显式限定一家，避免把一家附录投给其他公司。无附录直接摘要；不自动读PDF、补提取、修复JSON或追加请求。沿用现有七问、完整prompt和示例，原样带入完整第二阶段正文。

摘要执行（本轮未运行）：

```powershell
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py summary-run --source E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_summary --paid
```

执行仍复用原流式保存/预算/报告器。仅接受无恢复依赖的摘要计划，不再走旧“5恢复＋3摘要”准备流程。预算按冻结配置的峰时上界预留；usage计价沿用原执行器已核对的本轮周末单价（可由配置的execution_rates明确覆盖），二者分开。没有缓存细分时不声称精确扣费。定价日期仅使用配置声明日期，不匹配时原执行器停止发送；未来需另行核价后重新准备，不能编辑已冻结计划。本轮未真实验收费率/扣费。失败/partial正文全部保留，CLI返回2表示执行未全部完成，不是财务质量或填充率门槛。

第三阶段交接：使用已有摘要和原始年度元数据，完全离线：

```powershell
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py stage3-input --source E:/CodeBases/trade_eyes_keeper/data/fundamental_data/summary_best_effort_20261024_162500 --metadata E:/CodeBases/trade_eyes_keeper/data/fundamental_data/three_companies_fixed4_20261024_115306 --output E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_stage3_input
python E:/CodeBases/trade_eyes_keeper/scripts/financial_pipeline.py stage3-read --source E:/CodeBases/trade_eyes_keeper/data/fundamental_data/new_stage3_input
```

`--source`也可传一个完整摘要.md，此时必须给一个`--company`；`--metadata`可传运行目录、单年目录或data.json。身份未知的元数据单列，不猜归属；没有元数据、摘要为空、状态partial都不会清空已有正文，也不自动追加调用。

交接目录包含各公司完整摘要原字节副本、摘要状态、元数据正文、元数据.json、合并的模型输入.md和manifest.json。元数据保留原年度快照及原来源清单记录，明确区分清单声明与模型提取身份；结构表/政策沿用既定`segments_geographies`/`reporting_basis`范围，保留完整行、表头口径和issues，不复制全部三表或raw日志。其余财务内容已在摘要和旧归档中；本步不判断经济可比性。

未来第三阶段程序只需调用`read_stage3_input(Path(<交接目录>))`，即可获得完整模型正文、结构化元数据和清单；读取只核对交接文件是否变动，不新增财务语义验收。包内资料自足，不依赖原目录仍然存在；原绝对路径及哈希保留作来源记录。

## 旧入口与临时脚本

- 原`build_fundamental_data_v2.py`和`build_financial_context.py`是冻结执行实现，由统一入口委托调用；业务代码不迁移、不复制。
- 旧V1、V2 history/peers、旧恢复准备命令保留兼容与历史复现，但不是本流程推荐入口；旧peers不等于基于摘要的第三阶段研究。
- `.tmp/prepare_best_effort_20261024.py`与旧批次目录里的batch.py仅是历史运行记录。正式入口不导入、不执行、不读取它们；因旧冻结清单仍引用，禁止为“清理”而删除或改写。
- 本轮没有恢复旧研究引擎、调度、后台循环或新数据源。第三阶段交接包不是第三阶段分析结果。
