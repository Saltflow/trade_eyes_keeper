# 部署指南 — 五步上线

从头克隆到日报推送，全程 ~10 分钟。

---

## 第一步：服务器初始化 (1 分钟)

登录你的云服务器，执行一键脚本：

```bash
ssh root@你的服务器IP
bash <(curl -s https://raw.githubusercontent.com/用户名/仓库名/main/scripts/server_init.sh)
```

或者手动上传：

```bash
scp scripts/server_init.sh root@你的服务器IP:/tmp/
ssh root@你的服务器IP 'bash /tmp/server_init.sh'
```

脚本会自动安装：Python3、texlive (PDF 日报)、poppler-utils、screen。

---

## 第二步：配置 SSH 密钥 (1 分钟)

在**本地电脑**执行：

```bash
# 生成密钥 (无密码)
ssh-keygen -t ed25519 -f deploy_key -N ""

# 上传公钥到服务器
ssh-copy-id -i deploy_key.pub root@你的服务器IP
```

---

## 第三步：配置环境变量 (2 分钟)

```bash
# 复制配置模板
cp config/.env.example config/.env
cp config/config.yaml.example config/config.yaml
```

编辑 `config/.env`，修改这 4 项（必填）：

```bash
DEPLOY_HOST=你的服务器IP               # 必填
DEPLOY_SSH_REMOTE=ssh://root@你的IP/root/trade_eyes_keeper  # 必填
EMAIL_SENDER=your_email@yeah.net        # 必填
EMAIL_PASSWORD=你的邮箱授权码            # 必填 (不是登录密码！)
```

> **可选：飞书/Telegram 通知** — 启用后在群聊中实时接收日报和简报推送。
> 配置方法参考：[飞书 & Telegram 消息推送配置](feishu_telegram_setup.md)

编辑 `config/config.yaml`，修改：

```yaml
stocks: [601728, 000001, VOO]      # 你的股票列表
email:
  smtp_server: smtp.yeah.net       # 你的邮箱服务商
  smtp_port: 465
```

> **SMTP 授权码获取**：以 yeah.net 为例 — 登录网页邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 复制授权码。

---

## 第四步：首次部署 (3 分钟)

```bash
python ci_cd_deploy.py
```

脚本会自动：
1. 🌐 **前置检查** — 验证 `.env` 配置、SSH 密钥、网络连通性
2. 📦 **代码推送** — git push 到服务器
3. 🔧 **安装依赖** — 服务器自动 pip install
4. ✅ **系统测试** — 跑一次 `--once` 验证
5. 🕐 **迁移调度** — 清理当前项目旧 cron，按 YAML 注册日报、简报和显式启用的优化任务
6. ✅ **启动服务** — `trade-eyes.service` 管理调度与 Bot，并检查当前进程的本地心跳

---

## 第五步：验证 (1 分钟)

```bash
# 检查服务器状态
python ci_cd_deploy.py --mode investigate

# 在服务器项目目录查看服务状态
python main.py --status
systemctl status trade-eyes.service
```

服务就绪后按通知开关发送部署通知；禁用通知不会阻止部署。HTTP/HTTPS 管理服务已下线，
使用飞书/TG Bot 更灵活、更安全，无需开放管理端口，详见 [Bot 指南](feishu_telegram_setup.md)。

---

## 常见问题

| 问题 | 解决 |
|------|------|
| `SSH 密钥不存在` | 重新执行第二步生成密钥 |
| `无法 SSH 连接` | 检查 `DEPLOY_HOST` 是否正确、防火墙是否开放 22 端口 |
| `xelatex 未产出 PDF` | SSH 到服务器执行 `xelatex --version` 确认已安装 |
| `邮件发送失败` | 检查邮箱授权码是否正确 (不是登录密码) |
| `git push 报错` | SSH 到服务器执行 `cd /root/trade_eyes_keeper && git status` 查看状态 |
