# 泸州老窖同份年报的四块提取

用户授权：先改代码，按三表、股本每股指标、附注和元数据分块提取，脚本拼接；
使用上次同一份年报运行一次，旧结果不动，调用后不修改代码、流程或产物。
本文在正式请求前写好，不回填本次结果。

## 输入与命令

输入仍是泸州老窖 FY2025 原始年报，183页；不使用上次模型输出作新输入。
PDF SHA256：`7a2e321f049401fd34dfbf8c22964e804d16f077ece9382791cb2b2ce0c74676`。

```powershell
python -X utf8 scripts/build_fundamental_data_v2.py extract --source data/dataset_documents/c57e4b4d61d13a4242e34a80511773a5759123438baf2df98720e21d7321aab3/original.pdf --company 泸州老窖 --year 2025 --output data/fundamental_data/v2_luzhou_2025_chunks_first --paid
```

省略`--paid`只准备请求。正式输出目录必须尚不存在，不能覆盖旧目录。

## 固定流程

| 次序 | 块 | 只返回哪些字段 |
|---|---|---|
| 1 | 三表 | 公司/财期/范围/币种、期间字典、缺项、BS/IS/CFS |
| 2 | 股本每股指标 | 期间字典、缺项、股份分派事项、股本每股指标表 |
| 3 | 附注 | 期间字典、缺项、政策及附注明细 |
| 4 | 元数据 | 期间字典、缺项、管理层披露、公司/行业/分部/地区元数据 |

同一PDF输入只读一次，四个请求复用同样的输入前缀，各自任务和小schema置于末尾。
固定DeepSeek Flash、JSON模式、非思考模式，每请求最多32768输出token，按顺序执行。
每块仅一次请求，没有修复、重试、切模型、自动补提取或研究调用。
每次的原始响应与usage独立保存，未知usage不能当零；缓存效果不预先假定。

脚本保留原数值和文字，合并各块字段；同名期间的定义不同则加块名区分列标签。
缺块用空容器和明确缺项表示，状态为partial/incomplete，不冒称完整成功。
结构检查仅处理JSON和表格列数，不对照原财报、不执行数值复核或勾稽。

## 输出与冻结

主目录有`data.json`、`阅读.md`、`阅读.html`、`financial_values.csv`、`summary.json`。
`blocks/statements`、`blocks/capital`、`blocks/notes`、`blocks/metadata`各自保存请求、
原始响应、原始正文、receipt及可用时的数据和阅读页。

首次付费请求前，四个请求均已落盘，根`freeze.json`记录代码、配置、输入、请求哈希及四次上限；
`frozen_code/`保存本次代码与配置原字节。结束时程序生成`output_hashes.json`。
调用之后只读展示结果，不再更改代码、配置、请求或产物。

旧目录`api_demo_20261023_first`、`v2_luzhou_2025_first`保持原字节。
此前代码和配置的ZIP在`.trash/fundamental_data_v2_before_chunks_20261023/before.zip`，
新授权允许修改活动代码；不把旧freeze指向活动代码的哈希变化误称为旧产物被修改。
