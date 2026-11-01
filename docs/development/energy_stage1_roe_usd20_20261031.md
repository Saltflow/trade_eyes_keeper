# 能源年报第一阶段：ROE排序、20美元总预算

后续状态：本批已正常结束。用户随后授权普通失败不重发并继续后续任务，改用[三家公司轮转续跑](energy_stage1_rolling20_20261031.md)，仍承接本批原20美元总预算；以下保留首次执行合同及其历史门槛。

用户2026-10-31明确授权：“充了40，弄了20美元 按照ROE排序看看能跑多少跑多少”。新批次为`data/fundamental_data/energy_roe_stage1_usd20_20261031`。这20美元只属于本批，不并入四大行旧预算、历史费用或历史最大预留。

## 范围与排序

沿用2026-09-24报价快照筛选通过的能源池30家公司，`screening_resolved`的passed与原plan交集决定范围，不是实时重筛。按原筛选字段`normalized_adjusted_roe`降序，即FY2021—FY2025扣非加权ROE中位数。前五家：山煤国际29.36%、陕西煤业20.75%、晋控煤业19.97%、广汇能源19.32%、华阳股份19.12%。

逐家公司完成所有可用年度的本次尝试，再开始下一家公司；公司内最多20个块请求在途。FY2019—FY2025共有193份现存原年报、每份8个通用块加1个登记主行业块，共1737个逻辑任务。6份未找到及11个按旧采集记录上市前不适用的年度分别保留；陕西煤业2023缺件也不会阻断队列。原文不足不补造，字段/JSON/传输问题保留。

只执行第一阶段并离线生成其原有JSON/阅读页，不运行第二阶段、摘要、横评、研究或其他公告结构化。未更改原提取合同、schema、prompt、131072输出上限和任何旧结果。仍使用现有PDF准备、payload、流式传输、严格JSON解析和第一阶段机械组装器；行业profile为extractives/utilities。完整9块归属合同保留在财报前，具体执行块只在尾部；原财报公共输入只保存一次，动态组合的请求全文在每次发送前单独冻结存档。

## 预算和调用边界

调用前使用官方`GET /user/balance`查询，初始只读快照为CNY389.75；真正执行时重新查询并从该时刻开始累计。本批余额扣减可能包含同账户其他调用，只作账户范围的保守预算对照，不能称精确批次账单。Flash公开USD/CNY单价比例0.15用于预算等价，不当作汇率。费率核对：[美元](https://api-docs.deepseek.com/quick_start/pricing/)、[人民币](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；2026-10-31及11-01周末按0.003/0.15/0.60美元/百万token计价，离开冻结日期不再派发。

单一主线程维护总账，合计上限20美元；known usage计价与账户实际扣减分列，按较保守值守预算。每个在途任务保留“模型完整输入上限＋max输出＋高峰价”的临时额度，仅作并发预算约束；回执返回后释放，绝不计为消费。使用最大上下文预留会使预算尾部留有不足一个任务额度的余量，不自动降低输出上限凑预算。

每块最多一次发送、无自动付费重试/续写；成功块不重跑。连接失败且缺usage、缓存费用无法确定或余额核对失败时，停止派发并收齐在途回执，缺usage继续标未知；不把它按零或最大消费入账。401/402/403/429停止新派发。STOP同样只停止后续派发并保留在途回执。已启动标志及进程锁禁止自动重启付费批次。

后台由低权限Windows按需任务`TradeEyesEnergyStage1Roe20261031`承接，零定时触发器、无自动重启，不复活旧周期监控。调用、流式原文、reasoning、usage、费用、格式问题及请求哈希保存在本批execution下；任何预算停止或局部失败均保留第一阶段已有正文。`processed`不等于字段完备或财务复核完成。

## 验证与证据

启动前15项离线专项验证覆盖真实调度函数的20峰值、ROE公司顺序、预算停止、未知usage停止、用户STOP、单次执行、防重复及归属合同请求一致性；相关仓库回归218项通过。仓库pytest默认coverage参数因当前环境缺插件未能启动，实际定向回归显式清空addopts后完成，不声称全仓覆盖率验收。新脚本Ruff与格式检查通过，预检不调用模型。

本批[计划](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/plan.json)、[进度](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/index.html)、[执行启动](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/execution_started.json)、[费用账本](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/accounting.json)、[公司结果](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/company_results.json)、[第一阶段阅读页导航](../../data/fundamental_data/energy_roe_stage1_usd20_20261031/delivery/index.html)。动态文件只在对应步骤实际运行后出现，不能凭路径存在推断已完成。
