# 全项目架构总览

核对日期：2026-10-30。总图以当前本机源码能力为范围；本机与线上实际启用情况、开关及已核对的版本差异单独列在下文，不能将本机源码直接当成线上完整架构。当前未提交模块另见文末工作区快照；此前线上核查仍保留原日期，不代表本次重新验过生产。

本文集中保存全项目总图、飞书开关与对话路由、公共运行层、告警链路、搜参子系统和财报研究六张 Mermaid 图，以及环境对照、执行边界与源码导航。三条主业务分别是日常告警、策略搜参与财报研究；它们共享部分数据和基础设施，但执行、状态与发布边界不同。更细的公共运行层说明见[项目架构说明](architecture.md)，财报命令见[财报启动指南](guide/financial_pipeline.md)。

- [全项目总图](#全项目总图)
- [飞书开关与对话路由](#飞书开关与对话路由)
- [本机与线上环境对照](#本机与线上环境对照)
- [公共运行层放大图](#公共运行层放大图)
- [告警链路放大图](#告警链路放大图)
- [搜参子系统放大图](#搜参子系统放大图)
- [财报研究链路放大图](#财报研究链路放大图)
- [当前未提交模块](#当前未提交模块2026-10-30快照)

> 历史发布核查：2026-10-29 核验生产公共层为 v1.18.0（6a55894，构建 b7e30404）；不能将它当成当前所有环境的最新版本。当天已退役本机旧业务循环和专用 watchdog，并注销测试任务，历史数据保留；见[退役记录](development/shared_runtime_retirement_20261029.md)。本次只盘点本机未提交模块，没有重新核对或部署生产。下方 09:03 环境表同样为当时快照。

## 图例

- 实线：实际调用、控制或数据流；不是每个入口都会执行所有分支。
- 虚线：可选能力、显式文件交接、配置约束或只读查询；不表示自动触发。
- 图中一个框可能包含多个文件，不代表一个进程；独立子进程与批次已单独标注。
- “对齐”“完成”“成功”分别受各阶段合同约束，不代表财务口径已经人工复核。

## 全项目总图

```mermaid
flowchart TB
    subgraph ENTRY["入口"]
        CLI["main.py<br/>服务、日报、简报、优化、审计"]
        BOT["飞书 / Telegram<br/>认证消息与命令"]
        SCRIPT["独立批次脚本<br/>采集、财报研究、实验"]
    end

    subgraph HOST["服务与公共任务执行"]
        SERVICE["RuntimeService<br/>生命周期、心跳、Bot 接收器"]
        SCHED["ScheduleManager<br/>定时触发"]
        EXEC["公共 ExecutionRuntime<br/>持久队列、并发池、租约、恢复"]
        ASSIST["飞书助手<br/>assistant.enabled 为 true 才接入<br/>资料检索问题强制Pi；其余按harness_backend"]
    end

    CLI -->|默认 / --service| SERVICE
    SERVICE --> SCHED
    SERVICE --> BOT
    SERVICE --> EXEC
    CLI -->|--once / --brief| EXEC
    BOT -->|日报 / 简报命令| EXEC
    BOT -->|助手启用时，飞书会话持久入队| EXEC
    EXEC -->|conversation.turn| ASSIST
    SCHED -->|日报 / 简报| EXEC

    subgraph APP["业务模块"]
        DAILY["日报 / 简报编排<br/>Session、报告与通知决策"]
        ALARM["告警链路<br/>价格阈值 + 活动策略信号"]
        PERSONAL["个人监控<br/>持仓、提醒、行情观察、投递"]
        OPT["策略优化<br/>独立子进程"]
        AUDIT["标的画像审计<br/>公司 / ETF / REIT 等"]
        SANDBOX["助手研究沙箱<br/>原研究worker队列 → research.py<br/>Docker / research_worker.run_registered"]
    end

    EXEC -->|报表 worker| DAILY
    DAILY -->|触发告警评估| ALARM
    ALARM -->|alerts / signal_scan 写回 Session| DAILY
    EXEC -->|个人监控任务| PERSONAL
    ASSIST -->|受控工具| PERSONAL
    ASSIST -->|确认后的提案；保留原研究worker队列| SANDBOX
    CLI -->|--optimize| OPT
    SCHED -->|优化任务，原进程合同| OPT
    CLI -->|--audit-instruments| AUDIT

    subgraph DATA["数据基础"]
        SOURCE["外部数据源<br/>行情、公告、财报、公司行动"]
        FETCH["DataSource / StockDataFetcher<br/>多源适配、缓存、日期与复权校验"]
        PIT["时点数据与数据集<br/>行情、披露日财务、公司行动、目录"]
    end

    SOURCE --> FETCH
    SOURCE -->|回填与 Provider| PIT
    FETCH --> DAILY
    FETCH --> ALARM
    FETCH --> PERSONAL
    FETCH --> OPT
    SOURCE --> AUDIT
    PIT --> SANDBOX

    subgraph QUANT["策略、搜索与仿真"]
        SEARCH["搜参子系统<br/>SearchController + Solver<br/>候选生成、评价、Walk-forward"]
        STRATEGY["策略插件<br/>技术策略 / 基本面策略"]
        BACKTEST["统一 Backtester<br/>TradePlan → 模拟成交与评价"]
        GATE["完整性 / 留出 / 安全门槛<br/>同市场相对晋升"]
        ACTIVE["版本化策略产物<br/>按市场活动指针"]
    end

    OPT --> SEARCH
    SEARCH --> STRATEGY
    STRATEGY --> BACKTEST
    BACKTEST -->|评价结果| SEARCH
    SEARCH --> GATE
    GATE -->|通过后按配置或人工激活| ACTIVE
    ACTIVE --> DAILY
    ACTIVE -->|活动策略及参数| ALARM
    DAILY --> STRATEGY
    SANDBOX -->|复用策略与回测合同| STRATEGY

    subgraph OFFLINE["独立数据与研究链"]
        COLLECT["源资料批次采集<br/>筛选、PDF、行情、免费财务"]
        FIN["财报提取 → 跨年对齐<br/>单公司摘要 → 横评 → 正式研究"]
        LAB["保留的逐时点数据与估值适配<br/>指定MOE/估值评分/Ke实验已终止"]
        HISTORY["本机旧 fundamental_quant<br/>仅历史查询与文件路径"]
    end

    SCRIPT --> COLLECT
    SOURCE --> COLLECT
    COLLECT -->|行情 / 财务 / 文档目录归档| PIT
    COLLECT -.已有文件显式交接.-> FIN
    SCRIPT --> FIN
    SCRIPT --> LAB
    PIT --> LAB
    LAB -.保留业务的时点数据面板.-> STRATEGY
    BOT -.历史只读命令.-> HISTORY
    CLI -.历史只读选项.-> HISTORY

    subgraph OUTPUT["输出"]
        OPREPORT["日常与审计报告<br/>HTML / PDF / 图表 / JSON"]
        RESEARCHFILES["研究文件<br/>正文、阅读页、图表、原始回执"]
        NOTIFY["NotifierManager<br/>邮件 / 飞书 / Telegram"]
    end

    DAILY --> OPREPORT
    AUDIT --> OPREPORT
    DAILY -->|显式通知入口| NOTIFY
    FIN --> RESEARCHFILES
    LAB --> RESEARCHFILES
    PERSONAL -->|个人通知适配器| BOT

    subgraph BASE["横向基础设施"]
        CONFIG["配置 / 环境变量 / Prompt / Schema"]
        STATE["缓存与持久状态<br/>SQLite、文件快照、哈希、日志、费用账本"]
        OPS["tests / 验收脚本 / ci_cd_deploy<br/>部署、守护与运行核验"]
    end

    CONFIG -.配置.-> SERVICE
    CONFIG -.配置.-> OPT
    CONFIG -.配置.-> FIN
    EXEC --- STATE
    PIT --- STATE
    FIN --- STATE
    OPS -.验证与运维.-> SERVICE
```

## 飞书开关与对话路由

> 以下为 2026-10-29 研究问答修复候选；尚未部署。下方环境表的 09:03 线上快照保留修复前行为，不能视为候选已经上线。

```mermaid
flowchart TB
    MSG["认证通过的飞书消息"] --> CMD{"是否斜杠命令"}
    CMD -->|是| LEGACY["原命令分发器"]
    LEGACY --> OUT["回复 / 业务回执"]
    CMD -->|否| ENABLED{"assistant.enabled"}
    ENABLED -->|false| IGNORE["忽略普通助手消息"]
    ENABLED -->|true| ADMIT["权限与群寻址检查<br/>持久入队"]
    ADMIT --> PERSONAL{"明确的持仓读取或操作回执"}
    PERSONAL -->|是| RECEIPT["原业务查询 / 原子操作回执"]
    RECEIPT --> OUT
    PERSONAL -->|否| QUESTION{"是否需要资料研究<br/>含股票、估值、源码及连续追问"}
    QUESTION -->|是| PI["强制 Pi Agent 多轮循环<br/>不降级为 native 直答"]
    QUESTION -->|普通问答| BACKEND["按 harness_backend 选择 native / Pi"]
    INPUT["本轮文档与股票数据预查<br/>仅作为模型输入"] --> PI
    PI --> MODEL["Python 请求模型<br/>thinking enabled / 配置思考强度"]
    BACKEND --> MODEL
    MODEL --> DECIDE{"是否提出工具调用"}
    DECIDE -->|是| TOOLS["Python 校验并执行工具<br/>搜索、读原文、查数据"]
    TOOLS -->|结果 / 错误返回所选循环| MODEL
    DECIDE -->|否| EVIDENCE{"研究问题是否有本轮资料<br/>或已实际尝试检索"}
    EVIDENCE -->|已取得资料| OUT
    EVIDENCE -->|检索失败| LIMITED["仅解释实际尝试与资料缺口"]
    LIMITED --> OUT
    EVIDENCE -->|尚未检索| REPAIR["要求补检索一次<br/>仍不检索则拦截结论"]
    REPAIR --> MODEL
```

`assistant.enabled` 控制是否启用普通消息助手。修复后的资料研究问答强制使用 Pi；`harness_backend` 仍控制其他问答的 native/Pi 选择。研究所需 Pi 环境缺失时明确失败，不默默改成直接回答；部署需安装 Node >=22.19 及 `tools/feishu-pi` 的锁定依赖。斜杠命令、持仓查询、提醒及原子操作回执保留各自路径；持仓公司的研究分析仍进入资料研究链。

单股票预查结果只送入模型，不再拼模板发出；自然语言估值问题不再被旧历史研究查询截走。模型先分析本轮已有资料，不足时继续搜索、读原文或查数据。导航目录和旧会话不算本轮已核实资料；没有资料也没实际检索时，暂扣结论，要求补检索一次，仍未执行则明确失败。真实搜索失败会交给模型选择其他来源或说明具体缺口，不用模板冒充研究结果。

thinking 在实际模型请求中启用。检索和只读分析不要求确认研究提案；配置修改、脚本/回测执行和策略激活仍遵循原确认合同。工具次数不是质量分数，已有有效本轮预查数据时不强迫重复查询。证据与引用检查可以阻止无检索直答及无效源码引用，不能保证所有模型推理和投资判断都正确。

Pi 的共享实现仍在 [shared-runtime.mjs](../tools/feishu-pi/shared-runtime.mjs)，飞书通过 [harness.py](../src/interactive/assistant/harness.py) 调用。`--profile research` 协议能力保留，但 `research_report.py` 独立固定三轮研究没有接入它；本轮修复的是飞书研究问答，不复活旧基本面研究执行器。

## 本机与线上环境对照

此表为修复前环境快照，修复候选未部署。快照日期：2026-10-29。线上于 **09:03（Asia/Shanghai）** 经 SSH 只读核对配置、关键源码和既有对话审计；本机读取当前主配置及源码。下表不是全量源码同步证明，也不代表新发送了一条消息完成端到端验收。

| 项目 | 当前本机主配置 / 源码 | 线上已核对配置 / 源码 |
|---|---|---|
| 飞书助手开关 | 未显式配置，默认 `enabled: false` | `enabled: true` |
| 对话循环后端 | 未显式配置，默认 `native`；助手关闭时不运行 | `harness_backend: pi` |
| 普通对话模型 | 默认 `deepseek-flash`；未因源码存在就运行 | `deepseek-flash` |
| thinking | 模型客户端发送 `enabled`，强度默认 `high`；仅在实际模型路径生效 | 模型客户端发送 `enabled`，配置强度 `high` |
| 最大模型调用轮数 | 默认 6 | 配置 16 |
| 最大工具调用次数 | 默认 48 | 配置 48 |
| 通用 Pi 桥 | 代码保留；助手启用后的研究候选强制选 Pi，当前主配置仍关闭助手 | 已选作飞书助手后端，尚未替换本轮问答修复 |
| 关键文件版本 | 09:03时 `harness.py`、`settings.py` 与线上哈希相同；之后本轮修改了本机 harness/service，候选未部署 | 保留09:03所读版本，不能用本机候选解释现行线上行为 |
| 本机财报研究 | 显式批次脚本，不接 Pi；旧研究专用接入已退役 | 本轮未完整核对线上财报研究模块，不能将本机退役结论外推 |
| 本机测试任务 | `TradeEyesPersonalMonitorTest` 已主动停用 | 不代表生产助手停用；本次生产服务为 active/running |

差异来源：10 月 28 日公共层发布是限定 38 文件的生产覆盖，保留线上其他业务补丁与配置；没有整体同步本机的研究/优化实验，也没有拿本机关闭的助手配置覆盖线上。因此架构需要同时记录代码能力、配置条件和环境版本，不能只画一条无条件的“飞书 → Pi → 研究”。

上述配置摘录、关键源码 SHA-256 及版本差异的脱敏快照保存于 `.tmp/feishu_quality_audit_20261029/architecture_snapshot.json`。原对话证据单独保留，不复制到文档。历史审计能证明此前出现过多轮调用和直接回复两种路径；不能由某个历史结果保证当前每次回答都会检索或思考。

## 公共运行层放大图

```mermaid
flowchart LR
  Host[RuntimeService] --> Exec[core/execution.py]
  Host --> SDK[feishu_bot.py / 官方SDK子进程]
  SDK --> Ingress[interactive/ingress.py]
  Group[原测试群薄入口 --host-runtime] --> Host
  Host --> History[受限历史轮询Bot / personal_monitor_shared_test.py]
  History --> Chat
  Ingress --> Chat[assistant/service.py + store.py原子入队]
  Chat --> Exec
  Exec --> Pool[通用SQLite连接provider / 有界池]
  Pool -.同库注入.-> Chat
  Pool -.宿主所有权.-> Ingress
  SDK --> ChildPool[子进程本地ExecutionStore / 只复用连接]
  ChildPool -.本进程provider.-> Ingress
  Schedule[ScheduleManager / CLI / Bot] --> Reports[core/report_execution.py]
  Reports --> Exec
  Exec --> Personal[personal_monitor/tasks.py]
  Personal --> Store[原personal_monitor store/engine]
  Pool -.通用provider注入.-> Store
  Exec --> Worker[main.py --report-worker]
  Worker --> Notify[NotifierManager实际回执]
  Exec --> Health[core/execution_health.py]
```

SQLite 保存执行与业务事实，内存/IPC 用于唤醒；各进程拥有自己的连接。图中的测试群薄入口源码保留并接公共宿主，本机任务注册已注销，不再自动常驻。个人监控业务库与公共执行库职责保持独立，完整合同见[架构说明](architecture.md#公共运行层)。

## 告警链路放大图

```mermaid
flowchart TB
    TRIGGER["定时 / CLI / Bot<br/>日报任务进入公共执行器"]
    DATA["StockDataFetcher → Session<br/>行情、历史数据、技术指标、日期"]
    PRICE["价格条件分支<br/>ConditionChecker"]
    RULES["alerts 配置<br/>锚点、阈值区间、边界、连续次数与重置"]
    ENGINE["AlertProcessor → AlertEngine<br/>最低价相对锚点落入哪个区间"]
    RULESTATE["AlertStateManager<br/>标的 × 锚点 × 区间状态"]
    SINGLE["多层告警关闭或失败时<br/>保留 low < MA60 回退"]
    ACTIVE["按市场活动策略产物<br/>策略身份、参数、执行合同"]
    SIGNAL["策略信号分支<br/>_collect_signal_scan"]
    PLAN["build_trade_plan<br/>统一策略合同生成 TradePlan"]
    TODAY["最后有效交易日事件<br/>买入 / 卖出 / 强制退出、优先级"]
    SESSION["同一 Session 汇总<br/>价格警报 + 策略警报 + 扫描状态"]
    DECIDE["日报通知决策<br/>普通告警与可选日报触发条件分开"]
    NOTIFY["NotifierManager<br/>告警报告或普通日报，多渠道发送"]
    RECEIPT["渠道回执与报表任务结果<br/>成功 / 失败 / 部分 / 未知 / 跳过"]

    TRIGGER --> DATA
    DATA --> PRICE
    RULES --> ENGINE
    PRICE -->|启用多层告警| ENGINE
    ENGINE -->|候选区间| RULESTATE
    RULESTATE -->|允许生成的警报| SESSION
    PRICE -->|关闭或异常回退| SINGLE
    SINGLE --> SESSION
    DATA --> SIGNAL
    ACTIVE --> SIGNAL
    SIGNAL --> PLAN --> TODAY --> SESSION
    SESSION --> DECIDE --> NOTIFY --> RECEIPT
    RULESTATE -.持久化.-> STATEFILE["缓存目录 / alerts_state.json"]
```

- **价格条件与策略事件是两个来源。** 前者按锚点/阈值配置检查，后者读取已激活策略，以统一 `TradePlan` 的最后有效日期事件生成警报。价格告警不需要先运行一次搜参；信号扫描也不会现场寻找新参数。
- **多层规则状态有明确范围。** 连续次数阈值与区间变化重置由配置决定；`AlertStateManager` 不提供渠道送达确认，也不能概括为同日严格去重。其状态在候选警报生成时更新，未与后续发送成功做事务绑定。
- **通知决策与规则判断分开。** `main.run_once` 将两种警报汇入 Session；即使可选日报触发条件未命中，已有普通警报仍可走 `send_from_session`。没有警报时，普通日报是否发送再受频次/手动触发等设置控制。
- **个人提醒与运行健康告警另外维护状态。** 个人持仓/到期提醒走 `personal_monitor` 及其 outbox，执行健康事件走 `execution_health`；二者不共享 `alerts_state.json` 的价格区间规则。

核对入口：[ConditionChecker](../src/core/condition_checker.py)、[AlertProcessor](../src/alerting/alert_processor.py)、[AlertEngine](../src/alerting/alert_engine.py)、[AlertStateManager](../src/alerting/alert_state_manager.py)、[通知管理器](../src/notification/manager.py)、[main.py](../main.py)。

## 搜参子系统放大图

```mermaid
flowchart TB
    START["CLI / 定时 / Bot 优化命令<br/>沿用优化进程合同与互斥"]
    CONFIG["按市场配置<br/>策略、Solver、参数域、预算、窗口、Gate、执行合同"]
    PREP["行情与基准预检<br/>日期、公司行动、时点特征与数据合同"]
    WF["search.workflow.run_optimizer<br/>窗口分工、资源计划、合同哈希"]
    CTRL["SearchController<br/>预算、候选批次与 checkpoint"]
    SOLVER["Solver 插件<br/>genetic / local_genetic / random / simulated_annealing"]
    EVAL["EvaluationService<br/>只开放排名窗口，评价缓存<br/>CPU 标量 / 批量 / 进程后端"]
    STRATEGY["策略插件 + TradePlan<br/>统一回测和现金 / 成交 / 公司行动合同"]
    METRICS["排名评价结果<br/>收益、回撤、基准与约束指标"]
    GATE["CandidateGatePipeline<br/>可行性、惩罚和失败原因"]
    ARCHIVE["搜索归档与断点<br/>候选参数、评价、门槛、Solver 状态"]
    VALIDATE["ValidationController<br/>候选后置稳健性检查<br/>局部敏感性 / 可选股票池扰动 / 独立留出评价"]
    CANDIDATE["版本化候选运行产物<br/>验证、合同、诊断与可激活性"]
    RELATIVE["与同市场 incumbent 对照<br/>相同评价切片与基准<br/>收益改善与安全约束"]
    AUTO["相对晋升通过且自动替换开启<br/>publish_complete_run → activate_run"]
    MANUAL["显式 --activate-run<br/>重新校验完整性与可激活条件"]
    ACTIVE["按市场活动指针<br/>其他市场保持各自绑定"]
    CONSUMER["日报 / 今日信号扫描 / 回测消费者<br/>参考持仓仍按其绑定运行与合同读取"]

    START --> PREP
    CONFIG --> PREP
    PREP --> WF --> CTRL
    CTRL -->|ask| SOLVER
    SOLVER -->|候选参数| EVAL
    EVAL --> STRATEGY --> METRICS --> GATE
    GATE -->|评价反馈经 Controller tell| SOLVER
    CTRL --> ARCHIVE
    GATE --> ARCHIVE
    CTRL -->|搜索结束，保留入围候选| VALIDATE
    VALIDATE --> CANDIDATE
    CANDIDATE --> RELATIVE -->|通过且策略允许| AUTO
    CANDIDATE -->|显式选择| MANUAL
    AUTO --> ACTIVE
    MANUAL --> ACTIVE
    ACTIVE --> CONSUMER
    RELATIVE -->|未通过或不开启自动替换| KEEP["保留候选与诊断<br/>不自动改变活动指针"]
```

- **搜参不是 LLM 调用池。** 并行度、批大小和后端由搜索资源规划与 Solver 能力决定，不能套用财报 API 的 20 路并发或美元预算。
- **参数搜索、策略、评价解耦。** Solver 用 `ask/tell` 提议参数与接收评价，不直接访问留出窗口、执行成交或决定通知；统一策略和 Backtester 负责候选的交易计划及评价合同。
- **门槛有不同层次。** 排名阶段的 `CandidateGatePipeline` 会影响可行性及分数；搜索完成后 `ValidationController` 才执行配置要求的后置稳健性和独立窗口评价，不能将这些结果反馈成 Solver 的训练窗口。
- **候选发布与活动切换分开。** 候选文件可保留而不替换活动参数。自动切换要求同市场相对晋升通过且配置允许；人工切换也调用 `activate_run` 重新检查产物合同和资格。当前实现允许已记录的相对晋升作为激活路径，否则检查绝对留出通过条件，不能画成两条路径都必须再叠加同一绝对收益门槛。
- **消费者只读取确定版本。** 告警信号读取活动产物；已初始化参考持仓绑定运行、参数和执行合同，不因活动指针变化悄悄换成另一套策略。股票池扰动/benchmark 的诊断任务本身不激活结果。

核对入口：[run_optimizer](../src/search/workflow.py)、[SearchController](../src/search/controller.py)、[EvaluationService](../src/search/evaluator.py)、[Gate](../src/search/gates.py)、[ValidationController](../src/search/validation.py)、[相对晋升](../src/search/promotion.py)、[产物与激活](../src/search/artifacts.py)。

## 财报研究链路放大图

```mermaid
flowchart TB
    RAW["原始资料<br/>PDF / 行情 / 免费接口响应"]
    EXTRACT["第一阶段：按年度、按块提取<br/>financial_pipeline → build_fundamental_data_v2"]
    ALIGN["第二阶段：离线整合与跨年对齐<br/>build_financial_context"]
    CONTEXT["模型正文 + 阅读页<br/>保留来源、冲突、缺口与未定口径"]
    SUMMARY["单公司完整摘要<br/>summary-prepare / summary-run"]
    HANDOFF["第三阶段输入包<br/>公司摘要、元数据、来源哈希"]
    PEER["多公司横评<br/>run_peer_research"]
    DRAFT["正式研究 · 初稿"]
    DRAW["本地绘图"]
    REVIEW["正式研究 · 审阅<br/>读取实际图片"]
    REDRAW["本地重绘"]
    FINAL["正式研究 · 定稿<br/>读取修订图片"]
    DELIVERY["最终正文 / 阅读页 / 图表<br/>来源、token、费用、缺口与哈希"]

    RAW --> EXTRACT --> ALIGN --> CONTEXT
    CONTEXT --> SUMMARY --> HANDOFF --> PEER
    PEER -->|显式准备研究输入| DRAFT
    DRAFT --> DRAW --> REVIEW --> REDRAW --> FINAL --> DELIVERY

    CONTRACT["每批冻结输入、Prompt、请求与预算<br/>原始响应 / usage / 失败附件单独保留"]
    CONTRACT -.约束.-> EXTRACT
    CONTRACT -.约束.-> SUMMARY
    CONTRACT -.约束.-> PEER
    CONTRACT -.约束三轮执行.-> DRAFT
```

这条完整路径组合了多个显式命令，不是 `financial_pipeline.py` 一条命令自动执行到最终报告。正式研究入口也接受已声明的其他材料；上图表示本聊天四大行采用的交接路线，而不是所有研究都必须先横评。

第一阶段使用提取模型；第二阶段是本地机械整合，不新增模型调用。摘要、横评和正式研究分别有自己的准备与执行边界。`research_report.py` 的固定三轮依次为初稿、审阅、定稿；图片在轮次之间本地生成，后续轮次读取实际图片。

## 关键边界

1. **公共执行器的覆盖范围。** 日报、简报、飞书会话和个人监控任务接入公共执行器。策略优化仍由原进程合同与独立子进程执行；财报研究是独立批次，不能从公共队列并发数推断研究 API 并发数。
2. **服务宿主与传输。** `RuntimeService` 组合调度器、Bot 与执行器；飞书会话由 Python 持有凭据、模型 HTTP、工具校验和审计。Pi 是可选编排桥，不独立取得文件系统、Shell 或模型凭据。HTTP/HTTPS 管理监听入口已经退役。
3. **个人监控与策略账户分开。** 个人持仓、提醒、版本及 outbox 由个人监控业务库维护；共享执行器及连接 provider 不等于合并业务账户。计划任务和 watchdog 属于本机运维，不能据此认定生产已部署同一候选版本。
4. **研究与策略没有隐式接线。** 研究报告不会自动变成交易信号或活动策略。基本面特征只有经显式历史面板、时点与可用性合同接入策略；候选仍需回测、留出与晋升门槛。
5. **成交含义。** 图中 `TradePlan`、Backtester 和参考持仓描述模拟成交、评价与账户记录，不能据此宣称已连接券商实盘下单。
6. **源资料状态逐层区分。** 原件存在、文本可读、接口字段有值、模型提取成功、财务口径对齐和研究可用是不同状态。采集局部缺口标记为 partial，并保留实际数据；源站封禁、磁盘不足等全局问题另行处理。
7. **费用与预留分开。** 返回 usage 的计价、账户范围的实际扣减和在途最大预留分列；缺 usage 保持未知。历史最大预留不是实际消费，模型 completion 已包含 reasoning，不重复相加。
8. **输出与发送分开。** 生成研究 HTML、PDF 或图表不自动调用通知管理器。日常业务需要显式通知调用，个人提醒使用自己的投递适配器；发送回执区分成功、失败和未知。
9. **旧路径只读。** `src/fundamental_quant` 保留历史查询与已有文件指针；旧执行、自动研究、补修与发布调度已退役。`data/fundamental_quant` 下仍可保存 tokenizer、源资料及历史文件，目录名称不能用于判断旧执行引擎仍在运行。
10. **保留能力与已终止实验分开。** `fundamental_embedding` 只保留仍被其他业务使用的数据/估值适配，`experiments` 保留既有 benchmark 等工具；指定MOE训练、旧评分/消融与Ke实验已删除，后续不再尝试，见[终止记录](development/failed_quant_experiments_retirement_20261030.md)。助手的原研究worker队列与Docker沙箱仍保留，没有迁入公共对话队列，也不等于恢复这些实验。

## 模块与源码导航

| 范围 | 主要入口与实现 | 职责 |
|---|---|---|
| 主入口 | [main.py](../main.py) | CLI 分流、日报业务、优化、审计与活动策略消费 |
| 服务宿主 | [runtime_service.py](../src/core/runtime_service.py)、[schedule_manager.py](../src/core/schedule_manager.py) | 生命周期、调度、接收器、心跳及计划停止 |
| 公共执行器 | [execution.py](../src/core/execution.py)、[execution_health.py](../src/core/execution_health.py) | 持久入队、任务分道、并发池、租约、恢复与健康事件 |
| 报表执行适配 | [report_execution.py](../src/core/report_execution.py)、[report_cli.py](../src/core/report_cli.py) | CLI / Bot / 调度共用任务入口，内部 worker 调用报表业务 |
| 交互与助手 | [interactive](../src/interactive)、[assistant/service.py](../src/interactive/assistant/service.py)、[Pi 桥说明](../tools/feishu-pi/README.md) | 认证入站、命令、会话、受控工具、提案及编排 |
| 个人监控 | [personal_monitor](../src/personal_monitor)、[personal_monitor_runtime.py](../src/personal_monitor_runtime.py) | 业务存储、持仓、提醒、来源行情及投递 |
| 数据接入 | [data_source.py](../src/data/data_source.py)、[data_fetcher.py](../src/core/data_fetcher.py)、[data](../src/data) | 多源行情、缓存、技术指标、公告、公司行动与时点数据 |
| 标的画像 | [instruments](../src/instruments) | 标的分类、财务推导、基金穿透及审计 |
| 策略 | [strategy](../src/strategy) | 策略注册、行情与基本面面板、信号和 TradePlan 合同 |
| 搜索与晋升 | [search](../src/search)、[promotion.py](../src/search/promotion.py) | Solver、评价、门槛、验证、版本产物及按市场相对晋升 |
| 仿真与参考持仓 | [backtest](../src/backtest)、[ref_portfolio.py](../src/core/ref_portfolio.py) | 模拟成交、现金流、基准、评价与版本化执行合同 |
| 告警链路 | [condition_checker.py](../src/core/condition_checker.py)、[alert_processor.py](../src/alerting/alert_processor.py)、[alert_state_manager.py](../src/alerting/alert_state_manager.py) | 价格阈值与区间状态；与活动策略信号汇入 Session 后由通知层分发 |
| 报告与通知 | [notification](../src/notification)、[alerting](../src/alerting)、[templates](../src/templates) | 报告渲染、条件报警、渠道投递及回执 |
| 源资料批次 | [backfill_ashare_sources.py](../scripts/backfill_ashare_sources.py)、[采集指南](guide/ashare_source_backfill.md) | 筛选、原件/行情/免费财务归档、限速、断点复用与缺口 |
| 财报统一 CLI | [financial_pipeline.py](../scripts/financial_pipeline.py)、[fundamental_data](../src/fundamental_data) | 提取、离线对齐、摘要准备/执行与第三阶段交接 |
| 提取与对齐实现 | [build_fundamental_data_v2.py](../scripts/build_fundamental_data_v2.py)、[build_financial_context.py](../scripts/build_financial_context.py) | 年度分块提取、结构校验、跨年正文与阅读页 |
| 摘要与横评 | [run_company_summary.py](../scripts/run_company_summary.py)、[run_peer_research.py](../scripts/run_peer_research.py)、[handoff.py](../src/fundamental_data/handoff.py) | 模型传输与回执、单公司摘要、横评及输入交接 |
| 正式研究 | [research_report.py](../scripts/research_report.py)、[research_inputs.py](../scripts/research_inputs.py)、[research_rounds.py](../scripts/research_rounds.py)、[research_render.py](../scripts/research_render.py) | 冻结材料、三轮研究、本地绘图与最终交付；复用摘要执行器及交接依赖 |
| 共用数据与保留诊断 | [fundamental_embedding](../src/fundamental_embedding)、[experiments](../src/experiments) | 保留业务的时点数据、估值适配与 benchmark；指定MOE/旧评分/Ke实验已终止 |
| 旧历史接口 | [fundamental_quant](../src/fundamental_quant)、[退役说明](development/research_retirement_20261024.md) | 只读历史元数据与已有报告路径 |
| 配置与运维 | [config_store.py](../src/core/config_store.py)、[config](../config)、[tests](../tests)、[ci_cd_deploy.py](../ci_cd_deploy.py) | 配置合同、Prompt/Schema、测试、部署与运行核验 |

## 状态和文档维护

本图不固化 PID、实时进度、API 余额或运行批次费用。运行状态应读取具体批次的 launch/status、回执、冻结清单和验收报告；历史产物继续保持原样。

2026-10-25 的默认正式研究配置使用输入驱动的公司与行业范围，旧工程/白酒案例模板仅作为历史版本保留，不属于默认通用研究合同。旧五脚本目标未实现，相关旧执行说明已合并至[退役记录](development/research_retirement_20261024.md)。

修改架构时同步检查实际入口、执行机制、依赖和输出消费者，避免只更新模块名称而保留过时箭头。


## 清理前未提交改动与架构模块对照（历史快照）

本节固定记录2026-10-30 19:48的清理前状态，不代表当前仍未提交的文件。后续清理与提交结果见[清理记录](development/workspace_cleanup_20261030.md)。

盘点时间：北京时间2026-10-30 19:48；基准为本机 `master` 的 `9b3f6ee2afa464aa1d5410d5ba3275c0abce183b`。最近合并已包含公共执行层、版本标识与多市场公告等提交，本节只列该HEAD之后的工作区差异，不把已经提交的功能再次算作“未提交”。无暂存文件，本次不提交、不推送、不部署。以下数量固定为快照时点；原件下载会继续增加数据文件，结束复核时新增项仅为原件PDF，功能/配置/测试/文档清单未变。

Git共列出23,712个变化路径：140个已跟踪修改、44个已跟踪删除、23,528个未跟踪文件。其中文件层面的代码/配置/测试/文档共538个，另外23,174个在`data/`、`.runtime/`和`tmp/`。后者以资料、运行/验收结果、测试临时目录和本地依赖为主，不能按源码一并提交。

以下按路径主职责归类，每个文件只计一次；`main.py`、优化配置、测试和文档实际可能包含跨模块改动，表格不是可直接整文件提交的分组。M为已跟踪修改，D为已跟踪删除，新为未跟踪文件；文件数不等于测试用例数、代码行数或生产进程数。

```mermaid
flowchart TB
    CORE["公共执行内核/宿主/持仓业务核心<br/>已提交；不是整套仍未提交"]
    ENTRY["入口与通用配置<br/>6文件未提交"]
    DATA["数据与标的基础<br/>28文件未提交"]
    REPORT["日报简报与价格告警<br/>10文件未提交"]
    SEARCH["策略搜参与参考持仓<br/>21文件未提交"]
    ASSIST["飞书助手/Pi/研究沙箱<br/>16文件未提交"]
    FIN["新基本面财报链路<br/>52文件未提交"]
    RUNTIME["公共宿主外围调度/锁<br/>3文件未提交"]
    STOP["指定实验终止<br/>37个代码路径有Git差异<br/>专属测试另计"]
    ENTRY --> CORE
    RUNTIME --> CORE
    CORE --> REPORT
    CORE --> ASSIST
    DATA --> REPORT
    SEARCH --> REPORT
    DATA --> FIN
    STOP -.清理导出；保留共用适配.-> DATA
```

| 架构模块/主归属 | 文件数 | M | D | 新 | 当前改动范围 |
|---|---:|---:|---:|---:|---|
| 入口与通用配置 | 6 | 6 | 0 | 0 | main.py等跨模块入口、依赖和通用配置；同文件可能含多个主题，不能按整文件混提交。 |
| 文档与项目说明 | 99 | 19 | 0 | 80 | 架构、指南、合同、历史审计和项目记录；含文档目录下的验收产物。 |
| 运维与审计工具 | 8 | 2 | 0 | 6 | 数据快照上传/下载、迁移、费用与部署/核对工具；不自动等同于生产未部署业务。 |
| 策略搜参与参考持仓 | 21 | 20 | 1 | 0 | 策略配置与晋升、评价/验证、参考持仓、回测修复、保留的PB/期权研究与benchmark。 |
| 新基本面链路 | 52 | 1 | 0 | 51 | 财报提取、跨年整合、摘要/横评/三轮研究、披露结构、Prompt与版本配置；保留历史Prompt不等于默认使用。 |
| 数据与标的基础 | 28 | 18 | 0 | 10 | 采集/目录/下载与披露、交易日/行情时间、逐时点数据及行业/估值共用适配。 |
| 实验终止 | 37 | 2 | 35 | 0 | 指定旧估值/行业评分/消融、MOE及Ke的已跟踪删除；共享包导出清理。原未跟踪文件删除不会显示D。 |
| 日报简报与告警 | 10 | 9 | 0 | 1 | 通知正文/图表/模板、盘中行情与价格条件；不是未提交整个公共报表执行器。 |
| 公共宿主外围 | 3 | 3 | 0 | 0 | 进程锁、调度和历史调度迁移的剩余改动；ExecutionRuntime/RuntimeService核心已提交。 |
| 飞书助手与研究沙箱 | 16 | 8 | 0 | 8 | Pi/检索路由、命令及Telegram适配、沙箱Docker/部署辅助；通用SDK/持久队列核心已提交。 |
| 测试与夹具 | 232 | 52 | 8 | 172 | 跨模块测试、夹具、被退役测试删除；不把夹具文件数当用例数。 |
| 本机持仓验收配套 | 6 | 0 | 0 | 6 | 验收脚本、隔离配置模板和性能包；个人监控业务核心及薄入口已提交。 |
| 历史研究材料包 | 20 | 0 | 0 | 20 | skills/fundamental-research静态材料20文件；当前源码未发现引用，不属于已确认新财报执行闭包。 |

已提交且当前无工作区差异的核心包括：`src/core/execution.py`、`execution_health.py`、`runtime_service.py`、`report_execution.py`、`report_cli.py`、`src/interactive/ingress.py`、`src/personal_monitor/`及`personal_monitor_runtime.py`。这表示相对本机HEAD已提交，不等同于它们刚刚重新完成线上验收。

几个需要明确的边界：

- **新基本面不是全部未提交。** 固定三轮研究入口和共用摘要执行器已有提交；未跟踪的大头是提取/整合/横评入口、披露处理实现、配置和版本化Prompt。历史Prompt版本保留，不代表默认配置仍用旧版。
- **实验删除数与Git的D数不同。** 上次清理共删除27入口/17实现/16专属测试，其中部分文件原本未跟踪，删除后不会出现在`git status`。本次仅统计Git实际报告的差异。
- **历史研究材料单列。** `skills/fundamental-research/`有20个未跟踪静态文件；初步产品源码检查未发现调用；后续清理查到一个测试仍读取其中方法原文，现已将原文迁至`docs/design/company_summary_question_origins.md`并改测试依赖，旧20文件已删除。
- **公共层与个人监控的验收配套另计。** 脚本和配置模板仍有未跟踪文件，不能由此声称公共执行内核或持仓业务尚未提交。
- **不以未提交推断未部署。** 先前采用过限定文件发布/生产补丁；本节没有逐文件重新核对生产，不能仅看本机Git状态断言线上是否存在某个改动。

完整逐文件清单保存在本机`.tmp/uncommitted_architecture_20261030/modules.json`，原始Git状态及差异统计分别为`status.json`、`numstat.txt`。大批产物文件不复制进正文；可据清单逐模块审阅，但不得直接`git add .`混入数据或未审改动。
