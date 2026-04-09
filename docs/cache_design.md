# 回测历史数据缓存设计文档

## 1. 设计目标
- 建立baostock历史数据缓存，减少重复API调用
- 智能更新：除权时全量更新，否则增量更新
- 集成到日报流程：16:00先更新数据再回测
- 保持与现有系统兼容

## 2. 新增缓存结构
```
cache/historical/
├── metadata/          # 元数据：{stock_code}_metadata.json
└── data/              # 历史数据：{stock_code}_{start}_{end}.jsonl
```

## 3. 历史数据格式（JSON Lines）
**文件命名**：`510880_20240101_20250409.jsonl`
**每行格式**：
```json
{"date":"2024-01-02","open":10.5,"high":11.2,"low":10.3,"close":10.8,"volume":1000000,"amount":10800000,"adjust_factor":1.0}
```
**字段**：date, open, high, low, close, volume, amount, adjust_factor

## 4. 元数据格式
```json
{
  "stock_code": "510880",
  "last_updated": "2025-04-09T16:30:00",
  "data_start_date": "2024-01-01",
  "data_end_date": "2025-04-09",
  "last_dividend_date": "2024-06-10",
  "data_hash": "a1b2c3d4",
  "total_records": 730
}
```

## 5. 更新策略决策树
1. 缓存不存在？ → 全量更新
2. 期间有除权？ → 全量更新  
3. 缺失最新日期？ → 增量更新
4. 否则 → 使用缓存

## 6. 除权检测
- 数据源：baostock `query_dividend_data()`
- 类型：现金分红、送股、转增股本
- 阈值：默认0.01元/股

## 7. 数据验证点
1. 日期连续性验证（无缺失交易日）
2. 价格合理性（close在low-high之间）
3. 数据哈希验证（完整性检查）

## 8. 随机化参数
- 随机重试延迟：1-3秒
- 随机缓存检查：5%概率跳过缓存
- 随机数据验证：10%概率完整验证

## 9. 集成方案
```python
# main.py run_daily_task修改
def run_daily_task():
    if config['backtest']['enable']:
        historical_manager.update_all_stocks()  # 先更新数据
    backtest_results = backtest_framework.get_backtest_results()  # 再回测
    email_notifier.send_daily_report(..., backtest_results)
```

## 10. 配置新增
```yaml
baostock:
  enable: true
  adjustflag: "2"  # 前复权
  
historical_cache:
  enable: true
  cache_days: 30
  
dividend_detection:
  enable: true
  min_amount_per_share: 0.01
  
backtest:
  use_cache: true
  data_source: "baostock"
```

**设计验证总结**：
1. ✅ 目录结构：historical/metadata/和historical/data/
2. ✅ 文件格式：JSON Lines，支持增量追加
3. ✅ 元数据：含除权记录和数据哈希
4. ✅ 更新策略：智能决策树
5. ✅ 集成方案：保持API兼容
6. ✅ 验证机制：三重数据验证
7. ✅ 随机化参数：3处随机化设计

**下一步**：实现baostock数据源和缓存管理扩展。