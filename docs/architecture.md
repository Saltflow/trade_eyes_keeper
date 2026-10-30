# 项目架构与模块职责

核对日期：2026-10-29。架构图集中保存在[架构图独立页](architecture_overview.md)，本页说明执行机制、业务合同、模块职责与依赖债务。其他文档见[文档索引](README.md)。

## 当前发布与运行边界

公共运行层已于 2026-10-28 按独立提交 `c7d7656` 部署，实际采用 38 个明确文件的哈希覆盖，并保留线上业务补丁。本机其他研究、采集和优化实验没有随之发布。发布事实见[迁移验收记录](development/shared_runtime_migration.md#2026-10-28-已授权发布与生产切换)。10 月 29 日 09:03 另只读核对了线上飞书配置、关键源码与既有审计，范围见[本机/线上对照](architecture_overview.md#本机与线上环境对照)，不代表全量源码一致或新消息验收。

本机 `TradeEyesPersonalMonitorTest` 已于 10 月 29 日按用户授权注销；旧独立对话/消费循环、专用 watchdog、安装脚本及专属测试已退役。测试入口仅保留公共宿主适配与只读发现功能，三份历史数据库、源码及任务 XML 均保留。线上 v1.18.0（6a55894，构建 b7e30404）只读核实无旧业务进程和调度；本次没有重启生产。部分真实对话补验仍单列，不据此补写通过。见[退役记录](development/shared_runtime_retirement_20261029.md)。

## 公共运行层

飞书普通助手先受 `interactive.feishu.assistant.enabled` 控制。本轮研究问答修复候选强制资料问题走 Pi，并移除股票/估值模板短路；其他问答仍由 `harness_backend: native|pi` 选择循环，两种后端均支持工具。候选尚未部署。当前本机主配置默认关闭助手，线上配置启用 Pi，不能把本机未启用说成通用 Pi 已退役。具体开关、模板提前返回和模型工具循环见[飞书路由图](architecture_overview.md#飞书开关与对话路由)。

实时对话、持仓行情/到期/投递、日报与简报接入同一个
`src/core/execution.py`。SQLite保存入站和执行事实，内存/IPC只用于唤醒；
跨会话默认并发2、同会话保序，报表与到期使用独立容量。
`execution_health.py`共用同库事件/outbox和控制lane；不新增外部队列、端口或常驻进程。
持仓业务库、账户版本及一次性outbox仍由原store/engine维护。
报告声明可选的`JobSpec.starting_side_effect`，让启动意图与claim在同一FULL事务提交；省写前仍核对当前status/token/cancel。其他任务保持原合同。报表phase文件即时记录，共享诊断每10秒合并，最终回执和取消不合并；CLI复用已加载application。
执行器持有一个有界SQLite连接池，按线程、数据库及事务模式隔离；原持仓store通过通用provider借用连接，不反向依赖core、不另建一套池。本轮对话store和DurableIngress也接入该provider；宿主只在同库身份校验后绑定，SDK子进程本地构造ExecutionStore以拥有自己的连接，不启动执行器、注册或claim，不跨进程传递连接。四个缓存句柄是缓存上限，嵌套借用保留独立临时连接，不能据此声称全部连接硬限四个。对话每次借用都启用外键，提交/回滚和同步写盘保持。原CPU超门槛失败记录保留；2026-10-28用户接受必要的持久化开销，取消5%硬门槛，不将旧数值失败改写为通过。单次投递有界批处理，每条仍分别持久claim、提交意图和回执；提交结果未知不重发。
纠错候选从认证文字校验后读取，实际修改/撤销/重做经同事务历史与版本保护；
纯历史请求不能转成新导入。候选展示与写入共用`personal_tools.py`的事实来源解析，
完整事实加执行确认不按跨消息拼接处理；确认不刷新事实时间，旧事实不得越过新事实或版本水位。
普通持仓回复保留代码、数量、来源、事实时间及行情核心证据。命名账户的只读数量/来源/时间/待确认查询使用确定性展示；待确认截图逐账户、逐证券、逐批显示数量和金额，不凭列表位置拼接，也不推定单位、币种或期限。重复截图导入沿用原幂等结果时，回执明确说明本次未更改。超大诊断只缩短展示，原截图和审计全文不变。
宿主状态、报表结果与测试状态共用`runtime_files.py`的有界原子JSON写盘。
`RuntimeService`继续作为组合宿主，注入同profile执行器和可信Python接收器工厂，先锁数据库目录并校验活所有者，再构造Bot。接收器统一提供接单、暂停、停止和健康接口；旧消息轮询只是传输适配器，不再需要另一套模型、业务调度或健康循环。计划停止绑定owner、PID出生身份和宿主代际，通过原主循环处理，不新增端口。OS负责进程重启；异常退出后的中断摘要通过公共outbox去重，整进程死亡期间的即时通知需要外部观察者，不能由本进程保证。

公共运行层图见[独立图页](architecture_overview.md#公共运行层放大图)。历史版本、逐项验收、性能取舍和部署证据见[公共层迁移记录](development/shared_runtime_migration.md)。

## 财报与基本面研究

当前文件交接路线为：原 PDF → `financial_pipeline.py extract` → `context` 离线跨年整合 → `summary-prepare` / `summary-run` → `stage3-input` → `run_peer_research.py` 横评 → `research_report.py` 固定三轮研究。每一步需要显式执行；正式研究也可接受其他明确声明的材料，不强制先横评。

第二阶段不新增模型调用。正式研究三轮为初稿、读取实际图片的审阅、读取修订图片的定稿；图片在轮次之间本地生成。默认 v6 模板由实际输入确定公司数量、行业、角色和历史窗口。命令见[财报流程](guide/financial_pipeline.md)与[正式研究指南](guide/research_report.md)。

旧 `src/fundamental_quant` 仅剩四个历史读取模块，以只读 SQLite 返回已有元数据和文件路径。旧研究执行、补修、发布、通知和调度已退役；旧执行命令返回退役错误。统一边界见[旧研究退役记录](development/research_retirement_20261024.md)。

## 三条权威业务链路

### 日报、今日扫描与参考持仓

```text
main.py --once
  -> report_cli / report_execution
  -> 公共执行器 -> 独立 report worker
  -> SessionManager
  -> StockDataFetcher / DataSource / TechnicalIndicators
  -> ConditionChecker + AlertEngine
  -> 读取最近一次完整激活的 Search Artifact
  -> 活动 Strategy.make_signals(...) 生成 TradePlan
  -> Backtester 生成 EvaluationReport
  -> NotifierManager 渲染 HTML/PDF/Email/Feishu/Telegram
```

简报走同一报表执行适配，但业务只生成价格与锚点快照，不执行上图的完整日报扫描/回测。

今日信号是完整 `TradePlan` 最后有效交易日的事件；通知层只消费
`EvaluationReport`，不重新计算收益、胜率或持仓。

### 搜参、相对晋升与激活

```text
main.py --optimize
  -> search.workflow.run_optimizer
  -> SearchController
  -> Solver.ask()
  -> EvaluationService
  -> Strategy.make_signals()
  -> Backtester
  -> CandidateGatePipeline
  -> Solver.tell()
  -> ValidationController
  -> 完整候选运行产物
  -> 同市场 incumbent 相对晋升比较与记录
  -> 自动替换配置开启且相对晋升通过时激活；否则保留候选
  -> 或 main.py --activate-run <run_id> --group <market> 显式校验并激活
```

Solver 只能看到排名窗口。隔离窗口、最终留出窗口、策略判断和成交细节不得进入
`ask/tell`。当前 Solver 插件为 `genetic`、`local_genetic`、`random` 和
`simulated_annealing`。

候选发布不等于活动指针更新；自动相对晋升与人工激活都经过产物合同和可激活性检查，具体相对/绝对留出路径以 `search/artifacts.py` 为准。详细调用、状态及与告警的连接见[搜参子系统放大图](architecture_overview.md#搜参子系统放大图)；价格阈值和活动策略信号两条分支见[告警链路放大图](architecture_overview.md#告警链路放大图)。

### 已终止的量化实验

MOE/基本面定价训练、旧估值特征/行业评分/消融、Ke 搜参与低波组合及旧监控池状态回放已于 2026-10-30 删除。原因是效果始终未正，后续不再重新尝试；不属于新财报研究流程。仍被保留业务使用的数据合同、行业资料及估值适配保持。见[终止记录](development/failed_quant_experiments_retirement_20261030.md)。

## 包职责与扩展点

| 包 | 负责 | 主要扩展位置 |
|---|---|---|
| `src/strategy` | 参数到买卖决策和 `TradePlan` | `strategy/plugins/*.py` |
| `src/search` | Solver、候选评价、Gate、验证与产物 | `search/solvers/*.py` |
| `src/backtest` | 成交、资金仿真、基准和统一报告 | 通用执行策略，不允许策略 ID 分支 |
| `src/experiments` | 离线 benchmark 与搜索深度分析 | 只读生产接口，不激活产物 |
| `src/data` | 行情、公告、缓存和批量回填 | 新数据源或 store |
| `src/instruments` | 类型化标的、财务推导、ETF/REIT 穿透 | 新官方 provider |
| `src/fundamental_embedding` | 保留业务共用的逐时点数据、行业分类和估值合同 | MOE训练及指定评分实验已终止，不再作为扩展方向 |
| `src/session` / `src/models` | 日报运行态与跨步骤类型模型 | 不放策略判断 |
| `src/notification` | 同一报告对象的多渠道渲染 | 新通知适配器 |
| `src/core/runtime_service.py` / `schedule_manager.py` | 宿主、心跳与唯一任务调度 | 报表经公共执行器；优化沿用原进程合同 |
| `src/core/execution.py` / `execution_health.py` | 持久队列、claim、恢复和健康事件 | 可信 Python 模块注册 |
| `src/personal_monitor` | 个人账户、提醒、版本、行情观察与 outbox | 原业务 store/engine 与任务适配 |
| `src/fundamental_data` / 财报研究脚本 | 提取、整合、摘要、横评与固定三轮 | 显式文件交接合同 |
| `src/interactive` | 飞书长连接和 Telegram 轮询，共享命令分发 | 调用应用用例，不复制业务算法 |

新增策略只添加注册插件；新增优化算法只添加 Solver。具体步骤见
[`src/strategy/README.md`](../src/strategy/README.md) 和
[`src/search/README.md`](../src/search/README.md)。

## 架构审计结论

已经形成的稳定边界：

- 策略插件、Solver 插件、候选 Gate 和 Backtester 已分离，新增算法无需修改
  `SearchController`。
- 搜参、日报和扫描共用 `TradePlan + Backtester + EvaluationReport`。
- benchmark 位于 `src/experiments`，不会发布或激活生产参数。
- 逐时点基本面研究与活动技术策略隔离，具有明确的防前视合同。
- HTTP/HTTPS health server、Web 管理、OTP 和临时报告路由已整体下线。
  飞书/TG Bot 更灵活、更安全，无需开放管理端口。
- `ConfigStore` 将原始 YAML 与环境覆盖分开；管理命令锁内更新最新字段，避免
  旧启动快照覆盖并发编辑或写回密钥。`/schedule` 直接访问当前进程调度器注册表。
- 排名、逆序和留一验证保留同一完整执行合同，包括 raw OHLC、分红、税、拆股和
  基准行动。缺失必要数据或 Numba 明确报错。

仍存在的结构债务：

1. **共享合同所有权不够中立。** `TradePlan`、`PortfolioTrace` 和
   `EvaluationReport` 仍位于 `strategy/api.py`；Backtester 必须反向依赖
   strategy，strategy 的兼容扫描又会导入 backtest/search。后续应迁到中立的
   `src/contracts`，使依赖固定为“策略和回测都依赖合同”。
2. **`main.py` 仍是大型编排文件。** CLI、日报、扫描、审计和激活用例都集中在
   同一文件中。应逐步迁到 `src/application`，让 `main.py` 只解析
   参数并分派用例。
3. **Bot handler 仍直接调用应用编排。** 共用分发器已消除渠道分叉，后续可随着
   `main.py` 拆分，统一注入应用服务接口。
4. **数据采集和标的模型存在循环。** `data` 使用 `instruments` 的财务模型，
   `instruments` 审计又调用部分 data provider。后续应把数据合同放入中立模型包，
   provider 作为端口实现单向注入。
5. **搜索窗口/执行配置仍被 Backtester 读取。** `backtest/engine.py` 导入
   `search.config` 的 `WindowStats` 和约束读取函数。应把执行配置与窗口统计迁到
   中立合同，避免搜索层成为基础设施依赖。

这些是现有过渡依赖，后续重构需要单独验收；文档清理不改变这些依赖。

## 运行时状态目录

- `config/config.yaml`、`config/.env`：本地/服务器配置，不进入 Git。
- `cache/`：可再生缓存，但日常运行依赖其提升速度。
- `data/optimizer/`：搜索运行与活动指针。
- `data/point_in_time/`、`data/reference_universe/`：真实逐时点研究数据。
- `data/analysis/`、`data/instrument_audit/`：可再生研究和审计报告。
- `data/email_archive/`、`logs/`：运行审计记录。
- `data/runtime/`：服务锁与本地心跳，不进入 Git；目录随 `storage.data_dir` 移动。

Python 字节码、pytest/ruff 缓存、`test_cache/`、`.zcode/`、参考池 smoke
输出和服务器代码中转压缩包都属于临时文件，不应提交。
