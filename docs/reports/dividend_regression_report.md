# 股息数据回归问题调查报告

**调查日期：** 2026年3月18日  
**调查人：** AI助手  
**项目：** 股票量化监控系统

## 执行摘要

股息计算系统从正确显示**12个月每股股息总和**退化为仅显示**最新单次股息**，导致股息率计算不准确。此问题经历了三个阶段：

1. **最初正确阶段**（提交 `9e60b10`，3月9日）：使用`akshare` API获取股息历史并汇总过去365天的股息
2. **回归第一阶段**（提交 `9246880`，3月15日）：移除`akshare`依赖但未替换12个月汇总逻辑，导致系统返回`None`或错误数据
3. **回归第二阶段**（当前未暂存更改）：修复了网页爬虫解析但仍只返回最新股息，而非年度总和

**根本原因：** 当移除`akshare`时，12个月股息汇总逻辑被完全删除，且未使用网页爬虫的股息历史数据实现等效的汇总功能。

## 问题演变时间线

### 第一阶段：原始正确系统（提交 `9e60b10`）

**提交：** `9e60b10e5038a45c214ad82c7ae02eb512b7c50d`  
**提交信息：** "feat: Implement 12-month dividend summation system, fix dividend data accuracy"  
**日期：** 2026年3月9日 20:12:18

**关键特性：**
- 使用`akshare.stock_dividend_cninfo()` API获取详细的股息记录
- 过滤过去365天的记录
- 汇总所有现金股息（包括"年度分红"和"中期分红"）
- 验证合理范围（0.001–5.0元/股）
- 为股息率计算提供准确的年度总和

**示例输出（来自proj4llm.md）：**
- 601088 中国神华：3.240元/股（0.980中期 + 2.260年度）
- 601398 工商银行：0.306元/股（0.141中期 + 0.165年度）
- 601728 中国电信：0.274元/股（0.181中期 + 0.093年度）

### 第二阶段：Akshare移除与架构变更（提交 `9246880`）

**提交：** `9246880c9f03312bbc001f7f7d11f5171734e8be`  
**提交信息：** "refactor: Remove akshare dependency and consolidate dividend fetching architecture"  
**日期：** 2026年3月15日 22:16:05

**关键变更：**
- 删除所有`akshare`导入和API调用（从`data_fetcher.py`中删除了约240行代码）
- 移除了12个月股息汇总逻辑
- 引入新架构：
  1. **主要来源：** LLM提取缓存（来自公告解析）
  2. **备用来源：** 网页爬虫股息数据（新浪财经）

**修改的文件：**
- `src/data_fetcher.py`：移除akshare股息获取，添加`_fetch_dividend_from_web_crawler()`
- `src/announcement_fetcher.py`：移除587行基于akshare的公告获取代码
- `src/cache_manager.py`：添加`get_latest_llm_extraction_for_stock()`方法
- `config/config.yaml`：更新数据源配置

### 第三阶段：网页爬虫修复（当前未暂存更改）

**当前状态：** `src/web_crawler.py`有未暂存的股息解析改进：

**已做的改进：**
- 更好的表格检测（先通过ID `sharebonus_1`搜索）
- 健壮的表头行识别
- 改进的股息列解析
- 但仍只返回历史中的**最新单次股息**

**当前行为示例：**
- 股票601728（中国电信）：
  - 年度总和（过去365天）：**0.274元**（0.181 + 0.093）
  - 当前系统返回：**0.181元**（仅最新股息）
  - **结果：** 少报了34%

## 技术分析

### 当前股息架构

```
数据流：
1. data_fetcher._fetch_dividend_from_web_crawler(股票代码)
   ├── 1. 尝试LLM提取缓存（cache_manager.get_latest_llm_extraction_for_stock()）
   │    └── 返回None（配置中公告功能已禁用）
   └── 2. 回退到web_crawler.fetch_dividend_data()
        ├── 解析新浪财经股息页面
        ├── 构建dividend_history列表（所有股息）
        └── 返回字典包含：
            - dividend_per_share: 最新单次股息
            - last_dividend_date: 最新股息日期
            - dividend_history: 解析出的完整股息列表
```

### 为什么移除Akshare后出现"无数据"

1. **LLM提取缓存为空**：公告获取功能已禁用（配置中`announcements.enable: false`），因此缓存中没有LLM提取的股息数据。

2. **网页爬虫解析最初损坏**：在未暂存改进之前，新浪股息表格解析器经常失败，原因包括：
   - 依赖`class='datatbl'`，但新浪网站已更改
   - 表头检测不准确
   - 导致返回`None`值

3. **组合效应**：主要来源（LLM缓存）和备用来源（网页爬虫）都返回`None`，导致邮件报告中`dividend_per_share = None`。

### 为什么现在是"部分正确"

对`web_crawler.py`的未暂存改进修复了表格解析问题：

- **成功**：现在正确解析新浪股息表格并返回股息数据
- **限制**：仍只返回**最新股息**，而不是汇总12个月的股息
- **结果**：显示股息数据，但对于每年有多次股息的股票，显示值约为正确值的50-66%

## 代码对比

### 原始12个月汇总逻辑（已移除）

```python
# 来自提交9e60b10（简化版）
def get_total_dividends_last_12months(stock_code):
    dividend_df = ak.stock_dividend_cninfo(symbol=stock_code)
    twelve_months_ago = datetime.now() - timedelta(days=365)
    recent_dividends = dividend_df[dividend_df['announcement_date'] >= twelve_months_ago]
    
    total_per_share = 0.0
    for _, row in recent_dividends.iterrows():
        cash_ratio = row.iloc[4]  # 现金股息比例列
        if cash_ratio > 0:
            per_share = cash_ratio / 10.0  # 从每10股转换为每股
            total_per_share += per_share
    
    return round(total_per_share, 3)
```

### 当前股息获取（不完整）

```python
# 来自当前data_fetcher.py（第360-388行）
def _fetch_dividend_from_web_crawler(self, stock_code):
    # 1. 尝试LLM缓存（通常为空）
    llm_extraction = self.cache_manager.get_latest_llm_extraction_for_stock(stock_code, days=365)
    if llm_extraction:
        return llm_extraction.get('dividend_per_share')
    
    # 2. 回退到网页爬虫（仅返回最新单次股息）
    dividend_data = self.web_crawler.fetch_dividend_data(stock_code)
    if dividend_data and dividend_data.get('dividend_per_share'):
        return dividend_data['dividend_per_share']  # ← 仅最新单次，非汇总
    
    return None
```

## 根本原因识别

**主要根本原因**：在提交`9246880`中的架构变更移除了12个月股息汇总逻辑，但未使用网页爬虫的股息历史数据实现等效功能。

**次要因素**：
1. **过度依赖LLM提取**：设计为主要来源但在生产环境中禁用
2. **网页爬虫实现不完整**：解析完整历史但只返回最新条目
3. **缺乏验证**：没有测试比较年度总和与最新股息

## 影响分析

### 财务影响
- **股息率少报**：每年有多次股息的股票显示人为偏低的股息率
- **投资决策**：用户基于不准确的股息数据做决策
- **错误幅度示例**：
  - 601728：少报34%（0.181元 vs 0.274元）
  - 601398：少报54%（0.141元 vs 0.306元）
  - 601088：少报70%（0.980元 vs 3.240元）

### 系统影响
- **邮件报告**：显示错误的股息值
- **缓存完整性**：缓存的股息数据不完整
- **用户信任**：削弱对系统准确性的信心

## 建议解决方案

### 方案A：修改网页爬虫（推荐）
更新`web_crawler.fetch_dividend_data()`以从`dividend_history`计算12个月总和：

```python
def fetch_dividend_data(self, stock_code):
    # ...现有解析逻辑...
    
    if dividend_history:
        # 过滤过去365天的股息
        one_year_ago = datetime.now() - timedelta(days=365)
        annual_dividends = [
            d for d in dividend_history 
            if datetime.strptime(d['date'], '%Y-%m-%d') >= one_year_ago
        ]
        
        # 汇总股息
        annual_sum = sum(d['dividend_per_share'] for d in annual_dividends)
        
        return {
            'dividend_per_share': annual_sum if annual_sum > 0 else None,
            'last_dividend_date': latest_date,
            'dividend_history': dividend_history,
            'annual_dividend_sum': annual_sum
        }
```

### 方案B：修改数据获取器
更新`data_fetcher._fetch_dividend_from_web_crawler()`以从返回的历史数据计算总和。

### 实施步骤
1. **添加日期过滤逻辑**：选择过去365天内的股息
2. **汇总过滤后股息的`dividend_per_share`值**
3. **返回年度总和**作为`dividend_per_share`（如有需要，将`latest_dividend`作为单独字段保留）
4. **添加验证**：确保总和合理（0.5-20%的股息率范围）
5. **更新测试**：验证12个月汇总功能正确工作

## 测试策略

### 需要的验证测试
1. **单元测试**：验证网页爬虫为测试股票返回正确的年度总和
2. **集成测试**：使用已知股息历史的完整流水线测试
3. **回归测试**：与基于akshare的历史结果比较
4. **边界情况**：ETF（应返回None）、无近期股息的股票

### 验证用的测试股票
- 601728：应返回约0.274元（0.181 + 0.093）
- 601398：应返回约0.306元（0.141 + 0.165）
- 601088：应返回约3.240元（0.980 + 2.260）
- 512810/510880：应返回None（ETF）

## 预防措施

### 代码审查清单
- [ ] 架构变更时保留股息汇总逻辑
- [ ] 网页爬虫返回年度总和，而非仅最新股息
- [ ] 测试验证12个月总和与已知值
- [ ] 备用机制保持数据准确性

### 监控
- **数据验证**：比较股息值与历史范围
- **告警**：股息率超出预期范围时通知
- **定期审计**：定期审查股息计算准确性

## 结论

股息回归问题源于一个架构变更，该变更移除了必要的12个月汇总逻辑，但未实现等效功能。虽然最近的修复恢复了基本的股息解析，但对于每年有多次股息的股票，系统仍少报股息值34-70%。

**建议行动**：实施方案A（修改网页爬虫），使用已从新浪财经解析的现有股息历史数据，恢复准确的12个月股息汇总。

## 参考资料

1. **提交9e60b10**：原始12个月股息汇总实现
2. **提交9246880**：Akshare移除和架构变更
3. **proj4llm.md（第901-956行）**：原始12个月系统文档
4. **test_annual_dividend.py**：展示年度总和与最新股息的测试
5. **当前未暂存更改**：网页爬虫解析改进

## 附录

### 附录A：Git命令输出

```bash
# 显示提交历史
git log --oneline -20

# 显示提交9246880的更改
git show 9246880 --stat

# 显示当前未暂存更改
git diff src/web_crawler.py
```

### 附录B：配置状态

```yaml
# config/config.yaml（相关部分）
announcements:
  enable: false  # LLM提取功能已禁用
```

### 附录C：测试结果

运行`test_annual_dividend.py`显示：
- 股票601728在历史中有多次股息
- 年度总和（过去365天）：0.274元
- 最新单次股息：0.181元
- 当前系统返回：0.181元（正确值的66%）

---
**报告生成：** 2026年3月18日  
**下一步：** 实施12个月股息汇总修复