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

其中 $\mathrm{EPS}_{4Q}$ 为最近四个季度的每股收益之和。优先使用 TTM 口径，腾讯财经 API 未提供 TTM 时 fallback 到静态 PE。


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

日报和简报的策略信号使用各市场已激活的策略及其保存参数。候选参数的搜索、
排序、稳健性验证和留出期验证分开进行；研究候选不会仅因排名靠前而成为已激活策略。

具体策略、搜索方法、窗口和验收结果以对应市场的运行记录为准。


## 13. 回测约束

当前生产 Walk-Forward 使用 9 个月测试窗、每 3 个月滚动一次，共 22 窗口：
16 个排序窗口、2 个重叠隔离窗口、4 个留出窗口。留出期不参与候选排名或参数选择。
4 个留出窗口彼此重叠，收益和 Sharpe 按窗口等权汇总，回撤取最差，不能连乘为连续收益。

各市场分别使用其执行档案中的初始资金、佣金、交易单位、最短持有期和股息预扣参数。
正式评估使用未复权价格处理成交，并显式核算公司行动与现金分红；指标价格与执行价格
分别记录。具体金额、费率及评估日期以该次报告的执行合同为准。

---

*Stock Quant System · 指标参考: Wilder (1978), Bollinger (2001), Appel (2005)*
