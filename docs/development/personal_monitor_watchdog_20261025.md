# 本地测试机器人退出取证、监督与验收

> 历史监督器验收记录：专用监督器、安装脚本及旧独立业务循环已于 2026-10-29 按用户授权删除，本机任务注册也已注销。数据库及历史证据保留；下文命令仅为历史记录，不得据此重新安装或恢复任务。当前状态见[退役记录](shared_runtime_retirement_20261029.md)。

范围：本机用户 `one` 的独立测试群接收器；不部署生产。证据目录：`data/acceptance/personal_monitor_20261025/watchdog/`。

## 退出原因

直接机制已有同秒证据：旧接收器依附 Codex 执行会话25002，最后心跳为10月25日04:25:33.208；04:25:34.006该执行会话failed、exit_code=-1，没有Python traceback。同时宿主记录thread-state teardown、Shutdown、Agent loop exited。

因此高置信结论是接收器随宿主会话销毁而退出，原先没有独立监督或退出告警，状态文件留下旧running。谁/什么触发了宿主会话销毁，日志没有保留足够证据；不能把-1解释成具体OS信号。Windows相关时间段未见电源/休眠/重启、Python/Codex崩溃或Defender处置。Security进程审计无权限、计划任务Operational历史未开启。详见`forensics.json`。

## 当前实现

- Windows计划任务`TradeEyesPersonalMonitorTest`，当前用户one、Interactive/低权限、IgnoreNew、每分钟重复触发、失败后每分钟重启最多3次、无执行时长上限。实际任务父链是svchost/services/wininit，脱离Codex进程树。Interactive仅在该用户登录时可运行。
- OS任务启动`personal_monitor_watchdog.py --task-entry`，外层启动器为监督器单独保存stdout/stderr和实际退出码；即使监督器被强制结束，也把结果追加到`task_launcher_exit.jsonl`并返回给Windows。Python异常记录安全堆栈位置，启用faulthandler；不记录API密钥或消息正文。
- 监督器核对worker PID、创建时间、脚本及配置路径，匹配才接管/终止；子进程退出记录退出码和stderr路径。首次接管的旧进程没有Popen退出句柄，退出码可能未知，会明确记录null。
- worker异常或挂死按2/4/8…最多60秒退避恢复，10分钟内5次重启后冷却600秒；启动后30秒没有匹配状态则回收。单实例OS文件锁同时覆盖启动器、监督器和worker，重复worker在初始化状态/数据库之前拒绝。
- processing阶段独立900秒预算，普通无进展180秒；不会把正常长模型调用当成掉线。读取退避持续60秒时告警，进程继续重连，不进行重启风暴。隔离证明失效则停止接收和重启，等待真实只读复核，不自动延长证明。
- 健康告警只发显式测试群，不接收消息/不启动模型；即使隔离证明过期，也能发送“已安全停止”的系统健康告警。每个故障事件持久去重，恢复一次。明确failed退避补送；发送已提交但结果未知或崩溃时保留unknown，不盲重发。恢复后由恢复通知汇总未确认投递，已过时且确定未发送的掉线通知不再晚于恢复通知发送。
- `--stop`持久保存计划停止意图，下一次Windows触发也不复活；恢复需要明确清除此意图。不是靠删除状态或重新创建数据库去重。

## 现场验收结果

| 项目 | 结果及证据 |
|---|---|
| 原退出机制取证 | 通过，`forensics.json`；上游是谁触发宿主关闭仍未知 |
| worker强制终止 | 两次受控注入后均自动恢复；首次约13秒。掉线和恢复通知实际送达，`first_worker_fault.json`、`managed_worker_fault.json` |
| managed worker退出码 | 实测15，监督器持久记录退出码、PID、创建时间与stderr路径 |
| 监督器强制终止 | Windows下一轮自动恢复，安全接管存活worker；`supervisor_fault.json` |
| 外层退出日志与再次恢复 | 实测监督器退出码15被外层捕获；新监督器由Task Scheduler拉起，`wrapped_supervisor_fault.json`、本地`task_launcher_exit.jsonl` |
| 宿主生命周期隔离 | 真实父链为Windows服务进程，不是Codex；故障后无需本聊天命令重启。未退出用户当前Codex窗口来制造额外影响 |
| 真实到期投递 | 受控工具创建一分钟验收任务，跨worker重启后completed，outbox sent/attempts=1；提醒内容实际飞书读回。**不是伪造人工群消息，也不算自然语言入站创建验收** |
| 平台正文读回 | 4条worker掉线/恢复通知和1条到期提醒均以user_card_content读回；监督器恢复后的通知也单独记录 |
| 网络与不确定投递 | 自动故障注入覆盖429/5xx/退避、failed延迟、unknown不重发、恢复后的过时通知处理；未断开整机网络 |
| 生产只读复核 | 08:54:22 PID760395、同PID新鲜心跳、Feishu线程及HTTPS正常；处理器hash未变，非@非slash群消息仍ignored。SSH使用既有可信known_hosts和严格主机验证；未修改服务器 |

最终JUnit：`final.junit.xml`为152项通过，包含watchdog、单群接线和全部36项validation；追加来源修复后，`final_facts.junit.xml`为195项通过，涵盖Assistant工具接线、手动事实、跨消息与来源边界。相应源码/测试Ruff通过。各回归集有重叠，不累加。

## 持仓恢复及仍未完成

测试账户已按用户真实消息恢复持仓与账户版本。证券、数量、金额和原消息仅留本地验收证据；不反推汇率或市值。恢复保留原消息和失败回执，通过受控工具调用完成，不能算修复后自然对话入口自动重试已通过；相同来源不重复递增版本，回执已平台正文读回。

仍未完成：两笔逆回购的期限/到期日；港股截图遮挡值及币种估值；真实人工自然语言入站创建提醒；期望回报阈值的实际规则与所需研究输入；完整FeishuAssistant正式入口、全部真实图样本及历史批次分页。受控一分钟任务已验证真实到期投递，但不代替自然语言创建验收。

整机断电、休眠、注销或完全断网时，本机无法实时发送飞书通知。方案覆盖登录运行期间的进程退出及有界挂死；联网恢复后补报。没有部署独立外部主机心跳监控，不能承诺任何故障下永远在线。

## 状态和停止

```powershell
python -X utf8 scripts/personal_monitor_watchdog.py --config data/acceptance/personal_monitor_20261024/test_group.yaml --status
python -X utf8 scripts/personal_monitor_watchdog.py --config data/acceptance/personal_monitor_20261024/test_group.yaml --stop
```

状态命令检查监督器和worker真实身份、启动时间、阶段及心跳，不能把旧JSON的running当健康。独立库为`watchdog.sqlite3`，原始个人监控库不变。任务动作、工作目录、用户与重启设置已导出`task.xml`供复核。安装/更新本机用户任务使用`scripts/install_personal_monitor_watchdog.ps1`；不能修改为生产服务或第二条同App长连接。

## 补充修复：有来源的跨消息手动事实

PersonalTools保存经适配器认证的原消息ID、owner、chat、文本和时间；引用限同账户、同用户/会话、最近10条且15分钟内。明确“分别是”的数量与后一条代码按顺序一一关联，数量时间保留在原数量消息，不使用补代码时间刷新旧持仓。条件句、否定、更改/缺项、跨账户或跨日不自动拼接。模型提交的价格/市值/币种不作为手动持仓事实。日期只写10月25日时，仅允许复用本批先前已接受的真实时点；外部较新人工版本仍由CAS拒绝覆盖。证据a_confirmation_recovery.json、hk_fact_recovery.json、facts_final.json。
