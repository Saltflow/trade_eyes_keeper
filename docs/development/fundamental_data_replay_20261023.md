# 财报响应离线回放与链路调试

解析、拼合和展示的BUG先用已有真实响应回放，不通过新增付费请求验证。
原始运行目录只读，回放结果写新目录。本轮来源是泸州老窖FY2025的四块原始返回。

```powershell
python -X utf8 scripts/build_fundamental_data_v2.py replay --source data/fundamental_data/v2_luzhou_2025_chunks_first --output data/fundamental_data/v2_luzhou_2025_replay
```

该命令没有`--paid`，也不允许加此标志。回放不需要原PDF、tokenizer或API密钥。
它用本地`RecordedResponse`替代HTTP返回，复用相同的响应解析、分块接收、拼合和渲染函数。
缺少记录会报错，不会转成真实网络请求；输出目录不可位于原运行目录内。

## 先保存 再解析和展示

| 文件 | 保存时机与用途 |
|---|---|
| request.json | 执行前保存请求；回放保留当年的真实请求，不假称当年已使用新prompt |
| attempt.json | 处理开始前记录模式和时间 |
| response.body | 收到或读到HTTP原文后立即保存 |
| content.txt | 从响应信封取出的模型原文，JSON解析前保存 |
| parsed.json | JSON解析完成后立即保存，不受schema意见影响 |
| format_notes.json | 如有结构差异，仅记录，不排除整块 |
| data.json | 用于拼合的数据；每块保留原始解析内容 |
| 阅读.md / 阅读.html | 按现有内容生成表格，百分比、区间和文本直接展示 |
| receipt.json | 调用或回放终态；JSON错误包含行、列和字符位置 |
| events.jsonl | 逐步记录上述环节，便于确定故障发生在响应、解析还是展示 |

回放新增API调用为0。旧响应中的usage保存在`historical_usage`，不当作本次新用量或新费用。
日志不保存Authorization header或密钥。

## 显示约定

`35.07%`、`10-45`、`9.50%-2.11%`、文字事项均原样保留。
金额、股数、EPS仍优先提示模型输出十进制字符串，但它不再是展示的准入门槛。
列数不齐时阅读视图补空白位置/列名，不丢额外单元格；原始数据文件不改变。
额外字段独立显示，不能因字段不在预设schema中就抹掉其他内容。

结构处理不对照财报原文，不做财务正确性判断或勾稽，不修复历史JSON或修改旧响应。
这次验证的是处理既有返回的链路，不代表模型按更新后的prompt重新提取过。

## 回归和网络边界

测试fixture使用真实历史返回的最小摘录，覆盖百分号、区间、文本及原样保存。
回放测试令HTTP调用、PDF读取、tokenizer及密钥读取直接失败，以证明没有这些依赖。
真实四块回放在禁止socket连接和HTTP请求的进程中运行，并核对旧目录全部文件哈希。
