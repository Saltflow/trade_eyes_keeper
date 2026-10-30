# 财报数据局部标注修正：离线验收

日期：2026-10-24。用户授权仅修复整表拦截和有格式意见的整块展示降级，使用招行同一份2025年报的既有响应离线回放。没有修改prompt、schema、任务分块、32K输出限额、预算、传输、重试、截断处理或第二阶段合并算法。

## 改动及边界

- `combine_blocks`不再用表级校验结果决定整表去留。所有已解析表保留原始字段和行值，检查结果作为关联该表的局部标注；仅补充已有脚本派生的披露版本字段，并不覆盖模型意外提供的同名字段。
- 分块`format_notes`不再触发整块JSON阅读页和空CSV。正常表继续排版、导出；局部不合格式的表按明确列键展示，未声明列键明确标记，原值不映射到其他列，不猜测单位、期间或同义。
- 缺字段的说明记录局部保留，不让该记录的展示异常影响其他表。原始`response.body`、`content.txt`、`parsed.json`不修改。
- 原协议不兼容、JSON解析失败、供应商报告截断的处理保持原样。被截断的两块没有补全、修JSON、续写或追加调用。
- 第二阶段没有修改。本次验收是第一阶段保存/展示及回放；保留的不合约表不意味着已经通过自动多年合并验收，特别是未知列键和缺失类型字段仍需单独处理。

## 测试

通过`.tmp/verify_annotation_only_20261024.py`在封锁requests HTTP与socket连接的进程中执行：

```text
tests/test_fundamental_data_v2.py
tests/test_fundamental_data_api.py
tests/validation/
-q --tb=short -p no:cacheprovider
--basetemp=.tmp/annotation_only_final_bound_tests_20261024
193 passed（139个数据合约测试＋18个原API回归＋36个validation）
```

测试包括局部口径冲突、缺issues、错误kind、未知列键原值保留、披露版本字段碰撞、异常说明记录局部展示；截断、调用预算及固定请求计划原有回归仍通过。测试前后代码/config/样例哈希一致。

## 招行真实响应离线回放

来源：`data/fundamental_data/v3_cmb_2025_first_20261024/`。

最终新结果：`data/fundamental_data/v3_cmb_2025_annotation_replay_20261024_final/阅读.html`。

验收证据：`data/fundamental_data/annotation_only_acceptance_20261024_final/`，包含`acceptance.json`、`test_result.json`、`pytest.log`、`replay.log`及旧产物哈希对照。

| 项目 | 原结果 | 新回放 |
|---|---:|---:|
| 正常表集合 | 43张 | 62张 |
| 正常表集合中的行数（含标题行） | 369行 | 693行 |
| CFS行数 | 0行 | 58行 |
| CFS CSV原始值观察数 | 0 | 110（3个标题行没有值，不伪造单元格） |

全部8个可解析分块的62张表逐一对照原始响应：去除脚本新增的披露版本字段后，原始字段、科目、列、行、数字和文字完全相同。6个有意见分块不再整体降级；175.51%季度均值和200.03%期末值在原列下可读展示，原始错误kind仍保留并标注，未自动改成attributes。

对主HTML检查CFS表具有58行、表格行列数对应、无script标签；没有声称完成截图视觉验收。

`notes_operations`、`notes_assets`依旧为truncated，因此运行状态仍为partial，退出码2。它们原先输出已达上限、JSON未闭合，本轮没有修补或重提。

新增模型调用0、HTTP/socket尝试0。旧运行目录共2291个文件的集合及SHA256完全不变；10份回放HTTP正文和模型正文逐字节一致，8份解析对象与原记录完全相同。历史API usage仅为历史值，不算本轮新增用量。

改动前文件备份：`.trash/fundamental_data_before_annotation_only_20261024/before.zip`。
