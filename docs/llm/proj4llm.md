## Authoritative time and ranking contract (2026-08-02)

> This section supersedes every historical description below that calls the
> 12-month period Solver training, treats 13 windows as ranking windows, or
> puts standard deviation and Sharpe directly into the primary objective.

- The fixed horizon is 60 calendar months. The final 9 months are a one-time
  holdout; only the preceding 51 months are observable during selection.
- There are 14 rolling windows, each with 12 months of state lookback and 9
  months of evaluation, advanced by 3 months. The first 11 windows rank
  candidates, the next 2 are isolated because they overlap the holdout, and
  the final window is evaluated only after selection.
- `state_lookback_months: 12` initializes indicators, confirmation streaks,
  and signal locks. It is not Solver training data. Legacy `train_months` is
  accepted only as a configuration-read compatibility alias.
- The 252-session instrument eligibility count is computed over the complete
  instrument lifetime and must not reset at a state-slice boundary.
- One-level parameter sensitivity is a relative robustness check. Hard-gate
  failures are counted separately from numeric scores; the finite-neighbour
  drop and configurable feasible-neighbour ratio determine robustness. A
  neighbour or final selection is never required to have objective score
  greater than zero, because the cross-window range penalty can make an
  otherwise admissible objective negative.
- Instrument-pool robustness is an inner ranking-window cross-validation, not
  a search over symbol identities: finalists are replayed after each single
  instrument removal and reranked without opening the two isolation windows
  or final holdout. Future dynamic pool selection must search transferable
  feature/ranking rules (for example top-K conviction), never per-code include
  switches that turn timing optimization into historical stock selection.
- For ranking window i, define `M_i` as strategy return minus the median return
  of the three configured controls. The primary objective is
  `weighted_mean(M_i) - window_range_penalty * (max(M_i) - min(M_i))`; the
  configured penalty is 0.5.
- Sharpe and cross-window standard deviation remain diagnostics/Gate inputs;
  they are not duplicate objective penalties. The standard Gate still requires
  positive return in at least 6/11 windows, beating any two controls in at least
  6/11 windows, and positive mean `M_i`.

### 搜参数据源与标的池边界（2026-08-28）

- 配置中的监控标的、日报标的和回填标的池保持不变；点时数据缺失、覆盖不足或公司行为无法解释的标的，
  只在 `--optimize` 的候选输入预检阶段排除，并记录为 `search only`，不得从配置中删除。
- A 股 Baostock 价格覆盖完整但仅提供未拆解复权因子的记录，会额外探测 Yahoo；只有 Yahoo 同时满足覆盖起点和现金分红/拆分可执行性时才替换该搜参 bundle。
- Yahoo 公司行为时间戳先按 Unix 秒/毫秒解析，再解析 ISO 日期；并丢弃请求历史区间外的事件，避免时间戳被误读为公元 17/18 世纪日期。
- Solver、参数空间、预算、评分和 raw 成交/qfq 信号合同不变；搜参前统一检查公司行为可解释性，验证链路继续 fail closed。

## 基本面定价画像契约（2026-08-18）

- `src/fundamental_embedding` 是独立的逐时点研究链路，不进入当前技术面
  `SearchController`、活动策略或参考持仓。完整合同见
  `docs/llm/fundamental_embedding.md`。
- 训练只读取披露日不晚于特征日的财务快照，且每个训练标签必须在预测季度前完整
  实现。OOS 评价数据与当前无标签 `FundamentalPricingSnapshot` 分离。
- 四专家分别表示 value、cash return、quality 和 growth；12 维 embedding 由
  四专家信号、四 Gate 权重和四定价后信号组成。当前指数成员身份只用于评估分层，
  不能进入模型。
- “框架可用”不等于“有 alpha”。生产候选必须同时通过横截面宽度、字段覆盖、
  Rank IC、正 IC 季度、top-bottom spread、季度稳定、Gate 相对 uniform 增益和
  510300/510500/510880 三风格池鲁棒性验收。
- 画像驱动内在价值实验的详细合同见
  `docs/llm/intrinsic_value_strategy.md`。当前价格只允许进入反向DCF诊断，
  不得进入 DCF、DDM、盈利能力 DCF 或残余收益的现金流、增长和公允价值公式。
  股权现金流默认按7%/7.5%/8%个人必要回报率与2%永续增长估值，逐时点市场
  CAPM只作定价诊断和更高风险下限；显式主观风险只能在估值后形成买入折价。首轮86家公司
  的252/504交易日评价均未超过账面收益率强制基线，因此该模块保持离线候选，
  不得接入活动策略、搜参、日报或参考持仓。
- CAPM 股权 DCF 的买入价校准同样保持离线，合同见
  `docs/llm/capm_dcf_entry_calibration.md`。它按点时行业和财务数据将金融业路由为
  残余收益、周期业路由为穿越周期 FCFE、其他公司路由为普通 FCFE；三条链分别在
  训练期选择安全边际，再由合并 Gate 统一验收。任何一路或总体门槛不通过，均不得
  写入活动策略、搜参、日报或参考持仓。

### 冻结 CAPM-DCF 价值策略候选（2026-08-25）

> 本小节覆盖上文“CAPM-DCF 一律离线”的旧表述：通过广域训练/留出 Gate 的**冻结政策**
> 可以接入统一 `TradePlan -> Backtester` 链路作手动候选实验；它仍绝不自动激活，也不
> 替换现有技术面活动策略。

- `capm_dcf_value` 是独立注册策略，不向 Solver 暴露公司估值参数。广域训练集只选择
  估值路由、增长组合与基础安全边际；接收标的池只应用它，绝不按小池样本重新训练或
  因小池事件数拒绝该政策。
- 单家公司始终使用点时股权成本 `r_e = r_f + beta_i * ERP`。池平均 Beta **不**重估
  WACC/股权成本，只调节买入安全边际：
  `entry_fraction = clip(base_fraction * (pool_beta / train_beta_reference)^(-gamma), min, max)`。
  `train_beta_reference` 必须只来自冻结政策的训练集；默认 `gamma=0.32`、范围
  `[0.75, 0.95]`，因而低 Beta 池可将 `0.85` 放宽至最多 `0.95`。
- 历史校准与运行时采用同一成交语义：若 `entry_price >= P`，限价单已经可成交，历史标签
  在估值日以已知收盘价 `P` 成交，并从下一交易日开始评估持有结果；若 `entry_price < P`，
  则只在未来252个有效交易日内最低价首次触及限价时，以限价成交。不得把“目标价高于现价”
  误当作拒单。运行时在新估值快照当日只产生一次可执行买入事件；之后仅在首次向下穿越时
  买入；若 `P >= fair_value`，产生卖出决策，具体成交、手续费、最短持有期均仍由共享
  Backtester/执行器处理。
- DCF 每股价格以未复权口径计算。策略面板把公允价和买入价按同日
  `qfq_factor` 映射到统一回测的前复权价格尺度；两侧同时缩放，因而
  `raw_price <= raw_entry` 与 `qfq_price <= qfq_entry` 恒等，不把复权尺度错当成
  价值信号。没有可审计的价格因子、冻结政策、官方无风险利率、或估值快照超过
  550 天时，面板必须缺失并拒绝产生信号。
- `scripts/fetch_capm_dcf_risk_free.py` 以点时基准的自然季度最后有效交易日为锚点，读取
  A股的官方中债十年国债、港股的香港金融管理局两年期外汇基金票据、美股的美国财政部
  十年国债日曲线。每项只写带 `requested_date/source_date/source/maturity` 审计信息的
  真实值；缺失或超过8日陈旧的日期直接失败，不用未来、当期常数或跨市场利率回填。
- `capm_dcf_value` 的市场声明覆盖 A/HK/US，但候选必须逐市场独立提供：带发布日期的
  财务数据、同市场原始价格基准、官方无风险曲线、以及 `dataset.market` 完全匹配的广域
  冻结政策。港股财务只接受 HKEX 活跃证券表 + 披露易标题查询给出的发布日期及报告 PDF；
  美股只接受 SEC Company Facts 的已发布日期快照。报表币种与成交币种不同时，必须使用
  同一估值日或之前的显式点时汇率曲线；任何一项缺失即关闭该市场。当前只完成 A 股因果
  数据覆盖，尚未形成三市场完整候选产物，故无激活资格。
- `scripts/calibrate_capm_dcf_entry.py --market {a_share,hk,us}` 是冻结政策的市场本地
  生成入口：H/US 必须显式给出 `--benchmark-symbol`、`--market-currency`、官方曲线 JSON
  与必要的 `--fx-symbol`，并拒绝将 A 股冻结报告作为 `--frozen-policy-report` 应用到其他
  市场。`--fetch-risk-free` 只允许 A 股中债曲线。
- `scripts/backfill_point_in_time.py` 支持用 `--codes`、`--output-dir`、
  `--evaluation-date` 做隔离的 H/US 数据回填；H 股必须额外声明 `--market-only-symbol
  02800`，有 CNY 报表时声明 `--fx-symbol CNYHKD=X`，美股声明 `--market-only-symbol VOO`。
  `--hkex-report-kind year` 可在首次校准时只抓年度披露。HKEX 年报解析优先使用逐页的
  `pdfminer_stream`，按视觉行重建表格，并只从合并报表页读取当前审计数值；遇到跨业务段
  分行的收入小计时，才回退同一官方年报的五年财务摘要最新列。币种、单位和利润/收入的
  基本一致性校验失败即拒绝快照，不能以页面截断、当前基本面或跨市场利率回填。HKMA 的
  2Y EFN 官方端点可能按月末节奏发布，允许向后使用不超过45日的已发布值并审计其
  `source_date`；禁止未来填充，超过一个发布周期仍必须失败。
- `scripts/backtest_capm_dcf_value.py` 是该候选唯一的离线复现入口：它从点时市场
  存储读取 qfq 执行价、构造上述价值面板，并调用共享 `build_trade_plan` 和
  `Backtester` 生成 JSON。它不创建优化产物、不写活动指针、也不修改参考持仓。

## 当前唯一权威契约（2026-07-30）

> 本节是搜参、回测、日报与今日扫描的唯一当前契约。本文后续按日期记录的
> V1/V2、16 窗口、隔离窗口、A 股默认子集、`--all-markets`、`--strategy`
> 和 Bot preset 内容均为历史记录，已经废弃，不得作为当前实现依据。

### 通用搜索架构契约（2026-08-01）

- 当前源码按扩展职责分为 `src/strategy`、`src/search`、
  `src/backtest` 和 `src/experiments`。公共合同只从 `src.strategy` / `src.search`
  导入；具体策略只放在 `strategy/plugins`，具体优化算法只放在
  `search/solvers`。两类插件均自动发现，新增实现不得修改中央字典、
  `SearchController`、`Backtester` 或 `main.py`。离线 benchmark 和搜索深度
  分析位于 `src/experiments`，不得发布或激活生产参数。
- 生产代码不再包含 `GeneticOptimizer`、`ScoredEncoding`、策略专属
  `scanner.py` 转发层或 percentile 的独立评分扫描 API；候选参数直接以
  `Params` 在统一评价和验证链路间传递。旧 YAML 字段转换只允许存在于
  `src.search.artifacts` 读取/激活边界，核心搜索和策略实现不得回退到旧结构。
- 生产搜索链路固定为
  `SearchController -> Solver.ask/tell -> EvaluationService -> CandidateGatePipeline
  -> ValidationController`。`SearchController` 只管理预算、缓存、排名窗调度、
  排名档案和 checkpoint；其中不得按 Solver ID 或策略 ID 分支。新增优化算法只需
  新增并注册一个实现 `initialize / ask / tell / should_stop / finalists /
  state_dict / load_state_dict` 的 Solver 模块，再配置 `search.solver_id`。
- 当前注册 Solver 为 `genetic`、`local_genetic`、`random` 和严格单线
  `simulated_annealing`。`local_genetic` 保留随机初始化，生成阶段使用
  90% 交叉后局部变异子代和 10% 确定性配额随机移民；每个非移民子代至少
  变异一个活跃参数，最大档距按代数从 3 收缩到 1，并在 Solver 边界去重。
  生产搜参默认使用 `local_genetic`；预算保持每市场
  `30,000 + 5 × 25,000 = 155,000` 次、5 代不变。新 Solver 只可通过
  `search.solver_id` 显式选择。`--optimize` 使用跨进程单实例锁；日调度默认不
  自动启动全量优化，必须通过 `scheduler.optimize_enabled: true` 显式开启。
  要求梯度的未来 Solver 必须在启动前通过能力协商；当前
  Evaluator 声明 `cpu_scalar`、`cpu_batch` 与 `cpu_process`，无梯度、无 GPU 后端。神经网络若
  输出候选参数可实现 Solver；若直接输出逐日买卖信号，则必须实现策略插件。
- `ParameterSchema` 是唯一参数合同，声明类别、布尔、顺序、连续、权重、条件参数、
  合法邻域、变异步长、参数组和 `transfer_key`。旧 `ParamDim` 只在适配边界转换。
  `ParameterSpec.local_values()` 为所有 Solver 提供通用 ±n 档邻域；无序分类
  参数只能切换到其他合法类别。稳健性检查使用 Schema 生成的逐参数相邻档位，
  不再调用策略随机改变整组参数。
- `CandidateGatePipeline` 只消费不可变的排名窗原始指标。Gate Profile 的
  `hard / penalty / diagnostic`、阈值、区间、窗口数量和比例完全由配置声明；代码
  不硬编码窗口门槛。`standard` 当前要求至少 6/11 窗正收益，并在至少
  6/11 窗战胜三个基准中的至少两个；相对每窗第二强基准的平均超额也必须为正。
  三个控制基准按市场配置：A 股为 `510880 / 510300 / risk_free`，港股和美股为
  `VOO / BRK.B / risk_free`。排名 Gate、留出验收、标的池敏感性和激活必须读取
  同一控制集合，缺一项即失败闭合。
  `exploratory` 不具备激活资格。隔离窗和留出窗永远不能进入 Solver、Gate、SearchArchive、
  迁移先验、早停或代理模型。
- `FeatureRegistry` 注册现有 22 列技术输入并转为跨标的可比特征；特征使用
  `float32`/布尔 mask，现金和 NAV 使用 `float64`。`technical_ensemble` 是完整
  22 列与批量内核的参考策略，固定因子经济方向且基本面依赖为空。该策略的
  买入条件需连续 3 个交易日确认，确认状态只产生一次 entry event；条件必须
  先失效才会重新解锁。训练段负责预热确认状态，但测试账户从空仓开始，训练期
  已触发的 event 不得在测试首日重放。执行统一使用 `target_weight`：连续评分
  决定相对仓位，参数仅搜索单标的上限 15%/20%/25% 和总仓上限
  60%/80%/100%，由 Numba 通用执行器在状态变化时分配，不按策略 ID 分支。
- `PortfolioTrace`、`WindowStats`、`EvaluationReport` 统一携带三项决策诊断：
  `signal_event_count` 统计 false→true 的进出事件而非持续信号日，
  `cash_rejected_order_count` 统计因现金/最小手数无法成交的可执行买单，
  `concentration_hhi` 是有持仓交易日的平均持仓 HHI（单标的为 1）。优化和
  日报使用同一统计实现，现金批量内核与标量/目标仓位路径必须一致。
- 最终候选额外计算因果的“同入选篮子长持”诊断：每只标的只在第一次真实
  entry event 时买入一次，之后不卖、不加仓、不再平衡；禁止把未来最终入选
  名单搬到窗口首日。该诊断及其 `timing_value_add` 不进入 Solver、Gate 或
  SearchArchive。跨策略 benchmark 同时保存原始收益最高候选、Gate 最终候选
  和最终候选逐参数 ±1 档邻域，用于区分策略表达上限、Gate 取舍和搜索漏解。
- `EvaluationService` 优先调用 `prepare().evaluate_batch()`，否则使用
  `evaluate_one()` 标量回退；两条路径必须得到完全一致的信号、成交和指标。
  `ResourcePlanner` 每次只启用一个并行轴，禁止嵌套线程池超卖。
- **2026-08-02 authoritative execution correction:** scalar candidate fallback
  now runs in a persistent spawn-based `ProcessEvaluationPool`. The parent is
  the only owner of Solver `ask/tell`, Gate, archive and checkpoint state.
  Workers receive an immutable ranking-window snapshot once; subsequent IPC
  carries only columnar `CandidateBatch` shards and compact objective/raw Gate
  metrics/failure reasons. Full `WindowStats` are materialized only for final
  candidates. Worker completion order is restored to input order before
  `tell`; a partially failed batch is never told or checkpointed. Isolation
  and holdout data are never installed in ranking workers. Candidate worker
  processes force Numba/MKL/BLAS to one thread to prevent nested oversubscription.
- 新产物记录 Solver 配置及参数、特征、Gate、数据、成交、窗口和搜索合同哈希；
  SearchArchive 只写排名窗。checkpoint 仅在合同哈希相同时恢复，finalist 仍由
  排名窗 EvaluationService 重放，不读取隔离或留出数据。
- Search memory is bounded by `search.candidate_retention_ratio` (default
  `0.05`). `EvaluationService` retains only the current best
  `ceil(evaluated_so_far * ratio)` compact records. Genetic Solvers retain
  fixed parents plus current-phase leaders and compact them to one population
  at a generation boundary. The complete ranking history exists only in the
  JSONL `SearchArchive`; finalists are streamed back from that archive before
  full `WindowStats` materialization. Checkpoints persist only this compact
  replay state and are rejected when the configured retention ratio changes.
- Disk retention is independent of the in-memory candidate ratio.
  `search.run_retention_count` defaults to `3`: the newest three complete
  run manifests are retained, while the currently active run and every older
  run directory referenced by its immutable artifacts are protected in
  addition to that limit. Stale no-manifest or interrupted run directories do
  not count as results and are removed before the next search or after their
  failure notification.
- Real CLI `python main.py --optimize` is supervised by the lightweight
  `src.optimizer_guard` parent. A non-zero child exit or SIGKILL produces one
  shared failure summary with per-market archive progress, writes a transient
  `optimizer_failure.yaml` for notification assembly, removes the partial
  multi-gigabyte run directory, and leaves the previous active run untouched.
- 交互配置只允许修改实际生效且通过 Schema 校验的字段：solver、budget、
  gate_profile、positive_windows、majority_windows、min_pos、max_dd、
  window_range_penalty、workers、batch_size, candidate_retention_ratio，以及统一现金档位、手续费和
  初始本金。/switch_optimizer 只选择下一次 --optimize 的候选策略，不改变当前
  生产运行；data_years、旧 hard_constraints 和策略专属规则槽位不再对 Bot 暴露。
  配置先写临时 YAML 并调用正式 load_constraints() 校验，失败时不得替换原文件。
- 参考持仓是独立的持久化模拟账户。只有手动 /ref_date 重置会清仓并把 A/HK/US
  三个资金池绑定到当时最新的三市场完整已激活 run_id；后续搜参或激活不会静默
  切换既有参考持仓。绑定同时记录策略、参数哈希和完整执行合同哈希（本金、手续费、
  最短持有期、市场手数、汇率及策略执行参数）；任一合同无法恢复或变化时必须
  停单并提示再次手动重置。旧 YAML 没有绑定信息时可继续只读展示，但禁止交易。
- 简报参考持仓不得消费逐标的 scan_today 告警或自定义 20%/25% 仓位规则。
  它按固定 run 的全市场数据调用公开
  build_trade_plan -> TradingStrategy.make_signals，只执行计划最后有效交易日，
  并按 cash_cap 或 target_weight 通用声明成交。实盘参考账户买入使用触发日已知
  最高价、卖出使用触发日最低价、估值使用收盘价；同一
  run_id/date/symbol/side 事件只处理一次，避免早盘和午盘简报重复成交。
  日报继续只读，不调仓。
- 以下两项 CPU 批量和搜索深度结果属于 `technical-ensemble/1` 的历史
  `cash_cap` 合同，仅用于解释旧候选，不能作为 `technical-ensemble/2`
  一次性事件与 `target_weight` 合同的当前性能或收益依据。
- 2026-08-01 CPU 批量验收使用完整 A 股配置中 11 个具备完整历史的标的、
  11 个排名窗和同一组 1,000 个 `technical_ensemble` 候选。标量路径耗时
  `49.887s`（`20.05 candidates/s`），Numba `prange` 批量路径耗时
  `2.188s`（`456.95 candidates/s`），加速 `22.80x`；目标分最大误差为 `0`，
  峰值 RSS 为 `0.363 GiB`。批量内核只接管通用 `cash_cap` 合同，日期循环仍在
  每个候选内部串行；其他执行模型自动回退统一标量接口。
- 同日以完全嵌套的 RandomSolver 候选流重新执行 `technical_ensemble` 搜索深度
  实验，检查点为 `1,000 / 2,800 / 4,600 / 6,400 / 8,200 / 10,000`。三市场
  平均最优原始 WF 分为 `-2.858 / -1.911 / -0.356 / -0.356 / -0.356 /
  -0.356`；2,800 起三个市场均出现通过排名 Gate 的候选，4,600 后继续增加
  算力未改善最优分。按 5% 持续边际提升规则，平衡点为每市场 **2,800** 个
  候选。该实验不读取 2 个隔离窗或 1 个留出窗，不修改生产预算和活动策略。
- Gate 改为“6/11 正收益、6/11 胜至少两个基准”后，使用完整配置和上述平衡
  预算重新运行 `technical_ensemble`（RandomSolver，每市场 2,800 候选）。A 股、
  港股、美股分别有 `102 / 244 / 562` 个候选通过全部排名 Gate，选择均来自
  `best_eligible`，而非不可行候选兜底。排名窗结果依次为：A 股平均收益
  `12.31%`、最强基准超额 `4.12%`、`9/11` 正收益、`8/11` 胜至少两个基准；
  港股 `19.77% / 2.50% / 10/11 / 9/11`；美股
  `19.13% / 3.36% / 10/11 / 10/11`。独立留出仅港股胜最强基准：A 股
  `-3.99%`（超额 `-5.44%`）、港股 `13.18%`（超额 `10.41%`）、美股
  `6.86%`（超额 `-3.49%`）。因此本次仅是 Gate 与搜索候选验收，三市场候选
  不具备原子激活资格，也未修改活动策略。原始报告位于
  `data/analysis/strategy_benchmark/20260801_212051/`。
- `technical-ensemble/2` 完成一次性三日确认、训练事件不重放和目标仓位后，
  用完整配置按三市场各 2,800 候选重跑。RandomSolver 的 A/HK/US 排名窗
  平均收益为 `7.44% / 9.93% / 13.54%`，最强基准超额为
  `-0.76% / -7.34% / -2.23%`，留出超额为
  `-8.31% / -7.33% / -0.24%`。排名窗平均信号事件数为
  `95.3 / 31.3 / 29.8`，平均现金拒单为 `3.8 / 0 / 0`，平均持仓 HHI
  为 `0.183 / 0.374 / 0.280`。同入选篮子长持与策略的排名窗收益差仅
  `0.44 / 0.97 / 0.18` 个百分点；A 股留出期择时反而贡献 `-8.26` 个百分点。
  报告位于 `data/analysis/strategy_benchmark/20260801_233703/`。
- 同预算 GeneticSolver 将 A/HK/US WF 分从
  `-4.099 / -11.445 / -4.121` 改善到
  `-2.445 / -8.580 / -3.256`，A 股留出超额从 `-8.31%` 改善到
  `-1.48%`，但三市场留出仍全部未胜最强基准。Random 的 A 股最终候选仅把
  `weight_ma60` 下调一档，平均收益即可从 `7.44%` 升到 `8.63%`；GA 最终
  候选也都存在目标分更高的 ±1 档邻居。因此“2,800 次随机搜索边际平衡”
  只能证明继续盲抽的边际收益低，不能证明高维空间局部收敛；当前 Solver
  需要通用局部精修阶段，不能把漏解误判为策略表达上限。GA 报告位于
  `data/analysis/strategy_benchmark/20260801_234440/`。两轮均未激活候选。
- 新增独立 `local_genetic` 后，以相同数据、Gate、固定种子和每市场
  2,800 候选重跑 `technical_ensemble`。预算拆分为 700 个随机初始候选和
  3 代各 700 个子代，局部档距依次为 3/2/1，每代含 10% 随机移民。
  A/HK/US 均完成 2,800 次发行和 2,800 个唯一参数，停机原因均为
  `completed_budget`；重复运行的参数和分数完全一致。相对同指纹旧 GA，
  WF 分变化为 `+0.495 / -1.455 / -0.791`，留出超额为
  `-2.67% / -7.24% / -0.40%`，三市场仍全部失败。最终候选仍分别存在
  目标分高 `0.209 / 0.587 / 0.230` 的可行 ±1 档邻居，说明局部子代生成
  消除了重复预算但不能替代冠军精修。报告位于
  `data/analysis/local_genetic_benchmark/20260802_094638/`，未激活候选。

- 唯一优化入口是 `python main.py --optimize`。活动策略只由
  `config/config.yaml` 指定；一次运行该策略覆盖配置中全部有效的 A 股、
  港股和美股标的，各市场使用独立资金池、手数和汇率配置。
- 唯一决策与仿真链路是
  `StrategyMarketData -> TradingStrategy.make_signals -> TradePlan -> Backtester
  -> EvaluationReport`。策略只输出逐日逐标的买卖事件、连续评分、目标仓位
  与通用执行声明；
  搜参、日报和今日扫描不得按策略 ID 分支，今日扫描只读取完整计划最后一个
  有效交易日。
- `TradePlan` 不携带具体成交价。唯一 `FillPricePolicy` 由 `Backtester` 和
  优化器共享：买入价取触发日前一日、触发日、后一日最高价的最大值，后一日
  只用于悲观成交定价而不进入策略信号；卖出价取触发日最低价，NAV 始终用
  触发日收盘价估值。评价窗口最后一日缺少后一日价格时，买单标记待执行且
  不计入窗口。`cash_cap`、`target_weight` 和全部配置的可交易控制基准必须消费
  同一个成交合同，不得按策略、市场或参考标的另建成交路径。
- 权威搜参跨度为 60 个自然月：14 个窗口、12 个月训练、9 个月测试、
  3 个月步长。前 11 个测试窗参与排名，接下来 2 窗因与留出期重叠而隔离，
  最后 1 窗只作独立留出。隔离窗和留出窗不得参与排名、敏感性、删标的
  稳健性或候选选择。
- 所有策略按市场统一比较三个配置控制基准：A 股为无风险、510300、510880，
  港股和美股为无风险、VOO、BRK.B。可交易基准使用同样的 0.5% 单边费率、
  自身市场手数/汇率和悲观买价；同池静态等权仅作诊断，不参与 Gate。
  每窗排序仍记录相对三个配置基准中最强者的超额，Gate、留出验收和激活则
  要求超过任意两个基准，即相对当窗第二强基准的超额为正。
- 默认 `standard` Gate 要求至少 6/11 排名窗正收益、至少 6/11 窗战胜
  三个基准中的至少两个，且相对每窗第二强基准的平均超额为正；该门槛完全
  来自配置，可替换 Profile 或改为比例，再应用回撤、交易密度、稳定性和
  夏普约束。参数敏感性逐参数 ±1 档；标的池敏感性是独立且权重更高的门槛：
  前 20 个候选都要执行代码顺序不变性和逐一删标的，至少 80% 删减变体的
  平均“胜任意两个基准”超额仍为正，并按最差下降施加 2 倍惩罚。第一名
  不通过时必须回退到更稳健的后续候选，不能只在选完后写一条诊断。
- 日报加载最近一次完整成功激活的参数，不重新搜索；各市场以自身最后有效
  交易日为终点，评估最近 273 个日历日。日报也使用同一三重基准；其他
  展示参考标的可以保留，但不改变“最强三基准”超额口径。
- 胜率是在每个有效起始日比较策略与参考标的各自持有到评估终点的收益；
  参考价格仅向后对齐，禁止使用未来数据。周 NAV 按自然周聚合 OHLC，短周
  和单交易日周保留；季末持仓取自然季度最后有效交易日的真实模拟状态。
- `EvaluationReport` 是 HTML、PDF、邮件、飞书和 Telegram 的唯一数据源，
  显式包含测试期指标、各参考标的结果、初始/期末资产与现金、期末持仓、
  日 NAV、周 NAV OHLC、季末模拟持仓和胜率明细。
- 注册策略包括 `percentile`、`builder`、`simplified`、
  `regime_pullback`、`technical_ensemble` 和实验性 `ma60_band`。前三者保持历史决策并声明 `cash_cap`；
  `regime_pullback` 声明 `target_weight`，实现 MA200 上升趋势中的
  回撤准备、三日恢复单次确认、30 日正常退出和固定 3ATR 灾难退出。
  `technical_ensemble` 是 22 技术列、批量评价和跨 Solver 比较实现；
  `ma60_band` 只用于四只 A 股的固定规则诊断，不代表 A/HK/US 生产策略。
- `regime_pullback` 和 `technical_ensemble` 搜参只保存三市场完整候选，绝不自动替换当前活动策略。
  候选必须留出胜出且通过全部稳健性门槛，之后由
  `python main.py --activate-run <run_id>` 原子激活。
- 新产物写 `strategy_id`、参数结构标识、执行快照、11 个排名窗、2 个隔离窗、
  1 个留出窗、三重基准和删标的报告。旧 YAML 字段只能在
  `src.search.artifacts` 读取边界迁移，不得泄漏进核心。

## 2026-07-31：类型化标的画像与财务/基金穿透契约

- `python main.py --audit-instruments` 对 `config/config.yaml` 全量标的生成
  `data/instrument_audit/latest.json` 和 `latest.html`。当前画像只供独立审计
  与日报展示；优化器、历史 `StrategyMarketData` 和 `TradePlan` 禁止导入
  当前财务、当前 NAV 或当前 ETF 成分。
- `InstrumentType` 明确区分公司、指数 ETF、行业 ETF、债券 ETF、商品 ETF、
  REIT 和主动基金。`not_applicable` 不进入缺失率分母；`known_zero`、
  `not_meaningful`、`missing`、`stale` 和 `conflict` 必须保留原义，禁止用
  默认值伪造填充。
- 每个 `MetricValue` 保存数值、状态、报告期/as-of、实际披露日、来源、
  币种、说明和冲突备选值。历史查询只允许
  `published_at <= evaluation_date`；无法取得披露日的 Yahoo 快照只能用于
  当前审计，不能进入历史窗口。
- 公司估值使用当前价与最新已披露报表推导：`PB = 现价 / BVPS`；
  `PE_TTM = 现价 / TTM稀释EPS`，缺 EPS 时使用市值/TTM归母净利润；
  `ROE_TTM = TTM归母净利润 / 平均归母净资产`。`PB/PE` 只作交叉校验。
  利润或净资产非正时估值标记为 `not_meaningful`，不展示误导性负倍数。
- A 股累计报表先还原单季度。只有相邻报告期为 55–125 天时才计算环比；
  港股半年频率不得伪装成季度环比。四个连续季度不足时，可用最新已披露
  年报作为明确标注的 TTM 回退，并只计算可验证年同比。
- 指数/行业 ETF 保存发行人、跟踪指数、AUM、费率、NAV、溢折价、分红率、
  带日期的前十大成分和合计权重。成分公司复用同一公司画像；
  穿透 PE/PB 按盈利/账面收益率聚合，穿透 ROE 按穿透盈利/净资产计算，
  增长率只给逐只值及加权中位数。每项聚合必须显示其有效权重覆盖率。
- REIT 单列 NAV、P/NAV、披露口径 TTM FFO、每份 FFO、P/FFO、分派率、
  资产类型和出租率；不从普通净利润强推 FFO。商品和债券基金分别展示
  跟踪偏差或久期/到期收益率，不进入公司 PE/PB 缺失率。
- 数据合并优先级为配置的发行人/官方资料与 SEC Company Facts，其次才是
  Yahoo/QQ 和明确标注的基金披露镜像。冲突值不静默覆盖。日报只加载最近
  一次完整审计文件，不在日报或搜参过程中临时抓取财务/成分。

## 2026-07-31：`regime_pullback` 首次全量验收

- GA、逐参数敏感性和删标的稳健性只执行 11 个排名窗；参数和稳健性选择
  完成后，只有最终参数会一次性执行完整 14 窗并读取 2 个隔离窗和 1 个留出窗。
- 参数无关的窗口输入、日期序号和三重基准 NAV 按市场缓存；相同 genome
  去重。候选评估用固定顺序的 4 进程 `joblib/loky` 执行，随机种子和结果
  顺序不因并行改变。
- 使用 `config/config.yaml` 全量标的和生产预算完成 A/HK/US 三市场首轮：
  每市场 30,000 个参数均未通过 11 窗排名收益门槛，三组结果均为 0 个候选。
  运行未生成可激活参数，活动运行继续保持 `percentile`；不得因本次失败
  放宽最强三基准、7/11 胜窗或独立留出门槛。

## 2026-07-31：`regime_pullback` 搜索深度边际实验

- 搜索深度定义为 Phase 1 实际生成并评估的随机参数数，不是
  `phase1_top_keep`。当前生产配置中的 `10,000` 是 Phase 1 保留上限；实际
  配置预算为每市场 `30,000 + 5 × 25,000 = 155,000` 次候选评估。
- 实验固定生产随机种子 `20260726`，对同一候选序列使用严格嵌套的
  `1,000 / 2,800 / 4,600 / 6,400 / 8,200 / 10,000` 六个前缀。只评价
  11 个排名窗，2 个隔离窗和 1 个留出窗均未读取；因此搜索预算的选择没有
  污染留出集。
- 三市场平均最优原始 WF 分从 `-17.426` 提高到 `-17.118`，增加 10 倍
  搜索量只带来 `0.308` 分改善。相邻档位的效果改善率依次为
  `0.000% / 0.153% / 0.220% / 0.000% / 0.000%`；效果改善率按
  `score_index = 100 + mean_wf_score` 的相邻相对变化计算，避免负分直接
  相除造成符号失真。三市场合计实测评估时间由 `31.1s` 增至 `303.7s`。
- 所有深度、所有市场的排名收益门槛通过数均为 0。A 股、港股、美股在
  10,000 深度的最优原始 WF 分分别为 `-10.730 / -21.209 / -19.414`；
  对最强基准的 11 窗平均超额分别为 `-7.638% / -15.900% / -15.745%`，
  胜窗均为 `0/11`。这说明当前失败的根因是策略族/参数空间没有产生可用的
  择时优势，不是搜索深度不足。
- 按“后续所有算力增量均无法带来至少 5% 效果提升，且没有后来出现的
  三市场排名合格候选”定义，边际平衡点为每市场 **1,000** 个 Phase 1
  随机候选。该结论仅用于诊断 Phase 1；不自动修改生产搜索配置，也不构成
  放宽门槛或激活候选的依据。
- 可复现实验入口为 `python scripts/analyze_search_depth.py`；明细、JSON 和图
  位于 `data/analysis/search_depth/regime_pullback_20260731_011908/`。

## 2026-07-31：注册技术策略统一基准

- 可复现入口为
  `python scripts/benchmark_technical_strategies.py --depth 1000
  --market-workers 12 --evaluation-workers 1`。四个注册技术策略分别在
  A 股、港股和美股使用相同随机种子、每市场 1,000 个 Phase 1 候选、
  11 个排名窗、2 个隔离窗和 1 个留出窗；市场汇总使用等权口径。
- 主进程先为每个市场抓取一份不可变行情与基准快照，再将同一快照复制给
  四个策略进程。产物保存逐标的和基准 OHLC 指纹，防止并行数据刷新、缓存
  竞争或数据源回退破坏可比性。每个任务内部只开 1 个评价 worker，避免
  12 个外层进程再次嵌套多进程。
- 参数只由 11 个排名窗选择。2 个隔离窗和最后 1 个留出窗只在参数选定后
- **2026-08-02 benchmark scheduling correction:** when worker flags are omitted,
  the CLI selects exactly one process axis. A small strategy/market job set runs
  sequentially with all physical cores assigned to persistent candidate-scoring
  workers; a large job set uses strategy-by-market outer processes with one
  evaluator each. `--market-workers` and `--evaluation-workers` cannot both be
  greater than one.
  评价；留出结果不反馈选参。运行只写
  `data/analysis/strategy_benchmark/<timestamp>/` 下的 JSON/CSV，不写优化器
  活动指针，不生成候选运行，也不自动激活策略。
- 2026-07-31 首轮结果曾记录为：`builder +1.907%`（2/3 胜出）、
  `percentile -0.927%`（2/3）、`regime_pullback -4.047%`（0/3）、
  `simplified -9.810%`（0/3）。该次运行中旧策略、`regime_pullback` 和两个
  可交易基准没有消费同一成交价路径，且 `builder.absolute_discount` 使用了
  未来区间最大收盘价；因此该产物及其策略排序已作废，不得作为投资或激活依据。
- 修复后的唯一成交合同为“买入三日最高、卖出当日最低、NAV 当日收盘、窗口
  末买单待执行”。同时将 `builder.absolute_discount` 改为截至当日的历史运行
  高点，不再读取未来。用相同冻结行情、种子、1,000 个 Phase 1 候选和
  12 个外层进程重跑，三市场等权留出超额为：`regime_pullback -4.047%`
  （0/3）、`percentile -4.170%`（2/3）、`simplified -4.497%`（2/3）、
  `builder -9.980%`（0/3）。33 个排名窗平均超额依次为 `-13.225%`、
  `+1.601%`、`+3.068%`、`+2.901%`。
- `regime_pullback` 的结果与旧运行完全一致，因为它原先已经使用目标中的悲观
  成交路径；其三市场均保持现金，仍在三个留出窗输给最强基准。`builder` 留出
  均值从旧产物的 `+1.907%` 降至 `-9.980%`，且从 2/3 胜出变为 0/3，说明旧
  正结果不可采信。该差值同时包含成交路径统一、基准成本统一和重新选参的共同
  影响，不应全部归因于单一代码缺陷。
- 修复后没有任何策略通过三个市场的独立留出：`percentile` 与 `simplified`
  各胜 2/3，但分别在 A 股和港股发生显著负超额；`builder` 与
  `regime_pullback` 为 0/3。因此本轮不激活任何候选，活动策略保持不变。
- 修复后可复现产物位于
  `data/analysis/strategy_benchmark/20260801_004324/`；12 个策略×市场任务的
  并行纯计算耗时为 `76.625s`，数据预取发生在计时之前。生产配置仍为每市场
  最多 `30,000 + 5 × 25,000 = 155,000` 次候选评估，本 benchmark 不修改
  该配置。
- 旧 `simplified` 结果曾出现 33 个排名窗平均超额 `+9.539%`、三个留出窗全败，
  是明显的样本内选择偏差。`regime_pullback` 三市场均无排名合格候选，且
  排名/留出均为负；结合 1,000→10,000 搜索深度边际实验，本问题应优先
  修改策略状态机或参数空间，而不是增加搜索预算。

---

## 以下为历史记录（已废弃，不具权威性）

# 股票量化系统 - 关键设计决策文档

**文档版本**: v1.22
**最后更新**: 2026-07-15

---

## 2026-07-26: A-share optimizer integrity repair

- The 9-month test / 3-month step overlaps the newest 9-month hold-out.  The
  new `purge_overlapping_windows` switch excludes overlapping windows from
  ranking, constraints, GA selection and sensitivity.
- The current A-share horizon uses 13 strict ranking windows, 2 purged windows
  and 1 daily-report validation window; artifacts record those counts.
- Fast optimizer scoring now uses the configured primary benchmark and the
  same position fraction, fill model, lot handling and fees as daily reports.
- Sharpe shortfall and deterministic sensitivity checks re-rank the top
  finalists by a robust selection score.
- `python main.py --optimize` now runs A shares only; `--all-markets` retains
  the three-market entry point and an A-only publish retains compatible HK/US
  artifacts from the active manifest.
- Percentile alerts receive historical data and apply the optimizer's weighted
  aggregate signal; raw snapshot values are no longer treated as percentiles.

## 2026-07-27: A-share absolute-return robust selection

- Candidate gates use only the 13 ranking windows: weighted strategy return
  must be positive and at least 8/13 windows must be positive.  Purged and
  held-out validation windows remain excluded from every selection decision.
- The final robust score is `wf_score - 1.0 * sensitivity_drop`; sensitivity
  is evaluated for the top 500 ranking-qualified candidates.  All three
  values are configurable under `genetic_search`.
- Versioned artifacts, optimizer reports and the daily held-out backtest
  section record ranking-window absolute return, positive-window count,
  sensitivity diagnostics and selection score.  These are selection-only
  diagnostics, separately labelled from the validation-period result.

## 2026-07-26: Three-market backtest recovery

- Restored `main.py --optimize` and its `--optimize-v2` compatibility alias.
  The retained optimizer now runs and persists independent A-share, HK, and US
  percentile parameters instead of applying A-share parameters to every market.
- Prevented short-history instruments from collapsing a market's common
  walk-forward timeline. They remain in daily and brief price scans, while
  optimizer input requires one complete train/test window.
- Repaired interactive date-range backtests to preserve indicator warmup while
  reporting only the requested period.
- Restored the reference-portfolio `commission_rate` argument used by both
  brief reports. Early and afternoon briefs can rebalance and render again.
- Local full run used the configured universe and produced current optimizer
  parameter files for all three markets. Local daily and both brief report
  emails were rendered to `data/email_archive` with external delivery disabled.

## 2026-07-26: Registered optimizer runs and active strategy selection

- `main.py` now uses `argparse`: `--optimize --strategy <registered-name>`
  selects any entry from `src/analysis/strategies/STRATEGIES`; `--optimize-v2`
  remains a compatibility alias.
- Feishu and Telegram parse the same `/optimize <strategy> [fast|deep]` syntax.
  `/switch_optimizer` also discovers entries from the registry rather than a
  hard-coded strategy list.
- Each optimizer trigger writes versioned per-market artifacts under
  `data/optimizer/runs/<timestamp>_<strategy>/`.  Only a run with successful
  A-share, HK and US artifacts atomically updates `latest_strategy.yaml`.
  Daily reports, brief reports and interactive backtests resolve that newest
  complete timestamped run before falling back to legacy settings.
- Optimizer completion sends one three-market summary through the same
  `NotifierManager` fan-out used by daily and brief reports (email, Feishu and
  Telegram), including incomplete-run status without changing the active
  strategy.

---

## 2026-07-15 修复 (v1.22)

### 1. 图表 + 季度统一 9 个月窗口
- `config/config.yaml` `lookback_days: 730` → `189`（9 个月 × 21 交易日）
- `portfolio_strategy.py` 图表标题去硬编码"近2年"，改为 `len(navs)/21` 动态算月数
- 结果：第二张图从 2 年缩到 9 个月，季度从 7Q 缩到 ~3Q

### 2. 消灭重复调度 → 只有 health server 内嵌 APScheduler
- `ci_cd_deploy.py` 删除 cron 安装逻辑，改为**清理**旧 cron 条目
- `src/core/scheduler_manager.py` 移除 `_start_health_server()` 调用（health server 由 `--health-server` 显式启动）
- 生产健康进程只允许由持久化 `trade-eyes-health.service` 管理；部署器必须清理旧 `nohup` 循环，禁止 systemd 与 PID 文件双重拉起
- 结果：不再同时跑 crontab + OLD SchedulerManager + NEW ScheduleManager 三套调度

### 3. 简报信号名修复：用 hk/us 分开扫描
- `main.py run_brief_report()`：`scanner.scan("non_a_share")` → 改为 `scanner.scan("hk")` + `scanner.scan("us")`
- `feishu_notifier.py` brief：补上 `map_hk`/`map_us`（之前只有 `map_a`/`map_n`）
- 结果：V2 优化器产出 `_hk_`/`_us_` YAML 被正确读取，信号名不再是 "买入规则1"

## 📊 参考持仓跟踪 (v1.21)

持久化参考持仓（Ref Portfolio），用于日报/简报展示系统持续运行的仓位状态。

### 数据持久化

文件 `data/ref_portfolio.yaml`:
```yaml
ref_portfolio:
  inception_date: "2026-07-14"
  cash: 95820.50
  initial_capital: 100000.0
  trading_days: 3
  last_rebalance_date: "2026-07-15"
  last_reset_date: "2026-07-14"
  holdings:
    "601728": {shares: 500, avg_cost: 12.34}
  trade_log:
    - {date: "2026-07-14", code: "601728", action: "buy", shares: 500, ...}
```

### 核心模块

`src/core/ref_portfolio.py`:
- `RefPortfolio` dataclass: 完整持仓状态（现金/持仓/初始资金/交易天数）
- `RefPortfolioManager`: 加载/保存/重置/调仓/Nav 计算
  - `load()` / `save()`: YAML 持久化
  - `reset(initial_capital, inception_date)`: 清空持仓，恢复初始现金
  - `rebalance(alerts, prices, date)`: 根据 StrategyAlert 信号按现价调仓
  - `get_status(pf, prices)`: 返回可展示的状态摘要（Nav/回报/持仓/期初/交易天数）
  - `is_initialized(pf)`: 是否已设置期初日期

### 调仓规则

1. **只在简报时间调仓**（09:50 / 14:30）。日报不调仓。
2. **买卖互斥**: 同标的同日既有买又有卖信号 → 双方都取消。
3. **周末阻挡**: 周末（weekday >= 5）不执行任何交易。
4. **手数取整**: A 股 100 股/手、美股 1 股/手。
5. **买入限制**: 单次最多 5000 元，日累计不超 monthly_buy_limit。
6. **卖出比例**: 单次最多 25% 持仓（取整手）。

### 交互命令

`/ref_date [YYYY-MM-DD|confirm]`:
- 无参数: 显示当前基期 + 持仓状态（标的/持仓/现价/现金/期初/交易日）
- 有参数: 设置新基期 → 如果有持仓则要求确认 → 发送 `/ref_date confirm` 执行重置
- 重置行为: 清空所有持仓标的、恢复初始现金（100,000）、期初日期设为指定日期

### 简报集成

`main.py run_brief_report()`:
1. 获取数据 + 扫描信号（SignalScanner）
2. 加载 RefPortfolio → 如果已初始化，按信号调仓
3. 将 `ref_portfolio_status` 附加到 session
4. Email/Feishu/Telegram 三端展示参考持仓

### 日报集成

`main.py run_daily_task()`:
1. 加载 RefPortfolio → 只读，不调仓
2. 将 `ref_portfolio_status` 附加到 session
3. Email/Feishu/Telegram 三端展示参考持仓

### 展示内容

| 字段 | 说明 |
|------|------|
| 期初日期 | `inception_date` |
| 净值 (Nav) | 现金 + 持仓市值 |
| 回报率 | (Nav / initial_capital - 1) × 100% |
| 交易日 | 有交易的天数 |
| 持仓明细 | 代码/股数/现价/市值/成本 |
| 现金 | 当前可用现金 |

### 测试覆盖

`tests/test_ref_portfolio.py` — 25 项 TDD 测试，覆盖:
- 数据模型序列化/反序列化
- 持久化加载/保存
- Reset 清空重置
- 调仓：买入/卖出/互斥/周末/资金不足/手数取整
- Nav 计算与状态摘要
- 交易日计数

---

## 🔌 统一执行配置 (v1.20)

执行参数（monthly_buy_limit / commission / lot / fx）统一从 `config/optimizer_constraints.yaml` → `execution_params` 段读取。
禁止代码写死覆盖。

**配置段** (`config/optimizer_constraints.yaml`):
```yaml
execution_params:
  monthly_buy_limit: 15000
  initial_capital: 100000
  commission_rate: 0.005
  lot_sizes: {a_share: 100, hk: 100, us: 1}
  fx_rates: {a_share: 1.0, hk: 0.9, us: 7.0}
```

**读取模块** (`src/analysis/execution_config.py`):
- `ExecutionConfig` dataclass
- `get_execution_config()` 模块级单例（首次访问加载，避免重复 IO）
- `reload_execution_config()` 强制刷新（`/config set` 后调用）

**飞书交互**:
- `/config show` 显示 `月买入额度/手续费/初始本金`
- `/config set monthly_limit 20000` → 写 YAML → 自动刷新缓存
- 新增可配键: `monthly_limit`, `commission`, `init_capital`

**对齐范围**:
- 搜参（`StrategyOptimizerV2`/`SignalFnSearchEngine`）和日回报测（`PortfolioEvaluator._evaluate_signal_fn`）统一读同一份配置
- 消除 `fx_rates` dict 在两处文件中的重复定义
- 消除 `monthly_limit` 的三个不同硬编码值（15000/100000/inf）

---

## 📊 搜参报告升级 (v1.20)

搜参完成后自动构建完整报告（替代旧版纯文字摘要），不再依赖单独 `/daily` 调用。

**主函数**: `_build_optimizer_report()` 在 `main.py`，搜参后每组调用一次。

**报告内容**（限近9月测试期数据）:
1. **日回报测指标**: total_return, excess_return, max_drawdown, sharpe, avg_position
2. **参数摘要**: 解码后的 tau/w/buy_thresh/sell_thresh/pos_frac
3. **季末持仓**: 最后一个季度快照（代码/持股/成本/现价/盈亏）
4. **参数敏感性**: `PercentileSignalFn.random_perturbations(n=10)` → 10版随机扰动（全参数各 ±1~3 级别），记录最差版收益和 10 版范围
5. **跨天波动率**: `PercentileSignalFn.cross_day_volatility(lookback=5)` → 数据截止日前移 5 天各跑回测，记录收益波幅
6. **周K OHLC 表**: `_build_weekly_ohlc()` 从日线 NAV 聚合周 OHLC

**通知渠道**: `build_optimizer_summary(full_report=...)` 统一渲染为 HTML，邮件/飞书/TG 三端共用。

---

## 🔌 双搜参引擎架构 (v1.19 新增)

### 背景问题
分位评分引擎 (PercentileSignalFn) 之前只是"空壳"：`signal_fn` 仅用于往 YAML 写 `_engine` 标签，其 `evaluate()` 从未被遗传搜索调用。YAML 里实际是全局阈值格式参数 (`buy_1_signal: deviation_absolute`)，导致"信号名与指标对不上"。

### 解决方案：SignalFn 真正接入遗传搜索
- **SignalFn ABC** (`signal_functions.py`)：唯一替换点。新增方法：
  - `random_params/crossover/mutate` — genome 编解码（genome = `Params.values` 整数级别 dict）
  - `scan_signals(params, today, history)` — 引擎自身逻辑判断今日买卖信号（显示层用）
  - `describe_rules(params)` — 参数翻译成买卖规则名（报告用）
  - `engine_brief()` — 引擎买卖标准简介（/switch_optimizer 展示）
  - `execution_params(params)` — 解码执行阈值（买/卖分数阈值+仓位）
- **SignalFnSearchEngine** (`signal_fn_engine.py`)：把 SignalFn 包成遗传搜索器的 `StrategyEngine` 插件。encoding=Params，`evaluate_encoding` 调 `signal_fn.evaluate()` → 共享流水线 `simulate_portfolio`/`compute_metrics` → `WindowStats`。**engine 分支单线程（无 pickle 问题）**。
- **main.py**：`engine_type==percentile` → `engine=SignalFnSearchEngine(signal_fn)`；global → `engine=None`（100% 走旧向量化路径，criterion 1/2 逻辑零改动）。
- **strategy_optimizer_v2._build_report**：`hasattr(ss.encoding,'values')` 判定引擎模式。分位模式写真实分位参数 (`adx_pct_tau` 等, `_mode: signal_score`) + `_signal_fn_to_rules`（引擎自定义规则名 + `condition: __signal_fn__` 标记）。`_save_results` 序列化 `label` 字段。
- **SignalScanner.scan**：rules 含 `__signal_fn__` → 按 `_engine` 构造 SignalFn → `scan_signals()` 用引擎逻辑判断；否则走 legacy `condition` 评估（全局引擎/旧 YAML）。分位分派用 `_computed_cache`（compute_all 后的完整 DataFrame）算滚动分位。

### 关键不变量
- **全局引擎 = deprecated 但默认**，`engine=None`，走旧 FastEvaluator 向量化路径，日报版式与逻辑完全不变
- 旧 YAML（无 `_engine` 或 `_engine: global`, 真实 condition）永远走 legacy 路径
- 分位引擎信号名来自引擎自身：如 `分位评分买入 (score 0.29>0.19)` + detail `趋势强度分位(ADX)分位71%>46%`

### 评分引擎执行语义（`_score_sim_core`，主链路唯一决策仿真）
锁定于 `tests/test_score_engine.py`（18 项）：
1. **买入执行价 = 近 3 日收盘最高价**（v1.20 改为含滑点的 pessimistic 估价；之前是均价）
2. **卖出执行价 = 单日收盘价**（触发日，不平滑）
3. **同日互斥**：同一标的同日既触发买又触发卖 → 双向跳过
4. **月度买入额度**：统一从 `execution_config.monthly_buy_limit` 读取（搜参和日回报测同一值，禁止硬编码）
5. **允许回补**：卖出后可再买入（无 `shares==0` 永久壁垒）
6. 手数取整 / 手续费（0.5% 含滑点）/ 现金约束 / 评分需严格 `> 阈值`
7. **季度持仓成本快照**：`_score_sim_core` 每季度边界快照 `q_cb`(成本基础)+`q_price`(价格)
8. **港股手数**：`_get_lot_size` 返回 100；日回报测路径按标的均价分界（<100→1000, ≥100→100）
9. **汇率**：固定汇率从 `execution_config.fx_rates` 读取，搜参和验证路径均对价格矩阵乘汇率
10. **分位预热**：日回报测前 `PERCENTILE_WARMUP=252` 天禁止交易
11. **验证期限**：日回报测只用近 9 个月（`lookback_days` 默认 441 = 252 预热 + 189 交易）

### 测试矩阵（量化引擎，共 ~80 项）
- `test_score_engine.py`（18）：决策仿真全部执行语义
- `test_percentile_engine.py`（15）：scan_signals/score_timeseries/execution_params/describe_rules/engine_brief/genome
- `test_signal_scanner.py`（20）：`__signal_fn__` 分派 + `_params_from_yaml` + `_make_signal_fn` + legacy 兼容
- `test_signal_fn_engine.py`（5）：适配器编码操作 + evaluate_encoding + 端到端分位搜索
- `test_portfolio_strategy.py`（+7）：`_evaluate_signal_fn` 真实交易回归（防 100% 空仓复发）
- **测试目录结构**：`tests/__init__.py` + `tests/integration/__init__.py` 消除同名模块（`test_signal_scanner.py`）收集冲突；`conftest.py` autouse fixture 清理绑定到已关闭捕获流的 StreamHandler

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，涵盖架构选择、问题解决方案和技术路线。系统核心能力：策略搜索优化器、信号扫描器、xelatex 日报 PDF 生成、邮件提醒、健康检查服务器。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源降级（新浪→腾讯→Yahoo）
2. **配置驱动** - 技术指标/规则/简报通过 YAML 配置，避免硬编码
3. **文档驱动开发** - 所有设计决策存档至本文档
4. **防循环编码** - cycle_guard 自动检测重复错误模式
5. **Session 统一管理** - 全局 SessionContext 防止字段混淆
6. **全量验证** - 策略优化/信号扫描/回测分析基于 config 全量标的运行，禁止子集验证

---

## 🏗️ 架构现状 (v1.18)

```
┌─────────────────────────────────────────────────────────────────┐
│  配置层 (config/)                                               │
│  - config.yaml (股票/邮件/调度/简报)  - alerts.yaml (锚点)      │
│  - optimizer.yaml (策略模板/构建器)  - .env (API Key/密码)      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  缓存 + 数据源 (src/)                                           │
│  - data_source.py   (CSV 缓存 + 复权交叉验证 + 价格校验)        │
  │  - web_crawler.py   (新浪/腾讯/Yahoo 多源降级)                  │
│  - data_fetcher.py  (协调获取 → 调用指标计算)                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  指标计算 (src/)                                                │
│  - technical_indicators.py  (配置驱动 MA60/WMA/RSI/Bollinger)   │
│  - utils/etf_detector.py    (ETF 统一检测)                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  策略 + 分析 (src/analysis/)                                    │
│  - strategy_optimizer.py   (贝叶斯优化 + 两阶段 + 收敛图)       │
│  - signal_scanner.py       (共识扫描 + 每日警报 + 回测)         │
│  - portfolio_strategy.py   (共享资金池 + 贪心搜索)              │
│  - rule_engine.py          (YAML 驱动 + 表达式沙箱)             │
│  - indicator_library.py    (RSI/MACD/ATR/布林/ADX/量比)         │
│  - backtest_config.py      (回测时间线约束)                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  业务层 (src/)                                                  │
│  - condition_checker.py  - alert_engine/processor/state_manager │
│  - email_notifier.py     (告警/日报/简报 + xelatex PDF 生成)    │
│  - health_server/        (健康检查 + OTP + SSL + 报告链接)      │
│  - session_manager.py    (SessionContext Pydantic 模型)          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  模板 + 输出                                                    │
│  - report_daily.tex         (xelatex LaTeX 日报模板)            │
│  - appendix_methodology.md  (13节公式附录, Markdown→LaTeX)      │
│  - email_template.html      (邮件正文, 港式财报卡片布局)        │
│  - optimizer_report.html    (交互收敛图, Plotly 暗色主题)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ 核心架构决策

### 1. Session 数据流 (v3.0)

**决策**: Pydantic `SessionContext` 替代裸 DataFrame/dict 传递
**背景**: `price`/`low_price` 字段名称不一致导致邮件显示价格为 0.00
**实现**: `fetch_to_session()` → `check_from_session()` → `send_from_session()`
**类型安全**: `StockPriceData` / `AlertStock` Pydantic 模型，自动验证数据完整性
**文件**: `src/session/session_manager.py` / `src/models/schemas.py`

### 2. DataSource 统一数据源 (v3.2 → v3.2.1)

**决策**: CSV 缓存 + meta 文件替代 JSON 缓存
**特性**: 7 天保留 / 15:55 当日过期 / 复权交叉验证（双源 3 日 close 比对）/ 价格关系校验
**影响**: `data_fetcher.py` 删 ~170 行缓存代码，`backtest_framework.py` 删内存缓存
**文件**: `src/data/data_source.py`

#### v3.2.1 修复：缓存 bypass + 复权检测回归 (2026-05-25)

**问题**: 重构 DataSource 时丢失了 `_should_bypass_cache`，导致 15:55 后非当日缓存不会被强制刷新，除权后的前复权历史数据持续错误。
**根因**: `fetch_stock_data()` 缓存命中路径直接 `return cached_df`，完全跳过了时间检查和复权检测。
**修复**:
  - 恢复 `_should_bypass_cache(cutoff="15:55", granularity=per-stock)`，按标的粒度生效
  - 缓存命中前增加 bypass 判断，触发后进入增量/全量拉取 → `_check_forward_adjustment` → 合并/覆盖
  - 统一 `requested_start_ts = pd.Timestamp(requested_start.date())`，消除时间分量导致的首行误删 bug
**测试**: `tests/test_data_source.py` 14 个用例覆盖 bypass 边界、复权修正检测、ETF 场景、fallback 路径

#### v3.2.2 数据清理：debt_ratio 删除 + ROE 计算修复 (2026-05-27)

**问题 1**: `debt_ratio`（资产负债率）从腾讯 API `items[52]` 获取，但该字段实为**动态 PE**；`items[53]` 实为**静态 PE**。原映射完全错误，且 debt_ratio 对投资决策无直接价值。
**决策**: 全链路删除 `debt_ratio`（schemas、data_fetcher、web_crawler、email_notifier、模板、LLM analyzers）。
**影响文件**: `src/models/schemas.py`, `src/core/data_fetcher.py`, `src/data/web_crawler.py`, `src/notification/email_notifier.py`, `src/templates/email_template.html`, `src/analysis/llm_analyzer/*`

**问题 2**: ROE 原从 `items[52]` 获取，映射错误后数据不可信。
**修复**: ROE 改为**推导计算**: `ROE = (PB / PE) × 100`，与财报披露值误差 <0.2%（以 600000 实测验证）。
**文件**: `src/core/data_fetcher.py`

#### v1.17 简报增强 (2026-05-27)

**排序**: `send_brief_report()` 按锚点偏离率**升序排列**，跌幅越大越靠前；无有效锚点的股票 `dev_pct=None` 用 `float("inf")` 兜底排最后。
**新增收盘简报**: `config.yaml` 新增 `afternoon_snapshot`（14:30），与 `morning_snapshot`（09:50）共用同一函数，仅标签不同。
**日报时间调整**: `scheduler.run_time` 从 `16:00` 改为 `19:00`，确保 A 股收盘后数据完整（港股 16:00 收盘，美股隔夜）。
**CI/CD 同步**: `ci_cd_deploy.py` 自动注册 3 条 cron：09:50 早盘 / 14:30 收盘 / 19:00 日报 / 02:00 优化。
**文件**: `src/notification/email_notifier.py`, `config/config.yaml`, `ci_cd_deploy.py`

### 3. 数据获取

**主源**: 新浪财经历史数据 API → 腾讯财经 → Yahoo Finance（自动降级）
**股息**: LLM 提取缓存优先 → 网页爬虫备用
**缓存**: `cache/data/` CSV 格式，7 天保留期
**baostock**: 回测历史数据专用（前复权），日期格式 YYYY-MM-DD

---

## 🎯 策略搜索优化器 (v1.14 → v1.18)

### 🔑 权威设计契约 (v1.18, 2026-07-07) — 冲突时以本节为准

> 本节记录搜参 + 日报/简报的**最终设计契约**。历史章节（V1/V2 早期描述）若与本节冲突，一律以本节为准。实现细节见 `config/optimizer_constraints.yaml`（唯一真源）。

#### 1. 搜参：多窗口验证 + 测试集排名 + 验证窗口留出

- **Walk-Forward 多窗口**（取代早期"9个月单窗口 / 4阶段"设计）：
  - 训练 12 月 + **测试 9 月**，滑动步长 3 月，**14 窗口**，数据 5 年
  - 配置：`config/optimizer_constraints.yaml` → `walk_forward`
- **验证窗口留出**（v1.18, 2026-07-07）：`validation_windows: 1`
  - **最后 1 个窗口（最近 9 月）不参与 `wf_score` 排序**，作纯样本外验证
  - 代码：`genetic_searcher._compute_wf_score` + 模块级 `compute_wf_score` 均排除 `all_stats[-N:]`
  - 理由：最近数据若参与排序，选出的策略已隐含偷看验证期 → 胜率不置信
- **排名依据**：`avg(排序窗口测试期超额收益)`（排除验证窗口后的均值）
  - 代码：`strategy_optimizer_v2.py` → `rank_ws = all_ws[:-v_win]`；`avg_test_ret = mean([ws.test_excess_return for ws in rank_ws])`
  - 稳定性惩罚：`stability_penalty × std(窗口收益)`，防单窗口运气
- **禁止**：用训练期收益排名、用全期收益排名、事后挑测试期最优（回看偏差）、最近9月参与排序

#### 2. 收益评估：约束下最高收益，超额 vs 基准

- **目标函数**：在硬约束满足前提下，最大化测试期收益
- **硬约束**（`hard_constraints`）：最大回撤 ≥ -40%、平均仓位 ≥ 5%、收益标准差 ≤ 15%、月交易 1-100 次；不满足直接丢弃
- **收益基准**（超额收益 = 策略收益 − 基准收益）：
  | 分组 | 基准 |
  |------|------|
  | A股 | 无风险(2%) / 510300(沪深300) / 510880(红利ETF) |
  | 非A股 | 无风险(3.8%) / VOO(标普500) / BRK.B |
  - 配置：`config/optimizer_constraints.yaml` → `benchmarks`
  - **冲突时以此配置为准**（多处有基准实现，此处为唯一真源）
- **交易模型**：position_target（sigmoid 动态仓位），非固定分数

#### 3. 日报/简报：直接读 YAML 预估收益，不重新搜参

- **独立搜参任务（02:00 cron）继续更新 YAML** — 这是唯一的搜参入口
- **日报/简报只读取 YAML** Top1 策略的预估收益（近 9 个月测试期），**不再自己重新搜参/贪心选股**
- **废弃**：日报中 `PortfolioOptimizer.run()` 每日重新贪心搜索（导致结果天天大幅波动、"抽奖"效应）
- **展示数据**：Top1 策略的 `test_return`（测试期超额）、`test_drawdown`、`sharpe`、`_stocks`（选股）、`quarterly_holdings`（季末持仓）、平均现金仓位
- **今日信号**：`SignalScanner` 用 YAML 的 `rules` 条件字符串评估当日数据，`strategy_rank==1` 与 Top1 展示对齐
- **固定选股评估**：`PortfolioOptimizer.run_fixed(stock_selection)` — 对 YAML `_stocks` 跑一次确定性 `evaluate()`，产出 NAV 曲线 + 季末持仓，不搜索

#### 4. 三组独立搜参 + 独立资金池 + 验证期胜率 (v1.18, 2026-07-11)

- **三组独立搜参**（v1.18 2026-07-11 起）：优化器 V2 按 A股/港股/美股**分别搜参**，产出三份 YAML：
  - `*_a_share_strategies.yaml` / `*_hk_strategies.yaml` / `*_us_strategies.yaml`
  - 动机：港美股走势差异大，混搜时港股趋势性主导搜索空间，美股被迫用不适配的规则（如全是 trend_follow）
  - `run_optimization_v2` 用 `_detect_fine_group` 三分；`optimizer_constraints.yaml` benchmarks 加 hk/us（=VOO/BRK.B）
  - 回退：hk/us YAML 未生成时用 non_a_share YAML
- **三组独立资金池**（`_detect_fine_group`，日报展示层）：
  | 细分组 | 判定 | 资金池 | 主基准 | 规则来源 |
  |--------|------|--------|--------|----------|
  | a_share | 6 位纯数字 | 独立 100k | 510880 红利ETF | a_share YAML |
  | hk | 5 位纯数字 | 独立 100k | VOO | hk YAML |
  | us | 含字母 | 独立 100k | VOO | us YAML |
  - **`_detect_stock_group`（二分）保持不变** — risk_free/回测仍用 a_share/non_a_share
  - **标的池来自 config**（非 YAML `_stocks`）：`run_fixed(groups=)` 遍历 config stocks 按细分组分池，删/加标的立刻生效
  - `SignalScanner.scan(group)`：group∈a_share/hk/us，按细分组过滤标的 + 读对应 YAML
- **信号名按组翻译**：`_readable_signal(code, ..., map_a, map_hk, map_us)` 按标的细分组选对应 YAML 的信号名映射（A股 buy_1≠港股 buy_1≠美股 buy_1）
- **验证期胜率**（现场算，不信搜出来的窗口）：
  - `email_notifier._calc_validation_winrate()`：用 `run_fixed` 产出的 `nav_series` vs 主基准价格
  - 最近 9 月逐日算 forward return，"任意一天买入持有到期跑赢主基准的概率"
  - 完全离线，不依赖 YAML 搜出的数字
- **净值图**：每组一张（chart002=A股/chart003=港股/chart004=美股），Top1 绿线 + 30日布林带 + 红色基准线

#### 5. 未解禁定增展示 (v1.18, 2026-07-09)

- **数据源**：东方财富 `RPT_SEO_DETAIL` API（`web_crawler.fetch_placement_data`）
- **仅 A 股**（6 位纯数字），19:00 全量任务抓取（简报不触发）
- **只展示未解禁**：`is_locked=True`（当前 < 解禁日）；解禁日 = 上市日 + 锁定期
- **展示字段**：代码 / 名称 / 未解禁定增数额(亿股) / 占发行后总股本% / 定增价格 / 解禁时间
- **取最近一次**：多条按上市日期降序取第一条
- 代码：`_parse_placement_row`（占比+解禁日）、`_parse_lockin_years`（"3年"→3, "18个月"→1.5）、`email_notifier._build_placement_section`

---

### V2 优化器 (v1.17-beta, 2026-06-03)

**设计哲学**:
1. 模糊的正确好过精确的错误 — 不用贝叶斯精确拟合历史噪音
2. 永远正确的判断是废话 — 单维度信号（如纯量比）不区分方向，无信息增量
3. 好的机会不稍纵即逝 — 状态型信号替代穿越型，给足够判断时间

**V1 vs V2 架构对比**:

| 维度 | V1 (贝叶斯) | V2 (遗传搜索 + Walk-Forward) |
|------|------------|-------------------------------|
| 搜索方式 | 贝叶斯连续优化 | 离散网格 + 遗传算法 |
| 评估方式 | 单测试期排名 | Walk-Forward 14窗口评分 |
| 回测引擎 | PortfolioEvaluator 逐日Python | FastEvaluator numpy+numba 向量化 |
| 规则模板 | 2买3卖, 独立OR | 5买0卖, 离散选择 |
| 约束 | 仅回撤软惩罚 | 硬约束过滤：仓位/回撤/交易密度/一致性 |
| 性能 | 150次 × 2.9s = 7min | 25000次 × 10ms = 4min |
| 速度提升 | — | **约 290x** |

**V2 新增文件**:

| 文件 | 职责 |
|------|------|
| `config/optimizer_constraints.yaml` | 可配置约束：仓位/回撤/交易密度/一致性 |
| `src/analysis/optimizer_constraints.py` | 约束加载器 + WalkForward/Genetic/Discrete 配置模型 |
| `src/analysis/walk_forward.py` | 数据切片 → 统一日期轴 → 14窗口矩阵 |
| `src/analysis/fast_evaluator.py` | numba JIT 信号生成 + 组合模拟 + 指标计算 |
| `src/analysis/genetic_searcher.py` | 三阶段：随机粗筛(10000) → 遗传3代 → 精确验证 |
| `src/analysis/strategy_optimizer_v2.py` | V2 顶层入口，兼容 V1 OptimizationReport |

**V2 三阶段搜索流程**:
```
Phase 1 (粗筛): 30000 随机策略 → 向量化评估14窗口 → 过滤约束 → Top 10000
Phase 2 (遗传): 5000种 × 5代 × 交叉/变异 → 25000 新策略 → Top N
Phase 3 (验证): 精确 PortfolioEvaluator → Top 10 → YAML + HTML
```

**Walk-Forward 窗口设计** (实际值见 `config/optimizer_constraints.yaml`):
```
数据: 5年 (60个月)
训练 12月 + 测试 9月, 滑动步长 3月, 共 14 窗口
W1: [Train 0-12][Test 12-21]
W2:    [Train 3-15][Test 15-24]
... (每次滑动 3 月, 共 14 窗口)
评分 = mean(14窗口测试超额) - stability_penalty(0.5) × std(14窗口测试超额)
```

**约束系统** (`optimizer_constraints.yaml` — 唯一真源):
- **仓位下限**: 测试期平均持仓 ≥ 5%（`min_avg_position_pct`）
- **回撤上限**: 最大回撤 ≥ -40%（`max_drawdown_pct`）
- **一致性**: 收益标准差 ≤ 15%（`max_return_std_pct`，防单窗口运气）
- **交易密度**: 月交易 1-100 次（`min/max_trades_per_month`）
- 全部硬性过滤，不满足直接丢弃

### V1 优化器 (v1.14, 保留在 `--optimize`)

### 架构

```
config/optimizer.yaml → StrategyOptimizer → PortfolioEvaluator
        5条规则定义         贝叶斯优化(skopt)      24月模拟回测
        6买+5卖构建器       13+N 维参数空间        BacktestConfig 约束
              │
        ┌─────┴──────┐
    Phase A (训练)   Phase B (测试)


### Cumulative filing and FCF contract (2026-08-18)

- SEC Company Facts keeps each period's original filing and rejects later filings' comparative columns as substitute publication dates.
- SEC 10-Q and A-share interim flow values are fiscal-YTD inputs. The common normalizer derives standalone quarters before QoQ, TTM, or latest-quarter metrics are calculated.
- FCF is always OCF minus capital expenditures on the same cumulative basis, then normalized to standalone quarters. Reports expose latest-quarter FCF and TTM FCF separately; annual fallback is explicitly labeled.
- A source replacement migration removes legacy SEC rows produced by the old comparative-column mapper. A-share sources already declare cumulative periods and missing quarterly capital expenditures remain explicit missing data.
    0-12月搜索        0-24月最终评估
                     按12-24月外样本排名

### 日报邮件移动端版式重设计（2026-08-22）
- 每日 19:00 日报统一使用移动端优先的单列卡片布局；告警与无告警路径共用同一份完整 HTML 模板。
- 主机名、公网 IP 及系统信息保留在独立的“部署状态”信息条中，用于确认部署节点正确性。
- 监控标的、策略信号、A/HK/US 组合表现、图表、参考持仓、公告、标的画像和定增按纵向区块展示；深度持仓数据仍保留，PDF 继续作为附件。
- 日报数据进入模板前统一转义文本和链接，参考持仓不再追加到 `</html>` 之后；移动端归档验证覆盖 375px/430px 的响应式宽度约束。
```

### V1 回测时间线 (`BacktestConfig`)

| 阶段 | 月 | 交易 | 资金注入 | 用途 |
|------|-----|------|---------|------|
| 观察 | 0-6 | 禁止 | 无 | 指标暖机 + pre-filter |
| 部署 | 6-12 | 自由 | A/非A各+20k/月 | 训练目标 |
| 延续 | 12-18 | 自由 | 无 | 外样本延续 |
| 持仓 | 18-24 | 禁止 | 无 | 最终排名依据 |

### V1 条件构建器池

| 买入 | 卖出 | 描述 |
|------|------|------|
| deviation_cross | deviation_cross | MA60 偏离穿越 |
| rsi_signal | rsi_signal | RSI 超卖/超买 |
| bollinger_signal | bollinger_signal | 布林下轨/上轨 |
| volume_spike | deviation_absolute | 放量异动 / MA 绝对偏离 |
| deviation_absolute | trend_follow | MA 绝对偏离 / ADX 反转 |
| trend_follow / none | none | ADX 趋势 / 规则禁用 |

### V1 已知问题
- 回看偏差：150个策略事后挑测试期最好的，类"开卷考试"
- 过拟合：训练期-6.78%，测试期+18.25%，过度依赖回溯
- OR 逻辑：buy_1 和 buy_2 独立触发，不放量也能买
- 卖出无效：优化器频繁选出"只买不卖"

### V1 超额收益

真实收益 − 现金基准收益（A股 rf=2%, 非A rf=4.5%, 日复利 r_f/252）。消除注资虚胖，真实反映交易 Alpha。

### V1 全量运行结果 (2026-04-29)

- **A股 (18只, 150轮)**: Top-1 部署超额 -2.3%, 测试超额 +19.4%, 深跌抄底 + 永不卖出
- **非A股 (8只, 80轮)**: Top-1 部署超额 -3.9%, 测试超额 +49.6%, 布林信号 + 只选中海油
- 两个市场独立收敛到"买→持有→不卖"，主动禁用所有卖出规则

---

## 📡 信号扫描器 + 日报集成 (v1.15)

### 共识机制

加载最新优化结果 → 计算 Top-5 策略 → 对当日数据评估信号:
- **纳入监控**: ≥2/5 策略在 Top-5 中出现的构建器
- **纳入报警**: ≥3/5 策略在 Top-5 中出现的标的
- 每日 `--once` 运行时自动加载，产出共识信号 + 指标快照

### 回测嵌入

> ⚠️ **v1.18 变更**：日报不再每日重新贪心搜索/回测。改为直接读取最新 YAML（02:00 搜参任务产出）的 Top1 策略预估收益（近 9 个月测试期）。详见本文档「权威设计契约」章节。

### 日报邮件版式 (daily_mode, v1.18)

`_build_email_body(daily_mode=True)` 精简日报，去掉过时/冗余段：
- **删除**：MA60 偏离报警段（`alert_section`）、回测分阶段表（`backtest_section`）、旧组合分析段（`portfolio_section`）、价格表的 MA60/偏离/状态列
- **保留 + 重排**：走势图(收盘价) → 净值图(每组一张) → **搜参策略结果**（Top1规则+今日信号+组合卡片+季末持仓，一段讲完）→ 基本面表 → 公告 → **未解禁定增**
- **今日信号合并**：`SignalScanner.alerts`（A股+港股+美股 merge）并入搜参段，信号名 `buy_1`→`偏离穿越`（`_build_signal_label_map` 读 YAML params 翻译）
- **简报统一**：早/晚简报也走 `SignalScanner`（非 `build_strategy_suggestions` 旧逻辑），与日报信号一致
- 邮件从 ~89k 字符精简到 ~28k

### HTML 交互报告

自包含单文件 HTML (Plotly.js CDN)，暗色主题：
- 指标卡片行 / Plotly 交互收敛图 (可缩放悬停) / Top-10 策略表 (点击展开规则) / 最优策略详情
- 通过 health_server `/report/<token>` 路由提供，30 分钟时效链接
- Token 跨进程共享: `data/optimizer/.report_tokens.json` 文件

---

## 📧 xelatex PDF 日报 (v1.16)

### WeasyPrint → xelatex 替换

**原方案 (v3.6)**: WeasyPrint 将 `report_daily.html` 渲染为 PDF。
**问题**: 
- mathtext 公式是位图贴图，放大模糊
- 中文字体回退不稳定 (Noto Sans CJK SC 缺失字形)
- PDF 体积大 (644KB, 含 base64 图片)

**新方案**: Python → LaTeX 模板 → **xelatex 编译两次** → PDF 附件 (145KB)。
- 真 LaTeX 数学排版 (`amsmath`/`equation`/`split` 环境)
- 第一遍编译写入交叉引用 (.aux)，第二遍解析引用
- 附录: `appendix_methodology.md` → Python `markdown.markdown()` → `_esc()` 转义 → `$$...$$` 块直接注入 LaTeX

### LaTeX 模板: `report_daily.tex`

```
\documentclass[11pt,a4paper]{article}
\usepackage{xeCJK}
\setCJKmainfont{Noto Sans CJK SC}
```

- **ctexart → article+xeCJK**: 服务器未安装 ctex 宏包，改用 `xeCJK` + `article` 类
- **CRLF 修复**: Windows git 自动转 CRLF，xelatex 将 `\r` 视为 `^^M` 触发 Emergency stop。代码在编译前执行 `.replace("\r\n","\n")`
- **`_` 转义**: LaTeX 中 `_` 需转义为 `\_`，否则报 "Missing $ inserted"。`_esc()` 函数统一处理
- **颜色**: Navy 背景 / 白色表头 / 绿色正数 / 红色负数，港式财报风格
- **图表**: matplotlib 生成偏离度折线图 PNG (base64)，注入 LaTeX `\includegraphics`

### 编译流程

```
session → _build_tex_variables() → VAR 占位符替换
     → _esc() 转义 _ % & $ #
     → xelatex ×2 (-interaction=nonstopmode)
     → /tmp/report.pdf → read_bytes()
     → yagmail.send(attachments=[pdf])
```

编译耗时 ~2s，依赖: `texlive-xetex` + `texlive-lang-chinese` (约 500MB)

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/templates/report_daily.tex` | LaTeX 日报模板 (101行) |
| `src/templates/appendix_methodology.md` | 13节公式附录 (193行) |
| `src/templates/email_template.html` | 邮件正文 (已大幅缩减) |
| `src/notification/email_notifier.py` | `_generate_daily_pdf()` (250行) |

---

## 🔒 安全加固 (v1.16)

### OTP 安全

- **随机源**: `random.randint(0, 9999)` → `secrets.randbelow(10000)` (密码学安全)
- **审计日志**: 不再记录明文 OTP，仅记录 `"ID:****"`
- **文件**: `src/health_server/core/global_instances.py`

### Health Server SSL/TLS

- **自签名证书**: `openssl req -x509 -newkey rsa:2048 -nodes`, 365 天有效期
- **监听**: `ssl.wrap_socket()` 包装 TCP socket，默认端口 1933
- **IP 配置**: `config.health_server.public_ip` 优先 → `ifconfig.me` fallback → 127.0.0.1
- **路径遍历防护**: `os.path.basename()` 消毒文件名参数

### 三层防御闸门

```
pre-commit hook  →  ruff lint + import smoke + safety tests
     ↕
CI/CD pre-deploy →  ruff lint + import smoke + core tests  (pre-push check)
     ↕
import smoke test →  24 key modules import integrity  (standalone)
```

**文件**: `hosts/pre-commit` / `ci_cd_deploy.py` / `tests/test_import_smoke.py`

### 安全测试

`tests/test_security.py` — 18 个测试覆盖:
- 表达式沙箱 (禁止 `__import__`/`eval`/`exec`/文件访问)
- 路径遍历防护 (`../` `/etc/passwd` 攻击)
- Token 格式验证 (必须是 base64url)
- OTP 随机性 (卡方检验)
- 速率限制 (单 IP 60 秒内 ≤5 请求)

---

## 🚀 CI/CD 增强 (v1.16)

- **环境变量**: `DEPLOY_HOST` / `DEPLOY_SSH_REMOTE` / `DEPLOY_REMOTE_DIR` 替代硬编码
- **简报 cron**: 自动注册 `09:50 daily` crontab 条目 (此前需手动添加)
- **texlive**: 自动安装 `texlive-xetex` + `poppler-utils` 系统依赖
- **pre-deploy checks**: ruff lint + import smoke + core tests (部署前自动运行)

---

## 🧪 测试覆盖 (v1.16)

| 测试集 | 数量 | 说明 |
|--------|------|------|
| backtest_config | 15 | 时间线约束 + 资金注入验证 |
| indicator_library | 18 | RSI/MACD/ATR/布林/ADX/量比 |
| signal_scanner | 14 | 共识计算 + 警报触发 |
| strategy_optimizer | 18 | 贝叶斯优化 + 构建器 + 预筛选 |
| security | 18 | OTP/沙箱/路径遍历/速率限制 |
| import_smoke | 24 | 关键模块导入完整性 |
| **合计** | **107** | 覆盖核心新功能 |

```bash
pytest tests/ -p no:capture -q           # 全量
pytest tests/test_security.py -v          # 安全专项
pytest tests/test_import_smoke.py         # 导入完整性
```

---

## 🔧 技术债务清理 (v1.16)

### 已完成清理
- ✅ `colorlog` 死依赖删除 (零 import)
- ✅ `camelot` PDF 表格解析死代码删除 (从未安装, 永远静默失败)
- ✅ `report_daily.html` 删除 (WeasyPrint 模板, 已被 .tex 替代)
- ✅ `alert_section.html` 删除 (不再独立渲染)
- ✅ 33 个 stale test import 修复
- ✅ Health server 5 处 `Path().parent.parent` 回归修复

### 待清理
| 文件 | 操作 | 原因 |
|------|------|------|
| `email_notifier.py` 旧 report_link 生成 | 删除 (~30行) | 时效链接由 HTML 报告保留 |
| `email_notifier.py` 旧监控表构建 | 删除 (~50行) | 已替换为 `_build_daily_table()` |
| `email_template.html` | 大幅缩减 | 正文已精简为摘要卡片 |

---

## 📊 开源准备 (v1.16)

| 文件 | 说明 |
|------|------|
| `LICENSE` | BSD-3-Clause + 投资免责声明 (软件仅供研究，作者不对投资损失负责) |
| `pyproject.toml` | 项目元数据 (name/version/authors/dependencies) |
| `CONTRIBUTING.md` | 贡献指南 (代码风格/测试/PR 流程) |
| `CHANGELOG.md` | v1.12 → v1.17.1 完整变更历史 |
| `README.md` | 项目概述 + 日报预览图 + CLI 命令 |

---

## 📝 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v1.0 | 2026-03-01 | 基础功能: 数据获取 + MA60 条件 + 邮件 |
| v1.8 | 2026-03-15 | 移除 akshare, 股息 LLM 缓存架构 |
| v2.0 | 2026-03-20 | 回测框架 |
| v2.8 | 2026-04-10 | baostock 历史数据缓存 |
| v3.0 | 2026-04-15 | Session-based 数据流 (Pydantic) |
| v3.1 | 2026-04-18 | 配置驱动指标计算，模块职能隔离 |
| v3.2 | 2026-04-26 | DataSource CSV 缓存 + 复权验证 |
| v3.3 | 2026-04-26 | 投资组合策略 + 规则引擎 (YAML 沙箱) |
| v3.4 | 2026-04-27 | 早盘简报 + 锚点择优算法 |
| v1.14 | 2026-04-29 | 策略搜索优化器 (贝叶斯+构建器池+超额收益) |
| v1.15 | 2026-05-01 | 信号扫描器 + 回测嵌入日报 + HTML 报告 |
| **v1.16** | **2026-05-03** | **xelatex PDF 日报 + 安全加固 + 开源准备** |
| v1.17 | 2026-05-27 | 简报排序 + 收盘简报 14:30 + 日报 19:00 + debt_ratio 删除 + ROE PB/PE 推导 |
| v1.17.1 | 2026-06-04 | QQ 实时行情 + Eastmoney 删除 + 数据源健康探针 + 优化器 P0 修复 + 布林带列名统一 |
| **v1.18** | **2026-07-09** | **搜参多窗口验证契约 (14窗口/测试9月/验证窗口留出) + 日报直读YAML不重搜参 + 港股/美股独立资金池 + 验证期胜率 + 未解禁定增展示 + daily_mode版式精简 + 简报统一SignalScanner** |

---

## 🔗 相关文档

- [架构说明](../architecture.md) — 分层架构、数据流、模块职责
- [部署指南](../deployment.md) — 生产部署 + CI/CD
- [配置参考](../configuration.md) — config.yaml 详细说明
- [开发日志](../development/devlog.md) — 版本演进
- [贡献指南](../../CONTRIBUTING.md) — 代码风格 + 测试 + PR 流程

---

---

## 2026-05-17 邮件质量修复 + Pydantic 全量迁移

### 邮件渲染修复
1. **业绩增长列删除** — `email_notifier.py` 3 个 table section + 表头彻底移除死列
2. **告警行高亮 inline 化** — `<tr style="background:#fef9e7">` 替代 CSS class，兼容 Outlook/Gmail
3. **缺失值符号统一** — 43 处 `"-"` → `"—"` (em dash)
4. **邮件存档去嵌套** — `_save_email_copy` 不再包 `<html><body>`，仅 prepend HTML comment
5. **策略告警去重** — `signal_scanner.scan()` 按 `(code, rule_label)` dedup
6. **策略告警过滤** — 传统告警 table 中 `type=="strategy"` → `continue`
7. **港股分类修复** — 5 位代码不再误判为 A 股 (`len(code)==6`)
8. **行标区分** — 传统告警 `[MA60] 最低价 < MA60`，策略告警 `[策略]`
9. **Eastmoney 静默** — 删除假实现 WARNING 日志
10. **简报颜色 inline** — `style="color:..."` 替代 CSS class

### Pydantic 全量迁移
- `Rule` (rule_engine.py)
- `TradeRecord`, `StockMetrics`, `SubPeriodMetrics`, `PortfolioResult` (portfolio_strategy.py)
- `StrategyTrial`, `OptimizationReport` (strategy_optimizer.py)
- `SubPeriodMetrics` 去掉双重装饰器 (`@dataclass` + `BaseModel`)
- `__dataclass_fields__` → `hasattr(v, "label")` (strategy_optimizer.py:1037)
- `tests/test_rule_engine.py` 位置参数 → 关键字参数

### 数据清理
- `data_fetcher.py` 删除死字段 `earnings_growth`
- `email_notifier.py` 删除未使用 `earnings_growth` 局部变量
- `email_template.html` 删除僵尸 CSS (`.positive`/`.negative`/`.alert-row`)

**文档维护**: 本文档应在每次重大架构变更后更新  
**下次审查**: 2026-08-01

---

## 🎯 仓位目标模型 (Position Target Model) — v1.18-beta

### 动机
原有优化器使用 **Fixed-Frac（固定分数）** 交易执行：每次信号触发时，买入 `cash × buy_frac`、卖出 `position × sell_frac`。问题是 `buy_frac`/`sell_frac` 是静态的，无法根据市场信号强弱自适应调整仓位。

新模型将"买多少/卖多少"从固定分数升级为**动态仓位目标**：
1. 聚合全体股票的买卖信号 → **bullish_score** ∈ [0, 1]
2. sigmoid 映射 → **target_position**（目标仓位占 NAV 比例）
3. 每日渐进调仓（最多 ±10%/天），朝目标收敛

### 核心算法
```
每天:
  bullish = aggregate(buy_signals, sell_signals)
           = n_buy_active / (n_buy_active + n_sell_active)
  target  = sigmoid(slope × (bullish - 0.5) × 2 + bias)
  delta   = clamp(target - current_pct, -0.10, 0.10)
  
  delta > 0: 买入（候选=有买入信号的股票，等额分配，均价执行）
  delta < 0: 卖出（所有持仓按比例减持，均价执行）
```

### 参数
| 参数 | 范围 | 含义 |
|------|------|------|
| `position_slope` | 0.5 ~ 10.0 (20 档) | 仓位对信号的敏感度 |
| `position_bias` | -3.0 ~ 3.0 (20 档) | 基准仓位偏移（负数偏保守） |
| `max_daily_adjust` | 0.10 (固定) | 每日最大调仓幅度 |

### 与旧模式对比
| | Fixed-Frac (旧) | Position-Target (新) |
|---|---|---|
| 买入量 | `cash × buy_frac` 固定 | `delta × NAV` 动态 |
| 卖出量 | `position × sell_frac` 固定 | `delta × NAV` 动态 |
| 仓位控制 | 无全局概念 | 有全局目标仓位 |
| 月度上限 | 有 (15000) | **无**（删除了） |
| 每标每日操盘 | 不限制 | **最多 1 次** |
| 执行价格 | 3日6价均值 (buy) / 当日均价 (sell) | 同左 |

### 新增文件/改动
- `src/analysis/fast_evaluator.py`: 
  - `_aggregate_bullish()` — 信号聚合
  - `_sigmoid()` / `_compute_position_target()` — sigmoid 映射
  - `_simulate_position_target_python()` — 每日渐进调仓模拟
  - `FastEvaluator.evaluate_position_target()` — 新评估入口
- `src/analysis/genetic_searcher.py`: `StrategyEncoding` 新增 `position_slope`/`position_bias`（向后兼容）
- `config/optimizer_constraints.yaml`: 新增 `position_model` 段
- `tests/test_fast_evaluator.py`: 10 个新测试（信号聚合、sigmoid 映射、仿真场景）
- `scripts/preview_position_target.py`: 对比预览脚本

### 当前状态
- ✅ 核心算法实现 + 22 个测试全绿
- ✅ 旧 evaluate() 路径不受影响（增量添加，未替换）
- 🔄 预览脚本已部署到服务器（需 `python main.py --once` 先跑一遍数据）
- 📋 TODO: 集成到优化器 V2 搜索流程（`evaluate_position_target` 替代 `evaluate`）

---

## 📋 TODO / Roadmap (v1.18-beta)

| 项目 | 状态 | 说明 |
|------|------|------|
| 多渠道通知统一配置 | 🔄 开发中 | Telegram + 飞书群机器人 Webhook + 邮件，YAML 驱动，NotifierManager 统一入口 |
| 策略优化器 V2 | ✅ 已上线 | Walk-Forward 14窗口 + 遗传搜索 + numba 向量化；02:00 cron 更新 YAML，日报/简报直读 |
| **给定时间段回测工具** | 📋 TODO | 支持自定义起止日期 + 基准对比（如 510300）+ 训练/测试严格分离。可用于审计历史表现、验证策略在特定市场环境下的有效性 |
| 飞书日报原生卡片 | ✅ 已实现 | 飞书日报/告警不再复用邮件 HTML 转 Markdown，改为从 Session/DataFrame 直接生成多张飞书卡片（价格/基本面/技术指标）；飞书不渲染管道表格或 fenced code block，因此表格统一用纯文本等宽显示，避免单卡截断 |
| 飞书核心链路测试 | ✅ 已实现 | `tests/test_feishu_notifier.py` 扩展到 29 个测试，覆盖初始化、传输、日报/告警/简报、多卡分段、DataFrame 采集、告警码、技术字段、纯文本表格、中文宽度、格式化 helper；测试允许 Mock Session 接口但不 Mock 数据对象 |
| 飞书真实数据链路测试 | ✅ 已实现 | `tests/integration/test_feishu_real_data.py` 用最小 config 只放 `601728`，通过 `StockDataFetcher` 真实取数写入 Session，只启 Feishu；默认 patch `_send` 只验证一张价格卡片，设置 `FEISHU_E2E_SEND=1` 时才真实发送 |
| Telegram 交互 Bot | ✅ 已实现 | `main.py --interactive` 启动 Telegram 轮询 Bot，支持 `/help` `/list` `/add` `/remove` `/backtest`；白名单 + 限流安全层；纯 requests 轮询，不添加第三方依赖 |
| **参考持仓跟踪** | ✅ 已实现 | 持久化参考持仓（RefPortfolio），简报/日报展示系统持续运行的仓位状态（标的/持仓/现价/现金/参考 Nav/期初日期/交易天数）。只在简报时间（09:50/14:30）按量化信号调仓，禁止盘后交易，周末休市不交易。手动 `/ref_date` 重置时清空仓位并恢复初始现金，有防呆确认。详细设计见下方。 |

### 日报完整性与三基线回归修复（2026-07-26）

- 日报不再因 `daily_mode` 删除锚点、锚值、偏离和技术指标；邮件正文固定展示价格/锚点、基本面、技术面三张表。
- 统一回测入口重新消费 `benchmark_data`：A 股比较 `510880 / 510300 / 无风险`，港股和美股比较 `VOO / BRK.B / 无风险`。
- 基准累计收益、策略相对每个基准的超额收益、以及最近九个月“任意日买入持有到期跑赢”的胜率分别计算、分别渲染，禁止再将基准收益错误标为胜率。

### 优化简报完整性修复（2026-07-26）

- 三市场优化简报保留每个市场的验证期收益、三基线/胜率、验证期末持仓和最近八周 NAV 变化；不再在统一摘要路径中丢弃旧版回测信息。
- 搜参简报区分“按配置实际评估次数”和“遗传算法最终入围数”：默认配置为每市场 `30,000 + 5 × 25,000 = 155,000` 次评估，`5,000` 仅为最终保留池上限。
- 搜参元数据与验证期快照写入版本化参数产物；补发通知不需要猜测历史搜索规模。
- Telegram 在发送共享 HTML 摘要前将 `<br>` 规范化为换行，避免 Bot API 拒绝不支持的标签。

### Walk-Forward 验证集隔离（2026-07-26）

- `walk_forward.num_windows` 表示总窗口数，`validation_windows` 表示末尾严格留出的日报验证窗口；两者均在 `config/optimizer_constraints.yaml` 配置。当前为 14 总窗口 = 13 个历史排序窗口 + 1 个 9 个月验证窗口。
- 只有历史排序窗口可以参与：WF 加权得分、稳定性惩罚、硬约束筛选、GA 父代/子代保留和参数敏感性。验证窗口只生成日报/简报的样本外回测，不得用于候选排序或从备选策略中挑选“表现更好”的版本。
- WF 评分恢复为原始公式：`weighted_excess_return - stability_penalty * std(window_returns)`。遗留配置的权重少于排序窗口时，系统延用最后一个权重，确保不会因 `zip` 截断而丢掉任何历史窗口。
- 参数敏感性恢复为策略接口的通用能力：对全部离散参数生成 10 版、每维最多 ±3 档的扰动；仅在历史排序窗口上计算，作为报告诊断，不改变候选排名。新增策略无需改优化器即可继承该行为，也可按自身参数耦合规则覆写。
- 版本化产物和三渠道优化简报记录排序/验证窗口数量与敏感性摘要，明确标注验证集未参与排序。
- 搜参数据预检现在要求每个入选标的覆盖完整配置的 Walk-Forward 时间轴（训练期 + 最末窗口完整测试期）；历史不足的标的继续参与日报/简报，但不再截断市场公共时间轴、把 13 个排序窗口悄然降为 12 个。
- Walk-Forward 窗口从最新可用交易日向前排布：最末隔离窗口的结束日与日报九个月验证期的最新数据日对齐。指标计算所需的额外历史缓冲不会再把隔离窗口留在旧区间。
- 优化器日志输出完整窗口数量及首尾测试区间索引；若数据不足以构成完整配置窗口，会明确记录行数与配置需求，避免无候选结果难以审计。
- 优化数据回看期在配置窗口总长度之外保留 180 个日历日（而非 90 日），覆盖交易所节假日与多标的日期交集损耗；避免 A 股实际共同交易日比完整末窗只少数日而无候选。

### 统一现金档位执行与动态自选（2026-07-27）

- 所有注册策略的参数空间由 `TradingStrategy` 自动追加 `buy_cash_tier` 与 `sell_cash_tier`；`position_frac`、每规则比例和月度买入金额闸门不再参与当前策略执行。
- 策略输出统一为 `TradePlan`（买卖信号、强度、单笔买卖现金上限和预热期）。优化器、日报回测、简报及实时告警均消费同一份计划，避免扫描器另行解码阈值。
- 同日候选按策略强度成交，代码仅作稳定平手裁决；先卖后买，严格遵守现金上限、手续费、交易单位与最短持有期。
- 日报矩阵使用自选标的的日期并集与逐标的可交易掩码。新加入标的不会缩短既有标的历史，满足策略预热后自动加入；未满足者在报告中标为“预热中”。
- 版本化产物保存实际现金金额快照和严格 WF 验证起止日期。旧比例产物在读取时一次性映射到最近现金档位并标记迁移，下一次搜参写入原生 v2 产物。
- 每月最小/最大成交次数硬约束及交互配置入口已移除；成交笔数保留为优化简报与日报诊断指标。

### 信号契约与例行搜参恢复（2026-08-07）

- 修复日报/简报/PDF/飞书信号表格显示 `?`：`main.py:_scan_group()` 产出的 alert 字典补齐通知契约字段 `rule_label`（策略名+方向，如 "分位评分 BUY"）与 `current_value`（"评分 x.xx" 信号强度）；`label`/`detail`/`rule_id` 保留给 ref_portfolio 与交互端。消费方（email_notifier 日报表格/文本、简报表格、PDF LaTeX、feishu markdown）统一读取这两个字段，一处修复全部生效；新增 `_alert_value()` 辅助函数统一兼容 dict/对象两种 alert 形态，裸 `getattr` 在 dict 上恒返回默认值的问题一并消除。
- 修复例行搜参中断：`ci_cd_deploy.py` 部署时不再删除 `main.py --optimize` cron，改为幂等确保 `0 2 * * *` 例行任务存在（systemd scheduler 只覆盖每日 19:00 与简报，优化器因内存隔离需独立进程 + optimizer_guard，不能由 scheduler 线程承载）。遗留 `--once`/`--brief` cron 仍会清理（已由 systemd 覆盖）。

### 港股搜参数据源与 PDF 附录修复（2026-08-08）

- 港股例行搜参恢复：腾讯 fqkline 接口单次最多返回约 600-1000 根 K 线，超出后返回
  `{"code":0,"msg":"param error","data":[]}`（data 为 list），旧代码对 list 调 `.get()`
  崩溃，导致优化器 2006 天 lookback 下港股全被跳过（"港股: 无可用数据"）。`_fetch_historical_from_qq`
  改为按 2 个日历年的窗口分页拉取后合并去重（qfq 复权锚定最新数据，实测跨窗口价格一致，
  拼接无断层），并对 param error 结构做容错；A 股冷启动同样受益。
- 日报 PDF 附录修复：MD→LaTeX 转换器此前只支持单行 `$$...$$`，多行公式块被拆成空
  `\[\]` + 普通段落，导致 xelatex 报 `\mathrm allowed only in math mode`、附录公式乱码；
  转换器改为块状态机（`$$` 开关显示数学环境，块内行原样保留）。

### 逐时点基本面、原始行情与除权数据（2026-08-17）

- `python main.py --backfill-point-in-time-data` 与
  `python scripts/backfill_point_in_time.py` 回填配置内全部标的，产物写入
  `data/point_in_time/`；唯一详细合同见 `docs/llm/point_in_time_data.md`。
- 市场数据同时保存不复权 `raw_*`、前复权 `qfq_*` 与 `qfq_factor`，强制满足
  `qfq_price = raw_price × qfq_factor`。基本面历史估值只使用当日 raw 价格，
  禁止用今天锚定的前复权价格回推历史 PE/PB。
- 公司行动保存真实除权日、已知公告日和来源；不能可靠拆分现金、送转、配股时只记
  `adjustment_factor_change`，不得猜测股数或净资产。
- A 股季度基础历史来自 Baostock `pubDate/statDate`；沪市由免费上交所 XBRL
  补充并纠正归母/扣非利润、经营现金流和归母权益，深市从巨潮免费完整年报 PDF
  严格解析同类字段。官方值在真实披露日形成新修订，禁止提前覆盖。
- A 股与美股自由现金流统一为真实经营现金流减真实资本开支；任一字段或单位缺失就
  保持缺失。美股来源为 SEC Company Facts `filed/end`；港股适配器未完成前明确
  缺失。生产链路禁止付费源、模拟值、默认值和 mock 数据。
- `FundamentalFeaturePanelBuilder` 只消费上述逐时点存储与 availability mask；
  当前画像仍不进入技术策略或历史搜参，直至覆盖率和防前视验收通过。


### Point-in-time source coverage routing (2026-08-17)

- Market-history success and requested-window completeness are separate contracts. Reports expose requested/actual boundaries, calendar coverage ratio, and full/partial counts.
- A-share market history compares Yahoo only when Baostock starts later than the configured tolerance, then deterministically selects the earlier and denser real-data bundle.
- SSE fundamentals retain the official SSE performance feed and supplement the most recent three annual periods from official CNINFO-indexed PDFs because the legacy SSE full-statement endpoint is stale.
- Parsed annual-report values are cached by source URL with content and parser-contract hashes; the cache changes network cost only and never fabricates missing fields.
- No paid or simulated source is introduced. Partial coverage and parser failures remain visible diagnostics.

### 逐时点资本成本、个人必要回报率与 WACC（2026-08-21）

- 废弃内在价值实验中的9%/11%统一贴现率，也禁止将低Beta CAPM直接当作个人
  必要回报率。市场CAPM与投资者要求必须分别披露；模型不确定性和主观风险不得
  叠加进 WACC 或股权成本，主观风险只保留为买入价折价。
- `CapitalMarketAssumptionStore` 按日期保存中债十年国债收益率和沪深300前瞻隐含
  ERP；估计必须使用官方指数快照的PE、PB和股息率，从 `PB/PE` 得到指数ROE
  代理、从股息率/盈利收益率得到派息率，在五年内将增长和派息率淡化到2%/3%/4%
  终值增长并反解要求回报。3%为基准，2%–4%全部报告为敏感区间。
  估值只能读取 `as_of <= evaluation_date` 的输入。Beta 必须同时估计2/3/5年日频
  和周频OLS，保存样本数、标准误、95%置信区间和R²，以有效估计的中位数为中心、
  四分位区间为敏感性。禁止固定Blume向1收缩。历史已实现ERP必须使用含分红的
  全收益指数，价格指数不得冒充；未取得该数据前保持明确缺失，不得补默认值。
- 有完整市场输入时，市场股权成本与WACC的低/高边界必须由Beta四分位和ERP情景
  的组合直接推导；缺失逐时点市场输入时必须明确标记配置回退。股权现金流估值
  默认使用8%/7.5%/7%个人必要回报率的下行/基准/上行情景，逐场景实际贴现率为
  市场CAPM与个人要求的较高者，永续增长默认2%。
- 反向DCF只求解当前价格隐含的五年显式增长，并与基本面增长并排展示；其结果
  不得反馈到公允价值。改变现价必须只改变隐含增长、安全边际和价格区间，不得
  改变低/中/高内在价值或保守买入价。
- FCFF 代理使用 WACC；本版 OCF-CAPEX 是未取得净借款时的 FCFE 代理，DDM、
  盈利能力和残余收益模型同样使用个人必要股权回报率。WACC 使用
  官方年报现金及现金等价物、带息债务、利息费用、利润总额和所得税费用，并显示
  资本权重、税盾与净债务桥接。缺字段时明确回退，禁止伪造 WACC。
- CNINFO 主报表解析合同为 `cninfo-pdf-parse-5`：附注编号不得作为金额；现金必须
  取“年末现金及现金等价物余额”，不得把货币资金或受限现金混作可用现金。新版
  官方解析行替换同来源旧解析版本，不能由“错误字段更多”阻止修订。

### 生产调度可靠性（2026-08-20）

- `trade-eyes-health.service` 是生产环境唯一的日报/简报调度宿主；部署必须清理旧的
  无参数 `main.py` 独立调度进程，禁止两个 APScheduler 同时生成重复通知。
- 日报默认允许整点后3600秒内补跑，简报允许900秒，优化任务允许7200秒；均可在
  `scheduler.*_misfire_grace_seconds` 或单份简报的 `misfire_grace_seconds` 配置。
  所有任务必须 `coalesce=true`、`max_instances=1`，服务器短时高负载不得因
  APScheduler 默认约1秒的misfire窗口而直接丢弃整次推送。

### 日报移动端与发送频次（2026-09-02）

- 日报正文继续保留部署节点和公网 IP，用于确认线上版本；示例账户只展示
  NAV/回报/现金等摘要，不在日报展开持仓明细。
- 周 NAV 改为无脚本的移动端箱线图：优先使用日 NAV 序列计算周内四分位数，
  老缓存只有 OHLC 时使用可审计的 OHLC 区间回退，避免把长数值串贴进邮件。
- Bot 新增 `/report_frequency daily|weekly|off`（别名 `/daily_frequency`）。
  该开关只影响无告警的普通日报；价格/策略告警仍即时发送，手动 `/daily` 始终有效。

### CAPM 股权 DCF 买入价历史校准（2026-08-24）

- 新增独立的 `scripts/calibrate_capm_dcf_entry.py` 与
  `src/fundamental_embedding/dcf_entry_calibration.py`。该工具只生成点时审计报告，
  不得变更活动策略、参考持仓或线上参数；详细唯一合同见
  `docs/llm/capm_dcf_entry_calibration.md`。
- 贴现率严格为 `risk_free + beta × ERP` 的股权成本，忽略债权成本；终值增长固定
  2%。五年增长只由估值日已发布的营收、利润、FCF、CAPEX/OCF 与 ROE 数据构成。
  ERP 分段主观风险以 `g / 1.25`（ERP>5%）或 `g / 1.50`（ERP<=5%）保守折减，
  绝不可将风险乘数直接抬高公允价值。
- 年报 FCF 进入估值前必须通过收入、利润和现金转换率交叉核对；PDF 解析异常须被
  拒绝而不是截断。历史拆股/送配也必须按估值日已披露行动调整股本，确保每股 FCF
  与原始成交价格可比。
- 校准接受条件为：限价在未来252个有效交易日可触发，随后252日收盘至少半数高于
  成本，且每个ERP情景的触发覆盖、样本量和成功率均通过。完整网格、时间留出和
  失败原因必须写入报告；没有通过者不可用“最不差”参数替代。
- 交易日标签必须累计252个真实可交易日，不能从固定252条原始行情行中扣除停牌行；
  金融残余收益链的财务账龄只取账面、ROE与派息输入，禁止由无关的FCFE缺失将其
  判为陈旧。每个ERP压力情景无成交或无后验观察时，排序分数必须明确失败，不能
  因Wilson下限列表忽略空情景而选出极低、几乎不成交的限价。
- 默认的4%/12笔/50% Wilson证据门槛属于“估值链是否有足够研究样本”的安全合同，
  不是对广义股票池宣称20%买入频率。三条估值链各自必须有至少100个训练期事件；
  样本不足的链显式标记为不支持并只产生空信号，绝不借用其他链参数。2026-08-24
  的广义研究池留出中普通FCFE与金融残余收益链通过，周期FCFE因样本不足停用；
  当前配置的A股标的池本身没有足够逐时点历史证据，因而仍不得接入活动策略或执行
  全量搜参。以后只能在该池获得足够因果样本并通过同一统一回测Gate后，以手动候选
  方式接入。
- 大盘训练事件数只用于**选择与认证**政策，不能阻止小配置池对已冻结、已通过留出的
  政策做时间外应用。`--frozen-policy-report` 必须校验源经济合同，且仅从源报告的
  `validation_start` 起评价接收池；它不会按接收池标签重训或更改参数。接收池应用的
  小样本只提供迁移证据，仍须通过统一策略收益/回撤/三基准 Gate 才能成为候选。

### 搜参验证链路：raw/qfq 与 84 个月窗口（2026-08-26）

- 技术指标和信号固定使用 qfq；成交、持仓估值和 NAV 固定使用 raw 不复权价格。
  `PointInTimeMarketStore` 的 bundle 同时携带 raw/qfq 行情及公司行为，缺失、覆盖不足
  或 raw/qfq 覆盖不一致时验证 fail closed，不回退到 adjusted-only legacy cache。
- 除权除息日在统一执行 schedule 中处理：现金分红增加现金，送转股/拆股调整持仓数量
  和每股成本；现金 cap、target-weight、scalar、batch、Numba 与基准 buy-and-hold
  共用同一合同。
- walk-forward 固定为 `12/9/3/22`，其中 16 个 ranking、2 个 overlap purge、4 个
  9 个月 holdout 窗口；holdout 总窗口月数为 36，不进入 Solver。Solver ID、参数 schema、
  参数空间、预算和评分公式不变。
- 日粒度仍是默认。服务器负载过高时的 weekly 降载模式只登记为 TODO，启用前必须完成
  周/日一致性、负载和运行时对比验收；周粒度买入取周最高、卖出取周最低。
- Holdout 报告新增整体汇总：由于4个9个月窗口相互重叠，收益、最强基准超额和 Sharpe
  采用窗口等权均值，最大回撤取窗口最差值；不将重叠窗口错误串联复利。多数基准超额
  同时保留并作为 Holdout Gate 的相对比较口径。
