# 抗硬编码测试验证框架

## 设计目标
防止AI通过读取测试代码硬编码解决方案，验证系统逻辑正确性而非具体数值。

## 核心原则
1. **数学恒等式验证**：测试数学真理，任何有效数据都必须满足
2. **随机化参数**：使用随机股票、时间、价格等参数，无法预测测试场景
3. **增量质量控制**：每步开发≤150行增量，确保代码质量可控
4. **抗硬编码验收**：强制使用随机参数，禁止固定值依赖

## 目录结构
```
tests/validation/
├── __init__.py              # 包初始化
├── conftest.py             # pytest fixtures (随机化工具)
├── test_utils.py           # 随机化参数生成和验证工具
├── test_base.py            # 抗硬编码测试基类
├── README.md              # 本文档
└── (后续添加测试文件)
```

## 关键组件

### 1. RandomTestParameterGenerator
```python
# 随机化参数生成器
gen = RandomTestParameterGenerator(seed=42)
stock = gen.random_stock_code()          # 随机股票代码
time_point = gen.random_time_point()     # 随机时间点  
price_data = gen.random_price_data()     # 随机价格数据
```

### 2. BaseAntiHardcodeTest
所有抗硬编码测试应继承此基类：
```python
class TestMyFeature(BaseAntiHardcodeTest):
    def setup_method(self, method):
        super().setup_method(method)
    
    def test_something(self):
        # 使用基类的断言方法
        self.assert_price_relationships(price_data)
        self.assert_dividend_yield_formula(dividend, price, yield_value)
        self.assert_roe_consistency(pe, pb, roe)
```

### 3. 数学恒等式验证
- **价格关系**：`low ≤ close ≤ high` (允许相等处理涨停/跌停)
- **股息率公式**：`dividend_yield = (dividend_per_share / close_price) * 100`
- **ROE一致性**：`|roe - (pb/pe)*100| ≤ 5.0%` (5%容差)
- **MA60计算**：验证60日移动平均计算逻辑
- **振幅公式**：`amplitude = (high - low) / open * 100`
- **日变化**：`change = close - open`
- **涨跌幅**：`change_pct = (close - prev_close) / prev_close * 100`

## 测试编写规范

### ✅ 正确的抗硬编码测试
```python
def test_price_relationships_random(self, anti_hardcode_base):
    """使用随机数据测试价格关系"""
    for _ in range(10):  # 多次随机测试
        price_data = anti_hardcode_base.random_generator.random_price_data()
        anti_hardcode_base.assert_price_relationships(price_data)
```

### ❌ 错误的硬编码测试
```python
def test_price_relationships_fixed(self):
    """使用固定数据测试价格关系 - 易被AI破解"""
    price_data = {'low': 10.0, 'close': 10.5, 'high': 11.0}  # 固定值
    assert price_data['low'] <= price_data['close'] <= price_data['high']
```

## 验收标准
每步开发必须通过`@checkpoint-acceptor`验收：
1. **行数限制**：`git diff HEAD~1 --stat` ≤150行
2. **测试通过**：`pytest tests/validation/test_*.py` 全部通过
3. **随机化验证**：代码使用随机参数而非固定值
4. **功能正确**：最小示例运行正常

## 执行流程
```
[主代理] 设计实现 → git commit → 
         @cycle_guard验证循环风险 → 
         @checkpoint-acceptor验收增量
```

## 与cycle_guard协作
- **cycle_guard**：检测重复错误模式，防止循环编码
- **checkpoint-acceptor**：验收增量质量，确保抗硬编码特性
- **互补关系**：cycle_guard关注"为什么错"，checkpoint-acceptor关注"是否正确"

## 未来扩展
1. **跨数据源一致性测试**：Sina vs QQ vs Eastmoney
2. **配置驱动变异测试**：不同时区、缓存设置
3. **边界条件测试**：ETF特殊处理、缺失数据
4. **数据新鲜度测试**：过时数据检测和警告