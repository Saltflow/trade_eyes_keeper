# 服务器数据回收与中证 1000 五年补数

用户要求按当前时间节点回收服务器已有数据，并补齐当前中证 1000 成分股近五年
可取得的真实数据。本任务固定评估日为 **2026-09-21**，窗口为
**2021-09-21 至 2026-09-21**。使用原价、前复权行情、公司行动、带披露日期的
财报及官方年度报告补充字段；不修改生产策略或激活数据指针。

## 数据身份与位置

- 官方清单：https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000852cons.xls
- 成分表内日期：2026-09-21；1000 家，深交所 529 家、上交所 471 家。
- 原件 213,504 字节，SHA-256：
  `5cd9059c4b6cd2ca4907fa38f4ab2b2d5d913e60ecc3932e7ab6892a5274346c`。
- 清单与任务配置：`data/reference_universe/csi1000_20260921/`。
- 补数输出：`data/reference_universe/csi1000_20260921/dataset/`。
- 服务器回收：`data/server_imports/20260921/dataset/`，按原目录结构隔离保存。
- 可机读验收和预算：`data/analysis/data_acquisition_20260921/`。

这是一份当前成分股固定样本；历史行情不意味着具有历史成分股资格。上市不足
五年的公司只保存实际上市后的数据，不填造上市前行情。日线截至来源实际提供的
最后交易日，并单独保留实际起止日期。

## 资源与时间预算

| 项目 | 实测或估算 | 依据 |
| --- | --- | --- |
| 服务器快照 | 6,252 文件，200,088,417 字节（190.82 MiB），741 个去重标的 | 白名单数据目录逐文件盘点；729 个规范行情标的、669 个财报标的，另有普通历史/缓存 |
| 压缩传输 | 59,646,902 字节（56.88 MiB） | 后台压缩实际结果，耗时 5.405 秒 |
| 服务器余量 | 约 9.64 GiB 磁盘，1.06 GiB 可用内存 | 启动前实测；2 CPU |
| 本地余量 | 约 1,675 GiB 磁盘，23 GiB 可用内存 | 启动前实测 |
| 中证 1000 结构化输出 | 约 0.2–0.35 GiB，建议总预留 2 GiB | 每行约 115–123 字节，含财报、行动、元数据及导入归档 |
| 全池采集（原始估算） | 基础外推约 19 小时，运行预算 24–48 小时 | 原两路试采耗时 107.033 / 162.852 秒；该估算未包含后来补上的 Baostock 串行、限速及预算，不可继续当作新任务保证 |

原始 PDF 由现有供应商在请求时解析，任务保存结构化记录、来源 URL 和解析缓存，
不保留整套 PDF 原件。下载流量大于最终落盘大小，当前没有足够测量给出可靠流量
总值。源站限流、长时间不可用会延长耗时；检查点支持后续续跑。

服务器只承担短时低优先级导出，长任务在本地运行。每股单独进程释放 PDF 解析
内存；每次 1200 秒超时、每股最多两次尝试。2026-09-22 起推荐单工作进程续跑；
Baostock 在所有工作进程间串行连接，协议请求间隔 2 秒、每日预算 40,000 次，
失败指数退避，遇黑名单立即暂停。配置和解封检查见
[Baostock 访问控制](baostock_access.md)。

## 运行及续跑

本次已取得的两股试采数据与全池使用相同清单、窗口、配置指纹和输出路径，全池
将复用这两个有效检查点。试采仅用于数据连接和资源测量，不是策略收益验收。

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python scripts/run_reference_universe_isolated.py `
  --config data/reference_universe/csi1000_20260921/acquisition_config.yaml `
  --manifest data/reference_universe/csi1000_20260921/index_constituents_20260921_csi_1000.json `
  --output-dir data/reference_universe/csi1000_20260921/dataset `
  --evaluation-date 2026-09-21 --history-years 5 --official-pdf-recent-years 5 `
  --workers 1 --attempts-per-run 2 --company-timeout-seconds 1200
```

同命令可续跑；持有数据目录互斥锁时会拒绝第二个监督进程。黑名单必须先通过
官方状态检查确认解除，且本地额度可用。重试缺失财报会校验并复用同窗口行情，
不重新下载有效行情。封禁时报告为 `source_blocked`，不会创建下载子进程。
查看本次完整批次状态：

```powershell
Get-Content data/reference_universe/csi1000_20260921/dataset/reference_batches/20260921_73ef5673cdfe/supervisor_progress.json
Get-Content data/server_imports/20260921/transfer_status.json
```

`supervisor_progress.json` 包含监督进程 PID、活动公司及子进程 PID、完成/失败/复用
数量。后台 stdout/stderr、PID 与启动命令保存在清单目录。机器应保持开机且联网；
进程在睡眠时无法继续采集，重新启动后用上述命令续跑。

2026-09-21 21:45 的实机回执位于
`data/analysis/data_acquisition_20260921/background_receipt.json`：
服务器导出 PID 345469 已完成；本地传输 PID 60216 正在续传，中证 1000 监督进程
PID 24928 已复用两条检查点并新增完成一家公司。两个本地进程的启动终端均已退出，
进程继续运行。此处 PID 和计数只是启动时快照，当前状态以 JSON 进度文件为准。

回归验证：`tests/validation/`、清单刷新、快照校验、批量服务及隔离监督器合计
**104 passed，5 skipped**；跳过项为 Windows 未授予符号链接创建权限的测试。
归档链接拒绝、真实 NTFS 临时硬链接越界保护、校验失败恢复和有限重试均已通过。
相关脚本与测试的 Ruff 检查通过。

2026-09-22 的限流修复验收：原批次指纹未变，保留 931 成功、69 待补财报；
69 份行情全部可复用。封禁预检创建 0 个子进程，4,931 个数据及单股检查点文件的
哈希未变。此时任务已暂停，旧 `supervisor.pid` 与启动记录不代表进程仍在运行。
迁移日前的接口用量未记录，当天预算保守冻结至北京时间次日；预计解封时间也
不会触发自动重启。细节与回执路径见上面的访问控制文档。

## 验收边界

- 服务器快照需同时通过归档 SHA-256、清单 SHA-256 及全部文件大小/哈希验证，
  `transfer_status.state=complete` 才表示已回收到本地。
- 现有旧数据包含未知公司行动及分红公告日期缺失；不得为了显示完成而猜测日期。
- 下载 `success` 与严格回测可用不同。两股试采都有 1212 条日线及财报记录，
  但单只字段覆盖和公司行动解释仍需审计；全池结束后保留失败、过期和缺字段名单。
- 全池检查点须与 1000 家清单逐一对应。`latest_reference_backfill.json` 在采集
  期间可能是子进程的单股报告，不得用其 `complete` 判断全池已经完成。
- 原快照和本地 H1 补充保持独立，不将不同归一化日期的 qfq 价格直接拼接。
