# 配置、死代码与主链路审查（2026-09-20）

审查对象是当前本地工作区，包括开始审查前已有的未提交修改。不把这些问题归因于
本次清理，也不代表服务器部署状态。以下先记录修复状态；后续编号章节保留修复前的
复现证据和原行号，其中已删除路径仅用于解释历史问题。

## 修复状态（2026-09-20）

| ID | 当前处理 |
| --- | --- |
| A01 | 按用户选择统一完整执行合同；逆序/留一保留 raw OHLC、税前/税后分红、税额、拆股与基准，重新构建子集缓存和基本面上下文，缺数据拒绝执行 |
| A02 | HTTP 回调整体删除，飞书使用官方 SDK 出站长连接；启用开关与凭证先校验，持续限流/去重。按用户决定保留飞书显式 `["*"]`，空名单拒绝 |
| A03 | 飞书和 Telegram 共用命令分发，增删完整股票代码列表，不再逐字符写入 |
| A04 | 删除 Web 写入口；Bot/调度修改统一通过 `ConfigStore` 锁内重读、字段修改和原子替换，环境密钥不落盘 |
| A05 | 两份依赖声明加入 Numba，所有执行入口缺依赖时明确失败，移除零资产伪结果 |
| A06 | HTTP/HTTPS 服务、管理模板、OTP、报告 token 和协议 watchdog 整体删除，改为独立进程及本地心跳 |
| A07–A08 | 默认 CLI / `--service` 共用唯一 `ScheduleManager`，创建和在线重排均使用配置时区；部署迁移项目旧 cron，保留明确开关 |
| A09 | 指标与告警检查器共用指定路径；取消跨路径单例，显式错误路径/内容会报错 |
| A10 | 所有通知底层使用相同开关解析，启动通知通过统一管理器，在服务启动之后发送 |
| A11 | 删除 LLM 模块对标准库 JSON 的全局补丁 |
| A12 | 定义模块 logger，配置错误原样记录而不被 `NameError` 遮蔽 |

同时删除旧 Builder/Bayesian 配置及使用说明、无消费者运行配置、三个旧搜索字段、
旧参考组合 `rebalance()`、死常量、死导入和死局部变量。保留
`genetic_search.evaluation_workers` 的研究用途，未将合法算法常量泛化为配置缺陷。
配置写回并发测试额外修复了 Windows 文件锁初始化竞争。

合并回归：**536 passed，2 skipped**，包括 `tests/validation/`、交互链路、配置并发、
服务生命周期、部署迁移、通知开关、完整执行合同和搜索恢复。两项 skip 是现有 v3
manifest 用例，不作为通过验收。独立 Python 进程导入也通过，原市场/搜索循环已切断。
追加清理后，配置/告警/validation 复测 53 项、服务/部署复测 55 项、SDK 生命周期与
服务复测 41 项全部通过（与合并批次有重复）。实际 APScheduler + RuntimeService
启动、心跳和停止通过；在项目临时目录安装的实际 `lark-oapi 1.7.3` 也通过事件
序列化、公开 builder 和客户端构造/收尾验证，全程阻止外部连接。该检查另修复了 SDK
启动失败时缓存清理协程未回收的问题，正式环境依赖仍由 requirements 安装。

Ruff 主程序/src/scripts/config 的 `F401,F841,F821,F811` 通过，src 部署级
`F,E,S110`（排除历史长行 E501）通过；`git diff --check` 通过。

未运行全量真实搜参、真实通知或服务器部署；测试里的小矩阵仅验证合同传递，
不构成策略绩效结论。Bot 测试包含真实 Windows spawn 与本地假 SDK 的跨进程接收、
退出和回收，账号侧端到端消息验收仍需在实际环境完成。

## 1. 高优先级问题

| ID | 优先级 | 问题 | 主要位置 |
| --- | --- | --- | --- |
| A01 | P1 | 稳健性验证丢失原始执行价格与公司行动 | `src/search/validation.py:202` |
| A02 | P1 | 飞书关闭或鉴权配置缺失时，HTTP 入口仍可派发命令 | `src/health_server/handlers/health_handler.py:1024` |
| A03 | P1 | Telegram 增删标的将股票代码逐字符处理 | `src/interactive/telegram_bot.py:136` |
| A04 | P1 | Web 管理整份回写启动快照，覆盖新配置并落盘运行态密钥 | `src/health_server/handlers/health_handler.py:311` |
| A05 | P1 | 缺少 Numba 时返回零资产结果而不报错 | `src/backtest/engine.py:1611` |
| A06 | P1 | HTTPS 服务被固定 HTTP 探针判定失败并退出 | `src/health_server/core/health_server.py:141` |

### A01：标的子集验证改变执行口径

调用路径：`run_optimizer` → `ValidationController.run` → `_universe_robustness`
→ `_subset_manager` → `_prepare_wf_evaluation_contexts`。

`_subset_manager` 仅切片指标和复权 OHLC，未带入 `raw_price_matrix`、
`raw_price_high_matrix`、`raw_price_low_matrix`、`action_schedule`、
`benchmark_low_series`、`benchmark_action_schedules`。
`src/search/workflow.py:249` 随后将原始价格静默回退为复权价格，300 行的缺失行动表
变成空公司行动；基准路径 340 行也丢失公司行动。

这同时影响逆序检查与逐个剔除标的检查。PIT 数据存在 raw/qfq 差异或分红时，
候选原始排名和验证使用不同的成交、分红口径，可能误判顺序不变性，改变留一成绩、
罚分及最终候选选择。

纯内存复现：同一组 3 日 × 2 标的数据，指标价格设为 8、原始价格设为 10，
第二日现金分红分别为 1 和 2。仅将标的顺序从 `[0, 1]` 改成 `[1, 0]`：

```text
路径       execution valuation_prices[0]    cash_dividends[1]
ranking    [10.0, 10.0]                     [1.0, 2.0]
reorder    [8.0, 8.0]                       [0.0, 0.0]
```

修复方向：提供统一、类型明确的市场数据切片方法，切片原始 OHLC 和所有行动矩阵，
保留基准执行合同；缺少必需执行数据时拒绝验证。

### A02：飞书 HTTP 命令入口绕过启用开关和鉴权边界

`HealthHandler._handle_feishu_event` 无条件构造 `FeishuApp` 并派发。
`src/interactive/feishu_app.py:35` 将空白名单解释为允许全部；87 行在未配置加密密钥时
直接通过签名校验。普通消息路径未校验 `verification_token`，配置中的
`interactive.feishu.enabled` 也未成为 HTTP 派发前的条件。

实际 HTTP adapter 的 mock 复现：配置 `enabled=false`，清空飞书环境凭证，输入一个
无签名的 `/daily` 事件；结果返回 HTTP 200，`handle_daily` 被调用一次。
缺少应用凭证只阻止回复，不能撤销先前执行的命令。复现屏蔽了真实消息和任务启动。
是否能从外部到达该路由取决于实际网络部署，本次未探测服务器。

修复方向：入口先检查开关和完整鉴权条件；拒绝缺失鉴权配置；统一白名单规则。
复用有生命周期的客户端/限流器，避免每次 HTTP 请求新建实例而清空限流状态。

### A03：Telegram 参数合同与共享 handler 不一致

`src/interactive/telegram_bot.py:136`、138 传入 `cmd.stock_code` 字符串；
`src/interactive/commands/handlers.py:284`、319 的函数已接受 `list[str]`，内部直接遍历。
飞书分发使用 `cmd.codes`，所以同一条命令在两个渠道有不同结果。

mock 配置保存函数后，从 Telegram 实际分发入口复现：

```text
/add 600036       -> stocks=['6', '0', '3']
/remove 600036    -> 不保存，逐字符报告未找到
```

修复方向：传入 `cmd.codes`；在共享 handler 校验输入类型和完整股票代码，
以实际 Bot 分发至配置保存的链路做回归检查。

### A04：运行态配置快照被当成原始配置回写

`src/health_server/core/start_server.py:38` 将邮件密码、模型密钥等环境值注入运行态字典。
Web 标的管理在 `health_handler.py:578`、616 使用 `self.health_server.config` 的启动快照，
再通过 311 行的 `yaml.dump` 整份覆盖文件，没有在修改前重读原始 YAML。

mock 复现：原始文件已由另一个入口将日报改成 20:30，启动快照仍为 19:00。
Web 添加标的后，提交给写入函数的时间回到 19:00；运行态的占位邮件密码、API key
字段也被写入。复现只使用虚构占位值，未读取或写入真实密钥。

修复方向：分开原始配置、环境覆盖和运行态状态；所有配置修改通过一个受锁保护的
读取、字段更新、原子保存入口，持久化时不混入环境注入值。

### A05：执行器缺失被表示成无收益

`src/backtest/engine.py:66` 的 Numba 导入失败只设置 `HAS_NUMBA=False`。
评估器在 1062 行返回默认 `WindowStats()`，模拟器在 1611 行返回全零资产曲线、
零收益和零成交。`requirements.txt` 与 `pyproject.toml` 均未声明 `numba`。

纯内存复现：仅在复现进程设置 `HAS_NUMBA=False`，给 `Backtester.simulate`
初始资金 100000 和明确买单，返回：

```text
daily_values=[0.0, 0.0, 0.0], total_return_pct=0.0, total_trades=0
```

缺少依赖的环境会把运行失败解释为无交易或无合格候选。当前测试环境有该依赖，
不能因此把新环境的安装链路视为已验证。

修复方向：声明必要依赖，并在启动/执行前明确失败；若支持无 Numba 环境，必须提供
真正的 Python 模拟器并验证与加速路径一致。

### A06：Watchdog 的协议和地址没有跟随监听配置

`src/health_server/core/health_server.py:64` 可以启用 TLS，141 行却始终探测
`http://127.0.0.1:<port>/health`；三次失败后 160 行调用 `os._exit(1)`。
`ci_cd_deploy.py:168` 的 systemd 配置为 `Restart=always`。

因此证书加载成功后，服务可能进入反复自退和重启；仅绑定非回环地址也存在探针
无法到达的条件。mock TLS 端口拒绝 HTTP 后，确认连续三次探测都使用 HTTP，并触发退出。

修复方向：从实际绑定和 TLS 状态构造探针目标，并统一处理本地证书验证。

## 2. 配置与入口分叉

| ID | 优先级 | 证据、影响与修复方向 |
| --- | --- | --- |
| A07 | P2 | `main.py:2395` 使用旧 `SchedulerManager`，Health 在 `health_server.py:99` 使用新 `ScheduleManager`。旧入口无条件注册日报、缺省 15:30、没有优化器调度；新入口读取日报/优化器开关，缺省 19:00。同一 YAML 因启动方式不同而产生不同任务。统一任务注册入口。 |
| A08 | P2 | `schedule_manager.py:160` 和 221 构造 `CronTrigger` 未传时区。mock UTC 宿主机后，scheduler 为上海时区而 trigger 为 UTC；19:00 会变成上海次日 03:00。创建和重排触发器都应传配置时区。 |
| A09 | P2 | `src/core/data_fetcher.py:49` 调用无参 `TechnicalIndicators()`，后者固定读默认告警文件；`condition_checker.py:24` 却使用 `alerts.config_path`。自定义锚点规则可能没有对应指标值。另 `config/__init__.py:93` 的全局单例不会区分路径。应共享同一份已解析规则及其生命周期。 |
| A10 | P2 | `health_handler.py:210`、283 直调 `EmailNotifier._send_email`，绕过统一通知管理器。mock 证明同时设置 `SKIP_NOTIFICATIONS=true`、`notification.email.enabled=false` 后，OTP 路径仍调用 SMTP 发送。将服务邮件纳入统一发送策略，统一布尔开关解析。 |
| A11 | P2 | `src/data/llm/base.py:23`、34 在模块导入时全局替换标准库 `json.dumps/dump`，强制 `ensure_ascii=False`；显式传 `True` 也被覆盖。模块导入顺序影响全进程序列化。应仅在 LLM 请求本地指定编码选项。 |
| A12 | P2 | `main.py:1980` 的 `_fetch_benchmarks` 异常分支使用未定义 `logger`。mock 市场配置读取抛 `ValueError` 后变成 `NameError`，掩盖原错。正常日报此前已读一次同一配置，不能声称正常日报必然触发此分支。补模块 logger 和异常分支覆盖。 |

A09 本地复现：给 `StockDataFetcher` 指定 `config/custom-audit-alerts.yaml`，仍加载默认
`ma60/wma20/wma30/wma50`，连自定义路径不存在也不会在此处报告。

A11 本地复现：显式调用 `json.dumps({"label": "中文"}, ensure_ascii=True)`，
导入 LLM base 前输出 Unicode 转义，导入后输出未转义中文。

还存在一条独立的重启通知路径：`main.py:2240` 直接 `requests.post`，不经过通知开关；
2399 行的调用位于阻塞式 `scheduler.start()` 之后，可能在服务停止返回后才发“已上线”。
它的 Webhook 优先级是环境变量优先，与普通 notifier 的非空 YAML 优先不同。
应移到真实启动事件，并使用统一通知入口。

## 3. 死配置、死变量与分散默认值

下面区分“没有消费者”和“仅研究/测试使用”，避免直接删除仍有用途的兼容接口。

| 对象 | 当前读取情况 | 建议 |
| --- | --- | --- |
| `historical_cache`、`portfolio_strategy` | 当前主程序、src、scripts 中未找到配置键消费者 | 从可配置项中退休；历史 CSV 缓存实际由 `DataSource` 的范围/公司行动逻辑管理 |
| 顶层 `baostock` | 未找到这段配置的读取；同名字面量出现在行情数据源标签中 | 保留真正使用的 `point_in_time_data.market_history.baostock_socket_timeout_seconds`，避免把源标签误认为配置读取 |
| `data_source.primary/secondary/etf_force_web_crawler/fallback_to_web_crawler` | 未找到对应消费者；`type` 只在 `data_fetcher.py:32` 用于旧 akshare 提示，实例仍走爬虫 | 明确支持的路由合同，或删除误导的选择项 |
| `financial_reports` | 当前只发现 LLM base 读取 `max_llm_calls_per_run`，没有旧整套财报开关消费者 | 不要把整个段直接删除；先分离仍在使用的额度字段 |
| `LOG_LEVEL` | 模板声明，但代码不读取；主程序读 `logging.level` | 删除误导模板项或统一接入配置加载器 |
| `genetic_search.sensitivity_samples` | `search/config.py:372` 解析，验证器实际枚举全部确定性邻域 | 删除无效调节项；当前主链路文档已注明其无效 |
| `mutation_builder_rate`、`mutation_threshold_step` | 只有旧解析字段，没有实际消费者 | 退休旧字段，保留所选 solver 自己的参数 |
| `genetic_search.evaluation_workers` | 研究模块仍使用；生产主搜参使用 `search.workers`/`SEARCH_WORKERS` | 迁移前不要全删，文档区分用途 |
| `markets.py:10` 的 `MIN_EVAL_DAYS/MIN_TRADING_DAYS/RISK_FREE_*`、53 行 `_eval_lookback_days()` | 没有生产消费者；`_get_lot_size()` 仅测试调用 | 清理旧工具与无意义测试依赖 |
| `backtest/engine.py:870` 的 evaluator 确认天数字段 | 没有生产读取；信号确认由策略负责 | 删除重复状态，保留策略参数来源 |
| `ref_portfolio.py:26` 的 `DEFAULT_BUY_AMOUNT`、31 行 `BRIEF_WINDOWS` | 未使用 | 删除死常量 |
| `ref_portfolio.py:343` 的旧 `rebalance()` | 旧 20% 现金、25% 卖出、50000 上限合同，当前仅测试调用；生产用 `rebalance_plan()` | 作为旧入口退休，不能当成已证实的生产旁路 |
| `scheduler_manager.py:171` 的 `_start_health_server()` | 无调用 | 与旧调度入口一起清理 |

配置加载也分散在 `main.load_config`、Health 启动器、Bot 原始 YAML loader 和 HTTP
回调 loader 中。当前对环境变量覆盖、缓存、回写的语义不同，A04 是已发生行为差异的
具体证明。`src/health_server/core/global_instances.py:16` 一带的 OTP/会话/限流默认值
与 handler 内 Cookie 有效期另行维护，也是集中配置的候选。

RSI/MACD 等指标定义、252 日年化约定、数组列索引、策略离散搜索空间等常量，
不能仅因为是字面量就算缺陷。应先确认它们属于算法定义、协议常量还是用户应控制的参数。

## 4. 静态检查与验证记录

- AST 解析主入口、部署脚本、src、scripts、config 中 231 个 Python 文件：无语法错误。
- Ruff 定向检查 `F401,F841,F821,F811`：18 个未用导入、4 个未用局部变量、1 个未定义名称，
  无重复定义报告。未执行自动修复，以免混入本次清理之外的改动。
- 4 个未用局部变量：`scripts/backtest_justified_pb_value.py:67` 的 `store`；
  `scripts/justified_pb_ablation.py:141` 的 `windows`；
  `scripts/screen_csi300_500_justified_pb.py:237` 的 `prices`、422 行的 `entered`。
- `tests/validation/`：36 passed。
- `tests/test_market_optimizer_config.py` 和 `tests/test_search_runtime_config.py`：17 passed。
  首次有 2 项因默认 pytest 临时目录权限失败，改用仓库内全新临时目录后全部通过。
- 本机 Python 环境缺少 `pytest-cov`，定向测试用 `-o addopts=`，未做全量覆盖率验收。
- 删除目标不存在；仓库代码、文档和测试未再引用被删除的旧配置；修改文件的
  `git diff --check` 通过。

可复用的静态命令：

```powershell
python -m ruff check main.py src scripts config --select F841,F401,F821,F811
python -m pytest tests/validation/ -o addopts= -q
```

动态复现均使用内存数据或 mock，阻断真实配置写入、网络、邮件和子进程任务。
小矩阵只用于证明数据传递和控制流问题，不是策略绩效验证。本次未运行全量搜参、
真实通知或服务器部署；53 项定向测试通过不等于上述缺陷不存在。

## 5. 修复顺序和边界

1. 先处理外部命令入口、配置写回和标的增删，防止错误调用或破坏配置。
2. 修复执行合同切片和 Numba 依赖检查，确保搜索、验证与报告说的是同一结果。
3. 合并调度和通知入口，修复时区、TLS 探针与自定义告警路径。
4. 统一原始配置/运行态配置模型，再删除无消费者的字段、旧接口与死变量。

历史 `audit_hardcoded_defaults.md` 引用了已经不存在的旧分析模块，不能复用其行号或
问题数量。当前主链路文档也仍有旧窗口数字，应以代码与现行配置验证后更新。
参考组合的当日成交与独立分红合同、独立导入通知模块的循环依赖留待专项复核，
本轮不将尚未完成端到端验证的推测当成确定缺陷。
