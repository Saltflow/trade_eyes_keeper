# 飞书持仓监控：隔离测试入口

更新时间：2026-10-29。本机 `TradeEyesPersonalMonitorTest` 已按用户授权注销，匹配测试进程和任务均为零；数据库、旧源码及 Task XML 均已备份，不再自动拉起。生产公共层为 v1.18.0（6a55894，构建 b7e30404）。详见[退役记录](../development/shared_runtime_retirement_20261029.md)。

测试群业务入口必须显式使用 `--host-runtime`，接入 `RuntimeService` 与公共执行器；旧独立模型循环、旧消费入口、专用监督器及安装脚本已删除。本页命令仅供今后获准的隔离测试参考，不表示当前任务在运行，也不是重新启动授权。

复用现有应用时使用单独测试群的 API 历史轮询；业务统一通过公共宿主接入。配置及存储必须隔离，原生产 App 不得再开第二条 WebSocket。运行健康需核对实际进程出生身份、owner 和新鲜心跳，不能仅看旧 JSON 的 running 字段。

## 复用现有机器人：测试群 API 轮询

不要用生产 App 再开第二条长连接：同一应用的多个长连接会竞争接收事件，白名单不能隔离平台分发。此入口只对显式配置的一个测试群调用消息查询 API，不修改生产服务、配置或事件订阅。

前置条件：机器人已加入测试群；应用现有消息读取权限之外，还须具备 `im:message.group_msg`。在飞书开发者后台的当前应用中搜索该权限、申请开通并按提示发布生效。依据：[获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)。先用 `--probe` 实测权限，不能仅凭机器人已入群判定可以读取消息。

2026-10-24只读核查：用户提供的群详情读取成功；历史消息返回 HTTP400 / `230027`，明确缺 `im:message.group_msg`。服务器当前程序验证了群内非@、非斜杠消息会忽略。生产服务 active/running、心跳与PID一致、飞书线程存活。没有部署、重启、服务器文件写入或测试消息发送。

本次单群设置已保存在忽略提交的 `data/acceptance/personal_monitor_20261024/test_group.yaml`，仅包含群/发送者 ID及测试时点，不含密钥。通用模板为 `config/personal_monitor_feishu_group_test.yaml.example`。凭据在本机从既有配置的明确白名单字段读入内存；不会复制到测试YAML。

只读权限探针（不建持仓数据库、不调用模型、不发送消息）：

```powershell
python -X utf8 scripts/run_personal_monitor_feishu_group_test.py --config data/acceptance/personal_monitor_20261024/test_group.yaml --probe
```

权限通过后，正常入口还要求当天已核验的生产过滤证明，校验App、群和时点。证明过期需重新只读核查，不跳过此门槛：

```powershell
python -X utf8 scripts/run_personal_monitor_feishu_group_test.py --config data/acceptance/personal_monitor_20261024/test_group.yaml --production-filter-verified <当天重新只读核验的证明文件> --host-runtime
```

只处理配置群中白名单发送者的新消息，忽略机器人回复、删除/修改消息、@和斜杠命令。测试时不@机器人、不用`/`命令。正常模式先核对权限再创建独立状态，不会运行日报、优化、研究或传统命令。持仓、提醒、原消息时间、永久去重及分页进度均在独立测试库，重启保留；新库不会导入启动前的历史聊天。

本次单群及旧私聊入口回归67项通过，必跑validation36项通过，相关Ruff通过。真实本地probe再次确认群可读但history为230027，未建测试库或发送消息。

权限未通过时不要启动正常测试。准备好以后，在测试群发自然语言或脱敏持仓图核对回执、到期通知、纠错与重启去重。没有真实截图和实际可见通知，不能关闭对应人工验收。API轮询也不等于正式WebSocket入口已经实测。

## 已删除的私聊入口

2026-11-04，旧私聊发现launcher、专属配置示例及测试已删除，不再提供独立长连接启动命令。
群测试的公共宿主、权限探针、状态查询和停止入口继续保留；个人监控业务没有删除。
历史测试数据库、截图与既有验收记录保持原位置，不自动导入生产。
