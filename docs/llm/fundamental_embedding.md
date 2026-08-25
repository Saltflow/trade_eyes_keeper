# 基本面定价 MOE 与季度 embedding

本模块用于研究市场在价值、现金回报、质量和成长之间如何定价，并生成季度稳定的
公司画像。它目前是独立研究链路，不进入技术面策略搜参、日报持仓或生产激活。

## 数据合同

- 输入必须来自逐时点财务快照；每条财务记录必须保存报告期和真实披露日。
- 特征日只能读取 `published_at <= feature_date` 的财务值。
- 价格标签为未来 63 个交易日的前复权收益，减去同季度可用公司池等权收益。
- 训练行必须满足 `label_end_date < test_feature_date`。当前无标签画像使用独立
  `FundamentalPricingSnapshot`，不会混入 walk-forward 指标。
- 510300、510500 和 510880 的当前成员身份只用于分层诊断，绝不作为历史特征。
  当前成分会产生幸存者偏差，因此本研究不能直接当作可交易回测。

当前 19 个输入字段分为四组：

- value：盈利收益率、账面收益率、FCF 收益率、股息率；
- cash_return：FCF 收益率、股息率、FCF 利润率、现金转化率、CAPEX 强度；
- quality：ROE、净利率、扣非净利率、FCF 利润率、营收与利润增长稳定性；
- growth：营收/利润同比与环比、三年营收/利润 CAGR。

`financial_age_days` 也进入统一特征矩阵，用来表示财务数据账龄。缺失值只用训练集
中位数填充；availability mask 用于覆盖审计，不作为可预测输入。不使用 mock 或默认
财务数值提高覆盖率。

## 模型与 embedding

`CausalPricingMoE` 包含四个 ridge 专家。Gate 只根据预测季度之前已经实现的最近
专家误差分配权重，并设置 5% 最低权重防止专家静默消失。最终 12 维 embedding 为：

1. 四个标准化专家信号；
2. 四个当期市场定价 Gate；
3. 四个“专家信号 × Gate”的定价后信号。

公司画像使用因果 EWMA 平滑，默认新季度权重 35%。稳定性验收单独检查前四个公司
专家信号，不能让所有公司共享的 Gate 维度虚高余弦相似度。

## 验收标准

“框架可用”与“有可用 alpha”严格分开：

- 框架可用：零前视违规、至少 6 个 OOS 季度、至少 5 家测试公司、平滑后公司专家
  信号的季度中位余弦不低于 0.80。
- 生产候选：至少 100 家公司、至少 12 个 OOS 季度、季度横截面中位不少于
  100 家、每个专家至少 60% 行满足半数以上字段可用。
- OOS 有效性：平均季度 Rank IC 不低于 0.03、正 IC 季度比例不低于 55%、
  top-bottom 季度超额 spread 为正，且不显著落后单一全特征 ridge。
- Gate 必须相对 uniform 专家混合产生增量：Rank IC 至少高 0.01，或 MSE 至少
  低 2%。否则数据支持的是等权专家，不支持 MOE Gate。
- 跨风格诊断：三个当前成员池均需至少 30 行/季度和 12 个季度，最差池 IC
  不低于 -0.03，且至少两个池 IC 为正。

Rank IC、方向正确率和 top-bottom spread 衡量横截面排序；MSE/MAE 衡量校准；
季度余弦与 L2 turnover 衡量画像稳定性；Gate 有效专家数、最大权重和季度 JS
散度用于识别专家坍缩。小样本未通过预测门槛时不得靠调整门槛激活。

## 运行

本地现有 A 股样本：

```powershell
python scripts/train_fundamental_embedding.py `
  --data-root data/point_in_time `
  --output-dir data/analysis/fundamental_embedding_local `
  --market a_share `
  --universe-manifest data/reference_universe/index_constituents_20260818.json
```

807 家数据拉回后只替换数据根目录：

```powershell
python scripts/train_fundamental_embedding.py `
  --data-root data/reference_universe/point_in_time_20260818 `
  --output-dir data/analysis/fundamental_embedding_807 `
  --market a_share `
  --universe-manifest data/reference_universe/index_constituents_20260818.json
```

输出包含 `report.json`、全部 OOS 预测、当前时点 embedding CSV、Gate 权重图和
HTML 报告。当前 embedding 的 `source` 必须是
`current_unlabelled_snapshot`；评价预测则标记为 `walk_forward_evaluation`。

## 当前本地基线

8 家本地 A 股形成 184 个公司季度、15 个 OOS 季度。零前视违规，当前画像日期为
2026-08-17，稳定公司信号中位余弦约 0.969，因此框架路径通过。平均 Rank IC 约
0.013、正 IC 季度约 46.7%、top-bottom spread 为负，且 Gate 不优于 uniform
专家混合，因此明确不具备生产资格。该结果只用于冻结 807 家数据到达前的代码和
评价合同。

## 拆分画像与市场定价的后继实验

旧 CausalPricingMoE 冻结为 legacy_recent_mse_gate 强制基线。新的研究链路
使用 split-fundamental-pricing-1 合同，并且仍不进入生产策略：

1. CompanyExposureEncoder 只按固定经济方向生成 value、cash_return、quality
   和 growth 四维公司画像，不读取任何收益标签。financial_age_days 只降低
   数据可信度，不改变公司的经济含义。
2. 公司画像按季度 EWMA 平滑后再做当期横截面排名。原始画像、平滑画像和排名
   画像均独立保存。
3. MarketPricingModel 单独预测四个画像因子的有符号市场价格。因子价格可以为
   正或负，不受 simplex、非负权重或总和为一约束。
4. Walk-forward 训练只允许 label_end_date 小于 test_feature_date 的季度进入；
   目标是每个季度内的横截面收益排名，各季度等权。
5. 候选必须在完全相同的 OOS 行上对比零分、等权因子、静态因子价、EWMA
   因子价、横截面 Ridge、收益 Ridge、质量成长静态分和旧 MoE。缺少任何强制
   基线时框架验收失败。
6. 选择分为平均季度 Rank IC 减 IC 波动惩罚和排名换手惩罚。生产候选还必须
   成对战胜最强基线，满足平均 IC 增量、胜出季度比例和 bootstrap 下界要求；
   只获得正 IC 不足以通过。

运行命令为 scripts/train_split_fundamental_pricing.py。输出将公司画像保存到
company_exposures.csv，将市场状态保存到 market_pricing_states.csv；两者不得
合并成含义随市场漂移的 embedding。

### 86 家阶段结果（2026-08-19）

86 家偏深圳代码的非随机局部样本形成 1,862 个公司季度、15 个 OOS 季度和
1,233 条测试行，零前视违规。Kalman 候选平均季度 Rank IC 为 0.0522，正 IC
季度率 73.3%，top-bottom 季度超额 spread 为 2.00%；画像相邻季度中位余弦
为 0.987，平均变化从原始 0.586 降到平滑后 0.261。

平均 IC 最高的强制基线是 EWMA 因子价，为 0.0525；按同一稳定性和换手惩罚
后的选择分，最强基线则是收益 Ridge（0.0183），高于 Kalman 的 0.0041。
Kalman 相对收益 Ridge 的成对 IC 增量为 0.0008，胜出季度率 53.3%，95%
bootstrap 区间为 [-0.0870, 0.0840]，没有可靠增量。旧 MoE 仅为 0.0188 IC。
结果证明“固定公司画像 + 独立有符号市场定价”的方向明显优于旧 Gate，但当前
最复杂候选并未战胜简单基线，生产门槛必须保持失败。

最新 2026-08-18 Kalman 状态按 value、cash_return、quality、growth 顺序约为
[-0.0705, 0.0896, -0.0767, 0.0957]。这只是该偏置样本下的市场定价诊断，
不能外推为全 A 股结论；完整 807 家截面仍需沿用同一冻结合同复验。
