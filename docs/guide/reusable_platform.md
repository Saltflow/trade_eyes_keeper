# 同机共享资料与可复用执行库

2026-11-01。本机代码已接入统一边界；生产环境尚未发布本轮改动。开发与生产是两套独立单机部署，各自保有凭据、队列、授权和账本，不共享活动SQLite。新加坡/日本及中国大陆服务器验收是后续待办，不是本轮已支持的新市场。

## 模块与唯一事实源

| 能力 | 实际实现 | 原路径兼容 |
|---|---|---|
| 明确的部署权限域 | `src/shared_contracts/authority.py`，主项目默认组合在`src/platform_composition.py` | 可通过storage配置或环境变量注入 |
| 通用持久任务执行 | `src/task_runtime/execution.py` | `src.core.execution`指向同一个模块对象，未保留第二份执行器 |
| 源站访问状态 | `src/task_runtime/request_policy.py` | 原SQL/文件限流状态只读导入，不能通过新入口清空封禁 |
| 原件、中立解析、行情原响应 | `src/data_hub/` | 旧catalog、公告、研究/V2/API准备均通过受校验引用/共享解析接入 |
| 模型副作用与账本 | `src/model_gateway/` | 旧summary/peer/API/V2及新研究使用同一准入与真实attempt账本 |
| Pi/native工具循环 | `src/agent_runtime/` | `interactive/assistant/harness.py`只注入桥位置、工具和渠道错误策略 |
| 财务领域编排 | `src/research_pipeline/` | 仍是一份领域实现；不复制为第二套“financial_research”源码 |

公共库不反向导入财务、Bot、脚本或项目组合层。资料库唯一拥有原件索引和中立解析；模型网关唯一拥有实际模型副作用证据，领域库原账本保留为业务记录/投影和历史读取。费用不能把两处投影相加。领域合同、prompt与输出正文不由通用库修改。

## 配置同一权限域

默认正式运行使用本项目的`data/shared_platform`。同机另一个受信任项目要复用时，必须显式指向相同绝对目录：

```yaml
storage:
  authority_root: E:/SharedData/trade-eyes-platform
```

也可设置`TRADE_EYES_AUTHORITY_ROOT`。优先级为显式调用参数、storage配置、环境变量、主项目默认。公共库只接受明确的`AuthorityRef`，不从当前工作目录推断项目。

研究入口可以直接指定：

```powershell
python -X utf8 scripts/financial_pipeline.py run --company a_share:600938 --start-year 2022 --end-year 2025 --authority-root E:/SharedData/trade-eyes-platform --output data/research/my-plan
```

该命令没有`--paid`，只准备计划和核查本地缓存，不构成模型执行授权。真正执行仍需要`--paid`和明确`--budget-limit-usd`。源码迁移不会给历史研究追加预算。

`status`快照会展示`authority_id/root/materials_root/execution_db/requests_db/models_db/isolated`。不能只看代码版本一样就认定共享。为兼容既有隔离调用，显式非默认`--store-root`且未指定authority时，会使用该store下独立的`platform_authority`并标为isolated；跨项目共享应显式指定`--authority-root`。绝对authority身份在准备时写入spec，冻结worker运行目录改变后仍使用原身份。

同一个根的manifest身份、scope或隔离标记冲突时，禁止静默重写。scope分离资料及模型证据命名空间，但账户容量和访问限制仍共享。它是同信任域SDK隔离，不是跨OS用户的ACL；互不信任的项目不得直接得到数据库/凭据写权限。

新研究的真实执行队列使用`authority.execution_db`；各研究目录的`research.sqlite3`只保留领域状态与费用投影。`status.execution_store`展示实际队列路径。首次绑定要求旧队列没有存活owner或未释放claim；旧任务不搬迁、不重新领取。运行中的旧版本必须先停止派发并收齐在途，不能靠切换目录分割并发额度。

## 读取资料，不重建另一套缓存

```python
from pathlib import Path
from src.shared_contracts import AuthorityRef
from src.data_hub import DataHub

authority = AuthorityRef(Path("E:/SharedData/trade-eyes-platform"))
hub = DataHub(authority)
# 回调只实现来源协议；资料身份、原件封存、scope及解析由hub维护。
```

`ensure_original/register_original/ensure_parsed`接口以实际签名为准。已有旧原件可验哈希后引用原路径，不复制大文件；已有完整解析可只读登记。正式DatasetCatalog保存material-ref，读取必须回到配置的authority索引核对身份、根、scope和SHA，不接受manifest自报任意文件路径。兼容外部旧catalog的显式导出副本不成为新事实源。

预览截断属于`preview`视图，不写回完整材料。中立解析保留全文、页/DOM结构和必要稀疏页图片。明确新解析规格产生新rendition；已有成功、失败或unknown不会因为换项目重新解析/发送。

同官方版本/已知URL别名只有一个获取producer；下载后同SHA归并。明确新修订不能无证据套用无版本旧原件。两个未知URL内容是否相同，必须看到实际字节后才能判断。HTTP401/403与429、访问验证、已有共享阻断继续生效；局部404等缺口不拖停其他资料。

下载producer和解析producer分别使用公共`material_download=3`、`material_parse=1`通道；缓存命中不创建队列任务。每次只注册当次可执行handler，不自动领取历史遗留任务。超时后仍活着的handler保留容量，直到真正返回或其进程身份被证实死亡。

PIT既有CNINFO/HK财务布局算法仍是显式版本rendition，未替换为另一套数值计算。旧values-only记录可只读导入，并标明没有对应原件证据；不能为填补该溯源缺口自动重下载。默认元数据HTTP也经过同一源站策略；`AuthorityHTTP`禁止自动重定向，需要重定向的适配器必须逐跳校验并准入，不得传`allow_redirects=True`绕过目标源限制。

腾讯行情的实时视图、标的画像、ETF和筛选现在共用协议解析与源站策略。明确要求新鲜行情时可以重新获取快照，不设置隐藏的长期TTL；相同原响应SHA只存/解析一次。证券源时间、字段索引、币种和不同业务估值口径仍由各自视图校验，抓取时间不能冒充交易时间。

## 模型、费用与Pi

研究与维修共用authority中的模型容量，较宽松调用者不能提高已持久化上限。新旧入口的每个实际模型发送都先持久领取intent；中断且已发的结果保留unknown，不因换producer/项目重发。成功capture可跨领域Store复用；复用者不重复计费。未知费用与预留不写成实际消费。

领域预算仍是原run授权，不能用迁移重置。普通助手有独立的单次`request_budget_usd`技术额度/预留（默认US$1），不会继承金融研究授权。若其未配置可核查费率，仍记录usage、费用标`pricing_unavailable`未知，不按零计；该预留不能当实际扣费。当前金融新编排继续经过既有实时核价门槛，旧阶段兼容入口保留其冻结费率合同，不冒称已经重新实时核价。

旧普通助手`request_retries/image_request_retries`配置仍可读取，但不再自动重发已发送的模型请求；格式/截断/传输失败保留原响应及诊断，不自动续写或写入持仓。需要重新发起是新的显式业务决定，不能在网关层自动换intent绕过unknown。真实HTTP原字节与只提供JSON的适配器记录有不同capture_kind，不把后者称为原始HTTP字节。

Pi工具循环只接受注入的ModelPort、ToolRegistry、事件和策略；凭据与工具真实执行属于Python宿主。Feishu适配器保留原会话权限、检索要求和可重复状态查询例外。维修仍是候选→宿主测试→人工确认→新版本，不会因为通用库拆分获得shell、改预算、改财务prompt或自动应用权限。

## 跨项目导出

```powershell
python -X utf8 scripts/export_reusable_platform.py --namespace my_platform --output outputs/my-platform-source
```

输出必须是新目录，不能覆盖。只导出五个公共包、依赖说明、README及源码SHA清单；不会带入`.env`、Bot桥、金融合同、研究资料、SQLite或模型原响应，也不会安装依赖。导出后使用`my_platform.data_hub`等命名空间，不依赖另一个项目也叫`src`。

金融领域SDK仍从`src.research_pipeline`使用`ResearchSpec/start_research/run_research/get_status/cancel_research/resume_research`。它需要对应财务合同/来源插件与领域组件，故不混入纯执行/通用agent源包。需要迁移完整金融产品时应另带显式金融配置和经授权的资料导入，而非复制整个开发目录。

本轮没有生产发布。未来部署必须带上完整的新库及兼容入口；不能只覆盖`src/core/execution.py`这个shim文件。现有历史freeze、request、raw、usage和研究正文保持原字节；部署、付费运行和跨机器资料搬迁各自单独授权。
