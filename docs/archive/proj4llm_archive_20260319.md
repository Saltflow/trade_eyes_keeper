# LLM股票基本面分析模块 - 详细技术文档

## 概述
LLM分析模块使用DeepSeek API对A股股票进行基本面分析，包括行业地位、盈利能力、分红情况、风险评估等，为投资决策提供AI辅助分析。

## 系统架构

### 模块位置
```
src/llm_analyzer.py
└── class LLMAnalyzer
    ├── __init__()           # 初始化OpenAI客户端
    ├── analyze_stocks()     # 批量分析股票
    ├── _get_stock_info()    # 获取股票基本信息
    ├── _call_llm_analysis() # 调用LLM API
    ├── _build_analysis_prompt() # 构建分析提示
    └── _extract_summary()   # 提取文本摘要
```

### 依赖关系
```python
import logging
import json
import re
from openai import OpenAI  # 兼容DeepSeek API
```

## API集成配置

### DeepSeek API设置
```yaml
# config/config.yaml
llm:
  api_type: deepseek            # API类型
  api_key: ""  # API密钥，通过环境变量DEEPSEEK_API_KEY设置
  base_url: "https://api.deepseek.com/v1"  # API基础URL
  model: "deepseek-chat"        # 模型名称
```

### 环境变量支持
```bash
# .env 文件
DEEPSEEK_API_KEY=your_deepseek_api_key_here
```

### 客户端初始化
```python
def __init__(self, config):
    self.llm_config = config.get('llm', {})
    api_key = self.llm_config.get('api_key', '')
    
    if api_key:
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.llm_config.get('base_url', 'https://api.deepseek.com/v1')
        )
        self.model = self.llm_config.get('model', 'deepseek-chat')
    else:
        self.client = None  # 功能禁用
```

## 股票信息数据结构

### 股票基本信息（当前返回基础信息）
```python
stock_info_map = {
    '601728': {
        'name': '中国电信',
        'industry': '电信服务',
        'market_cap': '约5000亿元',
        'pe_ratio': '15.2',
        'pb_ratio': '1.2',
        'dividend_yield': '4.5%',
        'last_dividend_date': '2023-06-30',
        'last_dividend_amount': '0.2元/股'
    },
    '600938': {
        'name': '中国海油',
        'industry': '石油天然气',
        'market_cap': '约8000亿元',
        'pe_ratio': '8.5',
        'pb_ratio': '1.8',
        'dividend_yield': '6.2%',
        'last_dividend_date': '2023-06-30',
        'last_dividend_amount': '0.5元/股'
    }
}
```

### 信息获取策略
1. **当前实现**：返回基础信息（未来可从真实数据源获取）
2. **扩展方案**：集成akshare/tushare API获取实时基本面数据
3. **备用方案**：网页爬虫获取公开财务报告

## 提示工程（Prompt Engineering）

### 系统提示（System Prompt）
```python
system_prompt = "你是一个专业的股票分析师，擅长分析A股公司的基本面和投资价值。"
```

### 用户提示模板
```python
prompt = f"""
请分析以下A股股票的基本面和投资价值：

股票代码：{stock_code}
股票名称：{stock_info.get('name', '')}
行业：{stock_info.get('industry', '')}
市值：{stock_info.get('market_cap', '')}
市盈率(PE)：{stock_info.get('pe_ratio', '')}
市净率(PB)：{stock_info.get('pb_ratio', '')}
股息率：{stock_info.get('dividend_yield', '')}
最近分红日期：{stock_info.get('last_dividend_date', '')}
最近分红金额：{stock_info.get('last_dividend_amount', '')}

请从以下角度进行分析：
1. 基本面分析（行业地位、竞争优势、财务状况）
2. 盈利能力分析（毛利率、净利率、ROE等）
3. 分红情况分析（分红稳定性、股息率水平）
4. 风险评估（行业风险、公司特定风险）
5. 投资建议（适合的投资者类型、投资时点建议）

请以专业分析师的角度提供详细分析，并给出结构化的总结。
如果可以，请用JSON格式返回分析结果，包含以下字段：
- 基本面评级（1-5星）
- 盈利能力评级（1-5星）
- 分红稳定性评级（1-5星）
- 总体投资价值评级（1-5星）
- 关键风险点
- 投资建议
"""
```

### API调用参数
```python
response = self.client.chat.completions.create(
    model=self.model,          # 模型：deepseek-chat
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ],
    temperature=0.7,           # 创造性：0.7（平衡创意与确定性）
    max_tokens=2000           # 最大输出：2000 tokens
)
```

## 响应处理机制

### JSON响应解析策略
```python
def _call_llm_analysis(self, stock_code, stock_info):
    # 获取原始响应
    analysis_text = response.choices[0].message.content
    
    # 策略1：尝试提取JSON
    json_match = re.search(r'\{.*\}', analysis_text, re.DOTALL)
    if json_match:
        try:
            analysis_json = json.loads(json_match.group())
            return analysis_json
        except json.JSONDecodeError:
            pass  # 降级到策略2
    
    # 策略2：结构化文本分析
    analysis_json = {
        'stock_code': stock_code,
        'stock_name': stock_info.get('name', ''),
        'analysis_text': analysis_text,
        'summary': self._extract_summary(analysis_text)
    }
    return analysis_json
```

### 文本摘要提取
```python
def _extract_summary(self, analysis_text):
    summary = {
        'has_growth': '增长' in analysis_text or '成长' in analysis_text,
        'has_risk': '风险' in analysis_text or '谨慎' in analysis_text,
        'has_dividend': '分红' in analysis_text or '股息' in analysis_text,
        'sentiment': '中性'  # 默认
    }
    
    # 情感分析关键词
    positive_words = ['推荐', '买入', '看好', '优质', '低估', '机会']
    negative_words = ['谨慎', '回避', '高估', '风险', '卖出', '警告']
    
    positive_count = sum(1 for word in positive_words if word in analysis_text)
    negative_count = sum(1 for word in negative_words if word in analysis_text)
    
    if positive_count > negative_count:
        summary['sentiment'] = '积极'
    elif negative_count > positive_count:
        summary['sentiment'] = '谨慎'
    
    return summary
```

## 数据结构定义

### 分析结果格式
```python
# 理想JSON格式（LLM返回）
{
    "基本面评级": "4星",
    "盈利能力评级": "3星", 
    "分红稳定性评级": "5星",
    "总体投资价值评级": "4星",
    "关键风险点": "行业竞争加剧、政策变化风险",
    "投资建议": "适合价值投资者，当前估值合理"
}

# 文本格式（降级方案）
{
    "stock_code": "601728",
    "stock_name": "中国电信",
    "analysis_text": "详细的分析文本内容...",
    "summary": {
        "has_growth": true,
        "has_risk": true,
        "has_dividend": true,
        "sentiment": "积极"
    }
}
```

## 错误处理机制

### 多级错误处理
```python
try:
    # 1. API调用错误
    response = self.client.chat.completions.create(...)
    
    # 2. JSON解析错误
    analysis_json = json.loads(json_match.group())
    
    # 3. 数据格式错误
    if '基本面评级' not in analysis_json:
        raise ValueError("缺少必要字段")
        
except Exception as e:
    logger.error(f"分析股票 {stock_code} 时发生错误: {e}")
    return None  # 优雅降级
```

### 日志记录
```python
logging级别：
- INFO: 开始分析、完成分析
- WARNING: API密钥未配置、股票信息缺失
- ERROR: API调用失败、解析错误
```

## 使用示例

### 基本使用
```python
from src.llm_analyzer import LLMAnalyzer

# 初始化
analyzer = LLMAnalyzer(config)

# 分析单只或多只股票（analyze_stocks 支持单个或列表）
result = analyzer.analyze_stocks(['601728'])  # 单只股票
results = analyzer.analyze_stocks(['601728', '600938'])  # 多只股票
```

### 主程序集成
```python
# main.py 中的调用
llm_config = config.get('llm', {})
if llm_config.get('api_key'):
    logger.info("开始LLM基本面分析")
    analyzer = LLMAnalyzer(config)
    analysis_results = analyzer.analyze_stocks(config['stocks'])
    logger.info(f"LLM分析完成: {analysis_results}")
```

## 配置示例

### 完整配置
```yaml
# config/config.yaml
llm:
  api_type: deepseek
  api_key: ""  # API密钥，通过环境变量DEEPSEEK_API_KEY设置
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"
  
  # 可选：高级配置
  temperature: 0.7
  max_tokens: 2000
  timeout: 30  # API超时时间（秒）
  
  # 分析选项
  analysis_depth: "detailed"  # detailed/brief
  include_risk_analysis: true
  include_comparison: false
```

### 环境变量配置
```bash
# .env
DEEPSEEK_API_KEY=your_deepseek_api_key_here
LLM_MODEL=deepseek-chat
LLM_TIMEOUT=30
```

## 扩展性设计

### 1. 多模型支持
```python
class MultiModelLLMAnalyzer:
    def __init__(self, config):
        self.models = {
            'deepseek': DeepSeekClient(config),
            'openai': OpenAIClient(config),
            'claude': ClaudeClient(config)
        }
        self.active_model = config.get('llm', {}).get('model', 'deepseek')
```

### 2. 缓存机制
```python
class CachedLLMAnalyzer:
    def __init__(self, config):
        self.cache = {}  # stock_code -> (timestamp, result)
        self.cache_ttl = 24 * 3600  # 24小时缓存
        
    def analyze_stocks(self, stock_codes):
        results = {}
        for code in stock_codes:
            if self._is_cached_valid(code):
                results[code] = self.cache[code][1]
            else:
                result = self._call_llm_api(code)
                self._update_cache(code, result)
                results[code] = result
        return results
```

### 3. 批量处理优化
```python
def analyze_stocks_batch(self, stock_codes, batch_size=5):
    """批量处理，减少API调用次数"""
    results = {}
    for i in range(0, len(stock_codes), batch_size):
        batch = stock_codes[i:i+batch_size]
        # 构建批量提示
        batch_prompt = self._build_batch_prompt(batch)
        batch_result = self._call_batch_api(batch_prompt)
        results.update(self._parse_batch_result(batch_result))
    return results
```

## 性能考虑

### 1. API成本控制
- **Token估算**：每只股票约1500-2000 tokens
- **成本计算**：DeepSeek API定价 ≈ $0.001/1000 tokens
- **优化策略**：
  - 缓存分析结果（24小时有效期）
  - 批量处理减少调用次数
  - 选择性分析（仅满足条件的股票）

### 2. 响应时间优化
```python
# 异步处理
async def analyze_stocks_async(self, stock_codes):
    tasks = [self._analyze_single_async(code) for code in stock_codes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {code: result for code, result in zip(stock_codes, results)}
```

### 3. 限流控制
```python
class RateLimitedLLMAnalyzer:
    def __init__(self, config):
        self.rate_limit = config.get('llm', {}).get('rate_limit', 10)  # 10次/分钟
        self.last_calls = []
        
    def _check_rate_limit(self):
        now = time.time()
        # 清理过期记录
        self.last_calls = [t for t in self.last_calls if now - t < 60]
        if len(self.last_calls) >= self.rate_limit:
            time.sleep(1)  # 等待
        self.last_calls.append(now)
```

## 测试策略

### 单元测试
```python
def test_llm_analyzer():
    # 1. 测试初始化（无API密钥）
    config = {'llm': {'api_key': ''}}
    analyzer = LLMAnalyzer(config)
    assert analyzer.client is None
    
    # 2. 测试股票信息获取
    stock_info = analyzer._get_stock_info('601728')
    assert stock_info['name'] == '中国电信'
    
    # 3. 测试提示构建
    prompt = analyzer._build_analysis_prompt('601728', stock_info)
    assert '中国电信' in prompt
    assert '基本面分析' in prompt
    
    # 4. 测试摘要提取
    test_text = "推荐买入，增长潜力大，但有政策风险"
    summary = analyzer._extract_summary(test_text)
    assert summary['sentiment'] == '积极'
    assert summary['has_risk'] == True
```

### 集成测试
```python
def test_integration():
    # 需要真实的API密钥
    config = load_config()
    analyzer = LLMAnalyzer(config)
    
    # 测试单只股票分析
    result = analyzer.analyze_stocks(['601728'])
    assert result is not None
    assert '601728' in result
    
    # 测试批量分析
    results = analyzer.analyze_stocks(['601728', '600938'])
    assert len(results) == 2
```

## 部署注意事项

### 1. API密钥安全
- 使用环境变量存储API密钥
- 不在代码库中提交密钥
- 定期轮换密钥

### 2. 错误监控
```python
# 错误监控装饰器
def monitor_llm_calls(func):
    def wrapper(*args, **kwargs):
        try:
            start_time = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            logger.info(f"LLM调用成功，耗时: {elapsed:.2f}s")
            return result
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            # 发送告警通知
            send_alert(f"LLM分析失败: {e}")
            raise
    return wrapper
```

### 3. 健康检查
```python
def health_check():
    analyzer = LLMAnalyzer(config)
    
    # 测试API连通性
    try:
        test_result = analyzer.analyze_single_stock('601728')
        if test_result:
            return {"status": "healthy", "message": "LLM分析功能正常"}
    except Exception as e:
        return {"status": "unhealthy", "message": f"LLM分析失败: {e}"}
```

## 未来扩展方向

### 1. 数据源扩展
- 集成akshare获取实时财务数据
- 爬取公司年报、季报
- 集成宏观经济数据

### 2. 分析维度扩展
- 技术面分析（趋势、动量）
- 市场情绪分析
- 行业对比分析
- 估值模型集成（DCF、相对估值）

### 3. 输出格式优化
- PDF报告生成
- 可视化图表
- 多语言支持
- 个性化模板

### 4. 智能优化
- 学习用户偏好调整分析重点
- 历史分析结果对比
- 预测模型集成

## 单元测试清单

### 概述
股票量化系统包含完整的测试套件，覆盖核心功能、API集成、数据处理和邮件通知等关键模块。测试文件按功能分类组织，便于针对性测试和持续集成。

### 测试目录结构
```
tests/
├── unit/           # 单元测试
│   ├── test_basic.py              # 核心功能测试
│   ├── test_config.py             # 配置加载测试
│   ├── test_web_crawler.py        # 网页爬虫测试
│   ├── test_fixed_crawler.py      # 修复后爬虫测试
│   ├── test_crawler.py            # 原始爬虫测试
│   └── test_debug.py              # 调试测试
├── api/            # API集成测试
│   ├── test_llm_analyzer.py       # LLM分析器API测试
│   ├── test_sina_api.py           # 新浪财经API测试
│   ├── test_sina_historical_direct.py  # 新浪历史数据直接获取
│   ├── test_sina_150_days.py      # 150天历史数据测试
│   └── test_sina_120_days.py      # 120天历史数据测试
├── email/          # 邮件相关测试 (8个文件)
├── dividend/       # 股息数据测试 (3个文件)
└── integration/    # 集成测试文件
```

### 新增测试文件（针对三项改进）
- **`test_changes.py`** - **新增** - 验证三项改进（缓存、邮件表格、公告抓取）
  - 模块导入测试
  - 缓存管理器功能测试
  - 邮件表格拆分验证
  - 公告抓取器格式化测试

- **`test_integration.py`** - **新增** - 集成测试三项功能
  - 每日缓存功能测试
  - 邮件表格拆分集成测试
  - 公告抓取器集成测试

- **`test_announcement_real.py`** - **新增** - 公告抓取真实数据测试
  - SSE/SZSE公告API测试
  - 公告格式化和重要性过滤测试

### 测试运行命令
```bash
# 运行所有单元测试
python -m pytest tests/unit/ -v

# 运行API测试
python -m pytest tests/api/ -v

# 运行新增测试
python test_changes.py
python test_integration.py
python test_announcement_real.py

# 运行特定模块测试
python -m pytest tests/unit/test_basic.py -v
python -m pytest tests/api/test_llm_analyzer.py -v
```

## 实现问题与解决方案

### 问题概述
在实现三项改进（缓存增强、邮件表格拆分、公告抓取）过程中，遇到并解决了六大关键问题。

### 1. 依赖问题
**问题描述**：
- `akshare` 模块未安装，导致数据获取失败
- `openai` 版本不兼容（0.27.0 vs 1.0.0+），导致 `OpenAI` 类导入失败

**解决方案**：
```bash
# 安装akshare
pip install --user akshare

# 升级openai到兼容版本
pip install --user "openai>=1.0.0" -U
```

**影响文件**：
- `src/data_fetcher.py` - 依赖akshare获取股票数据
- `src/llm_analyzer.py` - 依赖openai>=1.0.0 API

### 2. 缓存序列化问题
**问题描述**：
缓存数据包含无法JSON序列化的类型：
- `pd.Timestamp` - 日期时间对象
- `np.integer`/`np.floating` - NumPy数值类型
- `NaN` 值导致序列化失败

**解决方案**：
```python
# src/data_fetcher.py 中的类型转换
for key, value in latest_data_dict.items():
    if pd.isna(value):
        latest_data_dict[key] = None
    elif isinstance(value, pd.Timestamp):
        latest_data_dict[key] = value.isoformat()
    elif isinstance(value, (np.integer, np.floating)):
        latest_data_dict[key] = float(value)
```

**影响文件**：
- `src/data_fetcher.py:121-126` - 添加类型转换逻辑
- `src/cache_manager.py` - 缓存存储/读取

### 3. 文件名解析问题
**问题描述**：
缓存文件名使用 `split('_')` 解析，无法正确处理包含下划线的股票代码（如 `600000_SH`）。

**解决方案**：
```python
# 修复前
parts = filename.split('_')  # ['600000', 'SH', '20250307']

# 修复后
parts = filename.rsplit('_', 1)  # ['600000_SH', '20250307']
```

**影响文件**：
- `src/cache_manager.py:58, 214, 261` - 所有文件名解析位置

### 4. Unicode编码问题
**问题描述**：
Windows控制台不支持某些Unicode字符（✓, ✗, ✅, ❌），导致测试输出编码错误。

**解决方案**：
```python
# 使用ASCII兼容字符替换
✓ → V  # 检查通过
✗ → X  # 检查失败
✅ → [PASS]
❌ → [FAIL]
```

**影响文件**：
- `test_changes.py` - 测试输出字符替换
- `test_integration.py` - 集成测试输出字符替换

### 5. 缓存验证不完整
**问题描述**：
缓存验证只检查基本字段，缺少基本面数据字段验证，导致使用不完整缓存数据。

**解决方案**：
```python
# 扩展必需字段检查
required_fields = [
    'open', 'close', 'high', 'low', 'ma60',  # 技术指标
    'dividend_per_share', 'dividend_yield', 'earnings_growth'  # 基本面数据
]
```

**影响文件**：
- `src/data_fetcher.py:55` - 扩展缓存验证字段

### 6. 公告抓取网络问题
**问题描述**：
SSE/SZSE官方API不稳定，经常超时或返回错误，需要多级备选方案。

**解决方案**：
1. **第一级**：官方API（SSE/SZSE）
2. **第二级**：Sina财经公告页面解析
3. **第三级**：降级返回空数据，不阻塞主流程

```python
try:
    # 尝试SSE官方API
    announcements = self._fetch_from_sse_api(stock_code, days)
except Exception:
    try:
        # 降级到Sina财经
        announcements = self._fetch_from_sina(stock_code, exchange, days)
    except Exception:
        # 最终降级
        announcements = []
```

**影响文件**：
- `src/announcement_fetcher.py` - 多级错误处理和备选方案

## 测试策略更新

### 扩展测试覆盖范围
原LLM测试策略已扩展到全系统测试，新增以下测试类别：

#### 1. 模块导入测试
验证所有修改后的模块能否正常导入和初始化：
```python
def test_imports():
    from src.cache_manager import CacheManager
    from src.data_fetcher import StockDataFetcher
    from src.email_notifier import EmailNotifier
    from src.announcement_fetcher import AnnouncementFetcher
```

#### 2. 功能完整性测试
验证三项改进的核心功能：
- **缓存功能**：每日缓存、字段验证、序列化处理
- **邮件表格**：表格拆分、格式正确性、数据显示
- **公告抓取**：API调用、数据解析、格式化输出

#### 3. 集成测试
验证各模块协同工作：
```python
def test_daily_caching():
    # 测试缓存命中机制
    # 第一次获取（无缓存）
    stock_data1 = fetcher.fetch_stock_data()
    # 第二次获取（应使用缓存）
    stock_data2 = fetcher.fetch_stock_data()
```

#### 4. 错误处理测试
验证系统对异常情况的容错能力：
- 网络错误时的降级处理
- 数据格式错误的优雅处理
- API调用失败的重试机制

### 测试环境配置
```yaml
# 测试专用配置
test_config:
  stocks: ['601728']  # 单只股票减少测试时间
  data_source: 'akshare'
  announcements:
    enable: false  # 测试时默认禁用公告抓取
  storage:
    cache_dir: './test_cache'  # 独立测试缓存目录
    data_dir: './test_data'    # 独立测试数据目录
```

### 持续集成建议
```yaml
# GitHub Actions 配置示例
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v4
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run unit tests
        run: python -m pytest tests/unit/ -v
      - name: Run integration tests
        run: python test_integration.py
```

---

## 附录

### A. DeepSeek API参考
- 官方文档：https://platform.deepseek.com/api-docs
- 模型列表：deepseek-chat, deepseek-coder
- 定价：$0.001/1K tokens（输入+输出）

### B. 相关配置项
```yaml
llm:
  # 必需配置
  api_key: "密钥"
  model: "deepseek-chat"
  
  # 可选配置
  temperature: 0.7          # 0-2，越高越有创造性
  max_tokens: 2000         # 最大输出长度
  top_p: 1.0              # 核采样参数
  frequency_penalty: 0.0   # 频率惩罚
  presence_penalty: 0.0    # 存在惩罚
  
  # 业务配置
  enable_cache: true
  cache_ttl: 86400        # 缓存时间（秒）
  rate_limit: 10          # 每分钟调用限制
```

### C. 故障排除
1. **API密钥无效**：检查密钥格式和权限
2. **网络超时**：增加timeout配置，检查网络连接
3. **响应格式错误**：检查提示工程，确保LLM返回正确格式
4. **成本超支**：启用缓存，优化提示长度
5. **速率限制**：实现限流控制，分批处理

## 邮件副本与变更追踪系统

### A. 邮件副本存档功能
新增邮件副本自动保存机制，帮助追踪邮件内容变化和验证系统输出。

#### 1. 实现原理
- **位置**：`src/email_notifier.py` - `_save_email_copy()`方法
- **触发时机**：每次调用`_send_email()`方法时自动保存（包括测试模式）
- **存储位置**：`./data/email_archive/`目录
- **文件名格式**：`YYYYMMDD_HHMMSS_邮件主题前50字符.html`

#### 2. 邮件副本文件结构
```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>邮件主题</title>
</head>
<body>
    <h1>邮件主题</h1>
    <div class="meta">
        <p><strong>发送时间:</strong> 2026-03-07 14:30:00</p>
        <p><strong>收件人:</strong> receiver@example.com</p>
        <p><strong>文件:</strong> 20260307_143000_股票日报_2026_03_07.html</p>
    </div>
    <hr>
    <!-- 原始邮件正文 -->
</body>
</html>
```

#### 3. 邮件变更分析方法
新增`compare_email_changes(days_back=7)`方法，分析最近邮件的变化趋势：
- 统计最近邮件的数量、日期范围
- 检查每封邮件是否包含公告
- 分析邮件大小变化趋势
- 提供可视化分析基础数据

### B. 官方公司公告系统 (v1.6升级)
系统已从新闻文章切换至**巨潮资讯网(cninfo)**官方公司公告API，获取真实公司公告（利润分配、停牌、年度/中期/末期报告等重大事项）。

#### 1. 核心特性
- **官方数据源**：巨潮资讯网(cninfo)官方API，中国证监会指定信息披露平台
- **真实公告**：获取真实公司公告（非新闻文章），包含有效cninfo链接
- **股息详情**：集成详细分红数据提取（送股比例、派息比例、除权日等）
- **智能分类**：支持30+种公告类型分类，自动提取重要公告
- **稳健设计**：解决中文编码问题，支持5列/6列API响应格式
- **ETF处理**：自动跳过ETF基金（使用不同公告系统）

#### 2. 技术实现
- **主数据源**：`akshare.stock_zh_a_disclosure_report_cninfo()` 获取官方公告
- **股息提取**：`akshare.stock_dividend_cninfo()` 获取详细分红数据
- **错误处理**：30天窗口回退，多列格式支持，移除模拟数据依赖
- **编码解决**：通过位置索引访问列数据，避免中文列名编码问题

#### 3. 配置与使用
确保`config.yaml`中`announcements.enable: true`，系统将自动获取最近7天重要公告并集成到邮件报告中。

### C. 配置示例
```yaml
# config/config.yaml
email:
  archive_dir: ./data/email_archive  # 邮件副本存储目录

announcements:
  enable: true  # 启用公告功能
  days: 7       # 获取最近7天公告
  include_in_email: true  # 在邮件中包含公告
```

### D. 使用场景
1. **质量保证**：通过比较邮件副本验证系统输出稳定性
2. **功能验证**：确认新功能（如公告显示）是否正确工作
3. **问题诊断**：分析历史邮件内容变化，定位问题根源
4. **合规审计**：保留邮件发送记录，满足合规要求

### E. 开发规范

#### 1. 临时文件管理
- **严格限制**：每个函数/模块不允许创建超过2个临时文件
- **自动清理**：临时文件必须在函数结束时清理或使用后立即删除
- **命名规范**：临时文件必须使用明确的前缀（如 `temp_`, `debug_`）便于识别
- **版本控制**：临时文件禁止提交到Git仓库，必须添加到 `.gitignore`

#### 2. 硬编码数据禁止
- **严禁使用**：禁止在代码中使用硬编码的模拟数据、示例数据、占位符数据
- **真实数据源**：所有数据必须来自真实API、数据库或配置文件
- **错误处理**：当数据源不可用时，应返回空值或适当错误，而非模拟数据
- **配置驱动**：所有参数应通过配置文件或环境变量设置

#### 3. 测试文件管理
- **单元测试**：测试文件应放在 `tests/` 目录，遵循项目测试结构
- **临时测试**：调试性临时测试脚本必须在调试完成后立即删除
- **生产代码**：生产代码中禁止包含测试逻辑或调试代码
- **环境分离**：测试环境与生产环境配置完全分离

#### 4. 代码质量要求
- **错误处理**：所有API调用必须有适当的错误处理和降级方案
- **日志记录**：关键操作必须有详细的日志记录，便于问题追踪
- **类型安全**：Python类型注解应尽可能完善，提高代码可维护性
- **性能考虑**：避免不必要的API调用和重复计算，合理使用缓存

#### 5. 部署安全
- **敏感信息**：API密钥、密码等敏感信息必须通过环境变量管理
- **依赖管理**：所有依赖必须在 `requirements.txt` 中明确指定版本
- **环境验证**：启动时验证必要的环境变量和配置项
- **错误监控**：实现适当的错误监控和告警机制

### F. 维护建议
1. **定期清理**：设置自动清理策略，避免邮件副本积累占用空间
2. **版本对比**：重要更新前后保存邮件副本，用于回归测试
3. **自动化检查**：集成邮件分析到CI/CD流程中

### G. 12个月股息总和计算系统 (v1.8升级)
解决原始股息数据显示不准确的问题（如601398显示4.54元/股，实际应为0.306元/股），通过计算过去12个月的总每股分红，包括年度分红和中期分红。

#### 1. 核心问题
- **原始问题**：部分股票（如601398工商银行）显示异常高的股息数据（4.54元/股）
- **根本原因**：系统仅显示最新单次分红，而非过去12个月的总分红
- **实际需求**：投资者需要过去12个月的总每股分红用于股息率计算

#### 2. 技术实现
- **数据源**：使用`akshare.stock_dividend_cninfo()`获取详细分红记录
- **时间窗口**：计算过去365天内的所有现金分红
- **分红类型**：包括"年度分红"和"中期分红"两种类型
- **单位处理**：自动处理不同单位（分转元、每10股转每股）
- **验证机制**：检查分红数据合理性（0.001-5.0元/股范围）

#### 3. 关键改进
```python
# 旧方法：仅获取最新单次分红
dividend = get_latest_dividend_per_share(stock_code)

# 新方法：获取过去12个月总分红
def get_total_dividends_last_12months(stock_code):
    # 1. 获取cninfo分红数据
    dividend_df = ak.stock_dividend_cninfo(symbol=stock_code)
    
    # 2. 过滤过去12个月记录
    twelve_months_ago = datetime.now() - timedelta(days=365)
    recent_dividends = dividend_df[dividend_df['announcement_date'] >= twelve_months_ago]
    
    # 3. 累加所有现金分红
    total_per_share = 0.0
    for _, row in recent_dividends.iterrows():
        cash_ratio = row.iloc[4]  # 现金分红比例列
        if cash_ratio > 0:
            # 从描述解析每股金额（默认每10股）
            per_share = cash_ratio / 10.0
            total_per_share += per_share
    
    return round(total_per_share, 3)
```

#### 4. 验证结果
- **601088中国神华**：3.240元/股 (0.980中期 + 2.260年度)
- **601398工商银行**：0.306元/股 (0.141中期 + 0.165年度)  
- **601728中国电信**：0.274元/股 (0.181中期 + 0.093年度)
- **ETF处理**：510880、512810返回None（ETF无标准分红）

#### 5. 系统集成
- **缓存更新**：缓存存储12个月总分红数据而非单次分红
- **股息率计算**：使用12个月总分红计算准确股息率
- **邮件显示**：邮件表格显示正确的每股分红和股息率
- **向后兼容**：保持API兼容性，仅更新内部逻辑

#### 6. 配置与使用
系统自动使用12个月总分红，无需额外配置。所有股票数据获取和邮件报告均已更新。

### H. 缓存绕过与数据新鲜度保障系统 (v1.9升级)
解决缓存数据过时问题（如使用周五数据在周一运行系统），确保在15:05后使用当日最新市场数据。

#### 1. 核心问题
- **原始问题**：系统在周一运行时仍使用周五的缓存数据（股价5.97元 vs 实际6.01元）
- **根本原因**：缓存检查仅验证字段完整性，不验证数据日期和时间
- **实际需求**：在15:05（市场收盘后）应使用当日最新数据

#### 2. 技术实现
- **时间判断**：配置`cache_bypass_cutoff: '15:05'`（可自定义）
- **缓存检查**：`_should_bypass_cache()`方法判断是否应绕过缓存
- **数据源降级**：akshare返回空数据时自动触发网页爬虫备用方案

#### 3. 关键逻辑
```python
def _should_bypass_cache(self, cached_data):
    # 规则：如果当前时间 >= 15:05 且缓存数据日期不是今天，则绕过缓存
    if cached_date == today:
        return False  # 缓存数据是今天的，可以使用
    
    now = datetime.now()
    cutoff_time = now.replace(hour=15, minute=5, second=0, microsecond=0)
    
    if now >= cutoff_time:
        # 当前时间 >= 15:05，需要今天的数据，但缓存数据不是今天的
        return True
    else:
        # 当前时间 < 15:05，可以使用旧数据
        return False
```

#### 4. 数据源降级流程
```python
# 1. akshare返回空数据时抛出异常
if stock_zh_a_hist_df.empty:
    raise ValueError("akshare返回空数据")

# 2. 异常触发网页爬虫备用方案
except Exception as e:
    logger.warning("akshare失败，尝试使用网页爬虫获取真实数据")
    return self._fetch_from_web_crawler(stock_code, start_date, end_date)

# 3. 网页爬虫尝试多个数据源（新浪财经、腾讯财经、东方财富）
data_sources = [
    self._fetch_from_sina,      # 新浪财经历史数据API
    self._fetch_from_qq,        # 腾讯财经API
    self._fetch_from_eastmoney  # 东方财富API
]
```

#### 5. 配置示例
```yaml
scheduler:
  run_time: '15:30'
  cache_bypass_cutoff: '15:05'  # 15:05后绕过非今日缓存
  timezone: Asia/Shanghai
```

#### 6. 验证结果
- **缓存绕过**：周一20:29检测到缓存数据日期2026-03-06不是今天，触发绕过
- **数据获取**：网页爬虫成功获取2026-03-09最新数据（收盘价6.01元）
- **股息计算**：使用最新股价6.01元计算准确股息率（4.56%）

#### 7. 系统集成
- **自动降级**：akshare失效时无缝切换至网页爬虫
- **时间感知**：智能判断何时需要最新数据
- **配置灵活**：支持自定义绕过时间阈值
- **日志透明**：详细记录缓存绕过和数据源切换过程

**文档版本**：v1.6  
**最后更新**：2026-03-09  
**适用版本**：股票量化系统 v1.9+