# 飞书持仓监控：隔离测试入口

更新时间：2026-10-29。本机 `TradeEyesPersonalMonitorTest` 已按用户授权注销，匹配测试进程和任务均为零；数据库、旧源码及 Task XML 均已备份，不再自动拉起。生产公共层为 v1.18.0（6a55894，构建 b7e30404）。详见[退役记录](../development/shared_runtime_retirement_20261029.md)。

测试群业务入口必须显式使用 `--host-runtime`，接入 `RuntimeService` 与公共执行器；旧独立模型循环、旧消费入口、专用监督器及安装脚本已删除。本页命令仅供今后获准的隔离测试参考，不表示当前任务在运行，也不是重新启动授权。

复用现有应用时使用单独测试群的 API 历史轮询；独立测试应用可用自己的长连接。配置及存储必须隔离，原生产 App 不得再开第二条 WebSocket。运行健康需核对实际进程出生身份、owner 和新鲜心跳，不能仅看旧 JSON 的 running 字段。

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

## 新建测试应用：独立私聊与长连接

以下为另一条可选路径。新应用与生产App不同，本机运行长连接接收器，无需服务器或公网端口。


## 1. 创建新的应用

1. 打开[飞书开发者后台](https://open.feishu.cn/app)，创建**企业自建应用**，例如“持仓监控测试”。不要创建群 Webhook 自定义机器人。
2. 添加“机器人”能力。
3. 在“凭证与基础信息”中查看新应用的 App ID、App Secret。只把 App ID 写入后面的测试 YAML；App Secret 仅放在本机专用环境变量中。
4. 权限管理申请应用身份权限：

   | 用途 | 权限 |
   | --- | --- |
   | 接收你发给机器人的私聊 | `im:message.p2p_msg:readonly` |
   | 以机器人身份回复 | `im:message:send_as_bot` |
   | 读取收到消息中的资源 | `im:message:readonly` |
   | 图片／文件资源能力 | `im:resource` |

   资源接口具体的任选权限要求以[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)页面和控制台当前提示为准。这套测试只接收私聊，不申请读取全部群聊消息或通讯录权限。
5. “事件与回调”选择**使用长连接接收事件**，添加 `im.message.receive_v1`。如果控制台要求先建立连接，先完成步骤 2，运行只读发现命令，保持终端运行，再返回控制台保存。
6. 创建版本并发布，**可用范围只选择自己**。若租户要求管理员审核，等待该测试版本审核通过。不要选择全员，不把机器人加入业务群。

官方依据：[长连接配置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)、[接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[应用发布与可用范围](https://open.feishu.cn/document/client-docs/h5/development-guide/step-4)。页面菜单名称可能随飞书控制台更新调整；配置原则是新 App、机器人能力、私聊事件、长连接、仅本人可用。

## 2. 创建本机测试配置

在仓库根目录用 PowerShell 执行：

```powershell
Copy-Item config/personal_monitor_feishu_test.yaml.example config/personal_monitor_feishu_test.local.yaml
```

打开新文件，把 `app_id` 改为新测试应用的 App ID，暂时保留两个白名单为空。这里不要填写生产 App ID，也不要复制生产配置。

用隐藏输入在当前 PowerShell 会话设置专用凭证，避免把密钥明文留进命令历史：

```powershell
$testSecretInput = Read-Host '新测试应用 App Secret' -AsSecureString
$env:PERSONAL_MONITOR_TEST_FEISHU_APP_SECRET = [System.Net.NetworkCredential]::new('', $testSecretInput).Password
Remove-Variable testSecretInput
```

仅设置此测试环境变量不会修改生产 `config/.env`。脚本也不会调用 `load_dotenv`。它只读取本地生产文件中的 App ID 做拒绝判等，不输出生产凭证。

## 3. 只读获取私聊 ID

先检查配置，再连接新测试应用：

```powershell
python -X utf8 scripts/run_personal_monitor_feishu_test.py --config config/personal_monitor_feishu_test.local.yaml --discover-chat --check
python -X utf8 scripts/run_personal_monitor_feishu_test.py --config config/personal_monitor_feishu_test.local.yaml --discover-chat
```

在飞书搜索新测试机器人并打开**私聊**，给它发“测试”。终端应只打印：

```json
{"chat_id": "oc_...", "sender_open_id": "ou_..."}
```

此模式只接收私聊并打印这两个 ID：不识图、不调用模型、不回复消息、不创建数据库、不启动个人监控时钟。群消息会被忽略。按 `Ctrl+C` 停止后，将打印的 ID 填进 YAML：

```yaml
allowed_chat_ids: ["oc_你的测试私聊ID"]
allowed_sender_ids: ["ou_你自己的openID"]
```

如果一直收不到事件，先核对新版本已发布、可用范围包含自己、机器人能力开启、`im.message.receive_v1` 已订阅、当前选择长连接，并确认没有第二个程序连接同一测试 App。不要通过连接生产 App 排查。

## 4. 业务验收入口

独立测试桥的模型、持仓和提醒执行循环已退役。此脚本仅保留上面的只读发现模式，普通模式会在建立连接或创建数据库前拒绝启动。业务验收必须通过公共 RuntimeService 和执行器；群测试使用前面的显式 --host-runtime 入口，独立 App 使用已审阅的隔离宿主 profile，不再另起一套桥。

历史测试数据库和截图保持原位置，不因退役删除，不自动导入生产。本次不恢复本机机器人，也不新增真实消息或邮件。
