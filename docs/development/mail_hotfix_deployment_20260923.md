# 2026-09-23 邮件修复上线与参考池无损迁移

用户明确授权部署和无损迁移后，于北京时间 **2026-09-23 22:53:34** 完成。

## 上线结果

- 基于服务器 `8bd9484a11faa27aea510317bef0ef7f2c597ae8` 应用本轮 19 文件补丁，
  未提交或推送 Git，未混入本地其他研究与配置修改。
- 日报策略信号明细、缺价估值、凌晨交易日截止和优化通知诊断修复已上线。
- 三池旧执行合同迁移成功，只更新 `execution_hash`。逐字段比较确认现金、
  持仓、成交、分红、拆股处理事件、策略、参数和起始日全部保留。
- 策略激活指针及两份生产配置保持原哈希不变。没有手工调仓、重置账户或激活
  新策略，没有发送测试邮件或 Bot 消息。
- `trade-eyes.service` 恢复为 active/running，新 MainPID `377663` 与新鲜心跳
  一致，scheduler 与 Feishu 线程运行。停止请求至服务就绪约 7.5 秒。

## 验证

- 本地修复组合回归：292 passed。
- 服务器原版本的隔离副本：212 passed（包含 `tests/validation/`）。两批有重叠，
  不累计成独立测试数。
- 独立验收于北京时间 22:56:55 确认服务同 PID 已稳定运行超过 201 秒，心跳
  年龄 1.55 秒；启动后日志无错误，`NRestarts=0`。
- 生产迁移前 dry-run 三池均 eligible，迁移后三池均 already_current；账户
  除合同指纹之外的全部字段与原始备份一致。
- 生产只读报价验证覆盖全部现有持仓：00883、QQQ、VOO 均有有效报价，三池
  `valuation_complete=true`、`missing_price_codes=[]`；验证前后账户文件未变。
- 生产交易日历实测：当晚 A/HK 已收盘，截止日为 9 月 23 日；美股尚在交易，
  截止日正确取 9 月 22 日。
- 日历包及三个缺失的纯 Python 依赖均与 PyPI 官方 SHA-256 核对后离线安装。
  原有 NumPy 2.2.6、Pandas 2.3.3 和 tzdata 2025.3 未升级。

## 备份与审计

服务器部署目录（仅本机管理员可访问）：
`/root/trade_eyes_keeper/data/deployments/mail_hotfix_20260923T144531Z/`

- `backup/`：原代码、生产配置和三池原始账户文件。
- `deployment.json`：步骤、依赖变更、前后哈希和就绪证明。
- `migration_applied.json`：三池迁移回执。
- `remote_hotfix.py rollback`：显式恢复入口；恢复前需重新核对生产账户状态，
  有迁移以外的变化时拒绝覆盖，不能用旧备份覆盖后续正常成交。
- 独立迁移审计：
  `/root/trade_eyes_keeper/data/reference_migrations/20260923T145331524122Z-e738a46d/audit.json`。

补丁 SHA-256：
`9b082df99133b3274fad1a278a932a90191effac667ffb79d1acb56a6ad52864`。
本地详细证据位于被 Git 忽略的 `temp_test_cache/mail_server_audit_20260923/`。

## 验收边界

本次解除的是已确认的旧合同绑定停单，后续参考调仓仍须通过既有数据与策略
检查。Yahoo 403/DNS 与部分 ETF 长历史不足仍是新策略优化的数据阻塞。
没有执行新的全配置池优化，也没有发出新邮件以验收 Gmail 投递后的实际显示。
图片按用户确认保留原实现，Gmail 放行后可见。
