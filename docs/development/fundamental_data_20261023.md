# 基本面数据与研究分离（2026-10-23）

本轮先清理并建立数据的三个独立验收边界；研究冻结，没有新研究流程，没有付费API调用。
这是过渡清理，不是“五脚本、每个100行”已达标，也不是三项业务验收全部通过。

## 实际结构与复用范围

```text
scripts/build_fundamental_data.py
  ├─ extract / check → src/fundamental_data/disclosure/
  ├─ history         → src/fundamental_data/history.py
  └─ metadata        → src/fundamental_data/metadata.py

现存研究 src/fundamental_quant/ → 可读取上述数据包
数据包 → 不导入 fundamental_quant、研究脚本、模型客户端或通知
```

| 数据子模块 | 复用部分 | 本轮收敛 | 单独验收边界 |
|---|---|---|---|
| 单份提取 | 原6个纯离线财报解析文件、来源哈希、原文与页码、缺失状态 | 从研究目录迁出，旧路径删除；统一最小阅读投影 | 真实原文/期间/单位可追溯；提取内容及语义完整性另验 |
| 多年整合 | CSV原精度输出 | 删除无调用方的旧导出框架；单公司多版本整合，精确重复政策/附注只写一次并列全部来源 | 变化保留、期间缺口可见、上下文不超调用方设定上限 |
| 公司/同行元数据 | 原配置的7家公司和两个手工候选组 | 独立配置、原文逐字引用、缺失/冲突清单；同行候选与可比性分开 | 业务/行业/地区/商业模式/会计口径依据齐全，再验同行覆盖与可比性 |

`disclosure/`仍有6个旧解析实现文件，承担现存冻结资料与研究调用的共享依赖。
没有把它们包装成“一个小脚本”。全量43字段旧包作为历史来源保留，不是默认最小阅读内容。
旧研究目录也没有整体搬家冒充已经整理完；当前残留规模见清理回执。

## 1. 单份财报/公告

默认阅读合同是：

- CFO、现金资本开支、投资收益：保留原精度、单位/币种、实际财期、缺失状态、原文与物理页。
- CFO减现金资本开支仅是基础FCF代理；保留其定义、待调整边界，不冒充正常化股东FCF。
- 管理层讨论原文，明确属于管理层陈述。
- 收入、存货、固定资产、无形资产、所得税、投资损益、财务费用七类政策及金额附注。
- 与FCF边界直接相关的调节表、政策变更、公司披露FCF、资本化/现金利息来源。

补充项目若只定位到整个财务章节，不将整章塞进最小上下文：保留
`unexpanded_source_refs`，标记 `broad_section_requires_targeted_extraction`。
这表示仍需定向提取，不能算已经取得完整最小信息。
缺失不填0，未找到不改称未披露；币种不明确时不擅自补成人民币。

当前实现仍是旧解析器的离线复用，没有新建API提取器。年报/中报有真实资料回放；
季度报有原始页面回归。任意类型公告尚未取得真实覆盖，不能把名称扩展当作支持完成。

```powershell
python -X utf8 scripts/build_fundamental_data.py extract --source data/fundamental_quant/deepseek_comparison_20261007 --output data/fundamental_data/my_disclosures
python -X utf8 scripts/build_fundamental_data.py check --source data/fundamental_data/my_disclosures --output data/fundamental_data/my_disclosure_checks
```

输入为已有冻结的 `sources/*.json` 与原PDF、document.txt；不下载、不上传、不调用模型。
输出包含不可变pack及 `minimum/<pack_key>.json`。重复构建复用版本，来源损坏明确失败。
fr-pack/26修复合并金额附注中中文编号表内分组被误当大节标题的问题；保留原失败测试。
清理前原字节也复现了第96页提前截断，修复后原回归要求保留到第119页。

## 2. 单公司连续多年资料

```powershell
python -X utf8 scripts/build_fundamental_data.py history --source data/fundamental_data/my_disclosures --issuer a_share:000568 --as-of 2026-10-23 --max-chars 100000 --output data/fundamental_data/my_history
```

`--max-chars`是调用方给出的字符上限，不是模型token估算，也不是本轮已约定的正式篇幅目标。
不预设必须七年；显示实际选中财期，以及已选最早/最晚年度之间不在输入中的年报年份。
不同年度、季度/中报、修订版本分别保存，不用后披露版本替换早期时点。
当前命令处理源清单中的同公司资料，不会自动筛选“最新N年”；未来披露会被拒绝。

去重只针对同公司、同类章节、相同标题、相同文本及相同已知口径。
只要文本或口径变化就保留；财务数字和管理层讨论不跨期去重。
被复用文本列出每次出现的报告版本、财期、披露日、章节、页码，原始包保持不变。
这只是阅读去重，不证明以前年度政策本期仍适用。

超限或来源失败时不生成可用 `context.md`；旧的context也会移除。
完整分块、缺口、实际字符数和超限量保留在 `history.json`，不会静默截尾。
去重及超限控制通过测试，不等于真实多年材料已经压缩到合适篇幅；仍需单独验收压缩效果。

## 3. 公司及同行元数据

独立输入为 `data/fundamental_data/local_manifests/fundamental_data_companies_20261023.json`，不再从研究组准备比较包。
初始公司/候选组从原研究配置迁入，记录配置路径和SHA；公司简称仍标记为配置标签。
9条初始行业、业务、会计准则记录逐字引用3家公司的冻结财报，记录pack版本与物理页。
字段值本身也必须出现在原文引句中；规范化行业编码或推断不作为披露事实写入。

```powershell
python -X utf8 scripts/build_fundamental_data.py metadata --source data/fundamental_quant/fr_packs_20261008 --manifest data/fundamental_data/local_manifests/fundamental_data_companies_20261023.json --output data/fundamental_data/my_metadata
```

检查行业、业务、分部、地区、商业模式、会计准则六类。缺失、互相冲突的记录显式列出。
同组配置或相同披露行业只产生候选关系，不据此确认同行、排除公司或断言名单完整。
所有公司对都列出候选/未定、依据、尚需考虑因素和双方缺失项。
运行成功仅表示缺口清单生成成功；不是同行识别已验收。

初始清单：7家公司、9条原文关联记录，42个公司字段中33个仍缺失；
分部/地区/商业模式尚未结构化，4家工程建设公司的六类元数据仍缺。
本轮只审核白酒冻结资料，未进行全行业发现或穷尽同行筛选。

## 删除与停用

- 删除混合材料总编排 `src/fundamental_quant/materials.py`：它反向调用研究prepare、
  混入历史估值、股本补数及研究准入，不作为三个数据模块的兜底。
- 删除 `scripts/build_fr_packs.py` 和 `scripts/export_research_materials.py` 两个旧入口，
  用一个离线数据命令承接本轮范围；没有把研究packet适配器搬进数据包。
- 删除已无生产调用方的旧逐期/纵表/市场/附注导出编排，仅保留CSV格式化。
- 删除混合编排专属测试；混合文件中独立的行情时点、重试及来源恢复测试继续保留。
- 原7个数据文件迁移后不留兼容转发；所有现存调用方直接改用新数据路径。

`main.py --fundamental-materials`返回 `mixed_materials_pipeline_retired` 和非零退出码；
worker的旧材料任务返回blocked，不进入研究。没有安装新的定时数据任务。
旧材料的一分钟续补定时器、恢复方法和已无调用方的重试判断也已删除；不再对退役入口反复续跑。
研究CLI、已有通知/历史读取、独立纵向和整份比较脚本继续按原清单标明；本轮未运行。

原字节备份：`.trash/data_split_20261023/before_cleanup.zip`。
迁移、依赖图、边界失败复现、离线回归和联网拦截证据：`.tmp/data_split_20261023/`。
本机三个模块回执与元数据阅读清单：`data/fundamental_data/acceptance_20261023/`。

## 本轮验证结果

- 大范围离线回归1399项通过；随后删除已无生产调用方的旧续补判断及其2项专属测试，
  最后80项数据/worker/validation重验通过。保留用例去重后1397项通过，含36项validation。
- 814个Python文件语法/静态依赖检查通过，无已删除模块导入；数据入口实际import不加载研究包。
- 修改范围Ruff通过；main原有诊断65降为64，没有新增，未宣称全仓lint通过。
- 12份原始财报重新构建与来源回放通过；最小数字36/36，4个过宽补充章节仍待定向提取。
- 三家公司各四份披露整合后分别305,976、276,166、237,888字符；10万字符拒绝检查均正确阻断。
- 测试和真实资料回放拦截进程的socket尝试均为0；付费API、通知、安装任务、提交及部署均未执行。
- 剩余数据包9个Python文件/3,018行，其中6个为复用解析器；旧研究区仍110文件/58,562行。
  这些真实依赖没有隐藏，不能宣称极简规模已完成。源码/回执以本机清理快照为准。

2026-10-30 整理：上面的公司清单包含一次采集的来源摘录，保留为已忽略的历史输入数据；会计规则网页与来源记录保留在`data/fundamental_data/source_references/accounting_rules/`。均未接入当前源码配置，未删除原始资料。
