# 正式研究v5交付基线（2026-10-24）

[完整真实报告](最终报告.html) · [Markdown](最终报告.md) · [执行来源与费用](provenance.json)

按用户接受的文本交付形式冻结。一次完整执行为固定draft/review/write三轮、一个入口、一个最终报告；付费前冻结代码/prompt/材料，后轮实际请求逐轮冻结。不自动重试、续写、补提取或追加请求。实际收到几年的历史就展示几年，三年不造七年空列；最新已知价是带真实行情日期的本地原始收盘价。

本次真实付费3次：API输入505,445、输出113,746（已含thinking 78,376），按返回usage及冻结单价计US$0.14237091，上限US$5。最终可见正文15,217个本地tokens。该费用不是服务商账单扣费查询，也不包含Codex用量；本次授权不延用到未来执行。

已知限制：两轮模型均未输出chart数据，最终0图。文本三轮跑通，真实图片审查尚未验收；按用户接受的文本报告保留，不自动补调。图表缺口与简化预测假设不会在本次打包提交中偷偷修补。真实报告副本与原始返回字节相同。

## 安装与离线验证

Python 3.10+。独立研究依赖：

```powershell
pip install -r requirements-research.txt
python -B -X utf8 -m pytest tests/test_research_report.py tests/test_research_three_rounds.py tests/test_research_theme_layout.py tests/test_research_valuation_form.py tests/test_research_latest_price.py tests/validation/ -q
```

网络被研究测试禁止，模拟模型响应明确标注；固定资料样例和tokenizer随代码入库，不依赖本机历史验收目录。validation涉及整个应用，需要仓库通用依赖；只验证研究时可省略tests/validation/。

离线准备示例（不调用API、不读取密钥）：

```powershell
python -B -X utf8 scripts/research_report.py --source tests/fixtures/research_release/peer/完整分析.md tests/fixtures/research_release/stage3/模型输入.md --market-snapshot tests/fixtures/research_release/market_snapshot.md --output .tmp/research_prepare
```

正式使用将source换成已有摘要/元数据/同行材料。自动取价从已有stage3清单识别公司，纯正文可传--companies；当前只读已有A股tencent_raw_day文件，不联网补价。手动--market-snapshot优先。历史财报提取与整合不是此正式研究入口的工作。

未来付费需显式本轮预算，在新目录准备后使用--execute与--paid；价格配置带有效UTC日期，过期须核对并重新准备，不能修改已冻结计划。API密钥由环境变量或本地config/.env提供，不入库。详见docs/guide/research_report.md。

## 保存与提交边界

本提交包含正式研究入口、直接依赖、当前prompt及所需公共契约、独立安装清单、便携离线测试和这份真实报告副本。没有把第一/二阶段业务、技术面、回测、调度或部署的其他未提交工作混进来。

完整付费请求/SSE/思考/receipt及架构核查原件继续保存在本机data/fundamental_data与data/acceptance，保持原字节；没有把整个运行目录或凭据提交到Git。本机只删除可重建pytest临时目录12个、5,261个文件，约201.98MiB，原始PDF和旧报告不动。
