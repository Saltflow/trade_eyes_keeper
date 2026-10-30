# 仓库导航与 Pi 多轮助手验收

用户明确授权 DeepSeek 真实验收，并要求机器人熟悉整个业务仓库、能借助 pi/dsh
一类外部harness自动多轮回答。此次选用真实 Pi Agent core 0.87.1 作为可选后端，
保留现有Python模型客户端、业务工具与确认流程，不接入通用shell或代码写入工具。

## 原两问复测发现

获授权后的两问真实DeepSeek调用分别2.08秒、2.51秒完成。公告标题/时间/每股数量
和非实时采集的事实回答正确，但第二问引用了未读取的错误行号，未算完整验收通过。
因此本轮同时加入引用范围校验、行号化源码证据及一次自动修正，并为公告设置增加
精确只读配置视图，避免模型提出自己无法完成的“继续查询当前公告配置”。

## 能力及边界

- 仓库概览、文件分页、关键词与调用位置搜索、源码读取、Python定义AST索引。
  不加载或执行业务源码。配置样例与代码默认值不冒充运行值。
- 只读范围涵盖源码、脚本、测试、说明、模板、配置加载代码及Pi桥本身；排除凭证、
  真实私有配置、运行数据、操作档案、Git内部与依赖目录；文件/目录链接和硬链接拒绝。
- Pi仅有Python注册工具。stdin/stdout传模型消息与工具请求，密钥不进入子进程，
  不启动CLI、插件发现或额外模型HTTP。真实配置修改与研究执行仍由服务端确认。
- 两后端共用轮数、调用数、重复查询、上下文压缩、时间和引用校验；不自动后端重试。
  来源校验验证已读路径/范围，不能证明模型语义结论必然正确。
- HTTP超时遵循连接/读取机制；总预算在调用前后检查，并独立清理Pi进程。

## 依赖取舍

服务器Node22.22.1满足Pi要求，PATH没有pi/dsh CLI。本次安装项目局部锁定依赖即可。
当前dsh Python SDK要求Pydantic>=2.12，而项目要求<2，且sdk-minimal默认带shell与
额外会话上传插件；本次没有安装dsh或改变Python环境。

官方依据：[Pi Agent API](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、
[SDK依赖](https://github.com/earendil-works/pi/blob/main/packages/agent/package.json)、
[dsh Python依赖](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/pyproject.toml)、
[dsh最小profile](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/cordis.patch.yml)。

## 验收记录

- Node实际子进程协议测试16项通过，包含服务全部14个工具schema、未知工具、错类型、
  嵌套值、轮数/次数上限、协议大小与取消。
- Python→真实Pi桥及预算/引用/进程清理测试9项通过。
- 公告配置测试88项通过；仓库工具44项通过，2项Windows真实符号链接用例跳过。
- 完整 `tests/interactive/` 与 `tests/validation/`：491 passed、7 skipped（Windows
  链接权限与既有环境条件）；上述Pi Python子进程用例实际运行，没有因Node跳过。
  Ruff及范围内diff-check通过。

## 线上部署（2026-09-28 21:23，Asia/Shanghai）

- 仅发布6个助手Python文件与6个Pi项目文件，项目局部执行锁定依赖安装；服务器
  Node22.22.1，真实子进程协议测试与 `bridge.mjs --check` 退出码均为0。
- 仅修改 `interactive.feishu.assistant.harness_backend: pi` 与
  `max_tool_rounds: 12`；其他配置语义保持一致，225个受保护文件哈希不变。
- 服务重启一次，MainPID由433845变为434705，NRestarts=0；新心跳ready/fresh，
  调度器与飞书线程存活。没有更改Python依赖、全局安装或发送测试飞书消息。
- 备份：`/root/trade_eyes_keeper/data/deployments/feishu_pi_20260928T132246Z_b65dbf06/backup`。
  本地脱敏证据：`temp_test_cache/feishu_docs_20260928/pi_deployment.json`。
- 真实Pi/DeepSeek多轮问答见下；服务就绪不等于实际聊天验收。

## 真实模型第一轮复核

- 使用独立SQLite与捕获回复函数，实际走已部署Pi和DeepSeek API，读取部署仓库及
  开放配置；测试拒绝变更提案、研究执行和飞书外发。
- 原公告两问分别6.78秒、5.63秒，各3次模型调用，实际使用配置查询和源码读取。
- 日报调用链18.06秒、12次模型调用，读取调度器、main及通知模块；缓存问题26.49秒、
  12次模型调用，跨cache_manager/data_source/data_fetcher/调用方和测试定位。
  后两问触及预算并明确列出未读到的环节，不代表整仓库所有结论均已验证。
- 四问均完成Pi循环，无引用范围拒绝；236条事件、4轮问答留档，SQLite完整性ok，
  无执行提案，无遗留Pi子进程，受保护文件哈希不变。
- 人工复核仍发现前两问末尾对只读公告字段承诺“可生成差异预览”，不作为最终通过。
  系统提示新增只读能力边界、未开放字段不可承诺查询、解释问题不推销变更及完整
  路径行号引用要求。此提示补丁的service/harness/validation回归62项通过，Ruff通过。
  补丁上线及复测结果见下。

## 提示补丁及最终问答复测（2026-09-28 21:36起）

- 仅替换 `service.py`，AST比对确认仅SYSTEM_PROMPT新增4行；未改配置或重装依赖。
  237项保护文件哈希保持一致。服务再次受控重启，PID434977，NRestarts=0，
  ready/fresh且调度器、飞书线程存活。两次发布分别重启一次，没有自动重启循环。
- 补丁备份：`/root/trade_eyes_keeper/data/deployments/feishu_pi_prompt_20260928T133631Z_a94e353a/backup`。
- 原公告第一问7.03秒/3次模型调用，第二问4.16秒/2次模型调用；真实读取部署源码、
  公告文档和当前配置。筛选窗口、37个标题关键词、每股5/3条、单页抓取与非实时局限
  回答有依据，不再承诺为只读公告字段生成修改预览。
- 独立问题“你能直接查当前 storage.cache_days 吗？”4.16秒/3次模型调用。
  模型先按前缀再查全部开放字段，明确当前工具未开放该实时值，未用源码默认值替代，
  也未把工具不可见等同于业务不存在。
- 三问均完成真实Pi循环，无引用范围拒绝；新增3轮/57条事件落入隔离SQLite，
  integrity_check=ok、actions=0。测试所属Pi进程全部退出，受保护文件未变。
- 本地证据：`temp_test_cache/feishu_docs_20260928/pi_prompt_deployment.json`、
  `real_pi_prompt_qa.json`。完整远端问答证据目录为补丁部署目录下
  `real_pi_qa_ee97bfb8/`，包含隔离SQLite、模型输入和结果；按主机权限保管。
- 最终健康检查 `pi_prompt_final_status.json`：仍为PID434977，源哈希匹配、配置
  未变，心跳与飞书线程正常，原4条receipt完整，生产SQLite完整性正常，无遗留Pi。
  人工语义复核记录在 `pi_prompt_semantic_review.json`。
- 仍有少量引用以相邻路径加简写L行号表达；自动检查只覆盖可识别的完整路径引用，
  不能将“无引用拒绝”解释为对每个语义断言的完整证明。
- 残留语义局限：第一问把 `max_llm_calls_per_run=30` 简称为整轮调用上限，
  但实际 `_enrich_announcements` 的计数按调用重置且仅计成功提取；该配置值
  不能证明全报告全局最多30次请求。本轮没有修改公告算法，不能宣称回答绝无误差。
- 此次真实模型问答使用捕获发送端，未通过飞书客户端接收/发送新消息；不能声称完成
  新版端到端飞书聊天验收。旧版本漏记正文无法追溯补齐，新问答留档从部署后生效。
