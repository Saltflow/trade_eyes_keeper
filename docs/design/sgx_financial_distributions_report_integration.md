# SGX 数据源接入范围

2026-11-04：按用户要求收缩，当前仅新增来源适配。

保留：

- SGX 延迟行情及一年未复权日线，进入现有 StockWebCrawler。
- SGX 公告目录，进入现有 AnnouncementFetcher 的普通及严格查询。
- 官方年报身份、实际财年期末和附件发现，进入现有 SourceService / AnnualDocument。
- 原件沿现有 DataHub 和通用 download_document 采集；SGX 只增加官方域名准入。
- 必需的证券代码、时区、交易日和市场标签；复用已有三次采集时钟。

已撤回：财务 Schema 扩充、财务画像模块、分派金额/TTM 计算模块、PDF 解析升级、研究执行器/模型提示修改，以及新增画像展示。

分红公告作为原始公告提供；本次不新增分红计算、税务选项合并或修订状态机。行情源没有提供的基本面字段保持缺失。年报后续处理沿原任务，不自动启动模型；G3B 原件已有的文字解码问题未在本次修复。

原大版本设计、代码和测试已备份到忽略目录 .tmp/sgx_scope_reduction_20261104/before.zip，不能将旧验收记录当作当前功能。

当前验证见 [验收记录](../development/sgx_integration_acceptance_20261104.md)。未部署生产。
