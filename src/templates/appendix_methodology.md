# 指标方法论附录

本文档说明 Stock Quant Report 中各指标的计算方式与信号来源。文末附回测约束说明。

---


## 1. 偏离度 (Deviation)

每股标的选定一条长期移动均线作为"锚点"，计算当前价格与锚点的百分比偏离。

$$
D = \frac{P - \mathrm{MA}_{60}}{\mathrm{MA}_{60}} \times 100\%
$$

其中 $P$ 为当日收盘价，$\mathrm{MA}_{60}$ 为 $P$ 的 60 日简单移动平均。$D > 0$ 表示价格高于均线（可能超买），$D < 0$ 表示价格低于均线（可能超卖）。


## 2. RSI (Relative Strength Index)

RSI 是一种动量振荡指标，衡量价格变动的速度和幅度，取值范围 $[0, 100]$。

$$
\mathrm{RSI} = 100 - \frac{100}{1 + \frac{\bar{G}_{14}}{\bar{L}_{14}}}
$$

其中:
- $G_t = \max(P_t - P_{t-1}, 0)$ — 当日涨幅
- $L_t = \max(P_{t-1} - P_t, 0)$ — 当日跌幅
- $\bar{G}_{14}$ — $G_t$ 的 Wilder 指数平滑 ($\alpha = 1/14$)
- $\bar{L}_{14}$ — $L_t$ 的 Wilder 指数平滑 ($\alpha = 1/14$)

**解读**: RSI < 30 视为超卖，可能反弹；RSI > 70 视为超买，可能回调。实际阈值由策略优化器动态搜索确定。


## 3. 量比 (Volume Ratio)

当前成交量与近期成交量均值的比值，衡量交易活跃程度的相对变化。

$$
V_r = \frac{V_t}{\mathrm{SMA}(V, 20)}
$$

其中 $V_t$ 为当日成交量，$\mathrm{SMA}(V, 20)$ 为过去 20 个交易日的成交量简单移动平均。$V_r > 1$ 表示放量，$V_r < 1$ 表示缩量。


## 4. 布林带 %B (Bollinger %B)

布林带是围绕价格移动均线的波动率包络线。

$$
\mathrm{Mid} = \mathrm{SMA}(P, 20) \qquad
\mathrm{Upper} = \mathrm{Mid} + 2\sigma_{20} \qquad
\mathrm{Lower} = \mathrm{Mid} - 2\sigma_{20}
$$

$$
\%B = \frac{P - \mathrm{Lower}}{\mathrm{Upper} - \mathrm{Lower}}
$$

其中 $\sigma_{20}$ 为 20 日收盘价标准差。$\%B = 0$ 表示价格触及下轨，$\%B = 1$ 表示价格触及上轨。


## 5. MACD (Moving Average Convergence Divergence)

MACD 是一种趋势跟踪动量指标，由三条线组成：

$$
\mathrm{MACD} = \mathrm{EMA}(P, 12) - \mathrm{EMA}(P, 26)
$$

$$
\mathrm{Signal} = \mathrm{EMA}(\mathrm{MACD}, 9)
$$

$$
\mathrm{Histogram} = \mathrm{MACD} - \mathrm{Signal}
$$

其中 $\mathrm{EMA}(P,n)$ 为 $P$ 的 $n$ 日指数移动平均。$\mathrm{Histogram} > 0$ 为多头趋势，$\mathrm{Histogram} < 0$ 为空头趋势。柱状图穿越零线常被视为趋势反转信号。


## 6. ADX (Average Directional Index)

ADX 衡量趋势强度，与趋势方向无关，取值范围 $[0, 100]$。

$$
\mathrm{TR}_t = \max(H_t-L_t,\ |H_t - C_{t-1}|,\ |L_t - C_{t-1}|)
$$

$$
+DM_t = \max(H_t - H_{t-1}, 0),\quad -DM_t = \max(L_{t-1} - L_t, 0)
$$

$$
+DI_{14} = 100 \times \frac{\overline{+DM}_{14}}{\overline{\mathrm{TR}}_{14}},\quad
-DI_{14} = 100 \times \frac{\overline{-DM}_{14}}{\overline{\mathrm{TR}}_{14}}
$$

$$
\mathrm{DX} = 100 \times \frac{|+DI_{14} - -DI_{14}|}{+DI_{14} + -DI_{14}}
$$

$$
\mathrm{ADX} = \overline{\mathrm{DX}}_{14}
$$

ADX > 25 表示强趋势，ADX < 20 表示弱趋势或横盘。


## 7. 股息率 (Dividend Yield)

股息率衡量标的的现金分红回报率。

$$
\mathrm{DY} = \frac{D_{12m}}{P} \times 100\%
$$

其中 $D_{12m}$ 为最近 12 个月内每股累计分红总额（含年报+半年报+季度），$P$ 为当日收盘价。分红数据从巨潮资讯网公告经由 LLM 结构化提取。


## 8. PE (Price-to-Earnings, TTM)

滚动市盈率，衡量市场对每单位盈利的定价倍数。

$$
\mathrm{PE_{TTM}} = \frac{P}{\mathrm{EPS}_{4Q}}
$$

其中 $\mathrm{EPS}_{4Q}$ 为最近四个季度的每股收益之和。优先使用 TTM 口径，东方财富 API 未提供 TTM 时 fallback 到静态 PE。


## 9. PB (Price-to-Book)

市净率，衡量市场对每单位净资产的定价倍数。

$$
\mathrm{PB} = \frac{P}{\mathrm{BPS}}
$$

其中 $\mathrm{BPS}$ 为每股净资产（Book Value Per Share），取最近一期财报数据。


## 10. 超额收益 (Excess Return)

超额收益衡量策略相对于"持有现金（含无风险利息）"的真实择时贡献。

$$
R_{\mathrm{excess}} = \frac{\mathrm{NAV}_{\mathrm{end}}}{\mathrm{NAV}_{\mathrm{start}}} - \frac{\mathrm{CB}_{\mathrm{end}}}{\mathrm{CB}_{\mathrm{start}}}
$$

其中 $\mathrm{NAV}$ 为策略组合净值，$\mathrm{CB}$ 为现金基准（每天复利 $r_f / 252$，A 股 $r_f = 2\%$、非 A 股 $r_f = 4.5\%$）。观察期和部署期的现金注入均与基准同步。


## 11. Sharpe 比率

风险调整后收益，衡量单位风险的超额回报。

$$
\mathrm{Sharpe} = \sqrt{252} \times \frac{\overline{R_d - r_f/252}}{\sigma(R_d)}
$$

其中 $R_d$ 为每日收益率序列，$\overline{R_d - r_f/252}$ 为每日超额收益率均值，$\sigma(R_d)$ 为日收益率标准差。年化因子 $\sqrt{252}$ 将日波动率转化为年化口径。


## 12. 策略信号来源

策略信号并非人工设定，而是由 **策略搜索优化器 (Strategy Optimizer)** 从 2 年历史数据中自动搜索得到。

- **搜索空间**: 6 个买入构建器 × 5 个卖出构建器，每条规则搜索 3 维（构建器选择 + 归一化阈值 + 仓位比例），外加 N 只股票的纳入/排除开关。
- **搜索算法**: 贝叶斯优化 (Gaussian Process)，150 轮迭代。
- **两阶段验证**: 训练期 0-12 月（部署期超额最大化），测试期 12-24 月（外样本排名）。优化器从未见过 12-24 月数据，防止过拟合。
- **共识机制**: Top-5 策略中 ≥ 2/5 使用的构建器信号纳入监控列，≥ 3/5 选择的标的纳入报警池。

每日日报中的"触发信号"是上述共识信号在当日数据上的实际命中情况。回测结果（超额收益、Sharpe）基于最新优化策略在完整 24 个月上的模拟。


## 13. 回测约束

| 阶段 | 月份 | 交易 | 资金注入 |
|------|------|------|----------|
| 观察期 | 0–6m | 禁止 | 无 |
| 部署期 | 6–12m | 自由 | A 股/非 A 股各 +20,000 CNY/月 |
| 延续期 | 12–18m | 自由 | 无 |
| 持仓期 | 18–24m | 禁止 | 无 |

**训练/测试分割**: 优化器仅见 0-12 月数据用于参数搜索。最终评估时所有候选策略在完整 0-24 月重跑，但仅按外样本（12-24 月）表现排名。

**资金池**: A 股/非 A 股各独立 100,000 CNY 初始资金，月注入不设单笔交易限额。买入/卖出费率 0.2%，A 股整手 100 股。

---

*Stock Quant System · 指标参考: Wilder (1978), Bollinger (2001), Appel (2005)*
