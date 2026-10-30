# 本地数据迁移与统一目录验收

用户授权将本地已下载业务数据同步到现有部署主机，建立完整索引，并以远端 ad-hoc 补取原始公开 PDF 和文字。此工作不改变默认策略数据集，不运行策略优化或生成全量策略验收报告。

## 实现

- `src/data/dataset_catalog.py`：SQLite 目录、文件哈希、元数据、原文来源和导入清单核对。WAL 单事务发布，失败保留上一份索引。顶层未知/私有目录明确留排除记录。
- `scripts/push_dataset_snapshot.py`：业务根和内容检查、冻结文件、SHA 去重、分块续传、独立导入快照和逐文件复验；不覆盖既有数据。
- `scripts/push_dataset_snapshot_sftp.py`：可选 SFTP 续传，默认 4 路、上限 8 路。批次协调器持有原操作锁，独立连接仅续写已经验证的 partial；全部写入连接退出后，再沿用原分块及物化校验。
- `scripts/export_dataset_catalog.py`：现有 SQLite 的只读一致快照，全量导出 6 份可读目录/元数据文件，带导出及索引版本，发布失败恢复旧文件。公开清单中的 URL 脱敏有明确标记及原 URL 哈希，受控 SQLite 原值不变。
- `src/data/dataset_documents.py`：已登记公开 URL 的有界下载、真实 PDF 校验、文字提取、SQLite 失败/退避/缺来源状态。缓存原件即使无 URL 也可提字，来源缺口仍保留。
- Bot 增加 5 个目录查询工具，既有 `query_research_data` 支持显式 `dataset_id`；写配置和回测仍须原有确认流程。

纳入原有时点库、reference universe、历史 server imports、行情缓存、公告缓存、分析产物、`cache/analysis`、`data/optimizer_recovery` 和 `data/instrument_audit`。配置、凭证、会话、账户持仓、日志、运行锁和非业务压缩包不迁移。排除不等同于下载丢失。

## 验证记录

- 交互、配置、调度、数据准备及 `tests/validation/` 回归：571 passed、7 skipped。
- 最终目录、文档、两种上传器、正式导出、Bot 和 validation 联合回归：194 passed、4 skipped，见 `delivery_final_tests.xml`。
- 真实 Pi Node 协议与全部 19 个工具 schema：16 passed。
- 涉及文件 Ruff 通过。跳过项为两个 Windows 创建链接权限测试、本地未安装 PyMuPDF、Linux guard 协议测试；后两项分别由远端实际 PDF 提字及 Linux 禁网 guard 烟测补验。

## 已部署与原文实证

8 个代码/说明文件已最小范围发布；随后单独补全目录根规则、来源版本隔离和缺口诊断。2026-09-29 00:28（Asia/Shanghai）部署验收时服务 PID 441052，ready/fresh，Feishu 与调度线程存活、NRestarts=0；01:04 发布导出脚本及说明未重启服务。配置 SHA256 始终为 `6a6d0610d6e9632df77a79050ad4e0e58a1582af2bce656741f401490f1e7b6b`；原 6 receipts、2 conversation turns、26 events 已备份且未改写。

首次远端原文补取处理 20 条：19 份原始 PDF 和文字成功，1 条因超过单份大小上限保留阻塞状态。独立复验全部 19 份文件的 PDF 头、非空 UTF-8 文本与 SHA256：PDF 76,029,236 bytes，文字 10,110,381 bytes，共 4,701 页。首次队列有 18,539 条缺来源 URL、2,361 条尚待下载；这些是该次索引快照数，后续导入后须重新查询。

传输期间再执行一批最多 20 条、300 秒的远端 ad-hoc：18 份成功，2 份超过单文件大小限制。累计 37 份真实 PDF 与文字，独立复验合计 PDF 140,690,563 bytes、文字 19,000,951 bytes、8,938 页，所有哈希匹配。该时点待下载 2,341 条、缺来源 18,539 条、大小阻塞 3 条；没有将缺口记为补齐。

本地最终范围完整扫描为 25,348 个文件、124 个数据集，扫描/解析错误 0；76 个大文件、二进制或未支持解析产物仍按文件登记。历史 `data/server_imports/20260921/manifest.json` 声明的 `progress.json` 和 `supervisor_progress.json` 因运行状态排除规则产生 2 条显式引用缺口，不是当前快照传输丢失。导出及 Bot 查询不能将文件枚举完整误称为内容解析完整或回测数据就绪。

## 传输与合并验收

2026-09-29 01:38:32（Asia/Shanghai）收到最终传输回执：`status=complete`，25,348 个业务文件、1,832,543,754 bytes 全部物化并逐文件 SHA256 验证。目标是服务器 `/root/trade_eyes_keeper/data/dataset_imports/local_b99754ccb9878dbd6d6c369bfe068ecc8b05cafeb4f5445e3ee51e0b91b0aaad/dataset`，既有数据和默认配置未覆盖。传输进程与协调进程均已退出，没有残留 partial 分块。

01:53:28 完成远端全量重建，build_id 为 `d7aecafdeb9549cc86ee22d634800ea5`，包含 136 个物理版本数据集、31,983 个文件、5,499,700,859 bytes。文件枚举完整，索引失败、解析失败和扫描错误均为 0；293 个未解析文件由 7 个大小超限和 286 个不支持格式组成，仍有完整文件登记。历史清单的上述 2 条排除引用使总体状态为 `completed_with_gaps`、`transfer_complete=false`，这不是当前上传失败；本次传输以独立完整回执为准。数据就绪保持 `not_validated`。

01:54:35 正式导出完成，export_id 为 `403753a803254aba80882b9f577bac05`，与上述 build_id 一致。服务器统一入口为 `/root/trade_eyes_keeper/data/dataset_catalog/README.md`；`datasets.json` 包含全部 136 个数据集，`files.jsonl` 有 31,983 行，`documents.jsonl` 有 49,350 行，`document_sources.jsonl` 有 98,249 行，`document_artifacts.jsonl` 有 159 行。全部按同一 SQLite 快照导出，不受查询分页上限影响。

合并后的原文条目状态为 available 94、missing_document 6,193、missing_url 43,063。available 包含已登记文字等资料，不能解释成 94 份 PDF；实际归档且关联的 PDF/文字对仍是 37 份。独立来源维度为 source_url_missing 43,099、source_pdf_missing 49,313，维度有重叠，不可相加。缺少 URL 的条目需要来源发现，不能只重试下载。

导入后的服务器本机 Bot 工具验收通过：两页返回全部 136 个数据集，选中上传的中证 1000 数据集（7,839 文件、1,000 个证券代码），实际读取 000012 行情 1,212 行及财报 30 期；归档目录关联 37 份原文，已索引文字哈希校验和读取通过。此抽样只验工具路径，不是全标的回测验收。测试禁止网络、模型和通知调用，未修改生产对话记录及配置。01:55 服务复验 PID 441052、NRestarts=0、ready/fresh、飞书线程存活、无 failure，配置 SHA256 不变，磁盘剩余 3,799,412,736 bytes。

冻结快照 manifest SHA256 为 `b99754ccb9878dbd6d6c369bfe068ecc8b05cafeb4f5445e3ee51e0b91b0aaad`：原文件 1,832,543,754 bytes，18,600 个独立内容对象，压缩包 215,982,816 bytes，分为 52 块。首次无压缩 SSH 清单上传超时，未完成导入；同一快照开启 SSH 压缩后完成初始化并开始分块。原生 SSH、Git SSH 和已安装 Paramiko 均呈现低速，不能认定为某一客户端独有故障；Paramiko 随机 1 MiB 校验为 103.297 秒，SFTP 为 74.062 秒。4 路各 256 KiB 只读探测总耗时 33.5 秒。

最终真实模型联调尝试被自动审批拒绝，理由为向 DeepSeek 发送这批资料的授权不足；该次模型请求未执行。改为服务器内实际 Bot 工具验收，测试进程禁止 socket 联网，模型和通知接口均不可调用：分页目录、全库原文缺口、归档 PDF 与文字关联、哈希验证后的文字读取通过，配置保持原值。此证据不代表真实飞书对话或新一轮模型回答已验收。

## 2026-09-29 用户授权后的真实模型联调

用户明确回复“授权发送，请完成联调”，此前阻塞解除。使用服务器真实 Pi harness 和 `deepseek-flash`，经生产助手的 handle、工具分派和审计路径执行；测试使用独立会话数据库，回复截获到测试记录，不调用飞书发送接口。

初测发现并修复三类问题：模型将 94 条 available 内容误说成 94 份 PDF/文字配对；漏传所选 dataset_id 而查询默认仓；分页结果重复长路径挤占上下文，使模型耗尽 12 轮预算反复重查。现在工具明确返回按文档去重的 PDF、文字和配对计数及范围说明；助手明确批次选择、raw/qfq/hfq 含义；索引直接给出带 +08:00 时区的 ISO 时间；发给模型的数据集条目只去掉与 physical_root 完全相同的 title，不删文件索引或物理路径。

回归：统计修复 123 passed / 2 skipped，范围及时间修复 94 passed / 1 skipped，最终分页修复 66 passed。三组均包含 `tests/validation/`，有重叠，不能相加视作不同测试数。Ruff 通过。跳过项为本地链接权限及未安装 PyMuPDF，既有远端实际提字证据不变。

最终真实模型检查（08:45—08:48 Asia/Shanghai）完成 4 个问答回合、15 次 API 调用：

- 统一目录和中证 1000 是否上传：5 次模型调用完成，实际使用两页目录、详情及文件工具；识别 136 数据集、31,983 文件、1,000 个证券代码与历史清单排除缺口，没有再次耗尽工具预算。
- 同一对话追问格式、覆盖和全库原文：4 次模型调用，实际带 `ds_b072176254259658a8eef92f` 查询 000012，返回 1,212 行行情、30 期财报，并正确解释 37 PDF、94 文字、37 配对及缺口不能相加。
- 独立对话读取已归档原文：4 次模型调用，实际 `read_dataset_text` 读取光迅科技 002281 的 2022 年年度报告开头，识别公司、披露日期、PDF/TXT 路径和 157 页原件。
- 追问全库原文缺口：2 次模型调用，实际不传 dataset_id 查询 49,350 条全库记录，准确报告 PDF/文字/配对数量、49,313 条缺 PDF 和 43,099 条缺来源 URL。

验收限度：原文问题第一轮仍把“还缺哪些原文”限定在已归档目录，但明确说明不代表全库；显式追问全库后返回正确结果。首轮样本行数的文字表述仍有外推歧义，应以工具中逐标的覆盖结果为准。本次确认实际工具调用、上下文保持、原文读取及核心数字正确，不承诺自然语言回答始终无歧义，也不代表所有数据通过回测就绪验收。

最终代码已发布，备份 `/root/trade_eyes_keeper/data/deployments/dataset_catalog_20260929T004521Z_4d10bb56/backup`。08:48 复核服务 PID 451555、NRestarts=0、ready/fresh、飞书线程存活；配置哈希不变，241 个受保护文件验证通过。全部测试 Pi 子进程已退出，独立审计数据库 integrity_check=ok，无配置提案或研究任务，无生产对话写入，飞书消息发送数为 0。真实 DeepSeek 联调已完成；本轮未重测飞书手机端收发。

结果证据：`live_qa.json`、`live_document_qa.json` 保留原始问答、工具参数、模型调用和引用核查记录；`live_qa_before_*_fix.json` 保留修复前证据；`live_semantic_review.json` 记录复核结论和上述限制。测试与部署产物位于本页末尾所列证据目录。08:48 完整目录导出已刷新，build_id 和各清单行数不变。

本次证据集中在 `temp_test_cache/dataset_sync_20260928/`：部署清单与备份位置、测试输出、快照清单、原文补取及独立文件复验。完整文件索引与原文状态以服务器 `data/dataset_catalog/` 中实际 SQLite 和逐次 JSONL 清单为准。
