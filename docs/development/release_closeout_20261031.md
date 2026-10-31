# v1.19.0 发布收尾复核（2026-10-31）

## 结论与范围

生产已于北京时间 **2026-10-31 00:37:48** 发布 `c51caaf` / v1.19.0。
本次在 **09:08—09:15** 只读复核，确认候选源码、实际磁盘构建、systemd 和公共宿主
一致，无需重复部署、重启或重新登记版本。本次没有修改生产文件、配置、数据库、
账户或策略指针，没有发送测试消息，也没有运行付费模型。

代码发布、宿主就绪及**同版本飞书 `/help` 真实消息往返检查已通过**：用户实际发送并
确认收到回复，生产同一 PID 的 HELP 分派与成功发送日志相互印证。
这只验收基础命令收发，不扩大为财报研究、附件、提醒或全量报告链路全部验收通过。
本记录不包含随后开发的跨股票实物分派实现或其发布。

## 当前生产证据

| 项目 | 本次结果 |
|---|---|
| 生产 Git HEAD / 登记源码 | `c51caaf01ad6769ec851a49fa83ab93ef592c783` |
| 发布版本 | `1.19.0`，`build_info.state=verified` |
| 构建 SHA-256 | `ec3ee80cbe6f095ee1ffc7fcd0160c9ad1bb027b723f115e3c4bfd01f1e77844` |
| 部署 ID | `20261031T003748325282+0800-ec3ee80c-4c9f1cea0d3e` |
| 实际磁盘与登记清单 | 新独立检查读取完整构建范围，`actual_build_matches_manifest=true` |
| 稳定文件审计 | 423 个审计文件与本地一致；修改 0、本地独有 0；远端独有仅 `.env` 和主运行配置 |
| systemd | `active/running`，MainPID `871668`，`NRestarts=0` |
| 当前宿主 | 同一 PID，`state=running`、`fresh=true`、`ready=true`、`failure=null` |
| 公共执行器 | `ready=true`、`running=true`、`active=[]`、failure 为空、等待时间 0 |
| 飞书接收 | 接收线程存活，`transport_ready=true` |
| 健康通知 | pending、unknown、sending、failed、exhausted 均为 0，当前 issues 为 0 |
| 旧健康服务 | `inactive/dead`、`masked`；不是另一个活动宿主 |
| 旧脚本注册/进程 | 本次审计未发现旧脚本的相关 cron 或在运行脚本引用 |
| 共享会话数据库 | 以 SQLite `mode=ro`、`query_only` 检查，`quick_check=ok` |

423 文件是本次源码、公开配置和冻结输入审计范围，并非全盘文件数。
既有发布回执中的 894 文件属于当时完整发布载荷，两个计数不可直接相减。
生产另有历史未跟踪测试文件，本次没有删除或改写；不将生产目录描述为 Git 全干净。

## 原审计误报

首次审计的 `disk_matches_registered_scope=false` 是审计脚本遗漏支持文件的口径错误。
旧公式仅选取 `main.py`、`pyproject.toml` 和 `src/`，而当前
`src/core/build_info.py` 已额外登记以下七项：

- `config/__init__.py`
- `requirements.txt`
- `tools/feishu-pi/bridge.mjs`
- `tools/feishu-pi/shared-runtime.mjs`
- `tools/feishu-pi/framing.mjs`
- `tools/feishu-pi/package.json`
- `tools/feishu-pi/package-lock.json`

旧审计的 `removed` 恰好为这七项，`modified=[]`、`added=[]`；七项实际文件均与本地
匹配。后续直接调用当前构建实现重新计算完整清单，确认磁盘与登记清单一致。
原始误报保留，不据此修改生产、补写清单或制造一次新发布。

## 既有发布回执与保护边界

只读核对生产目录
`data/deployments/integrated_v1_19_0_20261030T155358Z_c51caaf/`：

- `deployment_receipt.json`：状态 deployed，894 个载荷文件匹配，运行范围无额外文件，
  36 个明确退役路径已移除；启动前业务摘要不变。
- `final_verification.json`：新 PID 就绪、构建身份正确、新服务日志错误计数为 0；
  `.env`、主配置、三市场参考持仓及 `data/optimizer/latest_strategy.yaml` 的
  `protected_files_unchanged` 均为 true，重启后业务摘要一致。
- `acceptance.json`：既有服务器离线验收记录 4561 项、0 失败、0 错误、35 跳过，
  Python 应用源码编译和 Pi 预检通过。它是此前发布准备结果，不是本次重新运行。
- `cutover_database_snapshots/`：既有切换时的共享会话、个人监控 SQLite 快照仍在。
  本次不覆盖快照，也不把实时 WAL 数据库主文件哈希当作业务不变的证明。

本次记录了私有配置和参考持仓的当前哈希，未读取或保存凭据正文。
上述部署时保护结论来自原始回执；本次没有执行新的账户迁移或策略切换。
原回执的 `github_pushed=false` 只表示当时状态；当前推送状态应以本次 Git 远端检查
为准，不能据此误报发布仍未推送。

## 港股税后现金分红的既有生产状态

当前生产候选已包含统一税后现金分红实现。通过生产严格配置加载器读取三个市场配置，
均得到 `withholding_rates={a_share: 0.0, hk: 0.20, us: 0.0}`；这些是账户执行假设，
不表述为所有投资者适用的法定税率。相关配置与运行源码已纳入本次哈希核对。

因此该项的“等待人工发布决定”是旧状态，不能继续解释为税后分红代码尚未上生产。
完整业务合同回归结果由本轮专项验收单独记录；本次只读发布核对没有重跑优化器或
生成全量收益报告，不以配置存在替代分红账本各条路径的业务验收。
跨股票实物分派仍属于另一项功能，不能因现金分红已发布而视为已实现。

## 真实通道与任务验收边界

首次 09:11 以当前 `release.json.deployed_at` 为界只读统计共享数据库，未导出消息正文、
对话标识或密钥。以下统计保留首次快照时点，随后 `/help` 实测见下一节：

- 当前版本上线后 `actions`、`conversation_turns`、`conversation_deliveries`、
  `execution_runs`、`execution_health_outbox` 均没有新记录。
- 历史 conversation 为 9 completed、1 failed；历史 execution 为 12 succeeded、
  6 partial；健康 outbox 的 6 条 sent 均早于当前发布，不能移作新版本通过证据。
- 当前无 active 执行或排队等待；健康通知无未知投递、失败或待发送事项。
- 既有发布回执中的飞书认证与 Bot 元数据验证为 true，证明认证/身份接口检查，
  不能证明新版本已经处理过真实入站消息、回复或附件投递。
- 配置仍为 Asia/Shanghai、日报 19:00、优化 02:00 且优化启用；这是读取的配置，
  不冒充从宿主内存取得的下一次实际触发时间。

### 09:14 用户实发 `/help` 的同版本续验

用户随后明确确认已在飞书发送 `/help` 并收到回复。本次 09:15:54 只读读取既有
systemd 日志并核对新进程构建，得到以下北京时间事件：

| 事件 | 时间 | 同一服务 PID |
|---|---|---:|
| `command=HELP` 已分派 | 2026-10-31 09:14:36.632496 | 871668 |
| 飞书交互卡片发送成功（API `code=0` 分支） | 2026-10-31 09:14:37.751591 | 871668 |
| 传统命令入站路由 accepted | 2026-10-31 09:14:37.751815 | 871668 |

HELP 分派至成功发送相隔 1.119095 秒。读取时仍为相同 `c51caaf`、部署 ID、构建 SHA，
总体 ready、fresh、transport_ready 均为 true，failure 为 null。
日志没有供上述三条共同引用的请求 ID，因此机器侧采用同一版本/PID及时间邻接关联，
再结合用户确认接收；不伪称已按唯一平台消息 ID 全链追踪。
传统 `/help` 走命令分派路径，不要求它新增自然语言助手的 `conversation_turns`。

该真实入站和回复证据关闭本次发布的基础命令收发检查。模型问答、财报工具、文件附件、
个人提醒及完整报告链路仍按各自范围验收；不复用 `/help` 结果代替它们，也不重发历史
任务或启动第二个同 App 长连接。本次没有由代理发送新消息。

## 本次本地证据

- `.tmp/release_20261031/live.json`：稳定源码、服务和首轮构建审计；保留旧口径误报。
- `.tmp/release_20261031/remaining.json`：423 文件逐项一致及两个环境私有文件。
- `.tmp/release_20261031/closeout_remote.json`：完整构建范围重验、当前保护哈希、
  既有部署回执路径、旧健康服务及生产 Git 状态。
- `.tmp/release_20261031/channel_closeout_final.json`：总体 ready、执行器、只读数据库
  任务/投递统计、既有回执安全摘要、生产税率合同及调度配置。
- `.tmp/release_20261031/help_roundtrip_20261031.json`：同版本/PID的 HELP、成功回复及
  入站 accepted 事件时间；只保存事件类别与时间，不保存正文或完整用户/会话标识。

这些是本机检查证据；Git 跟踪的本记录只保存结论、范围和定位，不包含凭据或消息正文。

### 10:40 只读复核

本地开发期间再次读取生产，仍为上述部署ID、构建SHA及PID871668，systemd
active/running、NRestarts=0，总体ready/fresh/transport_ready为true。与09:08保护快照
可直接配对的主配置、环境文件及三市场参考持仓5项哈希均未改变。当前策略指针另记录
哈希，不将缺少同口径前值的一项冒称本次前后比对通过。证据为
`.tmp/release_20261031/final_host.json`。没有重启、部署或发送新消息。
