# 飞书 DeepSeek 助手实施与验收（2026-09-24）

本次实现自然语言问答、配置提案、确认执行、真实数据研究与 Docker 隔离。
现有斜线管理命令继续通过原分发器执行。未提交或推送 Git；仓库原有未提交改动保留。

## 实现范围

- 独立 DeepSeek 客户端，默认 `deepseek-flash`、非思考模式，使用 Tool Calls；
  复用原密钥和地址，单独限制超时、重试、工具轮次与对话上下文。
- 私聊直接提问，群聊核对本机器人身份后响应 @；会话和任务按聊天、发送者隔离。
  模型只能读取受限字段、查询实际缓存和生成提案，没有确认或直接执行工具。
- `ConfigProposal`、`ResearchJobSpec`、`JobResult` 分离；SQLite 保存操作、去重、
  状态和结果。确认绑定发起人及预览版本，默认 15 分钟有效。
- 配置使用字段注册表及现有市场解析器验证，通过 ConfigStore 在锁内检查版本、
  修改指定字段并原子保存。共享配置列出全部受影响市场，多文件分别预览确认。
- 研究任务冻结源码、脱敏配置、参数和批准范围，主机使用共享数据准备接口补数。
  缺失数据或策略依赖阻止执行；既有 PIT、raw 价格、公司行动、税费和基准合同保留。
- 专用 Docker 容器禁网、非 root、只读输入和根目录，只有输出目录持久可写，
  限制 CPU、内存、进程、时间和输出量。生成 Python 在第二个容器运行，
  可信基准结果作为只读输入，附加产物单独保存。
- 取消绕过慢速模型队列；确认、取消和关闭使用原子状态迁移。重启核验主机保存的
  结果收据及哈希；恢复中再次退出也能继续核对，结果不确定时不自动重跑。
- 脚本、JSON/CSV、日志和图表可通过文件附件获取。完整预览未送达不允许执行，
  结果通知失败可以重发而不重跑。上传前检查路径、链接、文件大小和凭证。

## 本地回归

最终合并回归：**586 passed, 2 skipped**，耗时 44.10 秒。
涵盖 `tests/interactive/`、`tests/validation/`，以及 ConfigStore、调度、迁移、
RuntimeService、市场配置、数据准备、交易日截止、PIT 补齐、raw/qfq 合同和部署工具。
跳过项为当前 Windows 测试用户不能创建符号链接，以及仅 Linux 支持的真实进程组
资源清理测试；两项均已在 Linux 部署主机回归通过。路径穿越等其他拒绝用例已通过。

其中助手相关 189 个用例（含 2 个跳过），完整 validation 36 个用例。
JUnit 记录位于 `temp_test_cache/feishu_assistant_final_tests.xml`；本轮相关文件 Ruff 通过。
恢复与停止边界、实际缓存查询均已纳入最终合并回归。
还覆盖真实共享访问保护、旧核心时长接口兼容、CAPM beta 的动态历史需求
（包括 WF 预热和仍可沿用的最老季度快照）、
汇率依赖、规范标识，以及 Justified PB 至少两只股票的横截面前置条件。
独立复核使用四只股票与实际基准缓存，在季度快照及最老有效快照上比较
550/800 天快照年龄设置，共 16 组完整 BetaEstimate 与全缓存计算逐字段一致。
测试环境缺少已声明的交易日历依赖，已将官方 PyPI 包安装在被忽略的
`temp_test_cache/assistant_dependencies`，不更改现有全局 Python 环境。

## 部署与账号验收

部署主机原服务 `trade-eyes.service` 与本地心跳一致，Docker 24.0.4 可用；
DeepSeek 官方模型列表确认包含 `deepseek-flash`，飞书应用令牌及本机器人身份查询成功。
部署主机使用实际密钥完成非思考 Tool Calls 往返：模型调用只读调度工具，
根据返回的 `scheduler.run_time=19:00` 生成答复，测试未发送飞书消息。
回执位于 `temp_test_cache/feishu_assistant_acceptance/deepseek_tools_smoke.json`。
另用当前助手的全部 6 个工具参数定义完成实际 API 往返，首轮和工具结果后的
第二轮均返回 HTTP 200，答复匹配读取的真实值；没有配置写入或飞书消息。
回执为 `temp_test_cache/feishu_assistant_acceptance/deepseek_full_schema.json`。
真实 DeepSeek 与 FeishuAssistant 的隔离配置闭环也已通过：读取运行时间、生成
完整预览、同一发起人确认后保存一次、重复确认不重写。验收工具只提供生产中的
`scheduler.run_time`，将独立副本的 19:00 改为 18:47；生产主配置、凭证和约束文件
哈希不变，没有飞书外发。回执为 `deepseek_config_roundtrip.json`，精确出站正文
为 `deepseek_roundtrip_outbound_payloads.json`，均在同一验收目录。
飞书附件预检也已通过：`/im/v1/files` HTTP 200、code 0，仅上传本轮无敏感
验收文本，未向聊天发送。回执为 `feishu_upload_smoke.json`。
现有 `allowed_chat_ids: ['*']` 保留。

主机只有约 1.74 GiB 内存，因此本机部署使用 **1 CPU、768 MiB** 的显式覆盖；
通用配置样例保留 **2 CPU、4096 MiB** 默认值。资源不足会作为任务失败返回，
不能据此删减用户批准的标的或区间。

基础镜像下载已使用 Docker Official Images 在 ECR 的公开分发地址。
服务器到 PyPI wheel 的传输不稳定，构建改用从官方 PyPI 下载并校验 SHA-256 的
独立依赖清单；构建上下文不含项目配置、密钥或数据。候选镜像只有完成真实隔离
验收后才赋予运行标签。现已完成构建，`trade-eyes-research:local` 的镜像 ID 为
`sha256:0d712d1e080dff48a630edadeb7ec6968f27595f3e3630ee83252d4cd4980e20`。
实际容器的 UID 65532、凭证环境隔离、输入/根目录只读、禁网、1 CPU/768 MiB
cgroup 限制及 Numba JIT 均通过；取消后 0.07 秒清理完毕，原三个共享容器不变。
回执为 `temp_test_cache/feishu_assistant_acceptance/image_smoke.json`。
生成 Python 也已用相同容器隔离参数调用项目的真实 `estimate_robust_beta`，
读取批准的 000333、600036 与基准缓存，产出 JSON、CSV、PNG。000333 历史不足，
如实返回 `beta: null`；600036 返回六个实际估计分量。已检查 JSON 和实际图表。
产物位于 `temp_test_cache/feishu_assistant_acceptance/generated_container_artifacts/`，
标记 `cache_diagnostic_only`、`baseline_completed: false`、`formal_acceptance: false`；
这证明生成代码和数据接口可在容器运行，并不代表策略回测成功。

已实际执行一次 PB 小范围补数：000333、600036，研究区间 2022-01-01 至
2025-01-01，基准 510880、510300；补齐了 000333 的 2187 行历史行情和 40 条
财报。最终仍有未解析公司行动、现金分红公告日期缺失、Yahoo 对 ETF 请求返回 403、
600036 财报受 Baostock 共享熔断限制等缺口，因此任务明确失败，没有进入回测。
回执为 `real_backfill_result.json` 和 `real_backfill_artifacts/data_readiness.json`。
首次补数与测试并行时，准备进程曾占用约 1.28 GiB RSS 和 0.53 GiB 交换空间，
主机可用内存降至约 73 MiB。该进程随后自行失败退出，没有发送人工终止信号。
据此增加 Linux 补数进程组内存监控、低可用内存保护与资源回执，并改为串行验收；
该新增保护已在部署主机用禁网进程实测：正常任务完成，低可用内存在启动前拒绝，
512 MiB 预算的测试进程在 517.7 MiB、6.5 秒时被清理；最低可用内存仍为
681.5 MiB，测试后恢复约 1.14 GiB，主服务 PID 不变。
证据为 `host_memory_guard.json` 及 `host_memory_guard_artifacts/`。

兼容性检查发现主机缺少本地新增的共享 Baostock 访问模块，
其余核心接口均可解析。已整理只接入共享访问控制的三文件候选补丁，保留远端原
回测、搜索和财报算法；候选限流回归 44 通过（一个无关的新季度行为用例明确排除）。
冻结源码的共享访问状态目录已按实际部署目录解析为绝对路径，不会逐任务另起计数。
生产现有 9 个月策略窗口保持不变，预算/Solver 等独立配置修改继续使用部署版本
解析器。助手修改或切换评估方案时，会补充校验至少 24 个月、连续留出窗口、
重叠隔离与有效排名窗口；研究请求单独执行至少 24 个月下限。
最终远端回归使用生产对应核心与原有测试、全部新增助手测试和完整 validation：
**516 passed, 2 deselected**，20.28 秒，无失败或跳过。明确排除的是未部署的
“查询未来季度限制”和独立行业分类脚本的共享熔断新行为；也未把本地尚未部署的
回填恢复功能测试套在旧主机核心上。助手内新增的兼容与内存保护用例全部通过。
服务已于北京时间 2026-09-24 02:17 启用助手并重启一次，02:20 完成就绪核验。
`trade-eyes.service` 的 MainPID 为 389100、NRestarts 为 0，
`read_service_status` 返回的 PID 与 systemd 一致，心跳 fresh/ready 均为 true。
日志确认助手初始化成功，SQLite 已建立 actions、audit、receipts 表；
飞书 SDK 接收进程 389109 有一条已建立的出站 443 连接。
此处 `[SERVICE_READY]` 是运维检查依据心跳与 MainPID 产生的标记，不是服务日志。
最终发布的 19 个文件与核验清单 SHA 一致，214 个无关源码文件及 7 个凭证、约束、
账户和策略指针文件保持不变，主配置除助手设置外语义不变。
实际启用 `deepseek-flash`，容器限制 1 CPU / 768 MiB，宿主补数进程组预算
768 MiB、保留可用内存 128 MiB，补数与容器阶段分别限时 60 分钟。
宿主补数内存保护通过 Linux `/proc` 每 100 毫秒主动监控与清理，
与 Docker 的 cgroup 硬限制不同。
部署回执为 `temp_test_cache/feishu_assistant_acceptance/deployment_service_ready.json`；
远端备份目录为
`/root/trade_eyes_keeper/data/deployments/feishu_assistant_20260923T181459Z_ec350f0b/backup`。

完整账号流程需要真实飞书会话中的来信、预览及发起人确认，尚不能以模拟事件或
API 令牌成功替代。测试会话已向用户询问。正式策略报告验收继续要求配置全量标的；
缓存限定的容器诊断只代表指定范围研究，不会激活策略或更新参考账户。

## 运维文件

- `scripts/check_feishu_research.py`：项目接口兼容性、镜像与资源预检；
  `--smoke` 仅使用显式标的和区间的现有真实缓存，不发消息、不自动补数。
- `scripts/build_feishu_research_image.py`：部署主机专用镜像构建和真实隔离探针。
- `scripts/deploy_feishu_assistant.py`：明确文件清单、前后哈希、私有配置校验、
  单独备份和原子替换；不执行 Git 操作，助手配置只合并指定键。
- 使用说明：`docs/guide/feishu_telegram_setup.md`；部署步骤：`docs/deployment.md`。
