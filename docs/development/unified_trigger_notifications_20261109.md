# 2026-11-09 唯一触发通知链路修复

## 事故证据

生产只读核对：01339 派息提醒由 11 月 4 日两轮请求“那01339下次派息到账日提醒我 一次性到时提醒”及“11月9日提醒我”创建。
工具选中了 personal_monitor_create_reminder，11 月 9 日 09:50:00（北京时间）生成个人 outbox，飞书确认后 reminder=completed。
对应管理触发器及 reports.daily 任务均未创建，因此没有进入 SMTP。
同一系统提示同时要求到期事件走 trigger_manage，又要求到期提醒走 personal_monitor；两套可执行工具和独立发送任务使错误选择真实落库。
另管理触发通知分支将 Telegram 固定为 None，并存在邮件独立路由开关，违反统一全渠道设计。

## 最终合同

所有定时、到期、条件通知只有 trigger_manage → 持久规则 → 既有 reports.daily/brief → NotifierManager 一条链路。
默认完整 daily，明确要求简报才使用 brief，条件证券不缩减配置监控池。
按 notification 总配置向所有启用渠道发送；邮件使用既有收件人，飞书使用原认证会话，Telegram 使用既有配置目的地。
每个启用渠道独立留下成功、失败、跳过或未知回执；不能以飞书成功代替全渠道成功。
删除个人提醒/规则/重发工具、立即提醒回复、MonitorEngine、PersonalMonitorRuntime、PersonalMonitorTasks。
保留持仓、截图、历史事实与回执，以及管理触发采集使用的时段函数。
删除 trigger_report_routes 特殊开关；持久绑定、配置目的地、会话授权、发送前撤销检查继续保留。

## 测试与发布边界

旧测试分别验证“个人会话提醒成功”及“已经选好 daily 的邮件发送”，没有验证唯一产品入口，甚至有断言启用邮件也不得构造的错误合同。
已移除退役行为测试，新增两轮认证请求到真实报告队列、旧工具拒绝、全池及幂等，以及三个真实通知适配器的分渠道回执验收。
模型输出与外部 SMTP/HTTP 在自动测试中受控替换；这些结果不宣称真实模型准确率或收件箱验收。
保留原始失败：本机默认 Node v22.17 不满足 Pi >=22.19；另旧持仓测试的临时业务根未安装 Pi 桥，已让夹具显式绑定实际桥。
线上迁移前个人账户/规则均为零，旧提醒仅一条 completed、outbox 仅一条 sent；没有待迁移提醒或旧执行任务。
发布只安装本次明确清单，停用历史 personal_monitor 模块，保留所有旧回执，不重放已发送消息。
发布证据位于 .tmp/unified_trigger_release_20261109，服务器候选及备份位于 data/operations/unified_trigger_20261109。

最终隔离候选相关全套回归：2200 passed，2 skipped，0 failed（105.55 秒）；覆盖 personal_monitor、interactive、triggers、notification、共享执行器、运行服务及 validation。
补充修复“提醒我决定是否滚存”被误拒绝的问题：仅放行提醒事项的局部疑问从句，能力询问、引用、否定和问号仍拒绝；包含实际创建不得落库的反例。
本次曾暴露隔离候选准备缺项（旧测试残留、.tmp 父目录、Node 路径及静态测试文件），均保留原始日志，并在补齐后完成上述最终整套回归。
