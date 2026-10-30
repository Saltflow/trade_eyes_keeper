### 日报增加监控港股公告（2026-10-30，工作区实现）

- 后续美股排查：本地和生产的 `VOO`、`BRK.B` 均仍在监控清单，但旧公告入口
  没有字母代码分支，直接跳过；不是此次港股改动删除了标的。新增 SEC 通道，
  BRK.B/BRK-B 自动转换，普通公司按 CIK submissions 及日期交叠历史文件查询；
  ETF 按基金目录份额 class ID 查询，避免混入同信托其他基金。
  SEC 复用既有联系人配置，缺失即失败；采集器请求间隔至少 0.2 秒。
- 最终本地公告/展示/validation 集合 164 项通过；以 HEAD 加本次文件构造的
  独立提交候选 50 项通过，不依赖工作区其他未提交的报表重构。
  新模块和测试 Ruff 通过；抓取器保留四处既有 BLE001。
  生产隔离验收使用实际 23 个监控标的（其中 8 只港股和 2 只美股），
  与先前本地 24 个标的结果分开记录；生产清单和配置不覆盖。
  首次隔离副本缺少 config 包导致收集失败，保留原记录，补入生产公开的
  config/__init__.py 和 alerts.yaml 后重跑同一测试集合。
  提交、定向部署和独立验收结果以 `data/deployments/announcements_hk_us_20261030/`
  回执为准，仅三个公告运行文件属于部署范围，未授权的实验改动不夹带。

- `AnnouncementFetcher` 将五位代码先路由到港交所披露易，内地 ETF 前缀排除
  仅作用于六位代码；沪深公告源、已有权益分派识别及每股 5/3 条规则保留。
- 新增 `src/data/hk_announcements.py`，复用官方证券目录内部 ID，以中文标题搜索
  获取日期、时间、分类、标题和原文链接；验证股份代码和官方 HTTPS 域名，
  按时间排序、URL 去重。结果截断时拆分日期窗口，每股最多 64 次查询；
  单日截断或异常结果报错并沿用逐股失败隔离，不能把空列表认定为没有公告。
- 港股以繁体/英文标题及披露分类识别重要事项和分红长窗口，按上海日历日期过滤。
  共用 session.announcements 及日报事件展示；港股不进入缺少币种字段的 A 股
  LLM 金额提取器，待处理分红金额继续使用原有港股分红事件来源。
- 最终定向回归 147 项通过，包含 tests/validation/ 全部 36 项、港股来源/路由/
  截断/日期/失败隔离、原 A 股权益分派回归、邮件及 PDF 事件展示。
  新模块和新增测试 Ruff 通过；抓取器忽略原有四处 BLE001 后通过，未扩改异常策略。
  PDF 测试核对生成 TeX，未冒充真实编译/版式验收。
- 真实采集读取当前全部 24 个监控代码，关闭正文/LLM、只采集公告，不外发。
  5 只港股 17 次标题查询后选出 22 条：00883=5、01816=5、00700=2、00728=5、01339=5。
  原始响应、查询参数、SHA-256、筛选结果和公告栏 HTML 片段保存在
  `data/source_validation/hk_announcements_20261030T003026/`。
  这是公告子链路验收，不是完整日报、优化或策略回测验收。
- 使用说明见 docs/guide/announcements.md。未部署、未发送新版日报，
  未提交或推送，工作区其他既有修改保留。

### 公共执行及健康管理层当前验收（2026-10-27，生产未部署）

> 本文按日期保存实现与验收历史，旧模块、命令及“未部署”等状态应结合日期阅读。当前入口见[文档索引](../README.md)，架构图见[独立图页](../architecture_overview.md)，旧研究统一边界见[退役记录](../development/research_retirement_20261024.md)。

- 原Task阶段1接管已完成：旧3进程gone、同一Task --host-runtime公共Host，持仓12笔/rev4及两Nov提醒保留。前两次原生guard失败及03显式post-exit补完原样封存；不把失败改绿。原生退出42后OS自动拉起新Host，唯一恢复摘要和跨重启到期各sent/attempt1、官方GET全文核对通过。原Task参数与无生产变更边界保留。
- v15源service cf600928…/tools7176fd69…补status-only事实投影和逐轮截图失败诊断。原失败单位/价格表述保留；原群查询31项、独立App真实WS43项、共享分片/诊断4项及M07映射311项通过，12仓与未来提醒未改；Root M13核95个原件引用及57现码。完整25选择candidate-overlay实际2181唯一=2174通过+7环境跳过、0失败，validation36/Pi2/Ruff151通过，不等同于已应用薄入口。M15 v15 Windows普通第3对业务CPU+5.882%、含启动CPU+8.413%失败，其他通过不抵消，禁止按旧v13结果退役。服务器三类正式通过；本机提醒独立进行、空闲未启动；进入一次非门禁诊断准备，不改阈值或重跑挑绿。旧源码尚未退役，生产未部署。当前唯一权威清单见[迁移验收](../development/shared_runtime_migration.md)，历史数字不能直接继承。

### 公共执行及健康管理层历史阶段（2026-10-27，以下为各自当时状态）
- v13双平台报表和内容闭环：Windows90/128原协议+225实际TG；服务器90/235+9最终清理+5本地内容/证据全过，257已观测身份退出且生产/SDK保持。各平台30PDF285页全文/字体/链接/目标坐标/逐页像素通过；Windows字体690原流差异均仅已解析六位subset名，其他字节零差，原rawSHA失败保留。Windows90HTML中61原字节等、29仅同部署IPv4集合顺序，所有其他正文/30PNG/225Feishu/225TG期望全等；Linux对旧本机实际HTML/Feishu/PNG全raw等。真实投递47源/7模板桥接明确两处历史delta：Email类AST不变，PDF金融TeX和模板不变，仅已审编译内存路径与延迟导入。M09/M14勾选，无新实发；Root批准binding91ad4bfc…后20:33Z启动双平台各3×600空闲，原数值/固定输入/旧基线不变。性能未完整判定前不切换原Task、不退役、不部署。
- v13 capture-v2 Windows正式90CLI完成，原128项及实际225条Telegram正文捕获完整性通过，三对整体wall低7.165%/4.234%/5.870%，CPU低7.488%/8.147%/7.294%，RSS低约3.5%，常驻数不增且各type独立过线。PDF30份/285页全文、链接/目标坐标与逐页像素全等；额外原始字体流SHA差异待严格subset名取证，旧false保留。服务器相同90次协议于20:15:54Z真实启动，生产core3/SDK/hash/心跳新鲜核对正常，启动可用内存999612416B。首次上传因新进程未配置DEPLOY_SSH_KEY而在任何负载前失败，原payload保留，仅恢复首次上传并显式使用既有密钥路径；控制器exact路径guard修复只限验收元数据，服务器实际timeout仍原2700秒。空闲/原生切换/退役尚未开始，无生产部署、无额外邮件。

- v13第三次完整回归shared_final_regression_v13_complete_v3实际2211通过、9环境跳过、0失败，2220唯一项/160前后/255实际加载源码/36validation/151Ruff及13独审核对通过。产品、夹具和原5秒超时不变；前两次失败原样保留，M16重新勾选。正式报表开跑前发现Telegram捕获fixture把requests.post的json=None/data正文选成null，旧225捕获不能作正文保真；独立reports_capture_v2三副本统一最小修复、五成员清单delta、613其他成员不变，15离线边界实际通过。产品freeze7c10不变，两variant的完整正文捕获成本都须实测；M14/M15/退役尚未放行，未发送额外邮件。
- v13第二次完整回归shared_final_regression_v13_complete_v2为2210通过、9环境跳过、1失败，产品/测试前后未变、外部连接尝试为0。失败ownership用例的first已释放、second仍未到available_at；Root实际读取System/Kernel-General/Id1日志，19:06:09.8587661Z→19:06:00.9056138Z系统时间回拨8.9531523秒，覆盖5秒断言窗口。原失败及System原XML/只读DB核对保留；后续v3用相同冻结源码、相同25选择和原超时重新完整通过，不修产品/放宽超时/用单项绿替代。
- v13首次完整2220项为2210通过、9环境跳过、1旧PDF测试夹具失败，zero external attempts；原结果和独审false均保留。该旧fixture直接假写PDF，已存在的实际compiler要求XDV后转换；编译器字节及被测日报方法AST与v12均相同。只修测试在_compile_pdf边界捕获TeX，原全部附录断言保留，35相关检查通过；151受影响文件Ruff通过，新增完整25选择在shared_final_regression_v13_complete_v2重新执行。额外160文件检查的20条旧validation lint保留，5文件哈希未变，不扩大本轮修复。v13产品/公开tar未变；服务器原生产优化占内存，900MiB启动资源线未过，未启动验收负载。
- v13仅推迟EmailNotifier三个日报绘图/PDF函数的导入，保留同名转发、参数、返回和异常行为，core/report/main不变。完整Host阶段诊断确认三次FULL合计117.595ms、无固定100ms等待或原子替换重试；简报初始化通知渠道却加载Matplotlib/PDF，manager约452/531ms，具体改进幅度仍待正式测量。冷简报在禁止绘图/PDF导入时完成渲染与本地ACK，1测试及2文件Ruff通过；新增完整回归正在跑。真实WS的57份声明源码现码逐项一致，不重复要求用户发消息。原性能失败、全部数据和邮件额度保留，M14保真及M16重新打开，未启动空闲、退役或部署。
- v12实际性能续验：双平台对话和提醒已按完整协议通过；提醒原包遗漏次级load_manifest更新，在任何case开始前拒绝，另包仅修两metadata后全新6case通过。服务器报表90执行、206核对及30PDF285页全文/逐页像素通过；Windows完整三对晚报耗时仍+9.5463%/+7.9427%/+9.5820%，两对CPU另超5%，第三对早报耗时+6.8758%。整体指标过线不能抵消逐type失败；M15仍失败，空闲/原生切换/退役未启动。服务器Date的-0000审计误解与Windows基线argv前缀误解分别修审核代码重读原90样本，原false保存，均未重跑测量或改产品/门槛。当前源仍core259736ec…/report1b89fb43…/main08116c22…；完整回归2168通过/9环境跳过/150文件Ruff及M13真实46项保持同源。详见shared_runtime_migration.md。
- v11实际失败保留：Windows前两对下午简报耗时+10.0474%/+11.2425%，第二对早报+7.1392%；服务器空闲经过夹具23:59合法行情时点，不能算纯空闲。原生I/O诊断后仅改core/report/main三处：claim同事务保存可选启动意图，原授权核验和默认重复effect写不变；报表非关键进度10秒合并；worker复用application。182定向通过，完整v12一次2168通过/9环境跳过/零失败，validation36通过、150文件Ruff通过；Root12项独立核对159前后+251实际加载源码及JUnit。最终core259736ec…/report1b89fb43…/main08116c22…已freeze；新core真实查询/一次到期/平台GET/公共排空46项通过，未重跑OCR、未改原持仓；分钟-149ms严格等式仍false保留。四个新签包54项及Root38项审核通过；报表双边仅固定业务展示as-of，父Host/SQLite/传输元数据及真实性能时钟不冻结。新性能测量尚未通过，不退役、不部署。

- 独立App真实WS十二个有界场景保留各阶段冻结源码；四个对话/入站消费者复用既有SQLite provider，实际查询/到期44项核对已通过。报表清理反例已修复：core=df878c89…493e97（仅新增5行RunContext.interrupt），report=37724e3f…5496b；实际200项定向检查通过，包含23项故障测试。最终core真实WS查询、创建提醒、唯一到期及公共计划排空46项核对通过，55份其他冻结文件和业务数据未变；模型分钟due_at秒精度使本次精确+60秒差56毫秒，源毫秒保存完整，旧严格探针false单列。最终两平台四类固定性能验收正在进行；v8/v9短报表失败及其他历史版本保留，不继承为新源通过。性能门槛未过前不退役旧实现，生产未部署。
- 最终core/report完整回归实际2149通过、1测试夹具失败、9环境跳过；夹具把临时目录误传配置而串用默认缓存，修正8处cache_dir绑定和4处本地日期表达式后validation36通过，其余产品及测试字节不变。限定范围组合2150个唯一通过、9环境跳过，149文件Ruff通过；原失败完整保存，不宣称单次全回归零失败。cycle_guard独审20项及268源码哈希核对通过。退役投影262项及同步接管42项假测通过，原生Task/五秒交接/custody尚未执行；真实门槛通过前不得把准备当M17完成。
- 原测试接收器因24小时生产过滤证明到期按保护停接单，非Task退出；仅重新只读核验后更新原证明，supervisor恢复唯一worker37852，原Task/持仓12条/revision4/43表（含sqlite_sequence）及旧历史保留。正常积压处理中已新增11月2日、11月5日北京时间09:50滚存提醒，后续切换不得丢失。该worker仍运行性能修改前已加载对象。生产未部署、未提交、未推送；追加修正版验收邮件已实际收件，不再额外发信。

### 公共执行及健康管理层历史阶段（2026-10-25，以下仅对应当时源码）

- 22:14北京续验状态：独立App真实WS已完成两原图OCR、单位补充和8权益入库、4H基线、提醒创建和唯一到期；首证券解析修复后，失败原句的四行更正/撤销/重做为revision7→8→9，十二行逐字段及提醒outbox保持核验通过。旧两图重发实际为幂等重复，版本9不变；不冒充水位拒绝分支实测。命名账户只读查询落到模型后对调两笔待确认市值、重复导入回执误称已更新，已计划排空Host并限于PersonalTools三个方法修复和复验。宿主315节点及模块化七批去重2074通过/9环境跳过有独立来源说明；不能声称单次最终源全量通过。最终Windows/服务器正式性能仍等待真实链路闭环，v3公开包仅准备及3秒smoke，未正式测量。原Task/原库/注册及生产未切换。
- 公共宿主新增接收器工厂、同库执行器注入、先租约后构造、统一暂停接单/排空及计划停止请求；公共健康对异常上一代宿主持久去重恢复摘要。最终宿主接线189+126=315个唯一节点通过，14文件SHA和测试前修改时间独立核验，16受影响文件Ruff及25文件编译通过。扩查未修改validation目录发现24条lint，单列保留，不能称全仓Ruff通过。原Task/原库/注册尚未切换，旧实现尚未退役。
- 独立App真实WS已收到UI消息并回复。首轮明确“创建独立验收账户”被账户文字校验误拒；随后验收runner把新消息创建/更新时间的88—92ms差异误判为编辑而静默，均已保存原始事件。已用公共Host计划排空、回收SDK，正在最小修复并自动续验，无需用户再发消息；测试失败不隐去，不伪造原持仓事实时间或App作用域身份。该变更触及的性能候选重新冻结。
- 本轮续验：最终执行器V6保留原业务事务，用通用provider复用有界SQLite连接；每run独立完成通知修复无关timer导致10次读取的确定性红测。持仓超长诊断只压缩呈现，原始审计不改。最终core/report/validation 591通过，最后interactive 922通过/9环境skip，补充测试整理177通过；完整143文件Ruff0。较早持仓+interactive集合1474通过，不与后续重叠计数；PM实际依赖的107项等待静窗后补测，不把AST等价冒称重跑。最终源码绑定及旧失败见主文档证据。
- Generation05真实24标的从公共入口获取并产出日报/早晚报；8卡片实际发送和正文读回，15页共120表行全部单元格核对。神华每股0.9800元、实施公告、除权/派息2026-10-26均显示。最终10页PDF与已检查版逐像素一致。用户明确授权额外一封修正版后只实际提交一次，Gmail实际INBOX正文完全一致、原始PDF哈希f549445a…完全一致，收件ID 1a172dcc6d123c73；旧邮件及账本不改。
- 独立验收App [平台标识已脱敏，原值见本机证据]已发布并开通用户确认的5项权限，SDK已真实连接并正常退出；用户已原生加入test-1933，真实GET身份及无连接前置通过，待静默性能窗口结束自动跑新入站。生产App不动，不开同App竞争连接。完整两环境性能测试进行中；真实聊天/持仓性能包上传被审批拒绝且未上传，改为空库生成的明确虚构公开夹具，真实数据验收留本机。链路/性能通过前不退役旧代码或本机注册。
- 对话、持仓行情/到期/投递、日报及简报复用同一SQLite执行器及宿主，不增加中间件、管理端口或常驻进程。
- 对话去重/封套/turn/入队同事务；跨会话并发2、同会话保序；每轮context/harness/cancel/OCR隔离。
- 持仓原业务事务/版本/规则及outbox保留；报表所有入口共用互斥，按实际子进程及渠道回执判结果，未知不盲重放。
- 热停用排空、配置失败保旧；公共健康outbox独立控制容量，不能以心跳或退出0冒充业务成功。
- 逐项实际证据、性能失败及未验收项见docs/development/shared_runtime_migration.md；真实WS、完整链路和性能门槛未通过前不退役旧实现/注册。
- 12:45真人再测暴露完整事实加执行确认被误判为15分钟跨消息补充，状态与写入校验不一致，原始失败及未改持仓事实保留。personal_tools.py统一来源解析并保留原事实时间、owner/chat/账户及CAS/水位保护；补非数字清仓、新元数据事实阻断及差异预览。最终候选隔离真实模型按实际失败原话三轮版本3→4→5→6通过，6次模型HTTP/0真实外发；真人群仍待续验。最终同源1174项中1173通过、1项等待次数断言失败（3次读超过上限2）；单项和10轮诊断通过但原因未定，M04/M16保持未勾选。53项新增矩阵已包含主集，测试结构3项错误修正后的完整重跑证据与旧失败分开保存。该时点11/18项通过；原测试任务05:15Z排空重载worker28428，原Task/33表/16告警保留，当时2分钟无新人工入站；随后自动化续验见下。上一源码1121项仅为历史基线，最终证据见confirmation_fix_20261025T0452Z。服务器组件RSS、提醒CPU的旧性能失败及报表内容缺口仍阻止退役。
- 生产未部署、未提交、未推送；原脏工作区及一致性数据库备份保留。另一个财报任务的既有工作不在本次交付范围。
- 13:46北京续验改为复用原输入，停止反复要求用户发测试消息。平台GET二次核实三条原消息，接收前快照副本经GroupPoller/真实Assistant/公共执行器/DeepSeek批量FIFO重放，6模型调用、20检查通过，版本3→4→5→6；全部真实外发0，不算fresh人工/WS。当前测试库只安全续办原失败更正一次到revision4，4H正确、8A及其他业务/去重保留，唯一新增回执已完整GET精确匹配；旧失败turn/status/reply不改。临时模块停用，幂等及执行前漂移零写入门禁通过；原OS任务受控停止/恢复，未增常驻服务、未触生产。automated_recovery_20261025/保留完整证据；M13的截图/行情/WS缺口及其余失败门槛仍不勾选。

### 第二阶段表格、共同口径与重复说明整理（2026-10-24）

- 第一阶段冻结、API调用0；仅修改独立第二阶段整理器/入口，未加统计模型、依赖或运行模块。
- 共用原文定义、同期间明确口径共显、差异按披露版本并列；未知身份与调整口径继续独立。
- 三家公司九份既有输入新建到 stage2_compact_20261024_150731_release，旧产物与冻结文件1639项哈希不变。
- 完整模型正文795889→680492 token（减少14.50%）；原始未解析附件286063不变，合计输入966554。
- 对齐目录不变，32121来源单元格全部保留；301政策/103管理层原文、7附件及11失败继续保留。
- 单元格保留改为正文span/行/表头/列定位，修正数值列与单元格例外展示；不做PDF财务复核。
- 222项离线回归通过，最终日期排序后26项目标重验通过；1259张HTML表结构通过，未做浏览器截图或LLM阅读质量验收。
- 详见 docs/development/financial_context_compact_acceptance_20261024.md；源码原字节备份与独立脚本在 .tmp/stage2_compact_20261024_150731/。

### 旧研究退役提交边界（2026-10-24）

- 删除旧 fundamental_quant 执行引擎与 tracked 纵向研究入口，只保留四个标准库历史读取文件。
- SQLite 使用 mode=ro；历史原状态与已有报告路径可读，run/notify_weekly 返回退役错误，不发送请求。
- 删除直接依赖旧引擎的 tracked 专属测试，加入只读历史测试；迁移后的工程财报数据测试仍保留工作区原文件。
- 本次提交只记录研究退役；第一/二阶段、CLI/Bot/调度的既有未提交工作以及技术面改动不混入本次提交。
- 实际完整工作区清理验收见本地 docs/development/research_retirement_20261024.md；该工作区回执不冒充此精确提交的独立验收。

### 原PDF一行命令跑三个数据模块：付费前冻结（2026-10-23）

- 用户明确授权：四川路桥、安徽建工、泸州老窖各2023—2025年年报，一次付费API运行；
  接入正式API后不再修改代码、产物或过程。本文在正式运行前登记，不按结果回填代码或说明。
- `build_fundamental_data.py run`直接接受原PDF目录/来源清单，先准备全部物理页与稀疏页图像，
  再执行9次提取、3次多年压缩、1次元数据整理；不依赖旧提取器/研究packet/研究transport。
- 原始响应、JSON、CSV、Markdown候选和分项检查均按固定规则排他新建；不重试、不补提取、
  不修JSON、不人工修结果。代码/源文件哈希在每次请求和结束时核对，最终保存产物哈希。
- 付费上限5美元、最多13次请求；实际usage单独统计，缺失保持未知，不冒称provider控制台对账。
  本地原文件预检9份2771页，文本估算约183万token。正式结果以首次回执为准。
- 单份引用错误/缺项、多年超限和元数据不足作为实际失败或待审结果呈现，不由修复循环掩盖。
  研究及外发继续冻结。五脚本规模目标不据此宣称完成。
- 详见 `docs/development/fundamental_data_api_demo_20261023.md`；付费前测试与原始输入核对
  `.tmp/data_api_demo_20261023/`；预定首次正式输出 `data/fundamental_data/api_demo_20261023_first/`。

### 数据与研究分开，先清理三类数据边界（2026-10-23）

- 数据独立到 `src/fundamental_data/`：单份披露、单公司多年整合、公司/同行元数据。
  原6文件解析器迁出后所有调用方直接更新，不留旧路径兼容壳；数据不导入研究。
- 删除materials混合总编排、build_fr_packs/export_research_materials两个入口及无人调用的旧导出框架；
  旧CLI/worker明确blocked，不向研究兜底。研究只更新必要import，没有开发或运行新研究。
- 一并删除旧材料的一分钟续补定时器、恢复方法及失去调用方的重试判断，避免退役后继续空跑。
- 默认阅读为CFO/现金Capex/投资收益、FCF代理及调整边界、MD&A、七类政策/附注。
  过宽的补充章节保留待提取引用，不把整章混进最小上下文，也不称已完整提取。
- 多年模块按单公司保留真实期间/修订版本；只合并完全相同的政策/附注且保留全部来源，
  超字符上限时不输出有效context、不留下旧context。真实压缩效果及语义质量仍需分别验收。
- 元数据初始7家公司、两个配置候选组；9条行业/业务/准则记录有冻结财报逐字引句和页码。
  42个字段中33个缺失；同组/同业标签只产生候选，不证明同行完整性或可比性。
- 回归发现清理前已存在的附注表内中文编号误截断（原字节复现第96页、预期第119页），
  修正层级/数值行判断，fr-pack/26；原失败断言保留。
- 没有达到五脚本/每个100行的最终规模约束；数据拆分不冒充最终实现，研究验收保持冻结。
  详细规则和独立验收见 `docs/development/fundamental_data_20261023.md`。
- 原字节备份 `.trash/data_split_20261023/`；迁移、依赖图、真实材料回放和测试回执
  `.tmp/data_split_20261023/`；本机交付 `data/fundamental_data/acceptance_20261023/`。
- 验证：1399项大范围回归通过，删去2项死续补专属测试后80项末次重验通过；
  保留用例去重1397项，含36项validation。814文件语法/依赖检查、数据入口不导入研究检查通过；
  修改范围Ruff通过，main既有65条降至64、无新增。socket尝试0，付费调用0。

### 删除model-led及无生产调用方的专用提取框架（2026-10-23）

- 1158项离线回归通过（含36项validation），817文件语法/导入检查无已删模块引用；
  修改FQ/测试Ruff通过，main既有65条诊断无新增；真实socket尝试0。

- 删除ResearchWorkflow、research_prompts、accept_model_led_research脚本及配置；
  service不再执行任何研究，旧配置/激活/预算不能恢复，CLI明确报退役并返回非零。
- 删除其唯一调用的自动补提取/上下文/单次模型编排，以及专用retrieval、封面审查、提取修复模块；
  service从2035降至362行，保留历史查询、有效性重验、渲染及已有通知。
- 研究合同/确定性计算因旧报告有效性验证和同行代码仍在使用而保留；现有FR材料解析未重写。
- 统一格式的API提取是后续方向；公开文档确认DeepSeek Files API为图片接口，PDF可先转原页图片。
  本轮未开发替代链路、未调用付费API、未外发或部署。详见实际入口清单的model-led追加说明。
- 原字节备份在 `.trash/model_led_retirement_20261023/`，导入边/测试回执在同名 `.tmp/` 目录。

### 删除第4条旧研究及兜底，盘点真实剩余入口（2026-10-23）

- 最终1330个不同当前测试用例通过，包含36项validation；最后修订88项全通过。
  main原有65项lint诊断未新增；其他修改FQ模块/测试Ruff通过。完整初始失败及处理记录保留。

- 删除service的 `_process()`、旧日常/验收循环、自动重估、同行及会计桥接编排，
  以及续跑队列、bootstrap、旧付费evaluation脚本和专用研究提示词。
- acceptance只保留现有流程共用的预算合同；没有选择现有model-led时明确返回
  `legacy_fundamental_workflow_retired`，旧授权不能开启任何旧研究兜底。
- 保留共享extract及来源/口径校验、历史原文/账本/稿件；未开发新研究引擎，未调用付费API或通知。
- `fundamental_entrypoints_after_cleanup_20261023.md` 列出真实CLI/worker/Bot、33个直接FQ脚本，
  区分model-led、纵向同行/Pi、整份比较以及独立付费编辑/审查，明确材料反向依赖尚存。
- 删除前原字节保存在 `.trash/legacy_research_retirement_20261023/`；依赖边和验证回执在
  `.tmp/legacy_research_retirement_20261023/`。验证结果另列在实际入口文档末尾。

### 删除早期单公司 PDF 研究链路（2026-10-23）

- 按用户要求删除贴文第2条 `PDFResearchWorkflow`，以及专用提示词、背景投影、可写会话、
  运行/恢复脚本和仅服务该流程的测试；移除service优先路由、专用调用协议及Files API上传。
- 当前PDF配置已删除；遗留启用配置明确报 `pdf_research_workflow_retired`，不得静默进入另一
  付费模式。历史PDF激活事件不再触发该研究；状态查询不再创建PDF会话表。
- 共享原文采集/渲染、token计量、同行计算及历史报告验证保留；历史稿件通过只读notebook可查。
  未迁移/删除数据库、资料包、旧账本或已交付报告。原字节备份在 `.tmp/pdf_workflow_retirement_20261023/`。
- 编号纠正：第4节是保留至今的最早 `_process()` 流程；第5节是边界说明，不是执行链路。
  本轮未删除第4条，未启动五脚本新实现，未调用付费API、通知或部署。
- 475项回归和最后修改后的70项控制/worker/集成检查通过（533个不同用例），Ruff通过；
  841个文件语法/导入检查无已删模块引用。历史笔记明确标为已退役。
- 详见 `docs/development/research_retirement_20261024.md`（原 `pdf_research_sessions.md` 已合并删除）；验证记录见上述本机清理目录。

### 附注边界修复及单次DeepSeek完整研究稿（2026-10-22）

- 修复主要项目注释标题别名、续页标题反复切断和未知同级标题不截断旧块；fr-pack/25。
  中交2025税项由103,176字符降至1,633，2026H1由100,570降至1,574；均保持完整原文，
  原PDF物理页视觉核对。8份近期披露的228个已定位七主题政策/附注段落逐字回放相同。
- 新指令授权一次直送请求：4公司财务、冻结报价、2019–25的28份完整年度讨论、4份最新中报讨论、
  近期228段完整会计政策/附注，以及异行业白酒样例。7份旧中报讨论原文不重复加入，数字仍保留。
- 独立薄脚本仅1次HTTP POST，普通Markdown输出；不走旧状态机、Pi循环、工具或独立审查，
  无自动重试。2026-10-22 20:09:23至20:12:39，HTTP200/stop，公开完整正文7,650字符。
- 输入预估930,826，服务实际930,856（差30个framing token）；输出40,203，其中思考35,346。
  缓存0；按当日官方非高峰费率核算¥1.091668，非独立发票对账。旧账本未重置或恢复。
- 报告目录`data/fundamental_quant/direct_markdown_input_20261022-200725`，正文未人工补写，
  标为未经独立复核的单次生成稿，无图、未发布、未发邮件。边界/导出及validation回归94 passed。
  详见`docs/development/direct_deepseek_report_20261022.md`。响应完成不等于财务结论已验证。

### 公司—财期Markdown/CSV研究资料阅读层（2026-10-22）

- 模型阅读格式改为Markdown内嵌CSV；财务期独立文件、单公司指标纵表和逐披露版本原文目录分开。
  同行组只是一份selection.txt文件清单，不再是新阅读层的最小资料单位。底层FR包、JSON/SQLite和
  旧付费会话保持原样，旧Pi入口尚未整体迁移，也未恢复运行。
- 真实导出43份财期、4份公司纵表、4份冻结行情、39份逐字管理层原文；2,904个单元格及单位、币种、
  合并/归母口径重建一致。单公司纵表共24,362 token；管理层原文787,441 token，不摘要、不改写。
- 新单次请求离线准备采用文件正文与异行业白酒样例；API标准消息信封842,255 token，预留5%输入
  余量及65,536输出仍在100万上下文内。没有发送请求。错误税项附注边界仍待修，未伪称资料齐全。
- 新导出测试、旧投影测试及tests/validation合计52 passed，Ruff通过；旧文件不变时复用，增减公司
  不重建其他公司。实现/使用及验收边界见`docs/development/company_reading_materials_20261022.md`。

### 数值输入膨胀根因与直送数值表离线修复（2026-10-22）

- 刚准备但未发送的直送包，其“财务面板”实际544,686 token：财务行198,510、趋势统计116,585、
  月度估值189,730等；不能将这些都说成财务数字。旧测试只检查完整性与总量小于650,000，
  没有验数值表密度，故容量通过不代表资料输入合格。测试已明确标为legacy capacity only。
- 新增`financial_value_table`：共享公司/期间与逐指标计量依据，数值按行位置成列，完整来源留在
  原冻结资料。43个公司/期间、2495披露值及409派生值逐项重建一致，财务行降至31,039 token；
  null/缺失/零/负数、大额小数、币种/单位/合并归母与年度中期差异均保留，不转换int32或舍入。
- 新直送准备器已接数值表；没有改旧Pi历史或宣称旧自动链路已经迁移。原文未摘要、无付费API、
  无邮件。附注税项章节越界、趋势和历史估值组织及整个原文输入仍未验收。
- 真实数值表回放与审计页：`data/fundamental_quant/numeric_payload_audit_20261022/index.html`。
  局部投影回归7项通过；与`tests/validation`合计43项通过，Ruff通过。完整验证结果单列在该目录，
  不能代替Agent研究验收。

### 工程组最终续审停止，真实验收未通过（2026-10-22）

- 复用原研究与审查会话及完整修订稿；新轮40次/3000万输入预留/150万输出预留/300分钟
  授权不变，没有发布或发送邮件。实际27次、输入17,349,229、输出186,838。
- 累计输出预留1,499,136，工具续轮的大额预留累积导致可发送余量不足；最后8192输出请求
  以length结束且公开正文为空，不能作为审查通过。付费已停止，原账本没有重置或回收继续使用。
- 服务返回缓存命中10,001,280、未命中7,347,949（57.6%）；按冻结非高峰费率核算¥8.2953266，
  非发票对账。旧轮费用与用量未知请求单列。上下文压缩及前缀改变未达到预期缓存复用。
- 协议、数字引用、页码与控制组合回归247项通过，最终停止/预算控制另27项通过；程序验证
  不替代研究验收。独立离线复核只消费已有稿与原文，具体实质问题保留，不能冒充API通过。
- 详见`docs/development/construction_final_review_continuation_20261022.md`及当前续审目录的
  `outcome.html`、`cost_receipt.json`、`offline_final_review/`。

### 简报昨收与开市校验上线（2026-10-21）

- 简报邮件、飞书原生表格及 Telegram 同步增加行情源昨收；经实时刷新、DataFrame、
  Session 无损传递，缺失显示 —。QQ 日期改读源时间戳，不再将旧报价改写成抓取当天。
- 定时任务抓取前检查交易所常规时段，抓取后、发送前检查本场有效报价；覆盖假日、
  午休、半日市、时区和夏令时，无可用开市报价时不发送，日历不可用按未满足处理。
- 本地和服务器隔离副本均 223 passed，无跳过/排除；全量 21/21 真实公开行情核对
  时间格式及昨收并生成只读价格预览。没有发送测试消息或运行带调仓副作用的简报。
- 23:18:08 定向上线，约 2.3 秒就绪；配置、账户、策略指针及实验功能状态不变。
  备份、精确文件哈希与独立验收见
  [本次修复记录](../development/brief_session_prev_close_20261021.md)。

### A 股历史回退既有失败修复与 PR #2 上线（2026-10-21）

- 根因是服务器测试请求 2020–2026 年，却将仅含 2020 年两天的样本标为完整。
  修正完整样本末端并补充末端过期仍须回退的回归；生产市场历史路由逻辑不变。
- PR #2 已合并为 `3d863f2`，定向适配瞬态公告失败的一次有界重试、诊断透传，
  并补齐 2018 真实 PDF 所需的既有 HKD 登记日匹配前置修复；保留现有美元/固定汇率路径。
- 本地最终 412 passed、2 skipped；服务器隔离副本 354 passed、2 skipped，
  包含 tests/validation，未排除原 A 股失败项。PR 精确源码另有 391 passed、2 skipped。
- 仅上线 3 个运行文件和相关测试/PDF；配置、账户、策略指针及其他运行代码哈希不变，
  实验功能继续不上线。22:53:54 服务就绪，新 PID 759609。
- 基线复现、验证、备份和独立验收见
  [本次部署记录](../development/ashare_fallback_pr2_20261021.md)。

### 白酒Pi局部续跑真实端到端验收（2026-10-19）

- 新轮fq-pi-liquor-e2e-20261019-152921，按用户授权3次/100万输入预留/6万输出预留/30分钟；原旧轮账本保持不变，工程组未运行。
- 复用完整结果及原实质审查，真实Pi独立会话自动11次只读工具回查，3次API后pass并自动消费；完整HTML、Bot数据源与邮件版本一致，SMTP接受且outbox delivered。新轮已关闭。
- 实际输入577,073、输出12,177；缓存命中190,208，未命中386,865，非高峰按绑定费率核算¥0.43937716，非独立发票对账。
- 真实前检修复局部续跑误用新研究准入：等待引用复核不重复预留旧研究全文/研究输出，仍逐请求按原账本计量；增加回归。
- 复核文字有1处长数字抄写错误，原始回执保留，正文与计算未使用该错误；单列非阻断QA，不宣称模型文字完全正确。详见data/fundamental_quant/pi_liquor_e2e_20261019-152921/delivery.html及review_qa.json。

### Pi独立引用复核自动派发与消费（2026-10-19）

- 默认后端切换为Pi；正常有效授权run/resume自动派发独立会话、执行只读工具、消费回执并进入统一发布，不再要求用户启动sub-agent或导入JSON。
- 原传输/费用账本/期限不变；每个局部复核默认最多3轮、16次工具、每轮16,384输出预留，配置与Skill随任务冻结。
- 执行证明绑定实际Pi会话和原传输state/result；补充锁后读取、父检查点修复、预算用尽后已有回执免费重放、回执导入提交窗口及来源篡改回归。
- 本轮只用真实Pi进程与离线原生回执验收，没有DeepSeek请求，没有工程组或重复邮件，旧付费账本不变。
- 离线主回归170项通过，另1项回执提交窗口故障回归通过；Skill/Ruff检查通过，原6个付费状态及父检查点hash未变。
- 详见[自动派发实现与操作边界（旧说明已合并）](../development/research_retirement_20261024.md)。

### 引用修正与独立离线复核自动接线（2026-10-19）

- 提交时按可证明规则整理引用；独立审查将纯引用问题转成冻结、可恢复的离线任务。
- 同时约束原资料、计算/估算、补充证据、方法与结果；修正回执不得改正文或经济判断。
- 复用真实白酒结果完成独立sub-agent原页核对及CLI消费，不新增付费调用，不运行工程组。
- 统一幂等发布和精确版本/渠道通知；未披露成功的SMTP状态可见且不盲重发。
- 当前artifact_inbox需要宿主启动独立执行器并消费回执，未配置后台自动派发；不得描述为无人值守。
- 本轮155项控制及验证回归通过，Skill/Ruff检查通过；不将离线回放表述为新的付费投研验收。
- 详见[实现、操作与人工边界（旧说明已合并）](../development/research_retirement_20261024.md)。

### HKEX 公告异常隔离定向上线（2026-10-18）

- 已拉取 GitHub PR #1（992d2ac，合并 11d8129），兼容现有生产缓存及美元分红路径，
  定向替换一个 HKEX 运行文件；短公告流式字节、页数、文本与内存缓存设上限，隔离单份失败。
- 本地回归 247 passed；服务器隔离回归 219 passed、1 skipped，并明确排除一个在原生产
  代码上同样复现的 A 股历史回退测试。包含 	ests/validation/，目标 Ruff/diff 检查通过。
- 未同步本地实验功能或配置；生产配置、参考账户和策略指针哈希不变。没有新策略激活、
  全池优化或测试消息发送。09:11:38 服务恢复，新 PID 733578；独立运行验收见回执。
- 具体范围、原代码失败复现和回滚备份见
  [定向部署记录](../development/github_hkex_hotfix_20261018.md)。

### Fundamental Quant 原图持续会话（2026-10-06）

- 压缩角色分离后的真实响应撞到8192输出上限（finish=length）；为原图压缩单独配置
  compact_max_output_tokens=32768，仍在原账本预留、仍检查完整JSON/引用覆盖后才切换。
  这是本轮压缩路径第二次修复回放；不能把被截断检查点作为有效状态。

- 真实压缩返回了审查结构而非检查点（缺facts），修复为把历史角色/文本显式包为资料，
  保留原图引用，在最后消息重申唯一压缩任务及必需字段。旧会话失败时仍不切换。
  回放保留508个原图引用及64项必需引用，84项相关测试通过；真实新检查点仍须运行验证。

- `pdf-protocol/4` 修复逐轮审查不及时闭环：审查已给出原页可定位且影响当前结论的
  blocking 问题时，先保存本轮审查并交回定向修正，剩余工具请求原样保留为后续补查。
  不改变审查结论、不放行受阻报告；修正结果继续独立复核。无明确原页依据的工具补查仍正常执行。
  提示词要求历史错误必须对应当前仍使用的依赖，避免无关旧记录持续阻塞新结论。
  真实审查响应回放和69项相关测试通过（并非财务报告验收）。

- 完整 JSON 语法失败支持同角色一次格式补正：校验原请求哈希、会话、角色、
  finish=stop 且原始正文未截断，再把完整错误响应交回原会话。重复失败、截断、
  重复字段及非本会话响应不走格式补正；不通过程序猜测修改结论。
  三一真实未转义引号响应已离线回放，旧错误和费用保留。
  四川路桥响应 finish=length、实际输出32768，原图研究/审查配置改为65536，
  独立于提取/压缩限制。传输按官方上限393216验证，仍逐次预留并受本轮200万总额度约束。
  依据 https://api-docs.deepseek.com/api/create-chat-completion/ ，未延长原截止时间。

- `pdf-protocol/3` 保留未提交记录的显式更正链：模型以新 id 替代暂存记录时，
  沿 supersedes 定位原已提交记录，审计保留中间稿与模型更正原因，不改财务值或伪造依赖。
  这是本轮更正合同的第二次修复回放，后续同根因仍失败将停止该路径。
  计算工具可读取同轮提供且通过原页/单位机械检查的记录，结果明确为未审查算术预览；
  同名变量冲突、伪引用继续拦截。工具同根因失败按模型响应计数，同批多工具不是模型重试。
  真实响应回放覆盖三一更正链及四川路桥七笔原失败计算（零付费调用）；记录于
  `data/fundamental_quant/acceptance/fq-pdf-acceptance-20261006-112042-pending-chain-and-calculator-replay.json`。

- 本轮第三次研究返回时，正常化盈利更正把原因写在 method，遗漏独立
  correction_reason，原实现直接中断公司。现先检查更正合同，保留未提交输出，
  将具体记录与缺项送回原会话，仅请求补正相关记录；原事实保持不变，补正后仍送独立审查。
  同类合同错误仅允许一次模型修正，重复失败停止；检查点保存待补正记录，重启不丢失。
  真实失败响应已离线回放，未改财务数值且零付费调用，回执为
  `data/fundamental_quant/acceptance/fq-pdf-acceptance-20261006-112042-record-contract-repair.json`。

- 真实验收 `fq-pdf-acceptance-20261006-112042` 的协议修复 `pdf-protocol/2`：
  独立审查原先未收到工具参数合同，回查连续返回 ValueError；新增共享工具合同、
  明确操作枚举/安全同义运算、显式单页回查及可行动错误信息。工具同根因失败两次阻止继续烧调用。
  代码计算预览现在随原图送审；缺依赖不再误报为同行。定性原文须显式类型，不能进入估值算术。
  核心指标使用规范 metric、原百分数单位和计算精度均明确说明；已有错误不被自动改成通过。
  已返回的四次真实工具失败离线回放均恢复原图，预算与原20:50:42截止不变；
  回执见 `data/fundamental_quant/acceptance/fq-pdf-acceptance-20261006-112042-protocol-repair-1.json`。

- 新增 `PDFResearchWorkflow`、连续 50 页 PDF 渲染及 Files API 哈希复用，
  双会话逐轮研究/独立审查、结构化模型压缩和 SQLite 原子检查点。
  原图链路绕过旧字符裁剪，不绕过费用账本、期限或逐份审查。
- 阶段结果即时展示，第一份有效报告即 staging/通知，三份后才切换政策。
  原始事实、更正和历史账本保留；撤回同步查询和原直链页面。
- 新 CLI `scripts/run_pdf_research.py` 支持 inventory/preflight/run/status/report。
  详细合同、能力限制及验收边界见 `docs/development/research_retirement_20261024.md`（原 `pdf_research_sessions.md` 已合并删除）。
  本次未自动恢复已停止付费轮次；程序测试不是三份真实报告验收。

### 技术面搜参自动晋升开关修复（2026-10-04）

- `auto_activate_if_better` 重新接入搜参发布链路：策略在同一市场、同一验证
  切片上优于现任且相对晋升通过时，开关开启便自动激活，仅更新该市场指针。
- 关闭开关、关闭晋升、没有现任、比较缺失或表现持平/变差时保留候选。
  候选产物和晋升依据继续留档，汇总回执区分候选与已激活状态。
- 保留历史策略执行快照，持有期不随新 YAML 回溯改变；相对晋升覆盖绝对
  表现门槛的既有语义保持不变。本次不运行生产搜参或修改真实活动指针。

### Fundamental Quant 连续基线与后台退出诊断（2026-10-05）

- 2026-10-04 21:34 的进程失踪与 Codex AppX 更新停止沙箱服务、终止应用的
  Windows 事件吻合；区别于 14:03 明确记录的三小时 `JobTimeout`。原始系统事件、
  RecordID 与推断边界保存在 `data/fundamental_quant/acceptance/exit_diagnosis_20261004.json`。
  事件未直接列出 Python PID，不能将具体 kill 路径宣称为完全证实。
- 新增 `scripts/install_fundamental_quant_task.py`：先生成可审查 UTF-16 任务 XML，
  `--install` 注册当前用户、最低权限、隐藏的独立 Windows 任务。任务不含密码，
  不覆盖同名任务，最多失败重启三次；只在用户已登录时运行。宿主自行维护原有
  每日/每周调度，任务层每日尝试启动并忽略重叠实例，避免依附 Codex 沙箱生命周期。
- 基线冻结最早预留的 500 家，先完成这些公司再扩张。`baseline` 与整个 bootstrap
  研究池分别计数；完成采集尝试仍不代表资料齐备。首轮实际配置采用 12 小时软时限、
  15 分钟收尾余量，来源限流、日模型 80 次及深研 20 家保持；不承诺 12 小时必定齐备。
- 连续进程跨北京时间午夜切换持久化预算日期，不退还前日支出。新增公司边界
  `--drain` 停止入口，后续升级可先完成当前请求再退出。
- 运行查询核对宿主/子 PID 及复用时间，旧 `running` 文件不再代表任务存活；
  记录当前阶段、公司、心跳、子进程退出码。首页独立展示初筛分布、原文/机械核对
  进度和真实公司缺口，不等待全池，不把中间结果伪装成候选。

### Fundamental Quant 首轮扩容（2026-10-04）

- `fundamental_quant.bootstrap` 独立于日常补数配置：启用后默认每天 500 个
  唯一发行人，日常仍为 50；发现行情、财报及同行共享同一个持久化额度。
- 首轮发现后冻结当前研究池到 SQLite 事件。每家公司首次采集结束（包括明确
  失败）或被有证据地排除后才结束首轮；完成标记跨重启保留，自动恢复日常额度。
  “已遍历”只表示尝试已结束，不代表资料齐备、全市场覆盖或候选核验通过。
- 待首次采集者优先，随后按缺失/最旧行情、A500 排序，避免 500 家额度被同一批
  A500 反复占用。跨日、重启保留队列，不退还已使用额度；后续上市公司走增量链路。
- 深研 20 家、模型调用 80 次、来源限流、三小时单批时限及发布核验规则保持原值。
  500 是每日额度上限，并非吞吐承诺；未推进完的公司保留到后续批次。
- 状态、HTML 和邮件展示实际生效上限及首轮进度。全池只枚举元数据，按公司读取
  财报 packet，并在公司处理后释放批次内存缓存，避免原文随池规模常驻内存。

### 邮件修复上线与参考池无损迁移（2026-09-23 22:53）

- 用户授权后部署本轮补丁，三池只更新旧执行合同指纹；现金、持仓、成交、
  公司行为事件、参数和活动策略指针均保持不变，没有重置账户。
- 服务器隔离回归 212 passed；服务重启后同 MainPID 心跳正常，scheduler 与
  Feishu 线程运行。生产全部现有持仓补价成功，三池估值完整，账户文件未改写。
- 备份、回滚入口和验收边界见 `docs/development/mail_hotfix_deployment_20260923.md`。
  Yahoo 403/DNS 与部分 ETF 长历史不足仍阻塞新策略优化；未发送测试通知。

### 邮件故障核对与参考账户保护（2026-09-23，部署前）

- Gmail 和服务器日志对照确认日报有真实信号但移动正文遗漏明细，现独立显示
  信号代码、方向及已有依据；通知只要缺行情便明确标记估值不完整，不能假造零价。
- 参考持仓对移出监控池的代码独立补取真实报价，不改变监控配置或清空账户。
  三池旧执行合同缺少新增的分红预扣税率绑定，需通过默认 dry-run 的严格迁移
  工具检查；生产迁移未执行，参数或其他执行项变化仍停单。
- 修复凌晨优化要求尚未收盘日期的准备语义，依交易所日历验收已完成交易日。
  优化未执行时明确显示零次评估、逐标的和基准的刷新/覆盖失败原因。
- 服务器 Yahoo 403/DNS 和部分 ETF 长历史不足仍需恢复真实数据；未进行生产
  部署、策略激活或全量优化。图片为 Gmail 显示拦截，按用户要求不作修改。
- 详细证据与边界见 `docs/development/mail_server_audit_20260923.md`。
- 本轮组合回归 292 passed（含 validation）；真实三池快照迁移预演全部符合
  条件，账户数据指纹不变且零生产写入。补丁以服务器 8bd9484 单独整理。

### Baostock 访问控制与补数恢复（2026-09-22）

- `src/data/baostock_access.py` 统一读取 `provider_access.baostock`；连接在进程间
  串行，从连接前持锁到 socket 关闭后。真实协议请求（含登录、退出、分页）
  每次间隔 2 秒，默认每日 40,000 次，计数按北京时间持久化，网络失败指数退避。
- 行情、财报和行业分类均使用共享入口。黑名单立即持久熔断，阻止隐式分页及
  自动重试；监督器报告 `source_blocked` 并保留待处理名单及原有失败证据。
  `scripts/check_baostock_access.py --refresh-official` 只读官网当前出口状态，
  明确解除后才清理黑名单，不清请求预算，不按预计释放时间自动重启。
- 同批次/窗口的有效行情可复用；检查 raw/qfq、行动、日期、元数据和 SHA-256，
  避免缺财报时重抓行情。错误响应不再被静默当作空成功，未来季度不发无效请求。
- 原中证 1000 批次指纹保持不变，931 个成功检查点、69 个待补财报；69 份行情
  均通过离线复用验证，封禁预检零数据请求，4,931 个原数据文件哈希保持一致。
  SDK 0.9.3 假传输分页和真实跨进程锁均有离线测试；实网解封后补数尚未执行。
- 配置、官方规则、恢复命令及验收边界见 `docs/development/baostock_access.md`。

### 服务器数据回收与中证 1000 补数（2026-09-21）

- `scripts/sync_server_market_data.py` 在服务器隔离目录后台导出行情、财报与数据
  缓存，生成归档和逐文件 SHA-256 清单；本地支持按归档身份续传、校验后解包。
  导入位于 `data/server_imports/`，保留原快照，避免服务器旧数据覆盖本地 H1 补数。
- `scripts/refresh_csi_reference_manifest.py --indices csi_1000` 获取官方 000852
  成分表，严格校验 1000 家并保留原始 XLS、日期及 SHA-256。默认仍为沪深 300
  加中证 500。当前成分只能用于当前样本回溯，不能冒充历史时点的指数成分。
- `scripts/run_reference_universe_isolated.py` 支持 `--history-years`、
  `--official-pdf-recent-years` 及最多四路并发。每家公司在独立进程运行，有超时、
  有限重试、互斥和逐标的检查点；汇总只读现有结果，不在结束时暗中重抓失败项。
- 本次固定窗口为 2021-09-21 至 2026-09-21，使用独立任务配置和两路并发。
  完整任务进度看批次 `supervisor_progress.json`，子进程的单股报告不代表全池完成。
  `success` 表示接口取得记录；原价/复权价、公司行动披露日期、财报字段和时效
  仍需独立验收。资源测算、命令与运行记录见
  `docs/development/data_acquisition_20260921.md`。

### 运行入口收敛与审查修复（2026-09-20）

- 用户确认逆序和留一验证统一完整执行合同：切片同时保留 raw OHLC、现金分红、
  税前/税额、拆股及基准行情/行动，重新建立子集评价缓存和基本面上下文。
  缺少必要执行数据或 Numba 时明确失败。
- HTTP/HTTPS health server、管理页面、OTP、临时报告链接和端口探针整体退休。
  使用飞书/TG Bot 更灵活、更安全；飞书采用官方 SDK 出站长连接，Telegram 采用
  轮询，共享命令分发、限流和去重。按用户决定保留飞书显式 `["*"]` 白名单语义，
  空白名单继续拒绝，Telegram 仍使用明确 ID。
- `RuntimeService` 管理统一 `ScheduleManager` 与 Bot 生命周期，默认 CLI 与
  `--service` 同路；`--interactive` 仅运行 Bot，`--status` 读取本地心跳。
  部署迁移旧服务和当前项目 cron，使用 systemd 当前 PID + 新鲜心跳验收。
- `ConfigStore` 统一 dotenv/runtime 读取与锁内字段修改、原子替换；不再将环境
  密钥或启动快照整份写回 YAML。Windows 文件锁初始化竞争由真实双进程回归修复。
- 告警指标和检查器共同读取指定路径，移除跨路径单例；LLM 不再全局修改 JSON。
  所有通知底层统一检查开关，重启通知在实际启动后通过 `NotifierManager` 分发。
- 清除无消费者配置、旧参考组合 `rebalance()`、死变量和死导入；保留研究用途
  `evaluation_workers` 及现有算法常量。审查记录中的原行号保留为修复前证据，
  当前状态见 `docs/development/config_mainline_audit_20260920.md` 开头的修复表。
- 下文涉及 HealthServer/HTTPS 的旧版本条目仅为历史记录，不再构成部署或使用说明。

### 配置清理与主链路审查（2026-09-20）

- 移除未被运行代码读取的旧版 Builder/Bayesian 配置，以及对应的旧入口、
  规则模板、两阶段回测使用说明；保留独立的历史变更记录。
- 当前搜参入口由 `config/config.yaml` 的 `optimizer.markets` 选择各市场
  策略与档案，参数、Gate 和执行合同位于 `config/optimizer_constraints.yaml`，
  相对晋升规则位于 `config/promotion_policy.yaml`。
- 对当前工作区的配置、死变量和入口旁路审查见
  `docs/development/config_mainline_audit_20260920.md`。审查发现与此次删除分开记录。

### 新浪期权数据源接入（2026-09-02）

- 新增 `src/data/option_data.py`，通过新浪公开接口读取中金所 IO/HO/MO
  指数期权和上交所 ETF 期权的合约月份、Call/Put 实时链与单合约日线。
- 沪深 300 可使用中金所 IO 或 510300 ETF 期权；中证 500 使用上交所
  510500 ETF 期权。中金所 MO 是中证 1000，不能标注为中证 500。
- `DataSource.option_data_source` 提供延迟初始化入口。ETF 行情请求按批次
  合并，新浪 GBK 行情、JSON 和 JSONP 响应分别解码/解析，并在接口异常时
  fail closed，不用空值或模拟数据填充。
- 新浪当前提供月度到期合约，不虚构周到期合约。周/月 K 线由
  `resample_option_bars(daily, "W-FRI")` 和 `resample_option_bars(daily, "M")`
  从日线聚合；`contract_month` 保留新浪的 YYMM 合约月份。

## 当前执行合同更新（2026-09-19）

- 港股现金分红采用账户层保守 `20%` 预扣假设；qfq 仅生成信号，raw 成交价和
  显式税后现金流共同计算 NAV。标量、Numba、批量、目标权重、静态/外部基准和
  三个参考持仓共享该合同，报告持久化税前分红、税额和税后分红。
- 现金分红必须有 `published_at <= ex_date` 的可审计公告时间。缺少此证据的
  Yahoo 事后 action 不进入历史回测或搜参，数据就绪报告会明确给出原因。
- `python main.py --universe-robustness [--group MARKET]` 是独立的 84 个月标的池
  诊断：每市场复用当前策略、Solver、预算与 `22/16/2/4` 合同，输出基线、
  leave-one-out、确定性比例删减及横截面输入排序不变性结果。它不会修改监控配置、
  创建候选、激活策略或改写生产 pointer。
- P4 基本面 embedding/DCF 和 P5 周粒度降载仍暂缓，未在本轮实现或启用。
- 日报的“三市场组合净值总览”独立读取最近 36 个日历月，不能复用搜参单个
  9 个月 Holdout。图表会裁掉三市场共同的、净值仍为 100 的指标预热/未建仓前缀，
  并标注“有效期自 YYYY-MM-DD”；这不改变上游 36 个月数据范围或任何回测指标。
  日报抓取为该视图增加 35 天日历缓冲；简报仍使用轻量历史窗口，避免为盘中快照
  放大数据请求。
- 完整行情矩阵保留告警/策略优先排序，但不在每只标的名称下重复展示“预警”或
  “策略”徽标；行动层只在上方决策板和行动清单中呈现。数据未就绪仍明确标识。

### 监控池 Gordon-Ke 搜参合同（2026-09-20，离线且未激活）

- 后续监控池 Gordon-Ke 研究统一只读取
  `config/optimizer_constraints.yaml` 的
  `value_research_profiles.monitoring_gordon_ke_existing_data_v2`（继承 v1）；需要预注册
  风险前沿时显式使用同一链条上的 `..._v3_risk_frontier`，入口仍为
  `scripts/run_monitoring_gordon_ke_search.py`。Ke 买入阈值、可选季度复核卖出
  阈值、每只标的的 g 生成法（固定 / 三年归母净利 CAGR / sustainable ROE）、波动
  窗口与惩罚、候选数、行业/单名上限及协方差收缩均由同一 profile 枚举；验证期不能
  进入排名。
- profile 固定为 research-only、不可激活，只读本地 PIT 数据；PB 在形成日使用 raw
  close，收益明确标为 qfq 代理。ETF 不因缺少公司 ROIC 被排除，而是等待当时可得的
  成分 look-through ROE/PB 快照；REIT 使用独立 FFO/分配/NAV 合同。没有该历史证据
  的非权益标的必须 fail closed，不能用今天的 ETF 估值倒灌。
- 搜参并列由 profile 声明的 `lower_top_n_then_lower_volatility_penalty_then_parameter_hash`
  决胜，并在报告中写出并列数和 hash；不再依赖脚本循环顺序。此前的
  `evaluate_*ke*` 临时脚本与其冻结常数保留为历史报告复现入口，不得作为新的研究或
  验证结论的配置来源。
- v2 的六只扩展股不是按回报、Ke 或验证收益挑选：runner 会从本地完整 PIT 对中复算
  训练期中位成交额、CSRC 行业首字母分散、训练形成日基本面覆盖和验证形成日的最低
  “可计算”覆盖，再核对输出是否仍是 `600519/601318/603259/601888/601919/601857`；
  任一不同即 fail closed。验证期回报仍完全不参与选参或扩池。
- v3 只在 v2 的训练期 finalist 上预注册并分别验证 40% / 55% / 70% / 85% / 100%
  的权益预算；每个预算各自按训练分数选参数，验证期只保留完整风险前沿，不能从验证
  表倒选一个预算。权益预算在每次季度复核时做独立的风险再平衡，不冒充 Ke 卖出信号；
  这样上涨后的持仓也会被降回预算，现金余额明确保留。
- v4 因 v3 的最低测试点正好是 40%，只向下预注册 20% / 25% / 30% / 35% 并保留
  40% 锚点；仍是相同 12 个已有 PIT 标的、6,426 个 Stage-A 和 1,440 个 Stage-B
  候选。40% 锚点与 v3 完全复现；低风险前沿是 20%: +2.25% / -3.70% MDD、25%:
  +2.82% / -4.61%、30%: +3.39% / -5.51%、35%: +3.96% / -6.40%、40%:
  +4.53% / -7.29%（均为 qfq 代理验证期），不构成线上收益声明。
- v5 只将 Stage-B `allocation` 增加为最小方差与 capped inverse-volatility 两臂，
  每个“权益预算 × allocation”各自以训练期选参并完整保留验证路径。训练期五档均选
  逆波动；40% 验证期逆波动为 +3.97% / -6.71% MDD / 7.08% 波动，最小方差为
  +4.53% / -7.29% / 7.37%。这证明当前数据下是收益与低波风险的取舍，不得以已见
  验证结果倒选 allocation 或激活策略。
- v6 发现全局 Stage-A 前 24 名均为 fixed g，故在不增加 Stage-B 总数（仍为 24）前提
  下，按训练排名给 fixed / 净利 CAGR / sustainable ROE 各保留 8 名 finalist，并保留
  全部 5 个预算 × 3 个 g × 2 个 allocation = 30 条验证路径。40% 最小方差路径中，
  fixed 为 +4.15% / -8.14% MDD / 7.08% 波动，净利 CAGR 为 +0.78% / -7.22% /
  6.39%，sustainable ROE 为 +5.14% / -6.19% / 6.29%。后者是一个值得前瞻验证的
  假设，不是可据已见验证期倒选的激活结论；所有结果仍是 qfq 代理。
- v8 只把 v6 的 12 只已有 PIT 标的扩大到 14 只，绝不扫描或持有完整 667 对文件。
  `20260920_055847` 主预检在 151 个数据合格扩展候选中，按冻结的训练前中位成交额及
  “每个 CSRC 行业首字母至多一只”规则，机械得出继原六只之后的
  `688111`（I65，软件和信息技术服务）与 `000963`（F52，零售）。预检产物会同时保留
  完整候选排序以供复算；扩池规则不读取收益、Ke 或验证期表现。v8 继承完全相同的
  搜参轴、训练/验证日期和 research-only 状态，因此其历史验证只能是同一已见区间上的
  稳健性探索，不能作为上线或线上 2% 的比较结论。
- v7 不再运行历史搜参，而从 v6 报告 hash 冻结两个前瞻观察臂：40% fixed g 的训练期
  对照，以及明确标为 validation-informed、不可激活的 40% sustainable-ROE 假设。
  清单要求只用 `2026-08-18` 后的新 PIT 数据比较，禁止中途改臂或参数；在同跨度线上
  NAV、原始分红/税费执行账和新 PIT 数据到位前，它仍不能触发部署。
- Gordon 策略原语不读取 ROIC，也不按 `instrument_type` 排除 ETF：只要某 ETF 在形成日
  有可审计的穿透 ROE、PB 和所选 g 方法所需输入，就与股票共用同一 Ke 搜参和组合优化。
  当前本地仅有 ETF 价格历史与日报当日 PE/PB 聚合，缺少上述逐形成日快照，故对历史
  回测暂时 fail closed；这不是资产类别排除，更不能拿今天的 ETF 估值回填过去。

### Ke + 低波动组合研究（2026-09-19，离线且未激活）

- 新增 `scripts/evaluate_ke_low_vol_portfolio.py` 与
  `src/experiments/ke_low_vol_portfolio.py`：冻结
  `gordon_implied_ke = g + (ROE_ttm-g)/PB`（`g=2%`），从 CSI300+CSI500
  当期成分的 PIT 基本面中筛出高 Ke、低 252 日波动候选；同一候选池同时比较
  长仓最小方差权重和等权对照。权重固定为单标的≤15%、行业≤25%、每行业≤2 名、
  协方差 25% 对角收缩；参数网格只读取 ranking 窗口。
- 参考快照为 `index_constituents_20260818.json`：800 只成分中 702 只有可用
  qfq 行情。由于首个形成日只有一只可投资候选，严格保留为预热窗；合同为
  `1 warmup + 13 ranking + 2 purge + 4 holdout`（9 个月测试、3 个月步进）。
- 产物 `data/analysis/ke_low_vol_portfolio/20260919_180917/` 显示：ranking
  选择 Ke≥10%、低波动惩罚 1.5、top-10；qfq 代理 holdout 的平均“相对每窗最强
  基准”净超额为 **-21.8133pp**，0/4 胜出，且最小方差平均比同池等权低
  **2.9987pp**。因此它没有超过旧 2.15pp 参考值，且风险配重在留出期没有带来
  增益。
- 该结果绝不进入活动策略或参考持仓：当前公司池仅是 2026-08-18 当期成分、无历史
  成分/退市成员；数据截至 2026-08-18（2026-09-19 时已 32 天陈旧）；持仓窗口中
  存在未拆解公司行动因子，raw 现金分红/税费/成交 Gate 均 fail closed。qfq 加配置
  0.5% 单边成本仅为研究代理，四个 holdout 又彼此重叠；需先完成最新全池回填、
  可执行公司行动和冻结后的新观察期，才可主张独立验证。
- 新增 `scripts/refresh_csi_reference_manifest.py`：从中证官方沪深300与中证500
  成分 XLS 生成带源文件哈希、HTTP 元数据和 `as_of` 的
  `index-reference-universe-1` 清单，绝不臆造权重。2026-09-18 的新清单为 800
  个当前成员；它仍不是历史成分史，不能消除历史窗口的存活者偏差。
- 该清单的隔离 PIT 回填先以 4 只样本验收：2017-09-18 至 2026-09-18 的行情、raw/qfq
  文件及带披露日期的财务报表均为 4/4 成功。完整 800 标的批次使用相同指纹和逐代码
  检查点继续运行，失败保留原因并可恢复，绝不覆盖 `data/point_in_time` 或活动策略。
- `evaluate_ke_low_vol_portfolio.py --current-industry-snapshot` 仅可把当前 Baostock
  行业快照用于“当前篮子”的行业上限；所有历史 ranking/purge/holdout 窗口仍只使用
  `--industry-history` 的已发布分类史，报告会显式记录两条来源与禁止倒灌的边界。
- 隔离基准回填已补齐 510300/510500 的 qfq 代理行情；其官方可得末日与成分股可能不同，
  因而评估器分别记录公司和最旧基准的数据截止日/新鲜度，任一超过阈值即令当前数据 Gate
  失败，而不以成分股的较新日期掩盖基准滞后。
- 评估器同时从 rolling holdout 中导出最后一个与所有 ranking 测试区间时间不重叠的
  9 个月 `final_chronological_lockbox`。它从参数选择中严格排除，并单独报告超额；这仅
  改善时间隔离，不会把当前成员池、qfq 代理或未解释公司行动伪装成独立可执行 lockbox。
- 研究 CLI 不再默认硬编码 2026-08-18 快照：未传路径时会校验并选择本地最新的
  `index-reference-universe-1` 清单，随后匹配同日期的隔离公司、基准与当前行业快照；
  旧快照仍可通过显式参数复现。若最新数据尚未完成，脚本应 fail closed，而非静默回退到
  陈旧数据。
- 新增 `scripts/validate_reference_universe_backfill.py`：完整批次结束后，它联合核对
  清单计数、批次完成状态、每只 raw/qfq 市场文件截至评估日、PIT 财报披露日期和当前行业
  覆盖率；公司行动的未解释诊断单独输出为 strict execution gate，不能被“数据文件齐全”
  的验收掩盖。回填未完成时返回非零并保留缺失清单，作为 fail-closed 的进度证据。
- 最新清单的 Ke 评估默认要求 `market bundle count == manifest count`；部分数据只能显式
  加 `--allow-partial-data` 做带标签的历史复现，不能生成“最新全池”结论或满足完整市场
  Gate。
- 当前篮子不再以空的“形成日当天公司行动”字段暗示可执行性；它会列出截至形成日已嵌入
  qfq 历史的所有未解释行动，并以 `current_basket_historical_action_ready` 单独设 Gate。

## Authoritative time and ranking contract (2026-08-02)

> This section supersedes every historical description below that calls the
> 12-month period Solver training, treats 13 windows as ranking windows, or
> puts standard deviation and Sharpe directly into the primary objective.

- The fixed horizon is 60 calendar months. The final 9 months are a one-time
  holdout; only the preceding 51 months are observable during selection.
- There are 14 rolling windows, each with 12 months of state lookback and 9
  months of evaluation, advanced by 3 months. The first 11 windows rank
  candidates, the next 2 are isolated because they overlap the holdout, and
  the final window is evaluated only after selection.
- `state_lookback_months: 12` initializes indicators, confirmation streaks,
  and signal locks. It is not Solver training data. Legacy `train_months` is
  accepted only as a configuration-read compatibility alias.
- The 252-session instrument eligibility count is computed over the complete
  instrument lifetime and must not reset at a state-slice boundary.
- One-level parameter sensitivity is a relative robustness check. Hard-gate
  failures are counted separately from numeric scores; the finite-neighbour
  drop and configurable feasible-neighbour ratio determine robustness. A
  neighbour or final selection is never required to have objective score
  greater than zero, because the cross-window range penalty can make an
  otherwise admissible objective negative.
- Instrument-pool robustness is an inner ranking-window cross-validation, not
  a search over symbol identities: finalists are replayed after each single
  instrument removal and reranked without opening the two isolation windows
  or final holdout. Future dynamic pool selection must search transferable
  feature/ranking rules (for example top-K conviction), never per-code include
  switches that turn timing optimization into historical stock selection.
- For ranking window i, define `M_i` as strategy return minus the median return
  of the three configured controls. The primary objective is
  `weighted_mean(M_i) - window_range_penalty * (max(M_i) - min(M_i))`; the
  configured penalty is 0.5.
- Sharpe and cross-window standard deviation remain diagnostics/Gate inputs;
  they are not duplicate objective penalties. The standard Gate still requires
  positive return in at least 6/11 windows, beating any two controls in at least
  6/11 windows, and positive mean `M_i`.

### 搜参数据源与标的池边界（2026-08-28）

- 配置中的监控标的、日报标的和回填标的池保持不变；点时数据缺失、覆盖不足或公司行为无法解释的标的，
  只在 `--optimize` 的候选输入预检阶段排除，并记录为 `search only`，不得从配置中删除。
- A 股 Baostock 价格覆盖完整但仅提供未拆解复权因子的记录，会额外探测 Yahoo；只有 Yahoo 同时满足覆盖起点和现金分红/拆分可执行性时才替换该搜参 bundle。
- Yahoo 公司行为时间戳先按 Unix 秒/毫秒解析，再解析 ISO 日期；并丢弃请求历史区间外的事件，避免时间戳被误读为公元 17/18 世纪日期。
- Solver、参数空间、预算、评分和 raw 成交/qfq 信号合同不变；搜参前统一检查公司行为可解释性，验证链路继续 fail closed。

### 三市场独立发布与 84 个月激活修复（2026-09-05）

- A 股、港股、美股的搜参、候选发布、相对比较和手动激活均按市场独立执行；
  某一市场没有候选，不再阻止其他市场保存候选或激活。
- 线上运行必须部署包含市场后缀的独立 `run_id`，并使用
  `python main.py --activate-run RUN_ID --group MARKET`；旧的单 run 三市场 gate
  不属于当前合同。
- 相对晋级比较的三市场默认策略只用于显式组合比较，市场本地比较会将比较范围
  收窄为一个市场，不修改 Solver、参数空间或评分公式。
- 激活校验按 artifact 中全部 holdout 窗口逐窗检查，多窗口 84 个月合同不再被
  旧的单窗口条件拒绝；每个窗口仍必须通过既有 Holdout 门槛。

## 基本面定价画像契约（2026-08-18）

- `src/fundamental_embedding` 是独立的逐时点研究链路，不进入当前技术面
  `SearchController`、活动策略或参考持仓。完整合同见
  `docs/llm/fundamental_embedding.md`。
- 训练只读取披露日不晚于特征日的财务快照，且每个训练标签必须在预测季度前完整
  实现。OOS 评价数据与当前无标签 `FundamentalPricingSnapshot` 分离。
- 四专家分别表示 value、cash return、quality 和 growth；12 维 embedding 由
  四专家信号、四 Gate 权重和四定价后信号组成。当前指数成员身份只用于评估分层，
  不能进入模型。
- “框架可用”不等于“有 alpha”。生产候选必须同时通过横截面宽度、字段覆盖、
  Rank IC、正 IC 季度、top-bottom spread、季度稳定、Gate 相对 uniform 增益和
  510300/510500/510880 三风格池鲁棒性验收。
- 画像驱动内在价值实验的详细合同见
  `docs/llm/intrinsic_value_strategy.md`。当前价格只允许进入反向DCF诊断，
  不得进入 DCF、DDM、盈利能力 DCF 或残余收益的现金流、增长和公允价值公式。
  股权现金流默认按7%/7.5%/8%个人必要回报率与2%永续增长估值，逐时点市场
  CAPM只作定价诊断和更高风险下限；显式主观风险只能在估值后形成买入折价。首轮86家公司
  的252/504交易日评价均未超过账面收益率强制基线，因此该模块保持离线候选，
  不得接入活动策略、搜参、日报或参考持仓。
- CAPM 股权 DCF 的买入价校准同样保持离线，合同见
  `docs/llm/capm_dcf_entry_calibration.md`。它按点时行业和财务数据将金融业路由为
  残余收益、周期业路由为穿越周期 FCFE、其他公司路由为普通 FCFE；三条链分别在
  训练期选择安全边际，再由合并 Gate 统一验收。任何一路或总体门槛不通过，均不得
  写入活动策略、搜参、日报或参考持仓。

### 冻结 CAPM-DCF 价值策略候选（2026-08-25）

> 本小节覆盖上文“CAPM-DCF 一律离线”的旧表述：通过广域训练/留出 Gate 的**冻结政策**
> 可以接入统一 `TradePlan -> Backtester` 链路作手动候选实验；它仍绝不自动激活，也不
> 替换现有技术面活动策略。

- `capm_dcf_value` 是独立注册策略，不向 Solver 暴露公司估值参数。广域训练集只选择
  估值路由、增长组合与基础安全边际；接收标的池只应用它，绝不按小池样本重新训练或
  因小池事件数拒绝该政策。
- 单家公司始终使用点时股权成本 `r_e = r_f + beta_i * ERP`。池平均 Beta **不**重估
  WACC/股权成本，只调节买入安全边际：
  `entry_fraction = clip(base_fraction * (pool_beta / train_beta_reference)^(-gamma), min, max)`。
  `train_beta_reference` 必须只来自冻结政策的训练集；默认 `gamma=0.32`、范围
  `[0.75, 0.95]`，因而低 Beta 池可将 `0.85` 放宽至最多 `0.95`。
- 历史校准与运行时采用同一成交语义：若 `entry_price >= P`，限价单已经可成交，历史标签
  在估值日以已知收盘价 `P` 成交，并从下一交易日开始评估持有结果；若 `entry_price < P`，
  则只在未来252个有效交易日内最低价首次触及限价时，以限价成交。不得把“目标价高于现价”
  误当作拒单。运行时在新估值快照当日只产生一次可执行买入事件；之后仅在首次向下穿越时
  买入；若 `P >= fair_value`，产生卖出决策，具体成交、手续费、最短持有期均仍由共享
  Backtester/执行器处理。
- DCF 每股价格以未复权口径计算。策略面板把公允价和买入价按同日
  `qfq_factor` 映射到统一回测的前复权价格尺度；两侧同时缩放，因而
  `raw_price <= raw_entry` 与 `qfq_price <= qfq_entry` 恒等，不把复权尺度错当成
  价值信号。没有可审计的价格因子、冻结政策、官方无风险利率、或估值快照超过
  550 天时，面板必须缺失并拒绝产生信号。
- `scripts/fetch_capm_dcf_risk_free.py` 以点时基准的自然季度最后有效交易日为锚点，读取
  A股的官方中债十年国债、港股的香港金融管理局两年期外汇基金票据、美股的美国财政部
  十年国债日曲线。每项只写带 `requested_date/source_date/source/maturity` 审计信息的
  真实值；缺失或超过8日陈旧的日期直接失败，不用未来、当期常数或跨市场利率回填。
- `capm_dcf_value` 的市场声明覆盖 A/HK/US，但候选必须逐市场独立提供：带发布日期的
  财务数据、同市场原始价格基准、官方无风险曲线、以及 `dataset.market` 完全匹配的广域
  冻结政策。港股财务只接受 HKEX 活跃证券表 + 披露易标题查询给出的发布日期及报告 PDF；
  美股只接受 SEC Company Facts 的已发布日期快照。报表币种与成交币种不同时，必须使用
  同一估值日或之前的显式点时汇率曲线；任何一项缺失即关闭该市场。当前只完成 A 股因果
  数据覆盖，尚未形成三市场完整候选产物，故无激活资格。
- `scripts/calibrate_capm_dcf_entry.py --market {a_share,hk,us}` 是冻结政策的市场本地
  生成入口：H/US 必须显式给出 `--benchmark-symbol`、`--market-currency`、官方曲线 JSON
  与必要的 `--fx-symbol`，并拒绝将 A 股冻结报告作为 `--frozen-policy-report` 应用到其他
  市场。`--fetch-risk-free` 只允许 A 股中债曲线。
- `scripts/backfill_point_in_time.py` 支持用 `--codes`、`--output-dir`、
  `--evaluation-date` 做隔离的 H/US 数据回填；H 股必须额外声明 `--market-only-symbol
  02800`，有 CNY 报表时声明 `--fx-symbol CNYHKD=X`，美股声明 `--market-only-symbol VOO`。
  `--hkex-report-kind year` 可在首次校准时只抓年度披露。HKEX 年报解析优先使用逐页的
  `pdfminer_stream`，按视觉行重建表格，并只从合并报表页读取当前审计数值；遇到跨业务段
  分行的收入小计时，才回退同一官方年报的五年财务摘要最新列。币种、单位和利润/收入的
  基本一致性校验失败即拒绝快照，不能以页面截断、当前基本面或跨市场利率回填。HKMA 的
  2Y EFN 官方端点可能按月末节奏发布，允许向后使用不超过45日的已发布值并审计其
  `source_date`；禁止未来填充，超过一个发布周期仍必须失败。
- `scripts/backtest_capm_dcf_value.py` 是该候选唯一的离线复现入口：它从点时市场
  存储读取 qfq 执行价、构造上述价值面板，并调用共享 `build_trade_plan` 和
  `Backtester` 生成 JSON。它不创建优化产物、不写活动指针、也不修改参考持仓。

## 当前唯一权威契约（2026-07-30）

> 本节是搜参、回测、日报与今日扫描的唯一当前契约。本文后续按日期记录的
> V1/V2、16 窗口、隔离窗口、A 股默认子集、`--all-markets`、`--strategy`
> 和 Bot preset 内容均为历史记录，已经废弃，不得作为当前实现依据。

### 通用搜索架构契约（2026-08-01）

- 当前源码按扩展职责分为 `src/strategy`、`src/search`、
  `src/backtest` 和 `src/experiments`。公共合同只从 `src.strategy` / `src.search`
  导入；具体策略只放在 `strategy/plugins`，具体优化算法只放在
  `search/solvers`。两类插件均自动发现，新增实现不得修改中央字典、
  `SearchController`、`Backtester` 或 `main.py`。离线 benchmark 和搜索深度
  分析位于 `src/experiments`，不得发布或激活生产参数。
- 三市场优化单元完全隔离：`a_share`、`hk`、`us` 必须在
  `config/config.yaml -> optimizer.markets` 中分别声明 strategy、Solver、Gate、
  Walk-Forward、execution 和 benchmark profile。`get_market_optimizer_config()`
  为每个市场创建独立约束实例；缺少字段、未知插件或不兼容参数直接失败，
  不读取 `optimizer.engine`、`strategy_by_group` 或全局 Solver/Gate 回退。
- `python main.py --optimize` 为每个市场生成独立 run、archive、checkpoint、
  readiness 和 HTML 报告；`--group` 只运行指定市场。候选必须使用
  `--activate-run RUN_ID --group MARKET` 显式激活，active manifest schema v4
  只保存每个市场的独立指针、strategy、Solver、配置指纹和激活时间。旧版单策略/混合
  manifest 不自动读取；日报、信号、回测和参考组合遇到未激活市场时 fail closed。
- 生产代码不再包含 `GeneticOptimizer`、`ScoredEncoding`、策略专属
  `scanner.py` 转发层或 percentile 的独立评分扫描 API；候选参数直接以
  `Params` 在统一评价和验证链路间传递。旧 YAML 字段转换只允许存在于
  `src.search.artifacts` 读取/激活边界，核心搜索和策略实现不得回退到旧结构。
- 生产搜索链路固定为
  `SearchController -> Solver.ask/tell -> EvaluationService -> CandidateGatePipeline
  -> ValidationController`。`SearchController` 只管理预算、缓存、排名窗调度、
  排名档案和 checkpoint；其中不得按 Solver ID 或策略 ID 分支。新增优化算法只需
  新增并注册一个实现 `initialize / ask / tell / should_stop / finalists /
  state_dict / load_state_dict` 的 Solver 模块，再在每个
  `optimizer.markets.<market>.solver_id` 中显式选择。
- 当前注册 Solver 为 `genetic`、`local_genetic`、`random` 和严格单线
  `simulated_annealing`。`local_genetic` 保留随机初始化，生成阶段使用
  90% 交叉后局部变异子代和 10% 确定性配额随机移民；每个非移民子代至少
  变异一个活跃参数，最大档距按代数从 3 收缩到 1，并在 Solver 边界去重。
  生产搜参默认使用 `local_genetic`；预算保持每市场
  `30,000 + 5 × 25,000 = 155,000` 次、5 代不变。新 Solver 只可通过
  各市场的 `solver_id` 显式选择。`--optimize` 使用跨进程单实例锁；日调度默认不
  自动启动全量优化，必须通过 `scheduler.optimize_enabled: true` 显式开启。
  要求梯度的未来 Solver 必须在启动前通过能力协商；当前
  Evaluator 声明 `cpu_scalar`、`cpu_batch` 与 `cpu_process`，无梯度、无 GPU 后端。神经网络若
  输出候选参数可实现 Solver；若直接输出逐日买卖信号，则必须实现策略插件。
- `ParameterSchema` 是唯一参数合同，声明类别、布尔、顺序、连续、权重、条件参数、
  合法邻域、变异步长、参数组和 `transfer_key`。旧 `ParamDim` 只在适配边界转换。
  `ParameterSpec.local_values()` 为所有 Solver 提供通用 ±n 档邻域；无序分类
  参数只能切换到其他合法类别。稳健性检查使用 Schema 生成的逐参数相邻档位，
  不再调用策略随机改变整组参数。
- `CandidateGatePipeline` 只消费不可变的排名窗原始指标。Gate Profile 的
  `hard / penalty / diagnostic`、阈值、区间、窗口数量和比例完全由配置声明；代码
  不硬编码窗口门槛。`standard` 当前要求至少 6/11 窗正收益，并在至少
  6/11 窗战胜三个基准中的至少两个；相对每窗第二强基准的平均超额也必须为正。
  三个控制基准按市场配置：A 股为 `510880 / 510300 / risk_free`，港股和美股为
  `VOO / BRK.B / risk_free`。排名 Gate、留出验收、标的池敏感性和激活必须读取
  同一控制集合，缺一项即失败闭合。
  `exploratory` 不具备激活资格。隔离窗和留出窗永远不能进入 Solver、Gate、SearchArchive、
  迁移先验、早停或代理模型。
- `FeatureRegistry` 注册现有 22 列技术输入并转为跨标的可比特征；特征使用
  `float32`/布尔 mask，现金和 NAV 使用 `float64`。`technical_ensemble` 是完整
  22 列与批量内核的参考策略，固定因子经济方向且基本面依赖为空。该策略的
  买入条件需连续 3 个交易日确认，确认状态只产生一次 entry event；条件必须
  先失效才会重新解锁。训练段负责预热确认状态，但测试账户从空仓开始，训练期
  已触发的 event 不得在测试首日重放。执行统一使用 `target_weight`：连续评分
  决定相对仓位，参数仅搜索单标的上限 15%/20%/25% 和总仓上限
  60%/80%/100%，由 Numba 通用执行器在状态变化时分配，不按策略 ID 分支。
- `PortfolioTrace`、`WindowStats`、`EvaluationReport` 统一携带三项决策诊断：
  `signal_event_count` 统计 false→true 的进出事件而非持续信号日，
  `cash_rejected_order_count` 统计因现金/最小手数无法成交的可执行买单，
  `concentration_hhi` 是有持仓交易日的平均持仓 HHI（单标的为 1）。优化和
  日报使用同一统计实现，现金批量内核与标量/目标仓位路径必须一致。
- 最终候选额外计算因果的“同入选篮子长持”诊断：每只标的只在第一次真实
  entry event 时买入一次，之后不卖、不加仓、不再平衡；禁止把未来最终入选
  名单搬到窗口首日。该诊断及其 `timing_value_add` 不进入 Solver、Gate 或
  SearchArchive。跨策略 benchmark 同时保存原始收益最高候选、Gate 最终候选
  和最终候选逐参数 ±1 档邻域，用于区分策略表达上限、Gate 取舍和搜索漏解。
- `EvaluationService` 优先调用 `prepare().evaluate_batch()`，否则使用
  `evaluate_one()` 标量回退；两条路径必须得到完全一致的信号、成交和指标。
  `ResourcePlanner` 每次只启用一个并行轴，禁止嵌套线程池超卖。
- **2026-08-02 authoritative execution correction:** scalar candidate fallback
  now runs in a persistent spawn-based `ProcessEvaluationPool`. The parent is
  the only owner of Solver `ask/tell`, Gate, archive and checkpoint state.
  Workers receive an immutable ranking-window snapshot once; subsequent IPC
  carries only columnar `CandidateBatch` shards and compact objective/raw Gate
  metrics/failure reasons. Full `WindowStats` are materialized only for final
  candidates. Worker completion order is restored to input order before
  `tell`; a partially failed batch is never told or checkpointed. Isolation
  and holdout data are never installed in ranking workers. Candidate worker
  processes force Numba/MKL/BLAS to one thread to prevent nested oversubscription.
- 新产物记录 Solver 配置及参数、特征、Gate、数据、成交、窗口和搜索合同哈希；
  SearchArchive 只写排名窗。checkpoint 仅在合同哈希相同时恢复，finalist 仍由
  排名窗 EvaluationService 重放，不读取隔离或留出数据。
- Search memory is bounded by `search.candidate_retention_ratio` (default
  `0.05`). `EvaluationService` retains only the current best
  `ceil(evaluated_so_far * ratio)` compact records. Genetic Solvers retain
  fixed parents plus current-phase leaders and compact them to one population
  at a generation boundary. The complete ranking history exists only in the
  JSONL `SearchArchive`; finalists are streamed back from that archive before
  full `WindowStats` materialization. Checkpoints persist only this compact
  replay state and are rejected when the configured retention ratio changes.
- Disk retention is independent of the in-memory candidate ratio.
  `search.run_retention_count` defaults to `3`: the newest three complete
  run manifests are retained, while the currently active run and every older
  run directory referenced by its immutable artifacts are protected in
  addition to that limit. Stale no-manifest or interrupted run directories do
  not count as results and are removed before the next search or after their
  failure notification.
- Real CLI `python main.py --optimize` is supervised by the lightweight
  `src.optimizer_guard` parent. A non-zero child exit or SIGKILL produces one
  shared failure summary with per-market archive progress, writes a transient
  `optimizer_failure.yaml` for notification assembly, removes the partial
  multi-gigabyte run directory, and leaves the previous active run untouched.
- 交互配置只允许修改实际生效且通过 Schema 校验的字段：solver、budget、
  gate_profile、positive_windows、majority_windows、min_pos、max_dd、
  window_range_penalty、workers、batch_size, candidate_retention_ratio，以及统一现金档位、手续费和
  初始本金。/switch_optimizer 只选择下一次 --optimize 的候选策略，不改变当前
  生产运行；data_years、旧 hard_constraints 和策略专属规则槽位不再对 Bot 暴露。
  配置先写临时 YAML 并调用正式 load_constraints() 校验，失败时不得替换原文件。
- 参考持仓是独立的持久化模拟账户。只有手动 /ref_date 重置会清仓并把 A/HK/US
  三个资金池绑定到当时最新的三市场完整已激活 run_id；后续搜参或激活不会静默
  切换既有参考持仓。绑定同时记录策略、参数哈希和完整执行合同哈希（本金、手续费、
  最短持有期、市场手数、汇率及策略执行参数）；任一合同无法恢复或变化时必须
  停单并提示再次手动重置。旧 YAML 没有绑定信息时可继续只读展示，但禁止交易。
- v4 活动索引不自动迁移旧 schema v2 指针；升级后若活动指针为
  v2（latest_strategy.yaml schema_version=2），load_strategy_run/load_latest_strategy_run
  均返回 None，参考持仓会统一停单、策略扫描退化为 0 信号。恢复方式为显式运行
  scripts/migrate_legacy_optimizer_pointer.py --apply（备份 + 原子迁移 + 绑定校验），
  不会重新评估 Gate、不修改参考持仓文件；新候选仍需通过验证后明确激活。
- 新增固定规则价值策略 `justified_pb_value`（仅注册与评估，未激活、未改配置）：
  PIT 季度财报面板（roe_ttm 百分比、book_yield=1/PB，季度锚点披露日可用+前向填充）→
  HistoricalDatasetEnricher → StrategyMarketData.fundamental_features → 同一
  TradePlan(target_weight) 执行引擎。买入条件 PB < (ROE%−2)/8（Ke=10%, g=2%），
  卖出条件 PB > (ROE%−2)/4（Ke=6%, g=2%），中间持有；每只≤20%、总暴露 100%、
  源变更/价格下穿买入带时一次性入场（引擎做槽位等比缩放）。A 股专用；入口：
  src/strategy/plugins/justified_pb_value.py +
  src/strategy/justified_pb_value_context.py；
  评估脚本 scripts/backtest_justified_pb_value.py（整段 84 个月 + 22 窗口 walk-forward，
  输出 data/analysis/justified_pb_value/<ts>/report.json、windows.csv）；
  测试 tests/test_justified_pb_value.py。数据源为本地 data/point_in_time（服务器
  fundamentals 仅 000958，A 股补数后可在服务器重跑）。
- 指数池筛选分析脚本 scripts/screen_csi300_500_justified_pb.py（沪深300+中证500
  全部成分，2026-08-18 指数成分表 + reference_universe PIT 数据集）：g 取
  min(前16窗中位窗口收益年化/2, 0%)，Ke=10% 筛选（PB<(ROE−g)/(10%−g)），剔除
  银行/证券/保险（J66/J67/J68）后按 justified/PB 取 top20，Ke=5% 作为卖出阈值，
  在最后4个窗口做样本外评估。注意：reference PIT 只有2020-08起72个月数据且公司行动
  仅存原始调整因子，几何改为20窗口(9m测试/3m步长/6m边界)，样本外评估用前复权价格
  口径（qfq，无佣金、无再投资）。产出 data/analysis/justified_pb_csi300500/<ts>/。
- 简报参考持仓不得消费逐标的 scan_today 告警或自定义 20%/25% 仓位规则。
  它按固定 run 的全市场数据调用公开
  build_trade_plan -> TradingStrategy.make_signals，只执行计划最后有效交易日，
  并按 cash_cap 或 target_weight 通用声明成交。实盘参考账户买入使用触发日已知
  最高价、卖出使用触发日最低价、估值使用收盘价；同一
  run_id/date/symbol/side 事件只处理一次，避免早盘和午盘简报重复成交。
  日报继续只读，不调仓。
- 以下两项 CPU 批量和搜索深度结果属于 `technical-ensemble/1` 的历史
  `cash_cap` 合同，仅用于解释旧候选，不能作为 `technical-ensemble/2`
  一次性事件与 `target_weight` 合同的当前性能或收益依据。
- 2026-08-01 CPU 批量验收使用完整 A 股配置中 11 个具备完整历史的标的、
  11 个排名窗和同一组 1,000 个 `technical_ensemble` 候选。标量路径耗时
  `49.887s`（`20.05 candidates/s`），Numba `prange` 批量路径耗时
  `2.188s`（`456.95 candidates/s`），加速 `22.80x`；目标分最大误差为 `0`，
  峰值 RSS 为 `0.363 GiB`。批量内核只接管通用 `cash_cap` 合同，日期循环仍在
  每个候选内部串行；其他执行模型自动回退统一标量接口。
- 同日以完全嵌套的 RandomSolver 候选流重新执行 `technical_ensemble` 搜索深度
  实验，检查点为 `1,000 / 2,800 / 4,600 / 6,400 / 8,200 / 10,000`。三市场
  平均最优原始 WF 分为 `-2.858 / -1.911 / -0.356 / -0.356 / -0.356 /
  -0.356`；2,800 起三个市场均出现通过排名 Gate 的候选，4,600 后继续增加
  算力未改善最优分。按 5% 持续边际提升规则，平衡点为每市场 **2,800** 个
  候选。该实验不读取 2 个隔离窗或 1 个留出窗，不修改生产预算和活动策略。
- Gate 改为“6/11 正收益、6/11 胜至少两个基准”后，使用完整配置和上述平衡
  预算重新运行 `technical_ensemble`（RandomSolver，每市场 2,800 候选）。A 股、
  港股、美股分别有 `102 / 244 / 562` 个候选通过全部排名 Gate，选择均来自
  `best_eligible`，而非不可行候选兜底。排名窗结果依次为：A 股平均收益
  `12.31%`、最强基准超额 `4.12%`、`9/11` 正收益、`8/11` 胜至少两个基准；
  港股 `19.77% / 2.50% / 10/11 / 9/11`；美股
  `19.13% / 3.36% / 10/11 / 10/11`。独立留出仅港股胜最强基准：A 股
  `-3.99%`（超额 `-5.44%`）、港股 `13.18%`（超额 `10.41%`）、美股
  `6.86%`（超额 `-3.49%`）。因此本次仅是 Gate 与搜索候选验收，三市场候选
  不具备原子激活资格，也未修改活动策略。原始报告位于
  `data/analysis/strategy_benchmark/20260801_212051/`。
- `technical-ensemble/2` 完成一次性三日确认、训练事件不重放和目标仓位后，
  用完整配置按三市场各 2,800 候选重跑。RandomSolver 的 A/HK/US 排名窗
  平均收益为 `7.44% / 9.93% / 13.54%`，最强基准超额为
  `-0.76% / -7.34% / -2.23%`，留出超额为
  `-8.31% / -7.33% / -0.24%`。排名窗平均信号事件数为
  `95.3 / 31.3 / 29.8`，平均现金拒单为 `3.8 / 0 / 0`，平均持仓 HHI
  为 `0.183 / 0.374 / 0.280`。同入选篮子长持与策略的排名窗收益差仅
  `0.44 / 0.97 / 0.18` 个百分点；A 股留出期择时反而贡献 `-8.26` 个百分点。
  报告位于 `data/analysis/strategy_benchmark/20260801_233703/`。
- 同预算 GeneticSolver 将 A/HK/US WF 分从
  `-4.099 / -11.445 / -4.121` 改善到
  `-2.445 / -8.580 / -3.256`，A 股留出超额从 `-8.31%` 改善到
  `-1.48%`，但三市场留出仍全部未胜最强基准。Random 的 A 股最终候选仅把
  `weight_ma60` 下调一档，平均收益即可从 `7.44%` 升到 `8.63%`；GA 最终
  候选也都存在目标分更高的 ±1 档邻居。因此“2,800 次随机搜索边际平衡”
  只能证明继续盲抽的边际收益低，不能证明高维空间局部收敛；当前 Solver
  需要通用局部精修阶段，不能把漏解误判为策略表达上限。GA 报告位于
  `data/analysis/strategy_benchmark/20260801_234440/`。两轮均未激活候选。
- 新增独立 `local_genetic` 后，以相同数据、Gate、固定种子和每市场
  2,800 候选重跑 `technical_ensemble`。预算拆分为 700 个随机初始候选和
  3 代各 700 个子代，局部档距依次为 3/2/1，每代含 10% 随机移民。
  A/HK/US 均完成 2,800 次发行和 2,800 个唯一参数，停机原因均为
  `completed_budget`；重复运行的参数和分数完全一致。相对同指纹旧 GA，
  WF 分变化为 `+0.495 / -1.455 / -0.791`，留出超额为
  `-2.67% / -7.24% / -0.40%`，三市场仍全部失败。最终候选仍分别存在
  目标分高 `0.209 / 0.587 / 0.230` 的可行 ±1 档邻居，说明局部子代生成
  消除了重复预算但不能替代冠军精修。报告位于
  `data/analysis/local_genetic_benchmark/20260802_094638/`，未激活候选。

- 唯一优化入口是 `python main.py --optimize`。活动策略只由
  `config/config.yaml` 指定；一次运行该策略覆盖配置中全部有效的 A 股、
  港股和美股标的，各市场使用独立资金池、手数和汇率配置。
- 唯一决策与仿真链路是
  `StrategyMarketData -> TradingStrategy.make_signals -> TradePlan -> Backtester
  -> EvaluationReport`。策略只输出逐日逐标的买卖事件、连续评分、目标仓位
  与通用执行声明；
  搜参、日报和今日扫描不得按策略 ID 分支，今日扫描只读取完整计划最后一个
  有效交易日。
- `TradePlan` 不携带具体成交价。唯一 `FillPricePolicy` 由 `Backtester` 和
  优化器共享：买入价取触发日前一日、触发日、后一日最高价的最大值，后一日
  只用于悲观成交定价而不进入策略信号；卖出价取触发日最低价，NAV 始终用
  触发日收盘价估值。评价窗口最后一日缺少后一日价格时，买单标记待执行且
  不计入窗口。`cash_cap`、`target_weight` 和全部配置的可交易控制基准必须消费
  同一个成交合同，不得按策略、市场或参考标的另建成交路径。
- 权威搜参跨度为 84 个自然月：22 个窗口、12 个月状态预热、9 个月测试、
  3 个月步长。前 16 个测试窗参与排名，接下来 2 窗因与留出期重叠而隔离，
  最后 4 窗只作独立留出。隔离窗和留出窗不得参与排名、敏感性、删标的
  稳健性或候选选择。
- 所有策略按市场统一比较三个配置控制基准：A 股为无风险、510300、510880，
  港股和美股为无风险、VOO、BRK.B。可交易基准使用同样的 0.5% 单边费率、
  自身市场手数/汇率和悲观买价；同池静态等权仅作诊断，不参与 Gate。
  每窗排序仍记录相对三个配置基准中最强者的超额，Gate、留出验收和激活则
  要求超过任意两个基准，即相对当窗第二强基准的超额为正。
- 默认 `standard` Gate 要求至少 6/16 排名窗正收益、至少 6/16 窗战胜
  三个基准中的至少两个，且相对每窗第二强基准的平均超额为正；该门槛完全
  来自配置，可替换 Profile 或改为比例，再应用回撤、交易密度、稳定性和
  夏普约束。参数敏感性逐参数 ±1 档；标的池敏感性是独立且权重更高的门槛：
  前 20 个候选都要执行代码顺序不变性和逐一删标的，至少 80% 删减变体的
  平均“胜任意两个基准”超额仍为正，并按最差下降施加 2 倍惩罚。第一名
  不通过时必须回退到更稳健的后续候选，不能只在选完后写一条诊断。
- 日报加载最近一次完整成功激活的参数，不重新搜索；各市场以自身最后有效
  交易日为终点，评估最近 273 个日历日。日报也使用同一三重基准；其他
  展示参考标的可以保留，但不改变“最强三基准”超额口径。
- 胜率是在每个有效起始日比较策略与参考标的各自持有到评估终点的收益；
  参考价格仅向后对齐，禁止使用未来数据。周 NAV 按自然周聚合 OHLC，短周
  和单交易日周保留；季末持仓取自然季度最后有效交易日的真实模拟状态。
- `EvaluationReport` 是 HTML、PDF、邮件、飞书和 Telegram 的唯一数据源，
  显式包含测试期指标、各参考标的结果、初始/期末资产与现金、期末持仓、
  日 NAV、周 NAV OHLC、季末模拟持仓和胜率明细。
- 注册策略包括 `percentile`、`simplified`、`regime_pullback`、
  `technical_ensemble`、`capm_dcf_value`、`valuation_aware_ensemble` 和实验性 `ma60_band`。
  `percentile`、`simplified` 和 `capm_dcf_value` 声明 `cash_cap`；
  `regime_pullback` 声明 `target_weight`，实现 MA200 上升趋势中的
  回撤准备、三日恢复单次确认、30 日正常退出和固定 3ATR 灾难退出。
  `technical_ensemble` 是 22 技术列、批量评价和跨 Solver 比较实现；
  `ma60_band` 只用于四只 A 股的固定规则诊断，不代表 A/HK/US 生产策略。
- `regime_pullback` 和 `technical_ensemble` 搜参按市场保存候选，绝不自动替换当前活动策略。
  候选必须留出胜出且通过全部稳健性门槛，之后由
  `python main.py --activate-run <run_id> --group <market>` 原子激活。
- 新产物写 `strategy_id`、参数结构标识、执行快照、16 个排名窗、2 个隔离窗、
  4 个留出窗、三重基准和删标的报告。旧 YAML 字段只能在
  `src.search.artifacts` 读取边界迁移，不得泄漏进核心。

## 2026-07-31：类型化标的画像与财务/基金穿透契约

- `python main.py --audit-instruments` 对 `config/config.yaml` 全量标的生成
  `data/instrument_audit/latest.json` 和 `latest.html`。当前画像只供独立审计
  与日报展示；优化器、历史 `StrategyMarketData` 和 `TradePlan` 禁止导入
  当前财务、当前 NAV 或当前 ETF 成分。
- `InstrumentType` 明确区分公司、指数 ETF、行业 ETF、债券 ETF、商品 ETF、
  REIT 和主动基金。`not_applicable` 不进入缺失率分母；`known_zero`、
  `not_meaningful`、`missing`、`stale` 和 `conflict` 必须保留原义，禁止用
  默认值伪造填充。
- 每个 `MetricValue` 保存数值、状态、报告期/as-of、实际披露日、来源、
  币种、说明和冲突备选值。历史查询只允许
  `published_at <= evaluation_date`；无法取得披露日的 Yahoo 快照只能用于
  当前审计，不能进入历史窗口。
- 公司估值使用当前价与最新已披露报表推导：`PB = 现价 / BVPS`；
  `PE_TTM = 现价 / TTM稀释EPS`，缺 EPS 时使用市值/TTM归母净利润；
  `ROE_TTM = TTM归母净利润 / 平均归母净资产`。`PB/PE` 只作交叉校验。
  利润或净资产非正时估值标记为 `not_meaningful`，不展示误导性负倍数。
- A 股累计报表先还原单季度。只有相邻报告期为 55–125 天时才计算环比；
  港股半年频率不得伪装成季度环比。四个连续季度不足时，可用最新已披露
  年报作为明确标注的 TTM 回退，并只计算可验证年同比。
- 指数/行业 ETF 保存发行人、跟踪指数、AUM、费率、NAV、溢折价、分红率、
  带日期的前十大成分和合计权重。成分公司复用同一公司画像；
  穿透 PE/PB 按盈利/账面收益率聚合，穿透 ROE 按穿透盈利/净资产计算，
  增长率只给逐只值及加权中位数。每项聚合必须显示其有效权重覆盖率。
- REIT 单列 NAV、P/NAV、披露口径 TTM FFO、每份 FFO、P/FFO、分派率、
  资产类型和出租率；不从普通净利润强推 FFO。商品和债券基金分别展示
  跟踪偏差或久期/到期收益率，不进入公司 PE/PB 缺失率。
- 数据合并优先级为配置的发行人/官方资料与 SEC Company Facts，其次才是
  Yahoo/QQ 和明确标注的基金披露镜像。冲突值不静默覆盖。日报只加载最近
  一次完整审计文件，不在日报或搜参过程中临时抓取财务/成分。

## 2026-07-31：`regime_pullback` 首次全量验收

- GA、逐参数敏感性和删标的稳健性只执行 11 个排名窗；参数和稳健性选择
  完成后，只有最终参数会一次性执行完整 14 窗并读取 2 个隔离窗和 1 个留出窗。
- 参数无关的窗口输入、日期序号和三重基准 NAV 按市场缓存；相同 genome
  去重。候选评估用固定顺序的 4 进程 `joblib/loky` 执行，随机种子和结果
  顺序不因并行改变。
- 使用 `config/config.yaml` 全量标的和生产预算完成 A/HK/US 三市场首轮：
  每市场 30,000 个参数均未通过 11 窗排名收益门槛，三组结果均为 0 个候选。
  运行未生成可激活参数，活动运行继续保持 `percentile`；不得因本次失败
  放宽最强三基准、7/11 胜窗或独立留出门槛。

## 2026-07-31：`regime_pullback` 搜索深度边际实验

- 搜索深度定义为 Phase 1 实际生成并评估的随机参数数，不是
  `phase1_top_keep`。当前生产配置中的 `10,000` 是 Phase 1 保留上限；实际
  配置预算为每市场 `30,000 + 5 × 25,000 = 155,000` 次候选评估。
- 实验固定生产随机种子 `20260726`，对同一候选序列使用严格嵌套的
  `1,000 / 2,800 / 4,600 / 6,400 / 8,200 / 10,000` 六个前缀。只评价
  11 个排名窗，2 个隔离窗和 1 个留出窗均未读取；因此搜索预算的选择没有
  污染留出集。
- 三市场平均最优原始 WF 分从 `-17.426` 提高到 `-17.118`，增加 10 倍
  搜索量只带来 `0.308` 分改善。相邻档位的效果改善率依次为
  `0.000% / 0.153% / 0.220% / 0.000% / 0.000%`；效果改善率按
  `score_index = 100 + mean_wf_score` 的相邻相对变化计算，避免负分直接
  相除造成符号失真。三市场合计实测评估时间由 `31.1s` 增至 `303.7s`。
- 所有深度、所有市场的排名收益门槛通过数均为 0。A 股、港股、美股在
  10,000 深度的最优原始 WF 分分别为 `-10.730 / -21.209 / -19.414`；
  对最强基准的 11 窗平均超额分别为 `-7.638% / -15.900% / -15.745%`，
  胜窗均为 `0/11`。这说明当前失败的根因是策略族/参数空间没有产生可用的
  择时优势，不是搜索深度不足。
- 按“后续所有算力增量均无法带来至少 5% 效果提升，且没有后来出现的
  三市场排名合格候选”定义，边际平衡点为每市场 **1,000** 个 Phase 1
  随机候选。该结论仅用于诊断 Phase 1；不自动修改生产搜索配置，也不构成
  放宽门槛或激活候选的依据。
- 可复现实验入口为 `python scripts/analyze_search_depth.py`；明细、JSON 和图
  位于 `data/analysis/search_depth/regime_pullback_20260731_011908/`。

## 2026-07-31：注册技术策略统一基准

- 可复现入口为
  `python scripts/benchmark_technical_strategies.py --depth 1000
  --market-workers 12 --evaluation-workers 1`。四个注册技术策略分别在
  A 股、港股和美股使用相同随机种子、每市场 1,000 个 Phase 1 候选、
  11 个排名窗、2 个隔离窗和 1 个留出窗；市场汇总使用等权口径。
- 主进程先为每个市场抓取一份不可变行情与基准快照，再将同一快照复制给
  四个策略进程。产物保存逐标的和基准 OHLC 指纹，防止并行数据刷新、缓存
  竞争或数据源回退破坏可比性。每个任务内部只开 1 个评价 worker，避免
  12 个外层进程再次嵌套多进程。
- 参数只由 11 个排名窗选择。2 个隔离窗和最后 1 个留出窗只在参数选定后
- **2026-08-02 benchmark scheduling correction:** when worker flags are omitted,
  the CLI selects exactly one process axis. A small strategy/market job set runs
  sequentially with all physical cores assigned to persistent candidate-scoring
  workers; a large job set uses strategy-by-market outer processes with one
  evaluator each. `--market-workers` and `--evaluation-workers` cannot both be
  greater than one.
  评价；留出结果不反馈选参。运行只写
  `data/analysis/strategy_benchmark/<timestamp>/` 下的 JSON/CSV，不写优化器
  活动指针，不生成候选运行，也不自动激活策略。
- 2026-07-31 首轮结果曾记录为：`builder +1.907%`（2/3 胜出）、
  `percentile -0.927%`（2/3）、`regime_pullback -4.047%`（0/3）、
  `simplified -9.810%`（0/3）。该次运行中旧策略、`regime_pullback` 和两个
  可交易基准没有消费同一成交价路径，且 `builder.absolute_discount` 使用了
  未来区间最大收盘价；因此该产物及其策略排序已作废，不得作为投资或激活依据。
- 修复后的唯一成交合同为“买入三日最高、卖出当日最低、NAV 当日收盘、窗口
  末买单待执行”。同时将 `builder.absolute_discount` 改为截至当日的历史运行
  高点，不再读取未来。用相同冻结行情、种子、1,000 个 Phase 1 候选和
  12 个外层进程重跑，三市场等权留出超额为：`regime_pullback -4.047%`
  （0/3）、`percentile -4.170%`（2/3）、`simplified -4.497%`（2/3）、
  `builder -9.980%`（0/3）。33 个排名窗平均超额依次为 `-13.225%`、
  `+1.601%`、`+3.068%`、`+2.901%`。
- `regime_pullback` 的结果与旧运行完全一致，因为它原先已经使用目标中的悲观
  成交路径；其三市场均保持现金，仍在三个留出窗输给最强基准。`builder` 留出
  均值从旧产物的 `+1.907%` 降至 `-9.980%`，且从 2/3 胜出变为 0/3，说明旧
  正结果不可采信。该差值同时包含成交路径统一、基准成本统一和重新选参的共同
  影响，不应全部归因于单一代码缺陷。
- 修复后没有任何策略通过三个市场的独立留出：`percentile` 与 `simplified`
  各胜 2/3，但分别在 A 股和港股发生显著负超额；`builder` 与
  `regime_pullback` 为 0/3。因此本轮不激活任何候选，活动策略保持不变。
- 修复后可复现产物位于
  `data/analysis/strategy_benchmark/20260801_004324/`；12 个策略×市场任务的
  并行纯计算耗时为 `76.625s`，数据预取发生在计时之前。生产配置仍为每市场
  最多 `30,000 + 5 × 25,000 = 155,000` 次候选评估，本 benchmark 不修改
  该配置。
- 旧 `simplified` 结果曾出现 33 个排名窗平均超额 `+9.539%`、三个留出窗全败，
  是明显的样本内选择偏差。`regime_pullback` 三市场均无排名合格候选，且
  排名/留出均为负；结合 1,000→10,000 搜索深度边际实验，本问题应优先
  修改策略状态机或参数空间，而不是增加搜索预算。

## 2026-09-05：84 个月注册策略统一基准

- benchmark 入口已切换到当前 84 个月合同：22 个窗口，其中 16 个 ranking、
  2 个 purged、4 个 holdout；holdout 指标按 4 个重叠窗口等权平均收益、超额和
  Sharpe，最大回撤取最差窗口。窗口产物同时记录 `role`、`role_index` 和
  `global_index`。
- 预取优先使用点时 raw/qfq/company-actions bundle：qfq 只用于指标和信号，raw
  用于成交、估值和基准；A/HK/US 基准均经过同一公司行为可解释性校验。
  A 股历史不足的配置标的只在 benchmark 输入阶段标记，不修改监控池。
- 最新离线运行命令为
  `python scripts/benchmark_technical_strategies.py --depth 1000
  --market-workers 12 --evaluation-workers 1`，产物位于
  `data/analysis/strategy_benchmark/20260905_093953/`。6 个纯技术策略的
  18 个市场任务完成；`capm_dcf_value` 与 `valuation_aware_ensemble` 因应用配置
  未提供历史基本面 context enricher 而 fail closed，未用 adjusted-only 或模拟
  数据替代，也未修改生产激活指针。
- benchmark CLI 现支持 `--markets` 独立运行市场。2026-09-05 对 A 股的
  `percentile`、`simplified`、`regime_pullback`、`technical_ensemble` 各运行
  60,000 个候选，仍使用 84 个月和 `22/16/2/4` 窗口；8 个标的通过完整历史校验，
  5 个标的因覆盖不足只记录在数据就绪报告中。独立产物位于
  `data/analysis/strategy_benchmark/20260905_124813/`。A 股 holdout 汇总为：
  `technical_ensemble +8.893% / -5.768% / -7.030% / 0.762`，
  `percentile +6.108% / -8.553% / -10.230% / 0.928`，
  `regime_pullback -3.383% / -18.043% / -7.580% / -1.335`，
  `simplified 0.000% / -14.660% / 0.000% / 0.000`，依次为收益/最强基准超额/
  最差回撤/Sharpe。`technical_ensemble` 相对当前活动指针 `percentile` 在收益、
  超额和回撤上改善，但 4 个 holdout 中只有 1 个跑赢最强基准，暂不自动激活。

---

## 以下为历史记录（已废弃，不具权威性）

# 股票量化系统 - 关键设计决策文档

**文档版本**: v1.22
**最后更新**: 2026-07-15

---

## 2026-07-26: A-share optimizer integrity repair

- The 9-month test / 3-month step overlaps the newest 9-month hold-out.  The
  new `purge_overlapping_windows` switch excludes overlapping windows from
  ranking, constraints, GA selection and sensitivity.
- The current A-share horizon uses 13 strict ranking windows, 2 purged windows
  and 1 daily-report validation window; artifacts record those counts.
- Fast optimizer scoring now uses the configured primary benchmark and the
  same position fraction, fill model, lot handling and fees as daily reports.
- Sharpe shortfall and deterministic sensitivity checks re-rank the top
  finalists by a robust selection score.
- `python main.py --optimize` now runs A shares only; `--all-markets` retains
  the three-market entry point and an A-only publish retains compatible HK/US
  artifacts from the active manifest.
- Percentile alerts receive historical data and apply the optimizer's weighted
  aggregate signal; raw snapshot values are no longer treated as percentiles.

## 2026-07-27: A-share absolute-return robust selection

- Candidate gates use only the 13 ranking windows: weighted strategy return
  must be positive and at least 8/13 windows must be positive.  Purged and
  held-out validation windows remain excluded from every selection decision.
- The final robust score is `wf_score - 1.0 * sensitivity_drop`; sensitivity
  is evaluated for the top 500 ranking-qualified candidates.  All three
  values are configurable under `genetic_search`.
- Versioned artifacts, optimizer reports and the daily held-out backtest
  section record ranking-window absolute return, positive-window count,
  sensitivity diagnostics and selection score.  These are selection-only
  diagnostics, separately labelled from the validation-period result.

## 2026-07-26: Three-market backtest recovery

- Restored `main.py --optimize` and its `--optimize-v2` compatibility alias.
  The retained optimizer now runs and persists independent A-share, HK, and US
  percentile parameters instead of applying A-share parameters to every market.
- Prevented short-history instruments from collapsing a market's common
  walk-forward timeline. They remain in daily and brief price scans, while
  optimizer input requires one complete train/test window.
- Repaired interactive date-range backtests to preserve indicator warmup while
  reporting only the requested period.
- Restored the reference-portfolio `commission_rate` argument used by both
  brief reports. Early and afternoon briefs can rebalance and render again.
- Local full run used the configured universe and produced current optimizer
  parameter files for all three markets. Local daily and both brief report
  emails were rendered to `data/email_archive` with external delivery disabled.

## 2026-07-26: Registered optimizer runs and active strategy selection

- `main.py` now uses `argparse`: `--optimize --strategy <registered-name>`
  selects any entry from `src/analysis/strategies/STRATEGIES`; `--optimize-v2`
  remains a compatibility alias.
- Feishu and Telegram parse the same `/optimize <strategy> [fast|deep]` syntax.
  `/switch_optimizer` also discovers entries from the registry rather than a
  hard-coded strategy list.
- Each optimizer trigger writes versioned per-market artifacts under
  `data/optimizer/runs/<timestamp>_<strategy>/`.  Only a run with successful
  A-share, HK and US artifacts atomically updates `latest_strategy.yaml`.
  Daily reports, brief reports and interactive backtests resolve that newest
  complete timestamped run before falling back to legacy settings.
- Optimizer completion sends one three-market summary through the same
  `NotifierManager` fan-out used by daily and brief reports (email, Feishu and
  Telegram), including incomplete-run status without changing the active
  strategy.

---

## 2026-07-15 修复 (v1.22)

### 1. 图表 + 季度统一 9 个月窗口
- `config/config.yaml` `lookback_days: 730` → `189`（9 个月 × 21 交易日）
- `portfolio_strategy.py` 图表标题去硬编码"近2年"，改为 `len(navs)/21` 动态算月数
- 结果：第二张图从 2 年缩到 9 个月，季度从 7Q 缩到 ~3Q

### 2. 消灭重复调度 → 只有 health server 内嵌 APScheduler
- `ci_cd_deploy.py` 删除 cron 安装逻辑，改为**清理**旧 cron 条目
- `src/core/scheduler_manager.py` 移除 `_start_health_server()` 调用（health server 由 `--health-server` 显式启动）
- 生产健康进程只允许由持久化 `trade-eyes-health.service` 管理；部署器必须清理旧 `nohup` 循环，禁止 systemd 与 PID 文件双重拉起
- 结果：不再同时跑 crontab + OLD SchedulerManager + NEW ScheduleManager 三套调度

### 3. 简报信号名修复：用 hk/us 分开扫描
- `main.py run_brief_report()`：`scanner.scan("non_a_share")` → 改为 `scanner.scan("hk")` + `scanner.scan("us")`
- `feishu_notifier.py` brief：补上 `map_hk`/`map_us`（之前只有 `map_a`/`map_n`）
- 结果：V2 优化器产出 `_hk_`/`_us_` YAML 被正确读取，信号名不再是 "买入规则1"

## 📊 参考持仓跟踪 (v1.21)

持久化参考持仓（Ref Portfolio），用于日报/简报展示系统持续运行的仓位状态。

### 数据持久化

文件 `data/ref_portfolio.yaml`:
```yaml
ref_portfolio:
  inception_date: "2026-07-14"
  cash: 95820.50
  initial_capital: 100000.0
  trading_days: 3
  last_rebalance_date: "2026-07-15"
  last_reset_date: "2026-07-14"
  holdings:
    "601728": {shares: 500, avg_cost: 12.34}
  trade_log:
    - {date: "2026-07-14", code: "601728", action: "buy", shares: 500, ...}
```

### 核心模块

`src/core/ref_portfolio.py`:
- `RefPortfolio` dataclass: 完整持仓状态（现金/持仓/初始资金/交易天数）
- `RefPortfolioManager`: 加载/保存/重置/调仓/Nav 计算
  - `load()` / `save()`: YAML 持久化
  - `reset(initial_capital, inception_date)`: 清空持仓，恢复初始现金
  - `rebalance(alerts, prices, date)`: 根据 StrategyAlert 信号按现价调仓
  - `get_status(pf, prices)`: 返回可展示的状态摘要（Nav/回报/持仓/期初/交易天数）
  - `is_initialized(pf)`: 是否已设置期初日期

### 调仓规则

1. **只在简报时间调仓**（09:50 / 14:30）。日报不调仓。
2. **买卖互斥**: 同标的同日既有买又有卖信号 → 双方都取消。
3. **周末阻挡**: 周末（weekday >= 5）不执行任何交易。
4. **手数取整**: A 股 100 股/手、美股 1 股/手。
5. **买入限制**: 单次最多 5000 元，日累计不超 monthly_buy_limit。
6. **卖出比例**: 单次最多 25% 持仓（取整手）。

### 交互命令

`/ref_date [YYYY-MM-DD|confirm]`:
- 无参数: 显示当前基期 + 持仓状态（标的/持仓/现价/现金/期初/交易日）
- 有参数: 设置新基期 → 如果有持仓则要求确认 → 发送 `/ref_date confirm` 执行重置
- 重置行为: 清空所有持仓标的、恢复初始现金（100,000）、期初日期设为指定日期

### 简报集成

`main.py run_brief_report()`:
1. 获取数据 + 扫描信号（SignalScanner）
2. 加载 RefPortfolio → 如果已初始化，按信号调仓
3. 将 `ref_portfolio_status` 附加到 session
4. Email/Feishu/Telegram 三端展示参考持仓

### 日报集成

`main.py run_daily_task()`:
1. 加载 RefPortfolio → 只读，不调仓
2. 将 `ref_portfolio_status` 附加到 session
3. Email/Feishu/Telegram 三端展示参考持仓

### 展示内容

| 字段 | 说明 |
|------|------|
| 期初日期 | `inception_date` |
| 净值 (Nav) | 现金 + 持仓市值 |
| 回报率 | (Nav / initial_capital - 1) × 100% |
| 交易日 | 有交易的天数 |
| 持仓明细 | 代码/股数/现价/市值/成本 |
| 现金 | 当前可用现金 |

### 测试覆盖

`tests/test_ref_portfolio.py` — 25 项 TDD 测试，覆盖:
- 数据模型序列化/反序列化
- 持久化加载/保存
- Reset 清空重置
- 调仓：买入/卖出/互斥/周末/资金不足/手数取整
- Nav 计算与状态摘要
- 交易日计数

---

## 🔌 统一执行配置 (v1.20)

执行参数（monthly_buy_limit / commission / lot / fx）统一从 `config/optimizer_constraints.yaml` → `execution_params` 段读取。
禁止代码写死覆盖。

**配置段** (`config/optimizer_constraints.yaml`):
```yaml
execution_params:
  monthly_buy_limit: 15000
  initial_capital: 100000
  commission_rate: 0.005
  lot_sizes: {a_share: 100, hk: 100, us: 1}
  fx_rates: {a_share: 1.0, hk: 0.9, us: 7.0}
```

**读取模块** (`src/analysis/execution_config.py`):
- `ExecutionConfig` dataclass
- `get_execution_config()` 模块级单例（首次访问加载，避免重复 IO）
- `reload_execution_config()` 强制刷新（`/config set` 后调用）

**飞书交互**:
- `/config show` 显示 `月买入额度/手续费/初始本金`
- `/config set monthly_limit 20000` → 写 YAML → 自动刷新缓存
- 新增可配键: `monthly_limit`, `commission`, `init_capital`

**对齐范围**:
- 搜参（`StrategyOptimizerV2`/`SignalFnSearchEngine`）和日回报测（`PortfolioEvaluator._evaluate_signal_fn`）统一读同一份配置
- 消除 `fx_rates` dict 在两处文件中的重复定义
- 消除 `monthly_limit` 的三个不同硬编码值（15000/100000/inf）

---

## 📊 搜参报告升级 (v1.20)

搜参完成后自动构建完整报告（替代旧版纯文字摘要），不再依赖单独 `/daily` 调用。

**主函数**: `_build_optimizer_report()` 在 `main.py`，搜参后每组调用一次。

**报告内容**（限近9月测试期数据）:
1. **日回报测指标**: total_return, excess_return, max_drawdown, sharpe, avg_position
2. **参数摘要**: 解码后的 tau/w/buy_thresh/sell_thresh/pos_frac
3. **季末持仓**: 最后一个季度快照（代码/持股/成本/现价/盈亏）
4. **参数敏感性**: `PercentileSignalFn.random_perturbations(n=10)` → 10版随机扰动（全参数各 ±1~3 级别），记录最差版收益和 10 版范围
5. **跨天波动率**: `PercentileSignalFn.cross_day_volatility(lookback=5)` → 数据截止日前移 5 天各跑回测，记录收益波幅
6. **周K OHLC 表**: `_build_weekly_ohlc()` 从日线 NAV 聚合周 OHLC

**通知渠道**: `build_optimizer_summary(full_report=...)` 统一渲染为 HTML，邮件/飞书/TG 三端共用。

---

## 🔌 双搜参引擎架构 (v1.19 新增)

### 背景问题
分位评分引擎 (PercentileSignalFn) 之前只是"空壳"：`signal_fn` 仅用于往 YAML 写 `_engine` 标签，其 `evaluate()` 从未被遗传搜索调用。YAML 里实际是全局阈值格式参数 (`buy_1_signal: deviation_absolute`)，导致"信号名与指标对不上"。

### 解决方案：SignalFn 真正接入遗传搜索
- **SignalFn ABC** (`signal_functions.py`)：唯一替换点。新增方法：
  - `random_params/crossover/mutate` — genome 编解码（genome = `Params.values` 整数级别 dict）
  - `scan_signals(params, today, history)` — 引擎自身逻辑判断今日买卖信号（显示层用）
  - `describe_rules(params)` — 参数翻译成买卖规则名（报告用）
  - `engine_brief()` — 引擎买卖标准简介（/switch_optimizer 展示）
  - `execution_params(params)` — 解码执行阈值（买/卖分数阈值+仓位）
- **SignalFnSearchEngine** (`signal_fn_engine.py`)：把 SignalFn 包成遗传搜索器的 `StrategyEngine` 插件。encoding=Params，`evaluate_encoding` 调 `signal_fn.evaluate()` → 共享流水线 `simulate_portfolio`/`compute_metrics` → `WindowStats`。**engine 分支单线程（无 pickle 问题）**。
- **main.py**：`engine_type==percentile` → `engine=SignalFnSearchEngine(signal_fn)`；global → `engine=None`（100% 走旧向量化路径，criterion 1/2 逻辑零改动）。
- **strategy_optimizer_v2._build_report**：`hasattr(ss.encoding,'values')` 判定引擎模式。分位模式写真实分位参数 (`adx_pct_tau` 等, `_mode: signal_score`) + `_signal_fn_to_rules`（引擎自定义规则名 + `condition: __signal_fn__` 标记）。`_save_results` 序列化 `label` 字段。
- **SignalScanner.scan**：rules 含 `__signal_fn__` → 按 `_engine` 构造 SignalFn → `scan_signals()` 用引擎逻辑判断；否则走 legacy `condition` 评估（全局引擎/旧 YAML）。分位分派用 `_computed_cache`（compute_all 后的完整 DataFrame）算滚动分位。

### 关键不变量
- **全局引擎 = deprecated 但默认**，`engine=None`，走旧 FastEvaluator 向量化路径，日报版式与逻辑完全不变
- 旧 YAML（无 `_engine` 或 `_engine: global`, 真实 condition）永远走 legacy 路径
- 分位引擎信号名来自引擎自身：如 `分位评分买入 (score 0.29>0.19)` + detail `趋势强度分位(ADX)分位71%>46%`

### 评分引擎执行语义（`_score_sim_core`，主链路唯一决策仿真）
锁定于 `tests/test_score_engine.py`（18 项）：
1. **买入执行价 = 近 3 日收盘最高价**（v1.20 改为含滑点的 pessimistic 估价；之前是均价）
2. **卖出执行价 = 单日收盘价**（触发日，不平滑）
3. **同日互斥**：同一标的同日既触发买又触发卖 → 双向跳过
4. **月度买入额度**：统一从 `execution_config.monthly_buy_limit` 读取（搜参和日回报测同一值，禁止硬编码）
5. **允许回补**：卖出后可再买入（无 `shares==0` 永久壁垒）
6. 手数取整 / 手续费（0.5% 含滑点）/ 现金约束 / 评分需严格 `> 阈值`
7. **季度持仓成本快照**：`_score_sim_core` 每季度边界快照 `q_cb`(成本基础)+`q_price`(价格)
8. **港股手数**：`_get_lot_size` 返回 100；日回报测路径按标的均价分界（<100→1000, ≥100→100）
9. **汇率**：固定汇率从 `execution_config.fx_rates` 读取，搜参和验证路径均对价格矩阵乘汇率
10. **分位预热**：日回报测前 `PERCENTILE_WARMUP=252` 天禁止交易
11. **验证期限**：日回报测只用近 9 个月（`lookback_days` 默认 441 = 252 预热 + 189 交易）

### 测试矩阵（量化引擎，共 ~80 项）
- `test_score_engine.py`（18）：决策仿真全部执行语义
- `test_percentile_engine.py`（15）：scan_signals/score_timeseries/execution_params/describe_rules/engine_brief/genome
- `test_signal_scanner.py`（20）：`__signal_fn__` 分派 + `_params_from_yaml` + `_make_signal_fn` + legacy 兼容
- `test_signal_fn_engine.py`（5）：适配器编码操作 + evaluate_encoding + 端到端分位搜索
- `test_portfolio_strategy.py`（+7）：`_evaluate_signal_fn` 真实交易回归（防 100% 空仓复发）
- **测试目录结构**：`tests/__init__.py` + `tests/integration/__init__.py` 消除同名模块（`test_signal_scanner.py`）收集冲突；`conftest.py` autouse fixture 清理绑定到已关闭捕获流的 StreamHandler

---

## 📋 执行摘要

本文档记录股票量化监控系统的关键设计决策，涵盖架构选择、问题解决方案和技术路线。系统核心能力：策略搜索优化器、信号扫描器、xelatex 日报 PDF 生成、邮件提醒、健康检查服务器。

### 核心原则
1. **真实数据优先** - 杜绝模拟数据，多源降级（新浪→腾讯→Yahoo）
2. **配置驱动** - 技术指标/规则/简报通过 YAML 配置，避免硬编码
3. **文档驱动开发** - 所有设计决策存档至本文档
4. **防循环编码** - cycle_guard 自动检测重复错误模式
5. **Session 统一管理** - 全局 SessionContext 防止字段混淆
6. **全量验证** - 策略优化/信号扫描/回测分析基于 config 全量标的运行，禁止子集验证

---

## 🏗️ 架构现状 (v1.18)

```
┌─────────────────────────────────────────────────────────────────┐
│  配置层 (config/)                                               │
│  - config.yaml (股票/邮件/调度/简报)  - alerts.yaml (锚点)      │
│  - .env (API Key/密码)                                         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  缓存 + 数据源 (src/)                                           │
│  - data_source.py   (CSV 缓存 + 复权交叉验证 + 价格校验)        │
  │  - web_crawler.py   (新浪/腾讯/Yahoo 多源降级)                  │
│  - data_fetcher.py  (协调获取 → 调用指标计算)                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  指标计算 (src/)                                                │
│  - technical_indicators.py  (配置驱动 MA60/WMA/RSI/Bollinger)   │
│  - utils/etf_detector.py    (ETF 统一检测)                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  策略 + 分析 (src/analysis/)                                    │
│  - strategy_optimizer.py   (贝叶斯优化 + 两阶段 + 收敛图)       │
│  - signal_scanner.py       (共识扫描 + 每日警报 + 回测)         │
│  - portfolio_strategy.py   (共享资金池 + 贪心搜索)              │
│  - rule_engine.py          (YAML 驱动 + 表达式沙箱)             │
│  - indicator_library.py    (RSI/MACD/ATR/布林/ADX/量比)         │
│  - backtest_config.py      (回测时间线约束)                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  业务层 (src/)                                                  │
│  - condition_checker.py  - alert_engine/processor/state_manager │
│  - email_notifier.py     (告警/日报/简报 + xelatex PDF 生成)    │
│  - health_server/        (健康检查 + OTP + SSL + 报告链接)      │
│  - session_manager.py    (SessionContext Pydantic 模型)          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  模板 + 输出                                                    │
│  - report_daily.tex         (xelatex LaTeX 日报模板)            │
│  - appendix_methodology.md  (13节公式附录, Markdown→LaTeX)      │
│  - email_template.html      (邮件正文, 港式财报卡片布局)        │
│  - optimizer_report.html    (交互收敛图, Plotly 暗色主题)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ 核心架构决策

### 1. Session 数据流 (v3.0)

**决策**: Pydantic `SessionContext` 替代裸 DataFrame/dict 传递
**背景**: `price`/`low_price` 字段名称不一致导致邮件显示价格为 0.00
**实现**: `fetch_to_session()` → `check_from_session()` → `send_from_session()`
**类型安全**: `StockPriceData` / `AlertStock` Pydantic 模型，自动验证数据完整性
**文件**: `src/session/session_manager.py` / `src/models/schemas.py`

### 2. DataSource 统一数据源 (v3.2 → v3.2.1)

**决策**: CSV 缓存 + meta 文件替代 JSON 缓存
**特性**: 7 天保留 / 15:55 当日过期 / 复权交叉验证（双源 3 日 close 比对）/ 价格关系校验
**影响**: `data_fetcher.py` 删 ~170 行缓存代码，`backtest_framework.py` 删内存缓存
**文件**: `src/data/data_source.py`

#### v3.2.1 修复：缓存 bypass + 复权检测回归 (2026-05-25)

**问题**: 重构 DataSource 时丢失了 `_should_bypass_cache`，导致 15:55 后非当日缓存不会被强制刷新，除权后的前复权历史数据持续错误。
**根因**: `fetch_stock_data()` 缓存命中路径直接 `return cached_df`，完全跳过了时间检查和复权检测。
**修复**:
  - 恢复 `_should_bypass_cache(cutoff="15:55", granularity=per-stock)`，按标的粒度生效
  - 缓存命中前增加 bypass 判断，触发后进入增量/全量拉取 → `_check_forward_adjustment` → 合并/覆盖
  - 统一 `requested_start_ts = pd.Timestamp(requested_start.date())`，消除时间分量导致的首行误删 bug
**测试**: `tests/test_data_source.py` 14 个用例覆盖 bypass 边界、复权修正检测、ETF 场景、fallback 路径

#### v3.2.2 数据清理：debt_ratio 删除 + ROE 计算修复 (2026-05-27)

**问题 1**: `debt_ratio`（资产负债率）从腾讯 API `items[52]` 获取，但该字段实为**动态 PE**；`items[53]` 实为**静态 PE**。原映射完全错误，且 debt_ratio 对投资决策无直接价值。
**决策**: 全链路删除 `debt_ratio`（schemas、data_fetcher、web_crawler、email_notifier、模板、LLM analyzers）。
**影响文件**: `src/models/schemas.py`, `src/core/data_fetcher.py`, `src/data/web_crawler.py`, `src/notification/email_notifier.py`, `src/templates/email_template.html`, `src/analysis/llm_analyzer/*`

**问题 2**: ROE 原从 `items[52]` 获取，映射错误后数据不可信。
**修复**: ROE 改为**推导计算**: `ROE = (PB / PE) × 100`，与财报披露值误差 <0.2%（以 600000 实测验证）。
**文件**: `src/core/data_fetcher.py`

#### v1.17 简报增强 (2026-05-27)

**排序**: `send_brief_report()` 按锚点偏离率**升序排列**，跌幅越大越靠前；无有效锚点的股票 `dev_pct=None` 用 `float("inf")` 兜底排最后。
**新增收盘简报**: `config.yaml` 新增 `afternoon_snapshot`（14:30），与 `morning_snapshot`（09:50）共用同一函数，仅标签不同。
**日报时间调整**: `scheduler.run_time` 从 `16:00` 改为 `19:00`，确保 A 股收盘后数据完整（港股 16:00 收盘，美股隔夜）。
**CI/CD 同步**: `ci_cd_deploy.py` 自动注册 3 条 cron：09:50 早盘 / 14:30 收盘 / 19:00 日报 / 02:00 优化。
**文件**: `src/notification/email_notifier.py`, `config/config.yaml`, `ci_cd_deploy.py`

### 3. 数据获取

**主源**: 新浪财经历史数据 API → 腾讯财经 → Yahoo Finance（自动降级）
**股息**: LLM 提取缓存优先 → 网页爬虫备用
**缓存**: `cache/data/` CSV 格式，7 天保留期
**baostock**: 回测历史数据专用（前复权），日期格式 YYYY-MM-DD

---

## 🎯 策略搜索优化器 (v1.14 → v1.18)

### 🔑 权威设计契约 (v1.18, 2026-07-07) — 冲突时以本节为准

> 本节记录搜参 + 日报/简报的**最终设计契约**。历史章节（V1/V2 早期描述）若与本节冲突，一律以本节为准。实现细节见 `config/optimizer_constraints.yaml`（唯一真源）。

#### 1. 搜参：多窗口验证 + 测试集排名 + 验证窗口留出

- **Walk-Forward 多窗口**（取代早期"9个月单窗口 / 4阶段"设计）：
  - 训练 12 月 + **测试 9 月**，滑动步长 3 月，**14 窗口**，数据 5 年
  - 配置：`config/optimizer_constraints.yaml` → `walk_forward`
- **验证窗口留出**（v1.18, 2026-07-07）：`validation_windows: 1`
  - **最后 1 个窗口（最近 9 月）不参与 `wf_score` 排序**，作纯样本外验证
  - 代码：`genetic_searcher._compute_wf_score` + 模块级 `compute_wf_score` 均排除 `all_stats[-N:]`
  - 理由：最近数据若参与排序，选出的策略已隐含偷看验证期 → 胜率不置信
- **排名依据**：`avg(排序窗口测试期超额收益)`（排除验证窗口后的均值）
  - 代码：`strategy_optimizer_v2.py` → `rank_ws = all_ws[:-v_win]`；`avg_test_ret = mean([ws.test_excess_return for ws in rank_ws])`
  - 稳定性惩罚：`stability_penalty × std(窗口收益)`，防单窗口运气
- **禁止**：用训练期收益排名、用全期收益排名、事后挑测试期最优（回看偏差）、最近9月参与排序

#### 2. 收益评估：约束下最高收益，超额 vs 基准

- **目标函数**：在硬约束满足前提下，最大化测试期收益
- **硬约束**（`hard_constraints`）：最大回撤 ≥ -40%、平均仓位 ≥ 5%、收益标准差 ≤ 15%、月交易 1-100 次；不满足直接丢弃
- **收益基准**（超额收益 = 策略收益 − 基准收益）：
  | 分组 | 基准 |
  |------|------|
  | A股 | 无风险(2%) / 510300(沪深300) / 510880(红利ETF) |
  | 非A股 | 无风险(3.8%) / VOO(标普500) / BRK.B |
  - 配置：`config/optimizer_constraints.yaml` → `benchmarks`
  - **冲突时以此配置为准**（多处有基准实现，此处为唯一真源）
- **交易模型**：position_target（sigmoid 动态仓位），非固定分数

#### 3. 日报/简报：直接读 YAML 预估收益，不重新搜参

- **独立搜参任务（02:00 cron）继续更新 YAML** — 这是唯一的搜参入口
- **日报/简报只读取 YAML** Top1 策略的预估收益（近 9 个月测试期），**不再自己重新搜参/贪心选股**
- **废弃**：日报中 `PortfolioOptimizer.run()` 每日重新贪心搜索（导致结果天天大幅波动、"抽奖"效应）
- **展示数据**：Top1 策略的 `test_return`（测试期超额）、`test_drawdown`、`sharpe`、`_stocks`（选股）、`quarterly_holdings`（季末持仓）、平均现金仓位
- **今日信号**：`SignalScanner` 用 YAML 的 `rules` 条件字符串评估当日数据，`strategy_rank==1` 与 Top1 展示对齐
- **固定选股评估**：`PortfolioOptimizer.run_fixed(stock_selection)` — 对 YAML `_stocks` 跑一次确定性 `evaluate()`，产出 NAV 曲线 + 季末持仓，不搜索

#### 4. 三组独立搜参 + 独立资金池 + 验证期胜率 (v1.18, 2026-07-11)

- **三组独立搜参**（v1.18 2026-07-11 起）：优化器 V2 按 A股/港股/美股**分别搜参**，产出三份 YAML：
  - `*_a_share_strategies.yaml` / `*_hk_strategies.yaml` / `*_us_strategies.yaml`
  - 动机：港美股走势差异大，混搜时港股趋势性主导搜索空间，美股被迫用不适配的规则（如全是 trend_follow）
  - `run_optimization_v2` 用 `_detect_fine_group` 三分；`optimizer_constraints.yaml` benchmarks 加 hk/us（=VOO/BRK.B）
  - 回退：hk/us YAML 未生成时用 non_a_share YAML
- **三组独立资金池**（`_detect_fine_group`，日报展示层）：
  | 细分组 | 判定 | 资金池 | 主基准 | 规则来源 |
  |--------|------|--------|--------|----------|
  | a_share | 6 位纯数字 | 独立 100k | 510880 红利ETF | a_share YAML |
  | hk | 5 位纯数字 | 独立 100k | VOO | hk YAML |
  | us | 含字母 | 独立 100k | VOO | us YAML |
  - **`_detect_stock_group`（二分）保持不变** — risk_free/回测仍用 a_share/non_a_share
  - **标的池来自 config**（非 YAML `_stocks`）：`run_fixed(groups=)` 遍历 config stocks 按细分组分池，删/加标的立刻生效
  - `SignalScanner.scan(group)`：group∈a_share/hk/us，按细分组过滤标的 + 读对应 YAML
- **信号名按组翻译**：`_readable_signal(code, ..., map_a, map_hk, map_us)` 按标的细分组选对应 YAML 的信号名映射（A股 buy_1≠港股 buy_1≠美股 buy_1）
- **验证期胜率**（现场算，不信搜出来的窗口）：
  - `email_notifier._calc_validation_winrate()`：用 `run_fixed` 产出的 `nav_series` vs 主基准价格
  - 最近 9 月逐日算 forward return，"任意一天买入持有到期跑赢主基准的概率"
  - 完全离线，不依赖 YAML 搜出的数字
- **净值图**：每组一张（chart002=A股/chart003=港股/chart004=美股），Top1 绿线 + 30日布林带 + 红色基准线

#### 5. 未解禁定增展示 (v1.18, 2026-07-09)

- **数据源**：东方财富 `RPT_SEO_DETAIL` API（`web_crawler.fetch_placement_data`）
- **仅 A 股**（6 位纯数字），19:00 全量任务抓取（简报不触发）
- **只展示未解禁**：`is_locked=True`（当前 < 解禁日）；解禁日 = 上市日 + 锁定期
- **展示字段**：代码 / 名称 / 未解禁定增数额(亿股) / 占发行后总股本% / 定增价格 / 解禁时间
- **取最近一次**：多条按上市日期降序取第一条
- 代码：`_parse_placement_row`（占比+解禁日）、`_parse_lockin_years`（"3年"→3, "18个月"→1.5）、`email_notifier._build_placement_section`

---

### V2 优化器 (v1.17-beta, 2026-06-03)

**设计哲学**:
1. 模糊的正确好过精确的错误 — 不用贝叶斯精确拟合历史噪音
2. 永远正确的判断是废话 — 单维度信号（如纯量比）不区分方向，无信息增量
3. 好的机会不稍纵即逝 — 状态型信号替代穿越型，给足够判断时间

**V1 vs V2 架构对比**:

| 维度 | V1 (贝叶斯) | V2 (遗传搜索 + Walk-Forward) |
|------|------------|-------------------------------|
| 搜索方式 | 贝叶斯连续优化 | 离散网格 + 遗传算法 |
| 评估方式 | 单测试期排名 | Walk-Forward 14窗口评分 |
| 回测引擎 | PortfolioEvaluator 逐日Python | FastEvaluator numpy+numba 向量化 |
| 规则模板 | 2买3卖, 独立OR | 5买0卖, 离散选择 |
| 约束 | 仅回撤软惩罚 | 硬约束过滤：仓位/回撤/交易密度/一致性 |
| 性能 | 150次 × 2.9s = 7min | 25000次 × 10ms = 4min |
| 速度提升 | — | **约 290x** |

**V2 新增文件**:

| 文件 | 职责 |
|------|------|
| `config/optimizer_constraints.yaml` | 可配置约束：仓位/回撤/交易密度/一致性 |
| `src/analysis/optimizer_constraints.py` | 约束加载器 + WalkForward/Genetic/Discrete 配置模型 |
| `src/analysis/walk_forward.py` | 数据切片 → 统一日期轴 → 14窗口矩阵 |
| `src/analysis/fast_evaluator.py` | numba JIT 信号生成 + 组合模拟 + 指标计算 |
| `src/analysis/genetic_searcher.py` | 三阶段：随机粗筛(10000) → 遗传3代 → 精确验证 |
| `src/analysis/strategy_optimizer_v2.py` | V2 顶层入口，兼容 V1 OptimizationReport |

**V2 三阶段搜索流程**:
```
Phase 1 (粗筛): 30000 随机策略 → 向量化评估14窗口 → 过滤约束 → Top 10000
Phase 2 (遗传): 5000种 × 5代 × 交叉/变异 → 25000 新策略 → Top N
Phase 3 (验证): 精确 PortfolioEvaluator → Top 10 → YAML + HTML
```

**Walk-Forward 窗口设计** (实际值见 `config/optimizer_constraints.yaml`):
```
数据: 5年 (60个月)
训练 12月 + 测试 9月, 滑动步长 3月, 共 14 窗口
W1: [Train 0-12][Test 12-21]
W2:    [Train 3-15][Test 15-24]
... (每次滑动 3 月, 共 14 窗口)
评分 = mean(14窗口测试超额) - stability_penalty(0.5) × std(14窗口测试超额)
```

**约束系统** (`optimizer_constraints.yaml` — 唯一真源):
- **仓位下限**: 测试期平均持仓 ≥ 5%（`min_avg_position_pct`）
- **回撤上限**: 最大回撤 ≥ -40%（`max_drawdown_pct`）
- **一致性**: 收益标准差 ≤ 15%（`max_return_std_pct`，防单窗口运气）
- **交易密度**: 月交易 1-100 次（`min/max_trades_per_month`）
- 全部硬性过滤，不满足直接丢弃

### Cumulative filing and FCF contract (2026-08-18)

- SEC Company Facts keeps each period's original filing and rejects later filings' comparative columns as substitute publication dates.
- SEC 10-Q and A-share interim flow values are fiscal-YTD inputs. The common normalizer derives standalone quarters before QoQ, TTM, or latest-quarter metrics are calculated.
- FCF is always OCF minus capital expenditures on the same cumulative basis, then normalized to standalone quarters. Reports expose latest-quarter FCF and TTM FCF separately; annual fallback is explicitly labeled.
- A source replacement migration removes legacy SEC rows produced by the old comparative-column mapper. A-share sources already declare cumulative periods and missing quarterly capital expenditures remain explicit missing data.

### 日报邮件移动端版式重设计（2026-08-22）
- 每日 19:00 日报统一使用移动端优先的单列卡片布局；告警与无告警路径共用同一份完整 HTML 模板。
- 主机名、公网 IP 及系统信息保留在独立的“部署状态”信息条中，用于确认部署节点正确性。
- 监控标的、策略信号、A/HK/US 组合表现、图表、参考持仓、公告、标的画像和定增按纵向区块展示；深度持仓数据仍保留，PDF 继续作为附件。
- 日报数据进入模板前统一转义文本和链接，参考持仓不再追加到 `</html>` 之后；移动端归档验证覆盖 375px/430px 的响应式宽度约束。
---

## 📡 信号扫描器 + 日报集成 (v1.15)

### 共识机制

加载最新优化结果 → 计算 Top-5 策略 → 对当日数据评估信号:
- **纳入监控**: ≥2/5 策略在 Top-5 中出现的构建器
- **纳入报警**: ≥3/5 策略在 Top-5 中出现的标的
- 每日 `--once` 运行时自动加载，产出共识信号 + 指标快照

### 回测嵌入

> ⚠️ **v1.18 变更**：日报不再每日重新贪心搜索/回测。改为直接读取最新 YAML（02:00 搜参任务产出）的 Top1 策略预估收益（近 9 个月测试期）。详见本文档「权威设计契约」章节。

### 日报邮件版式 (daily_mode, v1.18)

`_build_email_body(daily_mode=True)` 精简日报，去掉过时/冗余段：
- **删除**：MA60 偏离报警段（`alert_section`）、回测分阶段表（`backtest_section`）、旧组合分析段（`portfolio_section`）、价格表的 MA60/偏离/状态列
- **保留 + 重排**：走势图(收盘价) → 净值图(每组一张) → **搜参策略结果**（Top1规则+今日信号+组合卡片+季末持仓，一段讲完）→ 基本面表 → 公告 → **未解禁定增**
- **今日信号合并**：`SignalScanner.alerts`（A股+港股+美股 merge）并入搜参段，信号名 `buy_1`→`偏离穿越`（`_build_signal_label_map` 读 YAML params 翻译）
- **简报统一**：早/晚简报也走 `SignalScanner`（非 `build_strategy_suggestions` 旧逻辑），与日报信号一致
- 邮件从 ~89k 字符精简到 ~28k

### HTML 交互报告

自包含单文件 HTML (Plotly.js CDN)，暗色主题：
- 指标卡片行 / Plotly 交互收敛图 (可缩放悬停) / Top-10 策略表 (点击展开规则) / 最优策略详情
- 通过 health_server `/report/<token>` 路由提供，30 分钟时效链接
- Token 跨进程共享: `data/optimizer/.report_tokens.json` 文件

---

## 📧 xelatex PDF 日报 (v1.16)

### WeasyPrint → xelatex 替换

**原方案 (v3.6)**: WeasyPrint 将 `report_daily.html` 渲染为 PDF。
**问题**:
- mathtext 公式是位图贴图，放大模糊
- 中文字体回退不稳定 (Noto Sans CJK SC 缺失字形)
- PDF 体积大 (644KB, 含 base64 图片)

**新方案**: Python → LaTeX 模板 → **xelatex 编译两次** → PDF 附件 (145KB)。
- 真 LaTeX 数学排版 (`amsmath`/`equation`/`split` 环境)
- 第一遍编译写入交叉引用 (.aux)，第二遍解析引用
- 附录: `appendix_methodology.md` → Python `markdown.markdown()` → `_esc()` 转义 → `$$...$$` 块直接注入 LaTeX

### LaTeX 模板: `report_daily.tex`

```
\documentclass[11pt,a4paper]{article}
\usepackage{xeCJK}
\setCJKmainfont{Noto Sans CJK SC}
```

- **ctexart → article+xeCJK**: 服务器未安装 ctex 宏包，改用 `xeCJK` + `article` 类
- **CRLF 修复**: Windows git 自动转 CRLF，xelatex 将 `\r` 视为 `^^M` 触发 Emergency stop。代码在编译前执行 `.replace("\r\n","\n")`
- **`_` 转义**: LaTeX 中 `_` 需转义为 `\_`，否则报 "Missing $ inserted"。`_esc()` 函数统一处理
- **颜色**: Navy 背景 / 白色表头 / 绿色正数 / 红色负数，港式财报风格
- **图表**: matplotlib 生成偏离度折线图 PNG (base64)，注入 LaTeX `\includegraphics`

### 编译流程

```
session → _build_tex_variables() → VAR 占位符替换
     → _esc() 转义 _ % & $ #
     → xelatex ×2 (-interaction=nonstopmode)
     → /tmp/report.pdf → read_bytes()
     → yagmail.send(attachments=[pdf])
```

编译耗时 ~2s，依赖: `texlive-xetex` + `texlive-lang-chinese` (约 500MB)

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/templates/report_daily.tex` | LaTeX 日报模板 (101行) |
| `src/templates/appendix_methodology.md` | 13节公式附录 (193行) |
| `src/templates/email_template.html` | 邮件正文 (已大幅缩减) |
| `src/notification/email_notifier.py` | `_generate_daily_pdf()` (250行) |

---

## 🔒 安全加固 (v1.16)

### OTP 安全

- **随机源**: `random.randint(0, 9999)` → `secrets.randbelow(10000)` (密码学安全)
- **审计日志**: 不再记录明文 OTP，仅记录 `"ID:****"`
- **文件**: `src/health_server/core/global_instances.py`

### Health Server SSL/TLS

- **自签名证书**: `openssl req -x509 -newkey rsa:2048 -nodes`, 365 天有效期
- **监听**: `ssl.wrap_socket()` 包装 TCP socket，默认端口 1933
- **IP 配置**: `config.health_server.public_ip` 优先 → `ifconfig.me` fallback → 127.0.0.1
- **路径遍历防护**: `os.path.basename()` 消毒文件名参数

### 三层防御闸门

```
pre-commit hook  →  ruff lint + import smoke + safety tests
     ↕
CI/CD pre-deploy →  ruff lint + import smoke + core tests  (pre-push check)
     ↕
import smoke test →  24 key modules import integrity  (standalone)
```

**文件**: `hosts/pre-commit` / `ci_cd_deploy.py` / `tests/test_import_smoke.py`

### 安全测试

`tests/test_security.py` — 18 个测试覆盖:
- 表达式沙箱 (禁止 `__import__`/`eval`/`exec`/文件访问)
- 路径遍历防护 (`../` `/etc/passwd` 攻击)
- Token 格式验证 (必须是 base64url)
- OTP 随机性 (卡方检验)
- 速率限制 (单 IP 60 秒内 ≤5 请求)

---

## 🚀 CI/CD 增强 (v1.16)

- **环境变量**: `DEPLOY_HOST` / `DEPLOY_SSH_REMOTE` / `DEPLOY_REMOTE_DIR` 替代硬编码
- **简报 cron**: 自动注册 `09:50 daily` crontab 条目 (此前需手动添加)
- **texlive**: 自动安装 `texlive-xetex` + `poppler-utils` 系统依赖
- **pre-deploy checks**: ruff lint + import smoke + core tests (部署前自动运行)

---

## 🧪 测试覆盖 (v1.16)

| 测试集 | 数量 | 说明 |
|--------|------|------|
| backtest_config | 15 | 时间线约束 + 资金注入验证 |
| indicator_library | 18 | RSI/MACD/ATR/布林/ADX/量比 |
| signal_scanner | 14 | 共识计算 + 警报触发 |
| strategy_optimizer | 18 | 贝叶斯优化 + 构建器 + 预筛选 |
| security | 18 | OTP/沙箱/路径遍历/速率限制 |
| import_smoke | 24 | 关键模块导入完整性 |
| **合计** | **107** | 覆盖核心新功能 |

```bash
pytest tests/ -p no:capture -q           # 全量
pytest tests/test_security.py -v          # 安全专项
pytest tests/test_import_smoke.py         # 导入完整性
```

---

## 🔧 技术债务清理 (v1.16)

### 已完成清理
- ✅ `colorlog` 死依赖删除 (零 import)
- ✅ `camelot` PDF 表格解析死代码删除 (从未安装, 永远静默失败)
- ✅ `report_daily.html` 删除 (WeasyPrint 模板, 已被 .tex 替代)
- ✅ `alert_section.html` 删除 (不再独立渲染)
- ✅ 33 个 stale test import 修复
- ✅ Health server 5 处 `Path().parent.parent` 回归修复

### 待清理
| 文件 | 操作 | 原因 |
|------|------|------|
| `email_notifier.py` 旧 report_link 生成 | 删除 (~30行) | 时效链接由 HTML 报告保留 |
| `email_notifier.py` 旧监控表构建 | 删除 (~50行) | 已替换为 `_build_daily_table()` |
| `email_template.html` | 大幅缩减 | 正文已精简为摘要卡片 |

---

## 📊 开源准备 (v1.16)

| 文件 | 说明 |
|------|------|
| `LICENSE` | BSD-3-Clause + 投资免责声明 (软件仅供研究，作者不对投资损失负责) |
| `pyproject.toml` | 项目元数据 (name/version/authors/dependencies) |
| `CONTRIBUTING.md` | 贡献指南 (代码风格/测试/PR 流程) |
| `CHANGELOG.md` | v1.12 → v1.17.1 完整变更历史 |
| `README.md` | 项目概述 + 日报预览图 + CLI 命令 |

---

## 📝 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v1.0 | 2026-03-01 | 基础功能: 数据获取 + MA60 条件 + 邮件 |
| v1.8 | 2026-03-15 | 移除 akshare, 股息 LLM 缓存架构 |
| v2.0 | 2026-03-20 | 回测框架 |
| v2.8 | 2026-04-10 | baostock 历史数据缓存 |
| v3.0 | 2026-04-15 | Session-based 数据流 (Pydantic) |
| v3.1 | 2026-04-18 | 配置驱动指标计算，模块职能隔离 |
| v3.2 | 2026-04-26 | DataSource CSV 缓存 + 复权验证 |
| v3.3 | 2026-04-26 | 投资组合策略 + 规则引擎 (YAML 沙箱) |
| v3.4 | 2026-04-27 | 早盘简报 + 锚点择优算法 |
| v1.14 | 2026-04-29 | 策略搜索优化器 (贝叶斯+构建器池+超额收益) |
| v1.15 | 2026-05-01 | 信号扫描器 + 回测嵌入日报 + HTML 报告 |
| **v1.16** | **2026-05-03** | **xelatex PDF 日报 + 安全加固 + 开源准备** |
| v1.17 | 2026-05-27 | 简报排序 + 收盘简报 14:30 + 日报 19:00 + debt_ratio 删除 + ROE PB/PE 推导 |
| v1.17.1 | 2026-06-04 | QQ 实时行情 + Eastmoney 删除 + 数据源健康探针 + 优化器 P0 修复 + 布林带列名统一 |
| **v1.18** | **2026-07-09** | **搜参多窗口验证契约 (14窗口/测试9月/验证窗口留出) + 日报直读YAML不重搜参 + 港股/美股独立资金池 + 验证期胜率 + 未解禁定增展示 + daily_mode版式精简 + 简报统一SignalScanner** |

---

## 🔗 相关文档

- [架构说明](../architecture.md) — 分层架构、数据流、模块职责
- [部署指南](../deployment.md) — 生产部署 + CI/CD
- [配置参考](../configuration.md) — config.yaml 详细说明
- [开发日志](../development/devlog.md) — 版本演进
- [贡献指南](../../CONTRIBUTING.md) — 代码风格 + 测试 + PR 流程

---

---

## 2026-05-17 邮件质量修复 + Pydantic 全量迁移

### 邮件渲染修复
1. **业绩增长列删除** — `email_notifier.py` 3 个 table section + 表头彻底移除死列
2. **告警行高亮 inline 化** — `<tr style="background:#fef9e7">` 替代 CSS class，兼容 Outlook/Gmail
3. **缺失值符号统一** — 43 处 `"-"` → `"—"` (em dash)
4. **邮件存档去嵌套** — `_save_email_copy` 不再包 `<html><body>`，仅 prepend HTML comment
5. **策略告警去重** — `signal_scanner.scan()` 按 `(code, rule_label)` dedup
6. **策略告警过滤** — 传统告警 table 中 `type=="strategy"` → `continue`
7. **港股分类修复** — 5 位代码不再误判为 A 股 (`len(code)==6`)
8. **行标区分** — 传统告警 `[MA60] 最低价 < MA60`，策略告警 `[策略]`
9. **Eastmoney 静默** — 删除假实现 WARNING 日志
10. **简报颜色 inline** — `style="color:..."` 替代 CSS class

### Pydantic 全量迁移
- `Rule` (rule_engine.py)
- `TradeRecord`, `StockMetrics`, `SubPeriodMetrics`, `PortfolioResult` (portfolio_strategy.py)
- `StrategyTrial`, `OptimizationReport` (strategy_optimizer.py)
- `SubPeriodMetrics` 去掉双重装饰器 (`@dataclass` + `BaseModel`)
- `__dataclass_fields__` → `hasattr(v, "label")` (strategy_optimizer.py:1037)
- `tests/test_rule_engine.py` 位置参数 → 关键字参数

### 数据清理
- `data_fetcher.py` 删除死字段 `earnings_growth`
- `email_notifier.py` 删除未使用 `earnings_growth` 局部变量
- `email_template.html` 删除僵尸 CSS (`.positive`/`.negative`/`.alert-row`)

**文档维护**: 本文档应在每次重大架构变更后更新
**下次审查**: 2026-08-01

---

## 🎯 仓位目标模型 (Position Target Model) — v1.18-beta

### 动机
原有优化器使用 **Fixed-Frac（固定分数）** 交易执行：每次信号触发时，买入 `cash × buy_frac`、卖出 `position × sell_frac`。问题是 `buy_frac`/`sell_frac` 是静态的，无法根据市场信号强弱自适应调整仓位。

新模型将"买多少/卖多少"从固定分数升级为**动态仓位目标**：
1. 聚合全体股票的买卖信号 → **bullish_score** ∈ [0, 1]
2. sigmoid 映射 → **target_position**（目标仓位占 NAV 比例）
3. 每日渐进调仓（最多 ±10%/天），朝目标收敛

### 核心算法
```
每天:
  bullish = aggregate(buy_signals, sell_signals)
           = n_buy_active / (n_buy_active + n_sell_active)
  target  = sigmoid(slope × (bullish - 0.5) × 2 + bias)
  delta   = clamp(target - current_pct, -0.10, 0.10)

  delta > 0: 买入（候选=有买入信号的股票，等额分配，均价执行）
  delta < 0: 卖出（所有持仓按比例减持，均价执行）
```

### 参数
| 参数 | 范围 | 含义 |
|------|------|------|
| `position_slope` | 0.5 ~ 10.0 (20 档) | 仓位对信号的敏感度 |
| `position_bias` | -3.0 ~ 3.0 (20 档) | 基准仓位偏移（负数偏保守） |
| `max_daily_adjust` | 0.10 (固定) | 每日最大调仓幅度 |

### 与旧模式对比
| | Fixed-Frac (旧) | Position-Target (新) |
|---|---|---|
| 买入量 | `cash × buy_frac` 固定 | `delta × NAV` 动态 |
| 卖出量 | `position × sell_frac` 固定 | `delta × NAV` 动态 |
| 仓位控制 | 无全局概念 | 有全局目标仓位 |
| 月度上限 | 有 (15000) | **无**（删除了） |
| 每标每日操盘 | 不限制 | **最多 1 次** |
| 执行价格 | 3日6价均值 (buy) / 当日均价 (sell) | 同左 |

### 新增文件/改动
- `src/analysis/fast_evaluator.py`:
  - `_aggregate_bullish()` — 信号聚合
  - `_sigmoid()` / `_compute_position_target()` — sigmoid 映射
  - `_simulate_position_target_python()` — 每日渐进调仓模拟
  - `FastEvaluator.evaluate_position_target()` — 新评估入口
- `src/analysis/genetic_searcher.py`: `StrategyEncoding` 新增 `position_slope`/`position_bias`（向后兼容）
- `config/optimizer_constraints.yaml`: 新增 `position_model` 段
- `tests/test_fast_evaluator.py`: 10 个新测试（信号聚合、sigmoid 映射、仿真场景）
- `scripts/preview_position_target.py`: 对比预览脚本

### 当前状态
- ✅ 核心算法实现 + 22 个测试全绿
- ✅ 旧 evaluate() 路径不受影响（增量添加，未替换）
- 🔄 预览脚本已部署到服务器（需 `python main.py --once` 先跑一遍数据）
- 📋 TODO: 集成到优化器 V2 搜索流程（`evaluate_position_target` 替代 `evaluate`）

---

## 📋 历史 TODO / Roadmap (v1.18-beta)

> 本节保留为历史快照。当前可执行路线图以 `README.md` 与
> `docs/development/todo_backlog.md` 为准；其中 14 窗口、日报周 NAV 箱线图与持仓正文等
> 描述已被 84 个月 `22/16/2/4` 合同及当前移动端日报设计取代。

| 项目 | 状态 | 说明 |
|------|------|------|
| 多渠道通知统一配置 | 🔄 开发中 | Telegram + 飞书群机器人 Webhook + 邮件，YAML 驱动，NotifierManager 统一入口 |
| 策略优化器 V2 | ✅ 已上线 | Walk-Forward 14窗口 + 遗传搜索 + numba 向量化；02:00 cron 更新 YAML，日报/简报直读 |
| **给定时间段回测工具** | 📋 TODO | 支持自定义起止日期 + 基准对比（如 510300）+ 训练/测试严格分离。可用于审计历史表现、验证策略在特定市场环境下的有效性 |
| 飞书日报原生卡片 | ✅ 已实现 | 飞书日报/告警不再复用邮件 HTML 转 Markdown，改为从 Session/DataFrame 直接生成多张飞书卡片（价格/基本面/技术指标）；飞书不渲染管道表格或 fenced code block，因此表格统一用纯文本等宽显示，避免单卡截断 |
| 飞书核心链路测试 | ✅ 已实现 | `tests/test_feishu_notifier.py` 扩展到 29 个测试，覆盖初始化、传输、日报/告警/简报、多卡分段、DataFrame 采集、告警码、技术字段、纯文本表格、中文宽度、格式化 helper；测试允许 Mock Session 接口但不 Mock 数据对象 |
| 飞书真实数据链路测试 | ✅ 已实现 | `tests/integration/test_feishu_real_data.py` 用最小 config 只放 `601728`，通过 `StockDataFetcher` 真实取数写入 Session，只启 Feishu；默认 patch `_send` 只验证一张价格卡片，设置 `FEISHU_E2E_SEND=1` 时才真实发送 |
| Telegram 交互 Bot | ✅ 已实现 | `main.py --interactive` 启动 Telegram 轮询 Bot，支持 `/help` `/list` `/add` `/remove` `/backtest`；白名单 + 限流安全层；纯 requests 轮询，不添加第三方依赖 |
| **参考持仓跟踪** | ✅ 已实现 | 持久化参考持仓（RefPortfolio），简报/日报展示系统持续运行的仓位状态（标的/持仓/现价/现金/参考 Nav/期初日期/交易天数）。只在简报时间（09:50/14:30）按量化信号调仓，禁止盘后交易，周末休市不交易。手动 `/ref_date` 重置时清空仓位并恢复初始现金，有防呆确认。详细设计见下方。 |

### 日报完整性与三基线回归修复（2026-07-26）

- 日报不再因 `daily_mode` 删除锚点、锚值、偏离和技术指标；邮件正文固定展示价格/锚点、基本面、技术面三张表。
- 统一回测入口重新消费 `benchmark_data`：A 股比较 `510880 / 510300 / 无风险`，港股和美股比较 `VOO / BRK.B / 无风险`。
- 基准累计收益、策略相对每个基准的超额收益、以及最近九个月“任意日买入持有到期跑赢”的胜率分别计算、分别渲染，禁止再将基准收益错误标为胜率。

### 优化简报完整性修复（2026-07-26）

- 三市场优化简报保留每个市场的验证期收益、三基线/胜率、验证期末持仓和最近八周 NAV 变化；不再在统一摘要路径中丢弃旧版回测信息。
- 搜参简报区分“按配置实际评估次数”和“遗传算法最终入围数”：默认配置为每市场 `30,000 + 5 × 25,000 = 155,000` 次评估，`5,000` 仅为最终保留池上限。
- 搜参元数据与验证期快照写入版本化参数产物；补发通知不需要猜测历史搜索规模。
- Telegram 在发送共享 HTML 摘要前将 `<br>` 规范化为换行，避免 Bot API 拒绝不支持的标签。

### Walk-Forward 验证集隔离（2026-07-26）

- `walk_forward.num_windows` 表示总窗口数，`validation_windows` 表示末尾严格留出的日报验证窗口；两者均在 `config/optimizer_constraints.yaml` 配置。当前为 14 总窗口 = 13 个历史排序窗口 + 1 个 9 个月验证窗口。
- 只有历史排序窗口可以参与：WF 加权得分、稳定性惩罚、硬约束筛选、GA 父代/子代保留和参数敏感性。验证窗口只生成日报/简报的样本外回测，不得用于候选排序或从备选策略中挑选“表现更好”的版本。
- WF 评分恢复为原始公式：`weighted_excess_return - stability_penalty * std(window_returns)`。遗留配置的权重少于排序窗口时，系统延用最后一个权重，确保不会因 `zip` 截断而丢掉任何历史窗口。
- 参数敏感性恢复为策略接口的通用能力：对全部离散参数生成 10 版、每维最多 ±3 档的扰动；仅在历史排序窗口上计算，作为报告诊断，不改变候选排名。新增策略无需改优化器即可继承该行为，也可按自身参数耦合规则覆写。
- 版本化产物和三渠道优化简报记录排序/验证窗口数量与敏感性摘要，明确标注验证集未参与排序。
- 搜参数据预检现在要求每个入选标的覆盖完整配置的 Walk-Forward 时间轴（训练期 + 最末窗口完整测试期）；历史不足的标的继续参与日报/简报，但不再截断市场公共时间轴、把 13 个排序窗口悄然降为 12 个。
- Walk-Forward 窗口从最新可用交易日向前排布：最末隔离窗口的结束日与日报九个月验证期的最新数据日对齐。指标计算所需的额外历史缓冲不会再把隔离窗口留在旧区间。
- 优化器日志输出完整窗口数量及首尾测试区间索引；若数据不足以构成完整配置窗口，会明确记录行数与配置需求，避免无候选结果难以审计。
- 优化数据回看期在配置窗口总长度之外保留 180 个日历日（而非 90 日），覆盖交易所节假日与多标的日期交集损耗；避免 A 股实际共同交易日比完整末窗只少数日而无候选。

### 统一现金档位执行与动态自选（2026-07-27）

- 所有注册策略的参数空间由 `TradingStrategy` 自动追加 `buy_cash_tier` 与 `sell_cash_tier`；`position_frac`、每规则比例和月度买入金额闸门不再参与当前策略执行。
- 策略输出统一为 `TradePlan`（买卖信号、强度、单笔买卖现金上限和预热期）。优化器、日报回测、简报及实时告警均消费同一份计划，避免扫描器另行解码阈值。
- 同日候选按策略强度成交，代码仅作稳定平手裁决；先卖后买，严格遵守现金上限、手续费、交易单位与最短持有期。
- 日报矩阵使用自选标的的日期并集与逐标的可交易掩码。新加入标的不会缩短既有标的历史，满足策略预热后自动加入；未满足者在报告中标为“预热中”。
- 版本化产物保存实际现金金额快照和严格 WF 验证起止日期。旧比例产物在读取时一次性映射到最近现金档位并标记迁移，下一次搜参写入原生 v2 产物。
- 每月最小/最大成交次数硬约束及交互配置入口已移除；成交笔数保留为优化简报与日报诊断指标。

### 信号契约与例行搜参恢复（2026-08-07）

- 修复日报/简报/PDF/飞书信号表格显示 `?`：`main.py:_scan_group()` 产出的 alert 字典补齐通知契约字段 `rule_label`（策略名+方向，如 "分位评分 BUY"）与 `current_value`（"评分 x.xx" 信号强度）；`label`/`detail`/`rule_id` 保留给 ref_portfolio 与交互端。消费方（email_notifier 日报表格/文本、简报表格、PDF LaTeX、feishu markdown）统一读取这两个字段，一处修复全部生效；新增 `_alert_value()` 辅助函数统一兼容 dict/对象两种 alert 形态，裸 `getattr` 在 dict 上恒返回默认值的问题一并消除。
- 修复例行搜参中断：`ci_cd_deploy.py` 部署时不再删除 `main.py --optimize` cron，改为幂等确保 `0 2 * * *` 例行任务存在（systemd scheduler 只覆盖每日 19:00 与简报，优化器因内存隔离需独立进程 + optimizer_guard，不能由 scheduler 线程承载）。遗留 `--once`/`--brief` cron 仍会清理（已由 systemd 覆盖）。

### 港股搜参数据源与 PDF 附录修复（2026-08-08）

- 港股例行搜参恢复：腾讯 fqkline 接口单次最多返回约 600-1000 根 K 线，超出后返回
  `{"code":0,"msg":"param error","data":[]}`（data 为 list），旧代码对 list 调 `.get()`
  崩溃，导致优化器 2006 天 lookback 下港股全被跳过（"港股: 无可用数据"）。`_fetch_historical_from_qq`
  改为按 2 个日历年的窗口分页拉取后合并去重（qfq 复权锚定最新数据，实测跨窗口价格一致，
  拼接无断层），并对 param error 结构做容错；A 股冷启动同样受益。
- 日报 PDF 附录修复：MD→LaTeX 转换器此前只支持单行 `$$...$$`，多行公式块被拆成空
  `\[\]` + 普通段落，导致 xelatex 报 `\mathrm allowed only in math mode`、附录公式乱码；
  转换器改为块状态机（`$$` 开关显示数学环境，块内行原样保留）。

### 逐时点基本面、原始行情与除权数据（2026-08-17）

- `python main.py --backfill-point-in-time-data` 与
  `python scripts/backfill_point_in_time.py` 回填配置内全部标的，产物写入
  `data/point_in_time/`；唯一详细合同见 `docs/llm/point_in_time_data.md`。
- 市场数据同时保存不复权 `raw_*`、前复权 `qfq_*` 与 `qfq_factor`，强制满足
  `qfq_price = raw_price × qfq_factor`。基本面历史估值只使用当日 raw 价格，
  禁止用今天锚定的前复权价格回推历史 PE/PB。
- 公司行动保存真实除权日、已知公告日和来源；不能可靠拆分现金、送转、配股时只记
  `adjustment_factor_change`，不得猜测股数或净资产。
- A 股季度基础历史来自 Baostock `pubDate/statDate`；沪市由免费上交所 XBRL
  补充并纠正归母/扣非利润、经营现金流和归母权益，深市从巨潮免费完整年报 PDF
  严格解析同类字段。官方值在真实披露日形成新修订，禁止提前覆盖。
- A 股与美股自由现金流统一为真实经营现金流减真实资本开支；任一字段或单位缺失就
  保持缺失。美股来源为 SEC Company Facts `filed/end`；港股适配器未完成前明确
  缺失。生产链路禁止付费源、模拟值、默认值和 mock 数据。
- `FundamentalFeaturePanelBuilder` 只消费上述逐时点存储与 availability mask；
  当前画像仍不进入技术策略或历史搜参，直至覆盖率和防前视验收通过。


### Point-in-time source coverage routing (2026-08-17)

- Market-history success and requested-window completeness are separate contracts. Reports expose requested/actual boundaries, calendar coverage ratio, and full/partial counts.
- A-share market history compares Yahoo only when Baostock starts later than the configured tolerance, then deterministically selects the earlier and denser real-data bundle.
- SSE fundamentals retain the official SSE performance feed and supplement the most recent three annual periods from official CNINFO-indexed PDFs because the legacy SSE full-statement endpoint is stale.
- Parsed annual-report values are cached by source URL with content and parser-contract hashes; the cache changes network cost only and never fabricates missing fields.
- No paid or simulated source is introduced. Partial coverage and parser failures remain visible diagnostics.

### 逐时点资本成本、个人必要回报率与 WACC（2026-08-21）

- 废弃内在价值实验中的9%/11%统一贴现率，也禁止将低Beta CAPM直接当作个人
  必要回报率。市场CAPM与投资者要求必须分别披露；模型不确定性和主观风险不得
  叠加进 WACC 或股权成本，主观风险只保留为买入价折价。
- `CapitalMarketAssumptionStore` 按日期保存中债十年国债收益率和沪深300前瞻隐含
  ERP；估计必须使用官方指数快照的PE、PB和股息率，从 `PB/PE` 得到指数ROE
  代理、从股息率/盈利收益率得到派息率，在五年内将增长和派息率淡化到2%/3%/4%
  终值增长并反解要求回报。3%为基准，2%–4%全部报告为敏感区间。
  估值只能读取 `as_of <= evaluation_date` 的输入。Beta 必须同时估计2/3/5年日频
  和周频OLS，保存样本数、标准误、95%置信区间和R²，以有效估计的中位数为中心、
  四分位区间为敏感性。禁止固定Blume向1收缩。历史已实现ERP必须使用含分红的
  全收益指数，价格指数不得冒充；未取得该数据前保持明确缺失，不得补默认值。
- 有完整市场输入时，市场股权成本与WACC的低/高边界必须由Beta四分位和ERP情景
  的组合直接推导；缺失逐时点市场输入时必须明确标记配置回退。股权现金流估值
  默认使用8%/7.5%/7%个人必要回报率的下行/基准/上行情景，逐场景实际贴现率为
  市场CAPM与个人要求的较高者，永续增长默认2%。
- 反向DCF只求解当前价格隐含的五年显式增长，并与基本面增长并排展示；其结果
  不得反馈到公允价值。改变现价必须只改变隐含增长、安全边际和价格区间，不得
  改变低/中/高内在价值或保守买入价。
- FCFF 代理使用 WACC；本版 OCF-CAPEX 是未取得净借款时的 FCFE 代理，DDM、
  盈利能力和残余收益模型同样使用个人必要股权回报率。WACC 使用
  官方年报现金及现金等价物、带息债务、利息费用、利润总额和所得税费用，并显示
  资本权重、税盾与净债务桥接。缺字段时明确回退，禁止伪造 WACC。
- CNINFO 主报表解析合同为 `cninfo-pdf-parse-5`：附注编号不得作为金额；现金必须
  取“年末现金及现金等价物余额”，不得把货币资金或受限现金混作可用现金。新版
  官方解析行替换同来源旧解析版本，不能由“错误字段更多”阻止修订。

### 生产调度可靠性（2026-08-20）

- `trade-eyes-health.service` 是生产环境唯一的日报/简报调度宿主；部署必须清理旧的
  无参数 `main.py` 独立调度进程，禁止两个 APScheduler 同时生成重复通知。
- 日报默认允许整点后3600秒内补跑，简报允许900秒，优化任务允许7200秒；均可在
  `scheduler.*_misfire_grace_seconds` 或单份简报的 `misfire_grace_seconds` 配置。
  所有任务必须 `coalesce=true`、`max_instances=1`，服务器短时高负载不得因
  APScheduler 默认约1秒的misfire窗口而直接丢弃整次推送。

### 日报移动端与发送频次（2026-09-02）

- 日报正文继续保留部署节点和公网 IP，用于确认线上版本；示例账户只展示
  NAV/回报/现金等摘要，不在日报展开持仓明细。
- 周 NAV 改为无脚本的移动端箱线图：优先使用日 NAV 序列计算周内四分位数，
  老缓存只有 OHLC 时使用可审计的 OHLC 区间回退，避免把长数值串贴进邮件。
- Bot 新增 `/report_frequency daily|weekly|off`（别名 `/daily_frequency`）。
  该开关只影响无告警的普通日报；价格/策略告警仍即时发送，手动 `/daily` 始终有效。

### CAPM 股权 DCF 买入价历史校准（2026-08-24）

- 新增独立的 `scripts/calibrate_capm_dcf_entry.py` 与
  `src/fundamental_embedding/dcf_entry_calibration.py`。该工具只生成点时审计报告，
  不得变更活动策略、参考持仓或线上参数；详细唯一合同见
  `docs/llm/capm_dcf_entry_calibration.md`。
- 贴现率严格为 `risk_free + beta × ERP` 的股权成本，忽略债权成本；终值增长固定
  2%。五年增长只由估值日已发布的营收、利润、FCF、CAPEX/OCF 与 ROE 数据构成。
  ERP 分段主观风险以 `g / 1.25`（ERP>5%）或 `g / 1.50`（ERP<=5%）保守折减，
  绝不可将风险乘数直接抬高公允价值。
- 年报 FCF 进入估值前必须通过收入、利润和现金转换率交叉核对；PDF 解析异常须被
  拒绝而不是截断。历史拆股/送配也必须按估值日已披露行动调整股本，确保每股 FCF
  与原始成交价格可比。
- 校准接受条件为：限价在未来252个有效交易日可触发，随后252日收盘至少半数高于
  成本，且每个ERP情景的触发覆盖、样本量和成功率均通过。完整网格、时间留出和
  失败原因必须写入报告；没有通过者不可用“最不差”参数替代。
- 交易日标签必须累计252个真实可交易日，不能从固定252条原始行情行中扣除停牌行；
  金融残余收益链的财务账龄只取账面、ROE与派息输入，禁止由无关的FCFE缺失将其
  判为陈旧。每个ERP压力情景无成交或无后验观察时，排序分数必须明确失败，不能
  因Wilson下限列表忽略空情景而选出极低、几乎不成交的限价。
- 默认的4%/12笔/50% Wilson证据门槛属于“估值链是否有足够研究样本”的安全合同，
  不是对广义股票池宣称20%买入频率。三条估值链各自必须有至少100个训练期事件；
  样本不足的链显式标记为不支持并只产生空信号，绝不借用其他链参数。2026-08-24
  的广义研究池留出中普通FCFE与金融残余收益链通过，周期FCFE因样本不足停用；
  当前配置的A股标的池本身没有足够逐时点历史证据，因而仍不得接入活动策略或执行
  全量搜参。以后只能在该池获得足够因果样本并通过同一统一回测Gate后，以手动候选
  方式接入。
- 大盘训练事件数只用于**选择与认证**政策，不能阻止小配置池对已冻结、已通过留出的
  政策做时间外应用。`--frozen-policy-report` 必须校验源经济合同，且仅从源报告的
  `validation_start` 起评价接收池；它不会按接收池标签重训或更改参数。接收池应用的
  小样本只提供迁移证据，仍须通过统一策略收益/回撤/三基准 Gate 才能成为候选。

### 搜参验证链路：raw/qfq 与 84 个月窗口（2026-08-26）

- 技术指标和信号固定使用 qfq；成交、持仓估值和 NAV 固定使用 raw 不复权价格。
  `PointInTimeMarketStore` 的 bundle 同时携带 raw/qfq 行情及公司行为，缺失、覆盖不足
  或 raw/qfq 覆盖不一致时验证 fail closed，不回退到 adjusted-only legacy cache。
- 除权除息日在统一执行 schedule 中处理：现金分红增加现金，送转股/拆股调整持仓数量
  和每股成本；现金 cap、target-weight、scalar、batch、Numba 与基准 buy-and-hold
  共用同一合同。
- walk-forward 固定为 `12/9/3/22`，其中 16 个 ranking、2 个 overlap purge、4 个
  9 个月 holdout 窗口；holdout 总窗口月数为 36，不进入 Solver。Solver ID、参数 schema、
  参数空间、预算和评分公式不变。
- 日粒度仍是默认。服务器负载过高时的 weekly 降载模式只登记为 TODO，启用前必须完成
  周/日一致性、负载和运行时对比验收；周粒度买入取周最高、卖出取周最低。
- Holdout 报告新增整体汇总：由于4个9个月窗口相互重叠，收益、最强基准超额和 Sharpe
  采用窗口等权均值，最大回撤取窗口最差值；不将重叠窗口错误串联复利。多数基准超额
  同时保留并作为 Holdout Gate 的相对比较口径。

### 回测自动补数与窗口/日报重设计（2026-09-03）

- 普通 `/backtest CODE START END` 和 `--optimize` 在外层先准备 raw/qfq/公司行为
  行情包，缺失或不完整时通过 `MarketHistoryProvider` 补数并写回 Point-in-Time
  store；补数后再次校验覆盖、时间戳、raw/qfq 恒等式和公司行为可解释性。指标、
  `Backtester` 与 `evaluate_all_groups()` 仍是纯计算边界，不在仿真途中联网。
- 搜参不修改监控配置；补数仍失败的代码只在搜参输入阶段排除，并记录代码、来源、
  缺失区间和失败原因。新搜参结果始终先作为 candidate 保存，生产策略必须通过
  `--activate-run` 显式切换。
- 搜参报告保存每个窗口的 `role`、分组序号、全局序号和训练/测试日期，固定展示
  `22 / 16 / 2 / 4`（总数 / Ranking / Purged / Holdout）。Holdout 的整体收益、
  超额和 Sharpe 为四个重叠窗口等权平均，最大回撤取最差窗口。
- 19:00 日报首屏改为部署状态、A/HK/US 决策指标、告警/信号、Holdout 小卡片和
  图表；主机名、公网 IP 保留。周 NAV 只显示移动端 boxplot，日报不贴周 OHLC、
  最终持仓或季末持仓明细，详情通过时效报告链接/PDF 查看。
- Bot 频次使用 `/report_frequency daily|weekly|off`（兼容 `/daily_frequency`）：
  普通无告警日报按配置发送，告警即时发送，手动 `/daily` 强制发送。

### A 股估值策略独立 benchmark（2026-09-05）

- `scripts/benchmark_value_strategies.py` 专门为 `capm_dcf_value` 与
  `valuation_aware_ensemble` 组装历史上下文，不再把基本面/估值策略误当成
  无上下文的技术策略运行。benchmark 入口显式安装 `context_enricher`，并把
  上下文合同哈希纳入搜索输入指纹。
- `valuation_aware_ensemble` 的 A 股实验使用 60,000 个 Random 候选、84 个月
  的 `22/16/2/4` 窗口；`capm_dcf_value` 只有在当前 v2 冻结政策通过自身留出门槛
  后才允许进入搜参，旧 v1 政策不得静默复用。

### ETF Collar 严格 1x 与 crash-month neutralize（2026-09-03）

- 新增 `scripts/backtest_collar_nav_sizing.py`：510300/510500 的 100% Put、
  105% Call 每月按 close-mark NAV 重置 ETF 名义本金，Put/Call 均覆盖重置后的
  ETF 份数；使用连续分数合约作为研究近似，并对每日归因账本做最终 NAV 对账。
- 同时生成从 test_start 以相同初始资本重新开仓的 OOS 组合，避免把训练期亏损
  通过固定 ETF 份数转化为测试期隐含杠杆；历史新浪数据无 bid/ask 时，买入取 H、
  卖出取 L，日内估值取 close，到期按内在价值结算。
- crash-month 稳健性不再删除月份，而是将排名靠前的相对优势月份设为
  `R_collar,t = R_spot,t`，保留所有月份并按月度相对增长因子复利；结果写入
  `cache/analysis/collar_nav_resized/{510300,510500}/`。

### 云端三市场配置漂移与失败产物保留（2026-09-08）

- 云端更新代码不会自动更新被 Git 忽略的 `config/config.yaml`。部署必须在恢复
  云端配置、应用显式 `--sync-config` 后，使用严格运行时解析器校验三个市场；
  配置不兼容或上传失败立即停止，不恢复 `optimizer.engine` 或全局 Solver 回退。
- 搜参启动失败必须保存具体配置原因；无候选和失败运行同样保留独立市场的
  `run_summary.yaml`、状态 HTML、archive/readiness 及 Gate 淘汰统计，不能因为
  没有可激活 manifest 就被立即清理。保留数量按市场计算；诊断记录没有激活权限。
- 搜索完成、保留候选、通过激活 Gate 是三个不同状态；不得把无候选称为进程崩溃，
  也不得把 candidate 文件存在视为可生产激活。84 个月/PIT 数据合同不变。
- 事故证据、云端备份、全量验证及剩余数据限制见
  `docs/llm/optimizer_cloud_repair_20260908.md`。

- v3 冻结后的两项廉价 falsification（scripts/justified_pb_ablation.py → data/analysis/justified_pb_ablation/<ts>/）：
  尾部过滤消融（w11 弱市 / w17 牛市）：低波动过滤是双向主因（弱市保护、牛市削顶），剔除最热动量近似中性；
  m/2 校准：P(m/2 > 实际 BVPS CAGR) 随分桶单调 12%→72%，>8% 桶把 BVPS 增速 p25 从 1.35% 抬到 3.02%，
  但 corr≈0.05、中段平坦、12 个重叠 cohort（1–6 年视界），只能算弱的 quality-persistence 证据；
  g_fund 已从总归母权益 CAGR 改为 BVPS CAGR；选股集中度：12 窗 229 个持仓位仅 52 只不同标的、相邻窗重叠 10–16 只，
  即约 2–3 个独立组合，7/12 胜率不具统计显著性。

### 日报高密度决策版（2026-09-18）

- 19:00 日报以“决策板 → 完整行情矩阵”为固定阅读顺序。原先由 MA60/WMA 区间
  触发的行动清单已移除，避免把价格锚点提示误作独立交易指令。行情矩阵复用
  简报的三日数据新鲜度、锚点和偏离率合同，但按 A 股、港股、美股分组；桌面显示
  开盘/收盘/锚点/锚值/偏离，移动端隐藏非关键列而不产生横向滚动。过期数据保留为
  `数据未就绪`，不得静默从监控池消失。
- 主机名和公网 IP 固定在标题下的紧凑部署状态条。示例账户、最终持仓和季度持仓不进入
  日报 HTML；PDF 附件继续承担深度持仓、公告、定增、技术面和完整基准附录。
- 策略仅按 A/HK/US 独立市场展示收益、主基准超额、回撤、Sharpe 和交易数。只有完整
  `22/16/2/4` 产物（4 个有序 Holdout 窗口及所有数值）才显示 Holdout 汇总和 H1–H4；
  不完整产物只显示未就绪状态，禁止渲染 `H—` 或指标占位符。
- 邮件只嵌入一张归一化三市场组合曲线；周 NAV 箱线图已从日报正文移除。事件速览完整
  展示已收集的待处理分红、未解禁定增和公告；深度资料仍可通过
  PDF 和时效 token 报告链接查看。
- 上线前必须对账生产工作树。先保留可恢复的差异/未跟踪文件清单，再以命名 stash 使
  工作树满足部署脚本的 fail-closed 检查；不得 reset、覆盖或提交 `.repair-*`、证书/密钥
  备份、临时诊断脚本和线上配置备份。

### 日报估值矩阵精简（2026-09-18）

- 日报正文移除周 NAV 箱线图：邮件客户端无法可靠渲染其细线图形，组合走势保留为单张
  归一化总览曲线，深度 NAV 资料继续位于完整报告/PDF。
- 完整行情矩阵以 `PE / PB / 隐含 Ke` 替代锚点、锚值和偏离三列。隐含 Ke 仅为稳定期
  Gordon 诊断：`Ke = TTM EPS / 收盘价 + 2% = 1 / PE + 2%`，不进入策略、公允价值或搜参。
- 仅正 PE 显示隐含 Ke；PE≤0、缺失 PE/PB、ETF 无可解释估值字段或行情未就绪均显示 `—`，
  不使用 FCF、预测盈利或默认值填充。移动端显示收盘、PE、隐含 Ke，PB 和开盘列折叠。

### ETF 指数/成分股估值补全（2026-09-19）

- 日报 ETF 的 PE/PB 不再读取基金自身交易行情字段。中证系列从中证官方最新成分权重
  文件读取篮子后，批量获取成分股 PE/PB，并按有效权重的盈利/账面收益率作调和聚合；
  覆盖权重低于 80% 时不显示，禁止用部分低覆盖数据伪造完整估值。
- VOO 与标普500ETF 华夏共用 Vanguard 官方 VOO 完整持仓；QQQ 使用公开基金成分股
  聚合；日经ETF使用日经官方指数统计。若服务器 IP 被 Yahoo 或日经官网拦截，QQQ
  退到每日 Nasdaq-100 公开汇总，日经ETF退到公开 PER/PBR 日度表，并记录降级来源。
  QQ 实时估值同时修正为 A/HK/US 各自的 PB 字段。该解析器只用于日报展示，不进入
  搜参、信号、公允价值或交易路径。
- 黄金 ETF 和 REIT 继续显示 `—`，因为它们不代表一个可用企业盈利/账面价值聚合的
  普通股篮子。日报注释明确 ETF 的估值口径和这一边界。

### 日报待处理分红/分派事件（2026-09-19）

- 日报编排层在发送前准备待处理分红/分派，HTML 只渲染已准备好的事件，不在模板
  渲染过程中联网。公司公告沿用 LLM 已结构化的现金额、除权日与派息日；本地
  Point-in-Time 公司行为仅在上游提供明确派息日时补充。
- 境内 ETF 与交易所 REIT 读取公开基金分红表的“除息日 / 分红发放日 / 每10份分红”，
  换算为每份现金；5 位港股代码读取公开的“除净日 / 派送日 / 每股港元”日历，避免
  依赖公告 LLM 是否命中；VOO、QQQ 等海外权益 ETF 仅读取公开日历中的明确除权、
  派息日期。这条链路仅影响日报事件速览，不进入搜参、信号、回测或交易。
- 事件速览把 `未除权` 与 `已除权未派息` 放入同一状态列，并优先展示在普通公告和定增
  之前，按“待派息分红 → 未解禁定增 → 公告”的固定顺序完整展示，不截断任何已收集
  事件。只有同时具备可验证的除权日与派息日才显示，缺日期、已派息或金额不确定时不
  猜测状态，金额未知明确标为“待公告”。

### 日报配置池完整性（2026-09-19）

- 完整行情矩阵以当前 `config/config.yaml` 的 `stocks` 作为唯一标的池，不能从历史缓存或
  旧邮件副本继承已删除标的。某个配置标的缺行情、过期或未完成拉取时，仍须保留一行并
  标注 `未就绪`；不得因缺行而静默从日报消失。
- 单次完整日报在行情的 MA/WMA 锚点之后，必须继续写入 RSI、MACD、ADX、Bollinger
  和量比。数据获取器通过 `src.core -> src.data` 的包内导入调用该组指标，避免相对导入
  层级错误导致所有标的静默跳过扩展指标；ADX 派生 Series 必须保留输入行情索引，避免
  缓存合并后的非连续索引发生 pandas 对齐扩张而跳过整组指标。

### 日报真实链路预览验收（2026-09-19）

- 版式预览不能复用历史邮件归档；验收必须通过当前配置执行一次完整日报链路。预览中
  行情矩阵须与当次 `config/config.yaml` 的全部 24 个标的一一对应：已删除的 GOOG、QQQ
  不得出现，当前配置内的 `01339` 即使行情缺失也必须保留为 `未就绪` 行。
- 本次正常日报链路成功拉取 24/24 个标的，扩展技术指标计算为 24 成功、0 跳过；归档
  同时验证为单一 HTML 文档、24 行完整行情矩阵、17 条事件、两张 CID 图表，并包含部署
  状态条。该归档是供人工阅读与提交前决策的真实预览，而非注入旧样例数据的模板快照。
- 本机未安装 XeLaTeX，因此本地验收只覆盖 HTML 正文及嵌入图；生产报告环境已具备
  `xelatex`。部署后仍须在该环境复核 PDF 附件，但本次不因此改变部署、策略指针或交易
  行为。

### 最新 A 股 Ke + 低波动研究的数据时点防护（2026-09-19）

- `scripts/evaluate_ke_low_vol_portfolio.py` 的 Gordon 信号现在在每个形成日同时
  检查 ROE 的报告期、ROE 披露日和 PB 所依赖基本面的披露日，默认最多 400 天；任一
  输入过期、未来披露或缺失时，标的只会进入 `stale_fundamentals` 计数，不能参与 Ke
  排名、权重优化或当前篮子。该过滤与原始/raw 价格和 qfq 收益代理边界并存，避免把
  2020 年等旧财报静默用于后续形成日。
- `scripts/backfill_reference_universe.py --official-pdf-recent-years N` 仅覆盖该隔离
  回填批次的官方年报深度，不修改 `config/config.yaml`。深度进入 batch fingerprint，
  因此 6 年年报覆盖会建立不同的可审计检查点批次，不能把较浅的成功检查点误复用为
  历史 Ke 验证数据。
- 截至 2026-09-18 的半年度报告不可直接当作 TTM ROE 使用：半年度累计值必须先与上年
  同期和完整年度进行一致的 TTM 重建，且 PB 所用期末净资产/股本需要可解析的官方字段。
  在该重建合同完成前，研究宁可使用仍在 400 天时限内的已披露完整年度数据，也不把
  年化中报数伪装成 TTM。
- `scripts/validate_reference_universe_backfill.py` 在完整性验收之外增加了
  `fundamental_freshness` 收据：按可配置的报告期年龄列出当前/过期标的及覆盖数。该项
  是数据覆盖诊断，不将新 IPO 等不可估值标的错误地变成全池文件完整性的失败；真正进入
  Ke 组合的每只标的仍由 evaluator 的 ROE/PB 双输入新鲜度规则逐形成日 fail-close。
- A 股行情回填的“完整覆盖”现同时要求请求窗口的起点和终点都在容差内。此前只检查
  起点，可能把 `actual_end` 明显早于形成日的 Baostock 数据写成 `full`；现在会探测
  Yahoo 备用源、按整段覆盖优先选择，并将终点过期的成功检查点在恢复时重新抓取。该修复
  防止最新 Ke 形成日使用停在旧日期的价格，同时不把上市不足历史窗口的新 IPO 误作数据
  损坏。

### 统一两年评估合同（2026-09-20）

- `config/optimizer_constraints.yaml` 的 `evaluation_horizon_policy` 是唯一的时间
  口径：所有可执行的测试、搜参和回测的单个评估窗至少 24 个自然月；日报历史和独立
  回测命令的默认跨度为 36 个月。代码不再自行写入 9 个月、756 日或其它评估期常量。
- 三个生产市场统一采用既有数据可覆盖的 `84m / 24m / 12m / 5` 几何：3 个 ranking
  窗、1 个与最终窗重叠的 purged 窗，以及 1 个连续 24 个月且完全不参与候选选择的
  holdout。`run_optimizer`、市场配置解析和日报 artifact 读取都 fail closed：短窗、
  缺少连续 holdout、重叠但未 purge、或没有独立 ranking 窗都会被拒绝。
- 本地 Ke/低波研究改为共享 `66m / 24m` profile；Gordon-Ke 的最终验证改为连续
  24 个月。ETF collar 的默认回测跨度同样读取主配置，并拒绝低于 24 个月的
  `--test-days`。全程只调整代码和合同，没有触发回填、全量搜参、策略激活或部署。
- 旧的 9 个月 Justified-PB 脚本和其 ablation 仍保留，以便审计历史产物，但入口已经
  隔离并不能再产出当前研究结论；旧短窗 artifact 只能作为诊断，不能出现在日报参照中。
- 交互式 `/backtest`、CAPM DCF 回测、stateful Ke/PB replay 和 intrinsic-value
  横截面诊断同样在入口或配置对象处执行该下限；手工给出一段不足 24 自然月的日期
  不能绕过主配置。DCF entry calibration 的进场等待期与进场后结果期合起来也必须
  覆盖该下限，不能把任一半段单独作为策略结论。
- 标的池稳健性报告不再复制旧 `22/16/2/4` 文本或 JSON 数字；它从每个市场已解析
  的 walk-forward 合同生成窗口说明。这样变更主配置后，HTML、JSON、日报和可执行
  回测不会各自保留另一套时间口径。
# 飞书自然语言助手接入记录（2026-09-24）

- 新增 `src/interactive/assistant/`：独立 DeepSeek 工具对话、受限配置字段、原始
  YAML 版本化提案、发送者绑定的 SQLite 确认状态、单任务队列和 Docker 研究执行。
- 飞书只在消息以斜线开头时进入旧命令；普通私聊或明确 @ 当前机器人的群聊进入助手。
  模型无确认工具；提案默认 15 分钟有效，完整预览送达后由发起人确认。
- 配置保存复用 ConfigStore；调度增加运行时刷新及失败恢复。多文件拆提案，共享
  profile 显示全部受影响市场，实际 Solver/执行配置依照市场解析。
- 研究冻结代码与脱敏配置，主机通过共享 PIT/行情准备器补齐数据；非 root 离线
  Docker 运行统一执行合同，生成代码独立容器不得覆盖可信基准结果。结果和通知分别记账。
- 停止、重复确认、发送失败和重启不自动重复执行。添加原子状态迁移，阻止取消后
  继续配置写入；取消不排在慢速 DeepSeek 请求后面。附件外发前检查路径、文件大小和凭证。
- 构建/预检/显式文件部署工具及使用说明见 `docs/deployment.md` 和
  `docs/guide/feishu_telegram_setup.md`。现有未提交研究及邮件修复保持原范围。
- 验证结果与实际部署边界记录在
  [本轮开发验收](../development/feishu_assistant_acceptance_20260924.md)；
  不得把离线单元测试当作账号端消息验收。

# 飞书私聊无回复排查（2026-09-28）

- 用户实际私聊未收到回复。线上服务、助手开关、代码哈希、消息权限与已发布接收事件
  已核实，但助手 receipts 为 0；平台传输方式与真实入站仍需进一步核对。
- 补充分流原因及发送拒绝错误码日志，不记录正文、令牌或平台错误响应正文。
  相关飞书/路由测试与完整 validation 共 69 项通过。
- 登录后确认根因：飞书事件仍投递至已退役的 HTTP 回调，部署服务实际使用 SDK
  长连接。已切换为长连接并通过平台连接验证，20:08 发布应用版本 1.0.4，权限未变。
  当前应用机器人显示名为“试用clawbot”；用户已确认新私聊能收到正常回复。
  证据和后续结果见 [本次排查记录](../development/feishu_no_reply_20260928.md)。

# 飞书助手项目知识与问答留档（2026-09-28）

- 排查实际两问发现原工具仅开放配置/研究业务，没有项目文档，普通问答只存在内存。
  新增部署文档白名单检索、目录及按行读取工具；自动检索随每轮输入，不依赖模型
  主动发现资料。文档作为工具数据附带路径、行号和 SHA256，不赋予执行权限。
- 新增 SQLite conversation_turns/conversation_events，串联入队、问题、模型与工具
  调用、依据、答复、错误和送达。持久化前脱敏，限制长度和条数；默认保留30天。
  启动中断在途问答不重放，按聊天/发送者恢复有效期内成功问答；清空对话设置恢复边界。
- 公告规则说明按远端实际抓取/筛选/邮件调用链核对，单独维护，避免把本地未部署的
  其他研究文档覆盖到线上。使用与排查见 `docs/guide/feishu_telegram_setup.md`。

# 飞书仓库导航与 Pi harness（2026-09-28）

- 用户明确允许把项目资料交给既有DeepSeek，并要求机器人能自动多轮理解仓库。
  新增 `repository.py`：受限源码目录、分页、关键词检索、按行读取、AST函数定位；
  禁止越界、链接、凭证/运行数据读取，限制扫描范围并报告截断。
- 新增 `harness.py` 与 `tools/feishu-pi`：锁定 Pi Agent core 0.87.1，Node控制多轮，
  Python仍独占模型凭证、HTTP、业务工具、确认权限和SQLite。保留显式native后端，
  不因Pi异常自动重跑。Node不提供内置shell、读写或自动加载扩展。
- 真实原两问初测答对业务规则，但模型编错引用行号；新增行号化上下文、引用范围
  校验和一次自动纠正。当前公告设置新增只读配置视图，缺省与已配置值分开表达。
- 预算包括模型轮数、工具次数、上下文字符与总时间；Pi进程在超时/停止时收尾，
  最后一轮只总结已有证据。无修改正式回测或报告算法、数据准备与生效确认合同。
- 新验收及依赖取舍记录于 `docs/development/feishu_repository_harness_20260928.md`。

## 公告参数自然语言修改补齐（2026-09-28）

- 后台确认用户“怎么改成可编辑”被拒并被要求登录主机，是上一轮将七项
  `announcements.*` 标为只读导致，并非飞书或模型API权限故障。
- 七字段统一纳入配置注册表，支持范围/严格类型校验、版本绑定、确认后原子保存；
  未配置值与默认值分开，畸形旧值不回显，合法但超新范围的旧数值允许修复。
- 公告配置下次任务读取，不热改运行中任务；凭证/部署权限仍不可写。
  修改能力每次查当前工具，防止历史只读回复继续误导模型。
- 验收及线上发布记录见 `docs/development/feishu_announcement_config_20260928.md`。
# 2026-09-28：跨数据集目录与原文关联

`src/data/dataset_catalog.py` 建立独立 SQLite 业务数据索引，保留文件哈希、来源路径、解析状态、导入清单缺口及原文引用。`dataset_documents.py` 负责已登记公开资料的有界补取和文字提取；不合成原文，不改提供商风控状态。`push_dataset_snapshot.py` 将本地业务数据传入服务器独立快照。助手新增分页数据集/文件/文档查询和有界文字读取，`query_research_data(dataset_id=...)` 只改变本次只读查询的目标。配置、任务确认合同不变。目录完整与回测就绪必须分别说明。详见 `docs/guide/dataset_catalog.md`；实际验收和部署证据单独记录。

同名数据集按物理来源快照分别登记，避免新旧库覆盖统计与实际读取不一致；目录查询返回有界缺口例子和完整缺口总数。`export_dataset_catalog.py` 从只读一致快照生成全部可读目录及文件/原文关联清单。慢链路可选 `push_dataset_snapshot_sftp.py`（默认 4 路、上限 8 路），保持既有锁和全部哈希验证。实际迁移结果与验收边界见 `docs/development/dataset_catalog_20260928.md`。

### 2026-10-01：飞书助手思考模式、个股跨批次查询与网页搜索

- DeepSeek 客户端启用 `high` thinking，Pi 消息桥保留工具轮次要求的 `reasoning_content`；问答预算扩展到最多 16 轮、48 次工具调用和 300 秒，并阻止相同查询原样重复。
- 新增 `query_stock_fundamentals`，按股票代码遍历统一目录匹配的批次，调用现有 PIT 财报/行情和公司指标计算，不再把默认时点库缺失误当成全服务器无数据。
- 服务端在模型循环启动前自动预查用户消息中的六位股票代码，将只读结果及完整工具轨迹一并送入回答上下文。
- 新增只读 `search_web`，从公开网页搜索结果返回来源链接；外部摘要保留不可信标记，不作为财务验证或操作授权。
- 单测覆盖思考参数、PIT 个股自动发现、网页结果过滤和请求限制；部署/完整回归结果记录在当次交付中。

2026-09-29 授权真实 DeepSeek 联调发现模型将 `available` 误读为 PDF/文字齐备；目录改为另外返回按文档去重的 `documents_with_pdf/text/pdf_and_text` 及统计范围说明。提示明确内容可用、原文齐全与回测就绪的差别，并区分逻辑根与物理路径。回归覆盖单独 PDF、单独文字、配对、重复副本、已消失文件和空范围。

真实联调还修复了批次查询遗漏 dataset_id、时间戳猜测和分页上下文膨胀：模型侧移除与 physical_root 重复的 title，保留完整目录；提供 indexed_at_iso。最终四回合实际模型/工具检查完成，包含真实 PDF 文字读取；问原文缺口时仍建议明确“全库”，归档目录统计不代表全库。具体证据和未覆盖飞书客户端收发的边界见上述验收记录。

## 2026-10-02：飞书机器人拉取并解析 A 股报告 PDF

- 新增 CNINFO 完整年报/半年报发现接口，支持最新一期及指定年份，排除摘要；报告下载仍经目录登记和受限文档 worker，不接受任意 URL。
- 年报 PDF 和逐页文字归档到统一数据目录；扫描件按页渲染，调用 DeepSeek Flash 视觉输入分批识读并保留页码。每份报告限制 50 MiB、300 页，解析状态和结果在目录 SQLite 中持久化。
- 飞书新增 `fetch_a_share_report` 工具；默认不发送原 PDF，回答可引用原文页码，并可按 `text_file_id` 继续读全文。单测验证公告筛选、年份、归档复用、扫描识读和机器人参数路由。


## 2026-10-03：优化启动补数与上市时间轴

- 优化数据准备临时启用受支持的披露源路由；最多尝试两次，只对超时、429/5xx 等瞬时故障重试，403 不在同一来源重试。遵循 `Retry-After`、Baostock 共享限流和每市场最多 60 分钟预算。
- `data_readiness.json` 增加来源尝试、缓存复用/拒绝状态和结构化缺口类别；任一配置标的或基准未就绪时阻止正式搜参，并将邮件状态标为“策略数据未就绪”。
- 回测矩阵使用市场交易日并集，逐标的缺失行保持空值和不可交易，避免近期上市标的截短老标的历史。短历史仅在官方上市日与首根行情完全一致时接收；上市日前不造价，预热期仍按实际可用交易日计算。
- 上市日证据来自上交所股票清单、上交所基金官方查询接口（基金名单作为回退）或深交所股票清单，按代码存入 `point_in_time_data.output_dir/listing_dates/`，也写入行情包元信息和 `data_readiness.json`。官方请求将连接超时放宽至 30 秒、读超时放宽至 60 秒，仅对超时、连接中断及 429/部分 5xx 作一次有界重试；403 和无效数据不重试。未知或无法核验的上市日继续阻断，不以数据源最早价格日猜测。港股、美股及深交所基金尚无可用上市日路由，若其请求窗口早于可用行情则明确失败。
- 短历史基金数据源可能含上市前净值，必须裁去官方上市日前的行，并核对上市日存在第一笔真实交易；不能把上市前净值当作可交易价格。基本面补数回执按每只请求标的逐项确认成功或不适用，空回执和遗漏不能标作就绪。
- 美股公告抓取遇到年代错配、现金或日期与发行人记录不符的第三方文章时仅剔除该篇，继续查找后续候选；缺失的公告日期仍保持缺失并阻断完整数据包。VOO 的 2018–2020 年九次旧分红日期使用完整历史表补齐，逐项核对 Vanguard 金额、登记日和派息日，且拒绝距离除息日超过 14 天的可疑公告日期。2026-10-03 实测 VOO 全窗口数据包通过。
- BRK.B 最新年报 PDF 的文字层可能把空格压掉；无分红声明按规范化文本识别，同时仍要求官方年报中有 BRK.B 身份及独立当前零分红记录。Nasdaq 2023-06-05 的 BRK.B 最高价低于开盘价；仅当独立逐日行情与其他三项价格及成交量一致时修复单栏，并在证据中记录原值、修复值、URL 和响应哈希。2026-10-03 实测 BRK.B 全窗口数据包通过。


## 2026-10-03：证据约束的 Fundamental Quant 流式研究

- 新增独立研究链路与运行/状态/报告入口，保留原有策略、观察池和组合。广池发现、公司级补数和独立研究队列分别记录状态；缺失、冲突、过期与不适用不再降级为空值继续推理。
- 财务事实保存逐字引用、来源哈希、会计期间和原始单位换算；PIT 解析记录只作为初筛材料，正式研究另需原始财报核验。不同来源相同数值保留证据，冲突单元格隔离。
- 版本化提示词分离提取、同行、独立盲审、分析、实际结论交叉核验和一次更正；模型不能填最终估值和候选状态。模型请求无隐藏重试，错误与已知/未知 token 用量独立记录，全部审查纳入预算。
- 六项研究结论由确定性指标和核验状态生成报告；已撤回版本不能继续进入当前查询。离线评估提供冻结材料哈希、重复运行分组、故障注入和有分母的质量指标；无人工标签不能声明误拒绝率为零。
- 使用指南、运行合同及真实语料/模型/生产验收边界见 `docs/development/research_retirement_20261024.md`（原 `fundamental_quant.md` 已合并删除）。本地合成控制测试不代表真实全池研究或真实模型稳定性验收完成。

### 2026-10-04 Fundamental Quant 真实邮件验收

- 用户明确要求后，通过现有SMTP发送全池状态邮件`FQ-ACCEPT-20261004-085139`，并读取Gmail实际收件MIME正文核对哈希一致；Gmail消息ID为`1a1046660bf6729d`。1804家登记、181家阻塞、1623家待处理和0候选均与报告一致。
- 新增`accept_fundamental_quant_email.py`可复用入口；默认只准备，加`--send`才发送，持久化调度开关不变。验收回执与收到的HTML存入研究目录`acceptance/`。
- 修复通知假成功：SMTP跳过与部分拒收有明确返回，价投通知必须检查回执。93项相关测试通过。该结果只确认真实邮件链路，完整财报覆盖和真实模型验收仍未完成。

### 2026-10-04 Fundamental Quant 持续研究启用

- 用户明确授权现有DeepSeek服务、50家补数/20家深研/80次模型日限额及持续通知后，启用本机独立FQ worker，每天20:00研究、周六09:00周报；心跳、子任务结果和实际下一次运行时间分别可查。未部署远端、未注册开机启动、未改投资组合。
- 修复真实日批阻塞：财务补数跳过回测行情/公司行动；明确PE筛除提前停止普通深研；PDF逐文档隔离错误、600页默认上限和逐页缓存释放；已预留发行人中断可恢复但不重置额度，优先完成已有原文公司的证据。
- 明确退池来源同步撤回当前候选，保留历史和已推送版本更正；临时上市身份不能发布。报告覆盖完整登记池，并显示实际证据进展，不能把单批完成称为全池完成。
- 新增股东现金流桥接提案与逐项独立审查；原始事实不被调整覆盖。真实非财务封面的图像识读及独立上下文核验共用持久化模型预算，保存图像/原PDF哈希和限制；财务扫描页或未核验部分继续阻断，模型一致不代表真实性。
- 运行方式和验收边界见`docs/development/research_retirement_20261024.md`（原 `fundamental_quant.md` 已合并删除）；正式候选以最新`data/fundamental_quant/report.json`的已核验发布为准，调度ready不等于研究ready。
- 真实调用发现宿主旧模型凭据401；worker显式`--project-model-credentials`只选用配置目录`.env`模型凭据，保持其他配置/邮件凭据原规则。官方模型列表鉴权通过，FQ使用`deepseek-flash`。失败按服务/凭据不可逆指纹熔断，预算不重置。
- 真实模型曾输出单位错配、自行加总和递延税proxy，均未进入估值；提取合同升级1.4，代码验证原值/单位/币种并换算、生成事实ID，有问题提案只留审计。旧24项错误提案按响应与提取事件双重匹配隔离，原始事实及真实冲突保留。
- 10:23快照：6001登记、5644在池或待确认、357退池；REIT全量名录和5609家市值仍待补。11次真实模型调用，000009有24项新增数值通过机械检查但仍存在其他来源冲突，正式候选0。启用邮件`FQ-ACTIVE-20261004-102330`在Gmail实际收件，消息`1a104b959fc85a15`正文哈希核对一致。当前仅本机持续运行，未部署远端。

### 2026-10-04 Fundamental Quant 正式邮件排版

- 将FQ邮件的`pre`日志块替换为邮件兼容HTML：摘要卡片、研究阶段、有效候选、待解决事项、名录覆盖、今日额度。采用内联样式和限宽表格，不展示原始JSON或本地路径伪链接。
- 周报/更正/breaker通知从当前查询快照生成，周报只展示与待发送记录绑定版本一致的有效候选；撤回、过期和退池结论不重新进入候选正文。通知正文全部转义，Bot通知仍沿用原有文字通道。
- 71项邮件/集成/validation检查通过。实发`FQ-HTML-20261004-103411`已在Gmail收件箱确认（消息`1a104c31d8cef2bb`），收到HTML与发送前SHA-256一致。浏览器访问Gmail后跳转到workspace.google.com被自动安全审查阻断，未宣称Gmail或手机端视觉验收通过。

### 2026-10-05 Fundamental Quant 输出预算与证据优先续研

- 用户明确将本机每日输出token预留上限提高到5,000,000；80次调用和其他资源上限保持独立，保留已用预算。报告/邮件显示预留额度与上限，启动记录有效预算。
- 续研优先实际验证错误较少、证据缺口较小的已有原文公司；保留扣非字段页面检索权重，降低母公司单体报表的选页优先级，避免占用合并数据提取批次。
- 提取只允许可验证的PDF空白格式差异及明确每股单位别名；伪造单位、数字重排、跨文档引用仍阻断。预算阻塞仅在相关额度变化/换日并有余量时恢复，保留失败历史与发布核验门槛。
- 使用公司边界优雅退出后重启独立Windows任务加载修改；逐公司真实结果和测试边界以验收回执为准，不能把额度放大或回放通过等同于正式候选产生。

- 本轮真实恢复观察到两次8,192token输出截断，已阻断并记录；将单次上限提高为16,384后再次安全加载，保持每日5m与80次上限。在线提取改善仍须凭实际成功响应确认，不能从离线61→69回放推断。

### 2026-10-05 一次性快速验收

- 用户要求先验证链路与成本，新增独立持久化验收：3家目标、整轮120次调用/2m输出/10m输入预留、2小时新请求窗口，跨日重启不刷新；同时记入原日账，常规80次不再拖延此轮验收。
- 跳过重新发现全池，优先已有证据；初筛提取只请求相关字段，验收文档限额独立于每天4份；时点余额仅在实际年度日期锚点支持时进入资本计算，冲突仍阻断。
- 目标及同行证据确实变化才继续，全部发布/独立审查门槛保留；验收成败以有效发布记录判断，不以阶段字符串判断。终态报告结果和成本并发送既有HTML邮件，随后保持支出暂停等待用户决策。
- 小样本验收不等于全池策略或全部行业模板验收；不改变投资组合。恢复日常需明确关闭验收模式，禁止更换ID重置本轮预算。

### 2026-10-05 快速验收阻塞修复与保额续跑

- 真实失败定位为币种释义限定语漏识别、单次请求混合多份财报，以及巨潮资讯页脚水印触发正文缺页误报。币种检索/合同校验共用严格释义解析，提示词1.7和单份原文请求保留同文档、单位、外币表头、原文引用校验，禁止从记账本位币推断所有表格币种。
- PDF解析器升级fq-pdf-text-4，只将已目视核实的精确水印哈希、Artifact标记、右下角几何范围组合视为装饰，真实未知图片继续阻断。三家公司真实冻结页面回放正文误报19→0，19个封面仍须独立图像核验。
- 六次旧响应只做确定性引用修复回放，可通过机械检查事实0→6，均来自道氏技术；未写入研究事实、不能当成新模型或完整投资结论验收。其他公司未明确覆盖经营摘要的币种说明仍不放行。
- 新增显式resume_after_repair操作：只允许已停止且未耗尽的原验收，用新修复版本记录续跑历史及最多两小时窗口；原目标、已用调用/token、批次、操作预约及旧结果完整保留。定时调度不自动执行此操作，不允许靠重启/换ID刷新额度。
- 封面已通过核验的回执仅在原PDF、封面身份、原始请求/响应、模型与封面提示词均可复核时跨提取版本复用；当前正文完整性仍重新检查。验收材料保存在data/fundamental_quant/acceptance，正式资格仍须完整独立审查。

- 在线修复验收进一步发现模型把币种释义表误当作数值倍率证据，现有校验正确拒绝；先在公司边界暂停。提示词1.8明确币种定义与行/表单位各自用途，四个失败提案只更正单位引用的冻结回放0→4，不改数字、不自动写入事实。提取优先匹配折行后的财务标签，保留合并表续页与母公司表同页的可提取行，单份中报不再被要求提供其他年报的年度盈利。源数据与发布门槛不放宽。

### 2026-10-05 验收停滞及故障可见性修复

- 用户指出长时间没有可用研究版本、需要主动追问。确认此前局部测试/进程存活不能称为整链路通过，0正式候选的程序与模型故障必须显式报告。
- 修复单个空提取批次提前终止整家公司：在单份财报上下文隔离前提下按已尝试次数轮换所需财报；新尝试与新有效事实分开记录，未尝试证据可在原12批次上限内推进，模型异常不能作为持续进展。
- 模型格式失败保留无凭据诊断分类、解析阶段、内容长度与SHA256；有界保存助手正文到私有审计事件供回放，不存HTTP头、凭据或思考内容，不将原始失败正文放入日志/报告/通知。历史只有invalid_response的记录无法倒推具体原因。
- 公司终止推进立即发出稳定键去重的HTML阻塞通知，其他公司继续；整轮停止后邮件不再声称仍在补数。此类通知不构成投资判断，正式发布门槛不变。
- 本聊天增加每10分钟一次的本轮主动跟进，只报告实质进展/故障/完成，2026-10-05 22:25原截止时间与累计预算保持不变，终态停止本轮跟进；不扩张为永久额外token消耗。

- 一次真实记账诊断定位invalid_response为六个完全相同的重复currency字段。transport/3仅清理币种枚举值逐字相同的重复currency并保留原文、哈希和修复明细；冲突币种、重复数值/对象、非有限数仍拒绝。真实失败响应离线回放恢复6个提案，未自动接受为事实。175项相关回归通过，原账本/截止时间不重置。

- 实跑发现未访问优先会选到初筛无需的旧年报，增加当前缺口年份过滤，并在中报所需同期累计值已齐时转到年报；61项相关回归通过。内部单公司提取批次由12重新分配到24，逐项保留原计数及调整历史，不增加整轮120次调用、输入/输出token上限或22:25截止时间。
- 直接复用已冻结原文完成在线核对：300409选中2025年报，3项新事实全部通过原合同，事实235→238；代码初筛判定扣非PE及主营收益代理未通过，已按正常筛选流程记为screened/fail，停止普通深研。不是完整估值或正式候选验收完成。


### 2026-10-05 模型主导研究与三份有效报告发布门槛

新增 `research_contracts.py`、`research_calculations.py`、`research_prompts.py`、`research_workflow.py`、`research_release.py`。旧初筛保留为参考；候选偏离条件必须由模型解释并独立审查。事实、估算与逐项证据覆盖分离；有近期原始财务依据和现金流调整依据时，允许估算支持缺失提取字段，不能伪造来源或覆盖披露值。指标和区间由代码重算，JSON 持久化后复核。

报告先暂存，第一份通过就生成结构化 HTML 和通知；三家不同发行人、同一政策版本、当前证据且审查有效才原子发布。撤回/替代将旧暂存链接改为失效说明，取消旧通知，按实际发送回执发一次更正。Bot和邮件仍读取有效发布记录。合成测试报告不得计入真实验收。

入口：`python -X utf8 scripts/accept_model_led_research.py --prepare` 冻结缓存覆盖清单（不调用模型）；`--preflight` 检查原账本/截止时间/授权目标，阻塞返回退出码2；`--run` 仅在有效授权内执行；`--release-status` 检查有效报告与上线门槛。原停止账本不会自动恢复，不改变验收ID/金额/截止时间。默认 model_led.mode=staged；三份验收成功记录后调度接入新链路，mode=disabled 可阻止该路由。

取数修复：仅把实际命中目标财务行且覆盖指定财期的页面送给提取模型；显式财务报表列报币种只能用于同份报告、具连续上下文的对应报表。PDF旧页缺失不再一律阻塞全部结论。原始事实缺口及假设保留。30分钟没有第一份有效报告就记录并通知；相同证据不重跑，同根因最多两次修复回放且总预算与截止时间仍生效。

本次工程回归与冻结原文回放不等于三份真实报告验收。生产仍零正式报告且原验收停止；未获有效恢复授权前不得启动付费研究或宣称已上线。真实记录位于 data/fundamental_quant/acceptance/model_led_manifest_20261005.json、statement_currency_scope_replay_20261005.json、target_row_retrieval_replay_20261005.json。


### 2026-10-05 模型主导首轮真实失败修复

新授权验收 `fq-model-led-acceptance-20261005-144935` 保留独立账本，上限40次/4m输入预留/1m输出预留，截止2026-10-06 00:19:35北京时间。首3调用发现取数字段枚举缺失、无关历史中报上下文过大、输出截断及传输失败；尚无有效报告。研究提示词独立版本 `model-led-prompts/2` 明确引用命名空间、字段枚举、金额单位与股权现金流口径。研究上下文保留全部原始冻结数据，发送近期中报、全部年度/原文事实与显式引用依赖，优先近期报表与风险页；标记遗漏且不声称完整阅读。真实缓存回放请求预留由约50万降到17万字节，未改事实或原文。单次输出上限32768、请求等待120秒，本轮总预算和截止不变。worker将incomplete显示为blocked并汇总子公司错误，不能将进程完成当成研究完成。85项定向/validation检查通过，真实验收仍待研究与独立核验。

第二项真实问题：提取器把明确本页单位币种误标为financial_statements，原校验器正确拒绝；提示词1.10明确本页表头scope=null和精确指标/财期配对，保留跨页币种检查。自适应提取不再补取已有原文事实，循环按实际事实变化推进而非coverage元数据变化；每家公司结果写入验收outcomes，失败也刷新HTML。75项提取/研究检查及2项新增上下文/无变化控制回归通过。

本轮最终失败并暂停：16次调用，已知实际输入895878/输出182668，一次连接失败用量未知；输入预留3384534、输出预留475136。0/3有效报告，未上线；剩余615466输入预留不足当前最小完整四角色上下文627392（未含Capex补查与更正），未续费或改期。ROE提案将历史平均净资产比率混入当前净资产估值口径；股东分派重复扣减的建议也不得进入正式结论。提示词/3补充这两项约束，只经过离线检查，尚无真实报告验收。失败JSON及HTML通知回执存于data/fundamental_quant/acceptance/fq-model-led-acceptance-20261005-144935-failed.json；邮件SMTP已接收，飞书/TG发送回执成功，未宣称邮箱内显示已验证。旧账本sha256未变。


### 2026-10-05 ROE口径与真实现金流取数修复

用户要求修复不合理阻塞并让模型主导标的研究。`model-led-calculations/2` 将 `normalized_roe` 固定为代码的正常化盈利/当前归母净资产；保留 `historical_normalized_roe`（同财期、被引用的平均净资产）和不可作为估值输入的 `proposed_normalized_roe`。按每个模型端点实际显示精度解释舍入，记录 `roe_basis_reconciliation`；无法解释的提案只作为未核验诊断，不能通过 `calculation_refs` 支撑发布结论，不覆盖正确代码数值。真实四川路桥原失败包离线重算：历史13.8943%、模型13.9%、当前口径13.8376%，解释为分母和舍入，无计算阻塞。

真正的Capex缺口是检索漏取：四川路桥2025年报PDF154页是合并现金流量表和人民币元/2025年度表头，155页Capex=1,142,930,039.29元；156页母公司值不同。旧初筛priority泄漏导致目标页被挤掉；新`requested-row-coverage/2`按模型请求/财期/合并范围覆盖选页，去掉处置资产收款的宽泛Capex命中。同一真实event6053离线回放由8个错误/冗余页改成147/154/155/8四页，原文及hash不变。增加可选原始少数股东分红/股利利息合计/独立利息/租赁现金行，合计不得伪装单独利息；不新增强制字段门槛。

研究者action=estimate等但同时明确请求决定性缺失证据时执行该补查，保留部分估算；证据已足的可选后续请求不阻塞报告。提示词`model-led-prompts/4`要求评估调整的重要性，允许有证据的区间及敏感性，不能因缺非关键字段自动否决，也不能把未知重大调整当零。独立核验及三份报告发布条件保持。生产付费仍暂停、原预算账本未动；本次工程修复和真实资料离线回放不是新的真实模型验收。

审计产物：`data/fundamental_quant/acceptance/roe_basis_real_replay_20261005.json`、`cashflow_retrieval_replay_600039_20261005.json`、可读页面`cashflow_roe_repair_review.html`。

### 2026-10-05 用户授权重置模型主导验收额度

- 用户明确要求“继续吧 重置额度”，按上一轮上限建立新轮 `fq-model-led-acceptance-20261005-154831`：40 次模型调用、4,000,000 输入预留、1,000,000 输出预留、90 分钟，北京时间 2026-10-05 23:48:31 至 2026-10-06 01:18:31。旧轮原始账本逐条比对不变，哈希与授权写入 acceptance 下对应 authorization.json。
- 继续复用原三家主目标、三家备选及已有真实证据，独立核验与三份有效报告发布门槛不变。修复 30 分钟 checkpoint/修复计数跨轮串用，相关事件明确绑定 campaign_id；保留旧事件。
- 本次针对性与 validation 测试 60 项通过（旧 mock 的 _context 参数签名更新后，失败项单独回归通过）；ruff 通过。真实 worker PID 54392、子任务 PID 29056 已进入四川路桥 research_decide，尚不表示有效报告通过。自动跟进已绑定新轮额度和截止时间并恢复。

### 2026-10-06 本轮真实补查故障修复

- 本轮 `fq-model-led-acceptance-20261005-154831` 在16次调用后主动drain，保留40次/4M输入预留/1M输出预留及01:18:31截止。观察到补查日期提示相互矛盾、真实CFO仅作为表头上下文被拒收、会计政策编号/未付利息被误命中；另一次模型JSON重复键被拦截，未放宽解析器。
- extraction提示词1.11：精确期间请求同步requested_missing_cells和interim periods；请求指纹包含metric+完整日期；上下文页包含所请求数值时显式提升为目标页且仍受页数限制，所有引用单位校验保留。
- retrieval /3排除章节数字、未支付利息误命中，D&A优先补充现金流表并包含续页。model-led提示词/5把最近3次补查发现作为非事实诊断提供研究者，让其基于失败原因改换路线或明确估算/暂缓。
- 真实6179响应中CFO提案使用原6178冻结来源重新通过转换和事实校验，恢复三一2025CFO 19,975,261,000元，原响应/费用不修改，新增extraction_context_repair_replay审计事件；这不是独立审查报告。artifact: acceptance/context_fact_replay_6179_20261006.json。道氏真实6171回放见retrieval_300409_event6171_replay_20261006.json。
- 本轮已知实际输入563343/输出163145，输入预留1766041/输出预留524288。修复不新增模型调用，不重置额度，不延长截止。76项回归含validation通过；最终检索/抽取/币种45项通过，ruff通过。

### 2026-10-06 按角色分配输出预算与真实成本剖析

- 真实已停止轮28调用：13次research_decide输入796172/输出271124；12次extract输入153544/输出16031，但extract累计预留393216。输入预留按UTF-8字节计算，是保守额度单位，不能与实际token混称；输出为累计max_tokens预留，不是实际消耗。
- 新增`llm.extract_max_tokens`默认8192，并受全局max_tokens上限约束；提取请求实际传给服务的max_tokens与账本预留保持一致。分析与独立审查仍用原32768，图像任务仍最多2048。参数加入缓存版本标识。历史预算/收费记录不回退，不恢复已停止验收。
- 按原28调用序列估算，新的提取上限会让输出预留从917504降至622592，减少294912；这只是离线预算对照，不是重新执行后的真实费用或通过率。单次四川路桥四角色尝试实际223147输入/78997输出，但未通过验收，不能称为有效报告成本。详情`data/fundamental_quant/acceptance/budget_profile_20261006.json`。
- 54项角色预算、验收账本和validation测试通过；测试核对了API上限和预留一致、研究审查上限保持及非法配置拦截。没有新增付费调用。三份有效报告未交付的原因仍包括重复研究/补查、源数据口径和审查问题，增加额度不替代修复。

### 2026-10-06 审查后成本汇总异常与本轮停止

- 四川路桥已完成真实 blind/analyze/crosscheck（事件6204/6206/6208），但旧模型失败记录usage.input_tokens=null使 `_usage()` 抛TypeError。现仅累计合法已知整数；部分/全部未知用量单独计数，原事件不改。真实回放不再抛异常；51项工作流及validation测试通过，ruff通过。
- 去掉异常后，原真实审查仍未通过：部分引用是历史序列而非有效最终计算、盈利归属口径冲突、估算端点缺少独立提取事实、同行审查证据不足。诊断保存acceptance/review_typeerror_replay_600039_20261006.json；未发布为投资报告。
- 本轮fq-model-led-acceptance-20261005-154831累计28调用，实际输入1117304/输出343926，输入预留3449770/输出917504；输出剩82496低于当前三角色核验预留98304，且仍需补查。停止付费运行，0/3有效报告、未上线。保持额度/原01:18:31截止和所有旧账本，自动跟进按授权暂停；发送HTML失败通知并保存渠道回执。

### 2026-10-06 缓存前缀与费用口径修复

- 用户指出调用方命中折扣为输入1/10、输出1/40。此为用户提供的调用方计费条件，不能用未加权token减少量当作真实节省，也不能从输入命中推断输出命中。此前仅以总token判断裁剪更省钱缺乏依据。
- 发现sort_keys=True把变化context排在packet前，packet又把变化日期/coverage排在sources前；运输层_usage只保留prompt/completion总量而丢掉缓存细分。新增request_layout，稳定证据置前、任务上下文置后，保留完整原字段/数值及来源顺序。blind审查仍不接收分析者输出。提示词1.12/model-led-prompts6、transport4。
- 若提供方返回明确的prompt_cache_hit/miss、completion_cache_hit/miss、cache_read/create_input、tokens_details.cached_tokens，原名存入provider_cache_usage并随model_response/model_error事件留存；缺失仍为未知，不伪造命中率或追溯费用。
- 14对真实请求离线重排语义一致，用户消息平均相同前缀从4545字符到29371字符；不宣称真实cache命中或费用已下降。artifact acceptance/cache_prefix_replay_20261006.json。162项缓存/提示词/transport/validation测试通过；ruff通过。无新付费调用，旧账本和暂停状态保持。
- extract_max_tokens8192仅为有界JSON输出上限，不是财报输入或单公司总额度。现有token预留仍是保守执行边界，不是人民币费用；后续预算应另行按调用方明确回执和单价估算实际成本，不能在未授权时折算释放旧轮预留。

### 2026-10-06 原图研究会话恢复与压缩边界修复（停止轮离线回放）

- 原验收 `fq-pdf-acceptance-20261006-112042` 已于20:50:42到期。本次按用户“再调一下修好”修复代码，不新建验收、不重置额度、不恢复付费或通知自动化。历史账本与会话保持原样。
- PDF请求身份新增实际角色、输出上限、thinking、temperature等有效执行参数；历史缓存须核对实际调用参数。8192截断后调到32768可成为不同请求，完全相同失败仍去重；修改参数不释放已用预留或绕过截止。复用transport同步检查temperature和timeout。默认压缩输出提高到32768，示例配置单独列出研究/审查65536、压缩32768、近期记录保留8192。
- 压缩规划区分研究输出与压缩输出，调用前重新测量带真实指令、必要引用的完整请求，检查token、图数、图像字节和请求体积。真实失败6353/6354原508图不裁剪，输入估算878846，加32768输出后911614，容量可容纳；这不是模型生成合格检查点的证明。
- 行政暂停在同一有效授权恢复后可继续原检查点，不占技术修复次数；预算过期仍拦截。`open()`返回之前发生异常也会读取已持久化会话并写入可见阻塞，附原错误。代码、有效模型/PDF参数变化纳入恢复版本。
- 协议按研究/审查角色在真正调用前分别更新。已支付返回的审查结果优先消费，不再先升级两个上下文、触发无关压缩。审查反馈、后续阶段、纠正次数同一检查点提交，重启不重复追加反馈。
- 每次研究响应（含同时请求工具的响应）先持久化不可变记录，再执行工具；工具完成与待送回上下文另有检查点，压缩失败后恢复不会重复工具操作。重复原样提交已更正记录是幂等操作，不再次覆盖旧事实；旧事实及更正原因保留。区间引用`.low/.base/.high`纳入审查依赖，估算变化不能沿用旧结论审查。
- 新增`python -X utf8 scripts/replay_pdf_session_repairs.py --database data/fundamental_quant/research.sqlite3 --campaign fq-pdf-acceptance-20261006-112042 --tokenizer data/fundamental_quant/tokenizer/tokenizer.json --output data/fundamental_quant/acceptance/pdf_session_repair_replay_20261006`。生产库只读，在隔离临时库回放：四川路桥真实事件6288的38条事实、8个工具请求完整保存，7项算术预览成功；三一4项阻塞审查及3项后续工具保留，立即交回纠正，不形成通过结论。模型调用、上传、通知均为0；全部历史验收账本摘要前后一致。
- 产物：`data/fundamental_quant/acceptance/pdf_session_repair_replay_20261006.json`及HTML。离线回放、模拟故障/重启回归不计入三份真实有效报告；真实压缩、模型纠正及再次独立审查仍需后续有效授权运行验证。
- 最后复审补齐发布边界：独立审查开始前冻结`review_input_evidence_hash`，结果沿用该哈希；审查途中或发布前的新资料不能被旧结论“借用”。发布冲突记录为可见阻塞，下一次有效授权恢复先刷新资料并重走审查。发布不再把旧packet写回覆盖并发更新。
- 已核验报告先保存`publish_pending`，幂等通知入队后才`complete`；中间退出可恢复同一版本，不重复推送。已保存报告的投递恢复不调用模型，预算到期仍可完成已有报告通知；真实付费研究仍受原硬限制。恢复失败在运行结果单列`delivery_errors`，不只落日志。
- 最终回归：PDF会话、请求去重、压缩边界、审查、验收预算及`tests/validation/`合计199项通过；最终投递错误可见性调整后，4项发布/冻结/暂停回归再次通过。Ruff通过。worker实际主/子进程均停止、自动跟进仍PAUSED；本次0次付费调用，不宣称三份真实报告验收通过。

### 2026-10-06 已支付研究输出可见性修复

- 用户指出没有最终报告不应意味着已有数字和分析全部不可见。新增 `session_notebook.py`，只读历史会话和事件，输出 `data/fundamental_quant/research_notes/index.html` 及三家公司页面。顶部呈现数值、单位、财期、原文链接、实质研究记录和六项判断，保留逐轮版本、估算、计算提案、已有算术回执及独立审查争议；不将草稿改写为已核验报告或候选。
- `reporting.py` 在全池首页及公司页加入已有研究入口；`session_workflow.py` 在响应持久化后立即更新研究笔记，包括同时请求工具的响应。展示不再等待整份报告审查通过。导出移除私有推理、请求提示词及凭据，来源链接仅使用库中真实登记的文件。
- 可用 `python -X utf8 scripts/render_pdf_research_notes.py --root data/fundamental_quant` 从现有数据重建页面，不初始化模型服务、不新增取数、不修改历史账本。原全池报告快照保持，草稿不进入 Bot 的有效候选数据源。
- 停止轮真实成本：研究8次、审查24次、压缩2次；审查输入9091299 token，占总输入11760142的主要部分，其中21轮仍请求工具。历史研究响应含194条事实摘录、66项计算提案、11项估算，均含重复版本，不等于独立已核验事实数。总输出468803、输入缓存命中8352512、未命中3407630，不能将总token直接当作人民币费用。
- 三份年报原页人工核对2025营收／归母净利／经营现金流（亿元）：三一重工892.31／84.08／199.75，四川路桥1151.11／72.97／77.23，道氏技术81.56／5.07／10.74。基础披露值核对不等于正常化估值或完整研究通过。
- 本次无新增模型调用，生产库验收账本、会话及6368条事件摘要前后完全一致。静态检查4份HTML及7878个本地链接，无缺失；浏览器拒绝file URL，未绕过策略或宣称完成浏览器视觉验收。检查回执：`acceptance/research_visibility_verified_20261006.json`。付费运行及自动跟进保持暂停。

### 2026-10-06 研究重点与管理层讨论遗漏修复

- 复查真实输出：三家公司首批均覆盖管理层讨论，但旧提示词主要规定财务字段和估值合同，没有要求把变化、管理层归因、反证与持续性关联起来。首页仅突出最后一轮，又让后续报表和纠错记录掩盖早期经营讨论。不能把增加数值展示当作研究质量验收。
- `session_baseline.py` 向新会话两个角色提供已有结构化财务背景，保留原值、单位、来源、核验标记，限最近七个可用财年；排除未来日期、身份/来源日期冲突。不把数据商来源登记成已读PDF，不改变最终证据门槛。实际缓存可复用三一115、路桥112、道氏172条背景记录，不代表全部已核验。已停止会话和账本未改，不自动恢复。
- 提示词升级 `pdf-protocol/5`：研究记录优先解释经营变化，区分管理层原文与研究判断，连接分部、收付时点、并表变化、周期和一次性因素；缺量价贡献时不硬凑比例。审查同轮检查这些解释，压缩保留解释及反证，不能只留数字。无新增硬性字段门槛或独立模型调用序列。
- 研究笔记改为按批次呈现记录，基础数字折叠为参考。新增缓存原文离线补读页面 `research_notes/management_review.html` 与JSON，六个例子逐项写出变化、原话、判断、限制和后续区分性核查。例：路桥现金流增长解释为支付减少；费用下降涉及子公司出表；三一CFO受金融净放款减少影响；道氏资源分部统计口径改变。摘录均在对应缓存页通过去空白文字匹配，本次补读不能追认旧模型已完成这些分析，也未经过独立审查，不计正式报告。
- 本次新增DeepSeek请求0，生产验收账本、会话及事件摘要仍与原6368事件快照一致。100项相关回归和validation通过，新增启动会话集成验证另1项通过，Ruff通过。只验证代码和离线展示；真实成本下降和研究质量改善尚未验证。

### 2026-10-06 GPT-6 Luna 手动研究/审查闭环

- 按用户明确要求，使用两个 `gpt-6-luna` 子代理分别研究和独立上下文审查，冻结 protocol/5 提示词、本地财务背景及三份原PDF清单。仅四川路桥，两轮研究、两轮审查，附定向序列化/引用合同修复；无DeepSeek调用、下载、worker、通知或生产库写入。Codex子代理存在实际用量，但工具未提供可核对token账单，标未知，不标0。
- 第一轮阅读2023—2025年管理层讨论，原图核对关键页面；第二轮只补客户集中度和现金流补充表（2024 p335—336、2025 p284—285、p18）。研究者与审查者分别计算：CFO同比增加42.9527891224亿元，存货/经营性应收/经营性应付的同比调节贡献分别为12.6555993603、-61.7857238301、99.1963252661亿元，三项合计50.0662007963亿元；其他调节项净额-7.1134116739亿元是本次未拆解，并不代表原报表缺少这些行。未推断拖欠、坏账或正常化现金流。
- 实际输出暴露并修复 `session_review.py` 类型错误：显式定性文本不应强制币种/金额单位；ratio仍要求单位，但不强制币种。金额必须币种、定性不得用于算术、公司/期间/引用/数值类型检查仍有效。真实首轮两条定性事实和第二轮三条比例事实原样回放由误拦变为通过，不修改财务值。
- 审查增量遗漏旧依据时，校验错误现直接返回 `missing_page_refs` 和可读缺页明细，不再仅返回结论key。审查者对实际遗漏原页定向核查后提交 `review2_repaired.json`；旧审查保留。提示词protocol/6说明定性/ratio币种规则及完整结论应保留此前已核查依赖。
- 手动序列化脚本曾错误对整份JSON展开页码范围，破坏ISO日期与普通文本；保留损坏 `round1.json`，字段级恢复为 `round1_repaired.json`，事实值和结论不变。该问题属于本次手动工具使用，不能冒称DeepSeek模型故障。后续使用结构化引用，不对序列化全文作正则替换。
- 产物：`data/fundamental_quant/manual_luna_20261006/index.html`，含研究变化、原文页、独立审查、代码重算及范围限制。最终现有校验结果 `status=stage`、invalid_records=[]、blocking=[]、report_ready=false；六项中正常化盈利/FCF/资本回报/隐含回报及同行仍未建立，不能计入三份正式报告或宣布付费链路上线。手动使用全文定位加原页视觉核对，不是50页图片运输、自动调度或压缩验收。
- 159项相关回归与validation通过，Ruff通过；3份源PDF哈希匹配，生产验收/会话及6368事件摘要未变，报告107个本地链接检查通过。所有历史账本及暂停状态保留。


### 2026-10-07 DeepSeek 全文比较试运行及离线纠错

- 用户明确授权新轮60次调用、2000万输入预留、200万输出预留、90分钟。新账本 fq-full-deepseek-20261007-004910，2026-10-07 08:49:10—10:19:10 Asia/Shanghai，四个旧轮账本前后逐行一致；未恢复全池worker或发布候选。
- 新目标贵州茅台，同业五粮液、泸州老窖；各2023—2025年报及2026中报，共12份1814页。按公司持续读取完整文字，关键24页原图另行抽查。所有原始请求、返回、用量和阶段稿保存在 deepseek_comparison_20261007；这不是原50页图像协议的上线验收。
- comparison_transport.py 在解析前原子保存完整私有响应，使用持久化账本预留和绝对截止时间；原始reasoning不进入展示。首个12000输出上限被推理占用导致正文截断，保留原调用并在同账本显式改为32768后重放一次。其余阅读及三公司审查完成；汇总JSON仅末尾数组缺少]。新增受限单字符语法修复及真实原响应离线回放，保留blocked审查判断，不将语法修复当作研究通过。
- 本轮真实18次调用，input4514609、output246412，输入缓存命中1989376、未命中2525233；预留input4870962、output569056，无未知用量。到期后离线修正未增加付费请求。
- 修正明确的财期/币种/页码/千分位别名、表格折行引用标记、重复事实冲突检查。利息费用不当作现金利息，股利利润利息合计不当作普通股分红，元计价股本不自动当股数；原始事实与分类修正分开留痕。
- 独立审查发现实质串公司存货与不可复算正常化现金区间，也出现把自身上下文未含其他公司原文误判为全池缺失的错误。审查提示词增加明确范围约束；原负面审查完整保留。离线报告修正已核对错数，增加代码历史现金桥，撤回无依据正常化区间和总回报预测。
- 交付index.html和full_report.txt完整纠错稿，约1.2万字；正式验收仍未通过，纠错版未重新独立模型审查，不能算作三份有效报告或Bot候选。60项解析/预算/计算/审计和tests/validation回归通过；代码测试不替代报告验收。

### 2026-10-08：独立离线财报研究资料包

新增 `scripts/build_fr_packs.py` 的 build/status/check/render 入口，复用冻结财报及原PDF，输出43项主表字段、完整MD&A、七类政策/附注和补充资料关联。实现版本化长期缓存、同目录写锁、来源/缓存双校验、差异可见；FCF仅代码计算基础代理，不生成研究结论或续开付费轮。

真实年报/中报回放与独立检查修复了空白行借用下一行金额、现金流附注内部编号截断、期初/期间比较日期混淆、旧年报ASCII标题、括号/破折号负数、缓存原文未绑定及单中报缺年报依赖。新增资料包测试与 `tests/validation/` 合计60项通过。资料包默认不宣称会计语义全覆盖；列报币种及未定位细项明确保留。

使用和验收边界见 `docs/development/research_retirement_20261024.md`（原 `fr_material_packs.md` 已合并删除）；真实可读产物在 `data/fundamental_quant/fr_packs_20261008/index.html`。新增付费API调用0，未更改既有报告和费用账本，未提交或推送。


### 2026-10-09 单份 DS 提取试跑

新增 `scripts/run_fr_extraction_pilot.py`，仅用于显式授权的一份FR试跑：完整冻结文本、43项字段及7类会计合同；2次调用/40万输入预留/131072输出预留/30分钟/2元预算。复用独立费用账本与固定调用ID，默认禁用思考，无自动重试。原始响应、核对问题及HTML独立保留，不覆盖离线资料包。本次泸州老窖2023年报首调用返回120488输入/12556输出，37项数值在原PDF引用页字面核查命中，其中36项与本地一致，1项归母净利由DS补出，但分类和引用仍有缺口；唯一更正请求传输失败、usage未知，已收尾停止，不能记零费用或宣称资料合同全部通过。结果见 `data/fundamental_quant/ds_extraction_pilot_20261009/index.html`。


### 2026-10-10 资料缺口与基础FCF展示

DS试跑页增加离线计算 `derived_cashflow.json`：按同财期、同币种合并口径CFO减购建长期资产支付现金，单位转换使用Decimal。公司未披露FCF不成为资料包阻塞；缺少计算输入不填零，计算不改变原始事实及审查状态。其他非决定性缺口保留，后续研究可按影响通过harness搜索补证。本次只更新展示与计算，未执行搜索、未调用付费API、未修改历史账本。


### 2026-10-10 资料完整性双门槛

新增 `config/fr_pack_gate.json`、`src/fundamental_quant/fr_pack_gate.py`。普通企业默认六项关键财务字段全部取得，43项固定财务字段有效填充率达到85%，恰好两个gate；MD&A、附注缺口和公司披露FCF不另加完整性门槛。确认错误、重复字段、错期、无效数字不计填充，非关键不适用/未披露仅在独立确认的排除列表且原文依据存在时移出分母；尚未定位仍计缺失。推导FCF不额外充数。HTML及material_gate.json是派生验收视图，旧pack/source及账本保持不变。DS示例6/6、37/43=86.05%通过资料完整性；不替代会计审查或投资发布。金融及REIT需适用专属合同，不能据此普通企业配置宣称通过。


### 2026-10-10 第二阶段：跨年同行事实比较

新增 `peer_panel.py`、`peer_panel_render.py` 和 `scripts/build_peer_panel.py`，消费版本化FR资料包建立带原文引用的公司/财期/指标面板，计算实际年度区间CAGR、合并CFO/合并净利、基础FCF、平均归母权益简单ROE及BVPS。年报与半年报分组，期初日期保持原标签；同指标披露冲突留空，不静默选新值。原始数不被推导覆盖。

修复财务表阿拉伯数字行号残留导致归母净利润全体漏取，parser升到fr-pack/10，新缓存保留旧版本。从已有12份酒企报告与2023年报比较摘要构建2021—2025年度、2025H1/2026H1共21公司期记录；2021、2025产品吨成本/股数及管理层原文保存为独立source-review附件，未调用网络/模型API。九组产品吨成本使用匹配销量和收入/成本范围；财务公司CFO、产品组合及列报币种缺口显示限制。整包双门槛未通过的资料不提升为正式投资报告。

示例输出 `data/fundamental_quant/longitudinal_comparison_20261010/index.html`，含4张趋势图、BVPS、现金对比、9组吨成本、完整证据页、panel.json和独立review.json。摘要计算值绑定panel，CAGR格显示实际区间；方向性解释冻结到editorial_basis.json，证据变化须复核，不沿用旧解释覆盖新数据。命令：`python -X utf8 scripts/build_peer_panel.py --packs data/fundamental_quant/fr_packs_20261008 --output data/fundamental_quant/longitudinal_comparison_20261010`。


## 2026-10-10 长期同行自动研究（有界 DeepSeek 接口，真实付费验收待授权）

新增 `scripts/run_longitudinal_research.py`，入口为 prepare/estimate/run/resume/status/render，配置为 `config/longitudinal_research.json`。先白酒三家，再工程建设四家；配置承载公司组，生成器不写死任何公司结论。旧报告、账本、取数缓存不被重置，prepared页面不能当研究验收。

数据模块 longitudinal_data 读取不可变 FR pack、原始 PDF 与通过原页哈希/引文核对的旧提取结果。保存七年请求窗口和实际覆盖，计算实际增长区间、滚动趋势、累计合并 CFO/合并利润、BVPS、行业敞口与原文成本锚。历史估值只匹配披露日严格早于交易日的版本、未复权价格和历史股数；缺失则留空，不能用现股数倒算。原文目录、金额、管理层陈述与模型估算分别保留。

longitudinal_workflow 使用持续研究会话及独立审查上下文，JSON动作实现完整章节/原页/图像读取、原文查找、范围内财报及历史行情补查、声明式计算和证据估算。最终报告包含三个判断维度和六项结论。模型可提交阶段报告及局部修正；确定性引用、冲突、币种和单位检查不能被审查同意覆盖。同根因最多两次修复，重复工具失败停在可见检查点。完整历史持久化，压缩失败不截断旧上下文。

ComparisonTransport 复用原有明确账本和请求回执，增加可配置上下文/输出容量和图像token上界计量；base64字节仍计入请求体积，不当作文字token。新运行器研究单次输出预留32768、审查16384、压缩8192，保留至少一次审查预算。长请求读超时600秒仍受原截止时间约束；用量未知不按零计且不自动重发。费用按组和轮次分开，rate profile带来源与核对日期，不把费率估算称为账单。

run/resume 必须提供包含固定ID、起止时间、调用/input/output上限、模型能力来源、approval_reference及所有packet_hashes的授权JSON。授权绑定原账本/目录/模型/证据，重启不重置；prepare/estimate/status/render不创建付费授权。旧轮不可借新入口复活。

longitudinal_release 在同一个research.sqlite3内使用独立同行报告/通知表，不冒充旧投资候选。发布重新核验最终结果、审查覆盖、来源和版本。HTML、Bot、HTML邮件读取同一冻结结果；邮件排除巨量原文附件但不重新生成结论。撤回后有效查询消失、直接发布页显示撤回、已确认通知发一次更正；不自动重试未知发送结果。

运行示例（不付费）：
```powershell
python -X utf8 scripts/run_longitudinal_research.py prepare --as-of 2026-10-10
python -X utf8 scripts/run_longitudinal_research.py estimate
python -X utf8 scripts/run_longitudinal_research.py status
```

当前真实离线输入为白酒12份/21公司财期、工程17份（含一份额外2025H1）。首轮未发起新的DeepSeek请求或通知，不能宣称两份API研究报告已验收。历史行情/股数缓存存在实际缺口，页面列明；付费真实运行前需明确新的有效预算。


### 2026-10-10 工程组真实数据回放修复（不等于付费研究验收）

工程比较的稀疏页实际包含解析漏取：附注编号残留、整数/换行金额、合并与母公司表边界、报告标题误认列顺序、现金支付符号及重述版本处理。`fr-pack/13` 逐列核对付款约定后才转换支出方向，保留原值和证据；负数退款不盲目取绝对值。`longitudinal-data/4` 只在明确重述证据齐备时选择回顾比较数，旧披露仍保留，PIT不回填；列报币种与记账本位币不混用。

新增 `share_count_sources.py` 提取真实期末股数，禁止把人民币股本金当股数；`fr_image_evidence.py` 复用经原图核验的扫描表事实，重新校验原PDF、图片像素及复核回执，54条图像观察与普通文本提取分开标记。图像复核不是自动提取或独立模型审查的替代。新增公开来源2020/2021年报8份，合计25份财报、1886条事实、37个公司期；四家2019—2025年度七核心字段196/196取得。其他字段、早期股数和币种仍有缺口；中交2020合同资产冲突仍留空，不把核心齐备称为全部资料完整。

页面增加同年关键值对比、逐年7项覆盖、按公司年度的完整MD&A展开入口、原图证据、毛利率和资本效率图；累计现金转化按连续年度且币种一致的子区间分别显示。最终146项验证通过，重复prepare复用缓存。验收凭据见construction/repair_receipt.json；本轮付费调用0，未形成或发布DeepSeek最终研究结论，历史估值缺口未被掩盖。

扩展七年后离线计量发现初始请求重复携带审计原文超过上下文容量；research/2将重述、币种及符号证明置为可回查索引，原packet和read_fact完整返回不变，独立审查亦可读取external image证据。工程初始输入从1,051,614降为785,849 token，已预留32,768输出且满足1M上下文和5%计量余量；并非声称后续轮次无需压缩。此改动后42项调用器/工作流/展示定向回归通过，未启动付费运行。

### 2026-10-10 数据链路真实故障审计

最新生产优化邮件的港股09999失败已对照日志和data_readiness确认：短历史补上市证据错误调用A股专用路由；A股/美股实际搜参完成但未满足提升条件。日报另有失败扫描返回空alerts而被写成无信号的状态传播问题。故障与修复边界详见 `docs/development/data_chain_audit_20261010.md`。

长期比较prepare新增统一原件库离线发现与独立原报告覆盖清单，HTML明确显示工程组原件24/32；2019/2022比较数不代表相应完整MD&A。财报来源暂时错误新增持久化有界退避/租期，原工具失败保留重试合同，不恢复付费额度。自动缺口下载、增量重算及研究checkpoint恢复仍未接通，不宣称整体自动化已验收。

港股最小修复新增HKEX上市年鉴证据，接通disclosed provider内层与backtest外层。真实09999隔离回放48.3秒取得上市证据后，仍被26处公司行动因子缺现金/股数解释阻断（readiness_validation.json）；不能宣称恢复。日报/简报逐市场状态传播已修，实际TradePlan覆盖不足不再包装为正常无信号。相关离线回归通过，生产未部署，DeepSeek新增调用0。


## 2026-10-10 数据链路修复与限定部署

长期同行研究新增有界公开资料 `acquire/refresh` 入口，复用来源缓存、持久化尝试次数、退避时间与截止；同一采集 ID 重启不增加额度。原件覆盖与财务数字覆盖分开展示。下载成功后进行确定性解析和面板重算；已有研究检查点保留原版本，新证据须经对应版本审查，不能沿用旧结论发布。

研究工具遇到可重试来源等待时，保存 pending_tools 并停止模型续轮；恢复前检查 retry_at 与授权期限。增量事实、章节、计算和证据版本进入同一 effective_packet，HTML、Bot 与通知使用同一核验版本。旧付费验收账本不重置，本次未授权新付费调用。

技术面数据新增可追溯港股上市日、美元股息及历史汇率证据合同；原币种金额与港元估值换算分存，拒绝把美元直接当港元现金。来源封锁持久化冷却。旧港股策略产物缺少新币种合同则要求重新优化；A股/美股原生币种产物保持兼容。日报逐市场区分完成无信号、失败、阻塞、未运行与不适用，并核对实际扫描标的覆盖。

部署采用明确文件清单、生产基线哈希检查、原文件备份、验证失败回滚；保留生产未提交热修复、配置、账本和运行记录，不使用整个脏工作区覆盖。真实外部来源仍可不可用，数据/投资验收与代码部署验收分别记录于 `docs/development/data_chain_audit_20261010.md`。


## 2026-10-10 固定汇率口径更正

此前引入HKMA历史汇率作为港股美元股息换算前置条件不符合既有设置，已撤除此依赖。当前统一读取港股所选execution profile的固定汇率：USD/CNY=7.0、HKD/CNY=0.9，USD股息先按7.0/0.9折算港币，再由组合按0.9计人民币。原始声明币种、金额及固定配置快照分别保留；缓存换算从原值重建现金和复权因子，读取不覆盖旧证据。配置非法或缺失会明确失败，无外部汇率请求。

本地162项相关回归通过，网易26笔官方USD股息离线重放26/26通过、0外部请求、0付费模型调用。新执行身份包含完整固定FX配置；已知旧合同A/US原生现金兼容，旧港股产物缺少运行时币种证据仍需重新计算，不能用当前配置倒推旧结果正确。此项修正不表示全池行情或旧策略验收通过。生产部署回执位于data/deployment_audit/fixed_fx_20261010/。


## 2026-10-11 来源身份缓存与跨市场恢复

真实故障：港股组获取VOO时官方HTTP200页面缺少基金元数据，后续美股组获取成功，但原调度没有恢复港股组。新增官方基金身份366天缓存，绑定symbol、fundId、官方URL、完整响应SHA及有效期；复用时重验原文与身份，配置覆盖优先。临时缺元数据使用明确可重试异常，沿用数据准备最多2次与既有市场时间预算；403等来源拒绝不伪装成页面缺失。

每轮市场任务结束后，仅对no_data且全部原请求缓存已通过原截止日期校验的市场执行一次恢复。原失败记录保留，独占创建shared_data_recovery.json记录已消耗尝试与新run关联，重启不重置；已完成市场不重跑。恢复结果替代通知中的有效状态，异常仍明确失败，不把当前缓存就绪当成优化完成。

先测旧实现：调度2项失败；真实Vanguard响应等新测试9项失败。修复后组合144项通过（使用项目data/source_validation/hkex_listing/deps下已准备的exchange_calendars；默认Python缺该依赖的6项失败单独记录为环境问题）。生产/干净提交树及完整港股组验收另外存于data/deployment_audit/recovery_20261011/。测试覆盖来源与跨阶段恢复，不等价于全链路永久可靠。无付费模型调用，未更改策略阈值、股票池或激活条件。

交付一致性补充：干净HEAD回归另外发现15项失败，来自此前未纳入提交的no_data状态与通知热修复依赖。本次仅把其必要生产代码块和已有回归测试补入版本控制；未夹带无关自动激活变更。补齐后同一144项测试在干净提交树全部通过。


### 2026-10-11 基本面长期比较链路盘点（未启动付费研究）

入口：`python -X utf8 scripts/audit_fundamental_chain.py --root data/fundamental_quant/chain_audit_20261011`。实际盘点配置两组7家公司，并单列旧全池账本与运行状态；不得将旧6001公司/512 packet视作新长期比较覆盖。

- 有界公开采集`fq-public-chain-20261011`，12次文档请求全部成功，白酒补齐2019–2022各公司原年报，24/24要求槽位；工程32/32槽位、33份原件。57份PDF哈希全部核验。无付费模型调用。
- 修复MD&A旧标题和目录误识别，工程实质正文索引22/33→33/33；两组合计57/57均有实质正文入口。解析版本fr-pack/15。
- 修复旧版本资料包在prepare中优先复用，已有原文重算但不覆盖旧包；资料门槛算法/政策加入准备输入指纹，负数括号空格及页间拼数问题纳入fr-material-validation/2。
- 修复单公司坏估值缓存拖垮全组、缓存代码/公司标识校验、工具与prepare缓存根不一致。默认根明确为data/point_in_time；只有价格不足以估值时返回partial。未制造历史股份数、未用复权价或后年重述回填。
- 修复旧名录错误冒用今日快照日期，展示最后检查时间；不改变付费暂停或通知发送策略。
- 完整联合回归223 passed，含validation；两组再次prepare均reused=true，0新增下载/模型调用。

仍未完成：两组历史估值均0行（价格、PIT财务及历史股份数缺口）；中国交建3份近期扫描表文本关键项缺失，既有图像补证另列，尚非完整自动资料包；工程37处、白酒10处财务版本差异保留待核验；不少可选财务行仍未定位或解析不完整，原85%填充率门槛不下调。白酒资料门槛1/24、工程26/33通过，关键项齐备分别24/24、30/33。

新longitudinal命令尚未接每日自动运行；本地每日任务仍在旧PDF链路因到期预算停止，生产没有基本面配置/账本。两组无研究checkpoint或正式发布。当前修复与重算为本地验证，未部署新基本面调度、未恢复旧付费轮；不能宣称完整研究链路已上线。盘点页面`data/fundamental_quant/chain_audit_20261011/chain_audit.html`、逐项JSON与runtime_audit.json。

填充率追加只读定位：白酒310项parse_failed中261无金额、41单金额、8主表单金额但另一未验证表有双金额。单金额存在本期/比较期两种真实布局，下一步必须引入PDF列坐标分别提取，不得把首数强当本期或把空白记0。本轮未声称该项已修。


### 2026-10-11 Fundamental material pipeline repair

- Added a data-only daily route (`--fundamental-materials`,
  `fundamental_quant.materials.enabled`) to the existing supervised FQ worker.
  It uses the configured peer groups, public document ledger, isolated historical
  market cache and offline preparation; it never constructs a paid research
  service or renews expired acceptance authorizations. Source failures are
  persisted by stage/group; other groups continue. Same-day restart retains
  document reservations and the historical-fetch receipt.
- Local deployment switches only the FQ daily job. Materials prepared is distinct
  from independently accepted research. Public queries, material gate coverage,
  remaining evidence gaps and research status remain separate.
- Sparse financial rows now require PDF coordinate evidence to determine current
  versus comparative column. Blank cells remain missing; old packs rebuild under
  parser version 16. Historical PE/PB from the public provider are separately
  labelled and do not certify as-known disclosure vintage, historical shares,
  market capitalization or FCF yield.
- A-share price acquisition uses the existing shared Baostock access control in
  bounded child processes and immutable interval caches. Verified disclosure
  observations supply versioned historical financial events without backdating
  later restatements. No DeepSeek calls were made for this repair.

- Final local acceptance: 299 relevant and validation tests passed; FQ worker PID
  55940 completed actual daily data-only run `f11e452c475846bda40562c2c3f5ad02`
  with exit 0, fresh heartbeat, `materials_status=prepared`,
  `research_status=not_run`, paid calls 0. Output:
  `data/fundamental_quant/materials/2026-10-11/chain_audit.html` and
  `repair_acceptance.json`. All 57 PDF hashes and parser versions verified.
  Engineering gate improved 26/33 to 30/33; liquor remains 1/24 at the unchanged
  85% fill gate, while its critical fields cover 24/24.
- Real public history: 7 issuers x 1,882 raw daily rows, 658 monthly vendor
  observations. A second incremental run exposed empty Sunday responses (7
  queries); refresh version 2 repairs this entirely offline after verifying
  no exchange session. The failed attempt remains recorded, final replay made
  zero requests, and both prepared groups reused caches. Strict reconstructed
  PE/PB/FCF remain unavailable without historical shares; this is explicit.
- Independent checkpoint review additionally caught multi-group prepared.json
  overwrite and stale success after initialization failure. Both are fixed with
  regression tests; initialization failure preserves original receipts and a
  visible failed page. This is local data-pipeline deployment, not production
  server FQ activation or a new paid research acceptance.


### 2026-10-12 Fundamental materials continuation (offline)

- Three CCCC scanned financial statements now have hash-bound original-page
  evidence: 21 reviewed pages, 263 current/comparative observations, 36 arithmetic
  checks and 62 same-period cross-report checks. The joint material gate consumes
  verified original images while retaining the original text-only gate. This is
  not automatic OCR or investment-review acceptance.
- Corrected the previously transcribed 2023 restated parent profit from
  23,816,623,046 to 23,816,263,046 yuan. Four affected local material versions were
  archived and labelled with source_corrections.json; no longitudinal published
  research version was found. Original evidence and old values remain preserved.
- Parser 18 fixes sparse current/comparative columns, wrapped labels and dates,
  finance-company operating interest versus finance-cost interest, and CCCC
  investment-loss/interest labels. Explicit non-applicability only excludes an
  optional field when its entire consolidated amount note says none; blank cells
  never become zero. CCCC 2023 annual replay obtains 42/43 fields at the unchanged
  85% gate.
- Disclosed closing issued-share counts are available for all 57 source reports.
  Rounded counts retain precision and BVPS intervals. A full-period unchanged
  statement or verified zero-movement table permits bounded retrospective share
  intervals; these never become facts known before their disclosure. A+H total
  shares are not multiplied by an A-only quote. Strict PIT and retrospective
  valuations render separately.
- New PDFium re-extraction of stale packs cannot discard previously reviewed
  pdfplumber-layout citations: the alternative extraction is reproduced from the
  hash-verified original PDF and still requires literal quote/value matching.
  Receipt bindings additionally protect reviewed field identity and period.
- Five Wuliangye reports explicitly disclose reporting currency in the accounting
  policy but used a previously unrecognized sentence. The exact policy wording
  now links to its original page. Functional currency alone is not silently
  relabelled as directly disclosed presentation currency.
- Related source/parser/receipt versions invalidate only affected preparation
  caches. DeepSeek remains paused; daily material maintenance is local and free.
  Final counters, worker receipt and test results are recorded in
  data/fundamental_quant/materials/2026-10-12/repair_acceptance.json.

- Final replay: 369 tests passed, changed-file Ruff passed. Local worker PID
  41580 / child 35900 completed daily job f4a8012825284807ab7298c9d05668d5
  at 23:27 Asia/Shanghai, exit 0 and no run errors. All 57 source hashes and
  parser-18 versions verified; construction joint gate 33/33 (text 30/33),
  liquor 3/24, critical fields and closing-share sources 57/57. Repeat prepare
  reused both packets. No new public requests or DeepSeek calls in this repair;
  the day's seven earlier public history queries remain in the original ledger.
- Separate retrospective valuation coverage now includes 164 PE, 143 PB and
  140 basic FCF-yield monthly points. Strict PIT remains zero; 21 panel rows lack
  explicit presentation-currency evidence and 46 metric-period differences remain
  visible conflicts. These are not silently discarded or relabelled as failures
  of the companies. The receipt records these acceptance limits explicitly.


### 2026-10-13 Source-backed data-chain completion (no paid research)

- Reporting currency now accepts scoped glossary/statement declarations. Where
  no literal declaration exists, CAS19 inference requires the same report's CAS
  compliance statement, yuan statement-unit header, CNY functional currency and
  the hash-verified original Ministry of Finance rule. Reports label this as
  inference; conflicting direct declarations still block it, and no FX rate is
  fetched or changed.
- Comparative version differences are selected only after original adjustment
  columns, exact item bridges, or actual dated common-control consolidation
  evidence is checked. Raw observations and disclosure dates are unchanged;
  retrospective selection never mutates the PIT financial stream.
- Source-limited financial blanks have independent extraction-health evidence:
  original PDF coordinates, complete located notes, or source-bound reviewed
  receipts. Numeric eligibility retains the configured threshold. Blank,
  comparative-only and gross-versus-net disclosures never become invented zeros.
  Reviewed image evidence closes only its specific fields, retaining text health.
- Dated public share-change histories are cached with original HTML, hashes and
  daily request reservations. Already-disclosed report anchors can establish
  historical issued-share intervals; rounded counts preserve valuation ranges.
  Continuous conversion and A/H valuation gaps remain explicit. Sources are
  reconstructed dated vendor records, not falsely advertised as archived feeds.
- Preparation/cache identities include accounting rules, parser/review methods,
  disclosure receipts and share-history content. The local worker now reports
  material readiness separately from successful file generation and research
  review. Expired paid campaigns remain stopped; this repair does not authorize
  DeepSeek requests, publication or production-server activation.

- Final integration also corrected Baostock `netProfit` scope at acquisition and
  legacy-cache read boundaries: consolidated profit is retained as
  `net_income_total`, never parent profit or inferred parent equity. Original
  cache bytes remain unchanged. The two real groups' 12 same-vintage PE conflicts
  disappear on source-specific replay; unknown-provider conflicts still block.
- Public history refresh v3 uses the real exchange calendar and configured
  15:55 cutoff. Midnight requests resolve to the prior completed session; a
  successful target can advance later that day without erasing previous request
  totals. Failed/unknown requests are not automatically replayed. The actual local
  worker interpreter now has the already-declared exchange-calendars 4.13.2.
- Final combined regression: 561 passed, including tests/validation, real PDF
  failure fixtures, per-metric disclosure vintages, source scope, midnight and
  restart/request-ledger boundaries. The focused FQ modules pass Ruff; pre-existing
  models/point_in_time style findings are not presented as a clean whole-repo lint.
- The 2026-10-13 real-source audit contains 57 verified PDFs, complete critical
  fields in all 57, and no unresolved extraction or currency rows. Numeric
  eligibility remains 33/33 construction and 3/24 liquor: source-limited blanks
  were documented, not replaced by zero or counted as supplied amounts. MD&A
  chapters and original-source evidence were independently checked offline.
  See data/fundamental_quant/materials/2026-10-13/repair_acceptance.json and
  independent_material_review.json for measured results and their limitations.

- Final deployed local worker PID 51604 completed run
  1af997b57aed43738fb9cd26bff1bb80 with exit code 0, no error codes, and both
  group packets reused. The repetition added no public/model requests. The day
  retains seven share-history requests; history target correctly remains Oct 12.
  An additional 56 lifecycle/audit tests passed (overlap with the 561-test suite).
- A real 0.856-second supervisor reap window is now displayed as bounded
  `completing` only with a matching terminal child receipt and healthy parent.
  Missing/mismatched/expired receipts still expose interruption. Audit now uses
  the same canonical packet digest as authorization/status, while separately
  verifying embedded content hashes; the independent review's initial hash
  false positive is explicitly corrected and preserved in the audit history.

### 2026-10-15 Automatic historical backfill and measured comparison readiness

- Material eligibility now retains both raw numeric coverage and effective
  coverage. Only source-hash/page-bound, verified noncritical undisclosed fields
  leave the effective denominator. Critical gaps, unlocated fields, active parse
  failures and conflicts remain blocking. No blank becomes zero or an invented
  not-applicable status; later verified numbers revoke obsolete exclusions.
- The public-only daily material path acquires six pre-window originals per
  issuer (two FYs and matching H1/Q3 legs) for historical financial vintages.
  Explicit minimum-year propagation fixes hidden seven-year catalog/source
  cutoffs. These documents are marked historical valuation seeds, not extra
  operating years or additional annual-report gate requirements. Opening equity
  may support the first operating year's ROE without extending growth intervals.
- Source selection consistently rejects abridged quarterly `正文` as well as
  summaries/English editions. Existing archives and receipts remain intact.
  Incomplete originals become versioned full-text correction tasks, with a
  separate two-operation/600-second public-source correction ledger; the original
  42-operation/3600-second seed ledger is never reset. Actual original coverage,
  not an old download-success result, determines whether the task is complete.
- Parser v24 distinguishes standalone-quarter and cumulative statements. Explicit
  four-column quarterly headers select cumulative columns 3/4; missing cumulative
  labels or incomplete four-column rows cannot silently yield YTD figures.
  Historical short names can be identified by labelled security code plus issuer
  short name on the original cover, retaining the literal name and page evidence.
- Old FY/H1 relative headers require matching report-period evidence, and sparse
  same-row amounts require original PDF coordinates. Cross-page extraction keeps
  the full literal quotation, including intervening headers; the cleaned parsing
  text is separate. Rejected observation quotes now create visible issues instead
  of silently losing otherwise obtained financial fields. A prior same-issuer
  company-profile legal name may identify a later report only after its original
  PDF hash, page text, security code and disclosure ordering have been checked.
- Historical shares preserve disclosed precision. Already-known anchors bridge
  compatible dated events; vendor retrospective intervals remain non-PIT.
  Exact values, source-precision ranges, vendor observations and retrospective
  reconstructions are separately rendered. Continuous conversion registration
  dates and A/H class counts/quotes require their own adapters and remain visible.
- Known transient history failures have at most one retry after 30 minutes per
  issuer/effective date. Merged old errors do not consume attempts again; unknown
  transmission states never authorize blind retries. Share-source retry receipts
  similarly retain a maximum of two daily attempts and a cooldown.
- `material_acceptance.json/html` measures each company's actual seven-year core
  financial, FCF, BVPS and original MD&A coverage, latest interim gate, source
  hashes, current extraction errors and historical valuation months. This is
  input/computation acceptance, not model research approval. `backfill_queue.json`
  separates executable missing-data tasks, unsupported adapters and verified
  nondisclosures. The local public-only worker's total timeout is 7200 seconds to
  accommodate bounded source stages plus preparation; paid authorization is unchanged.
- Actual artifacts live under `data/fundamental_quant/materials/2026-10-15/`.
  `actual_acceptance_receipt.json` records before/after paid-token ledger snapshots;
  `material_acceptance.json` provides per-company results rather than treating
  successful file generation as financial or investment acceptance. Public-source
  failures remain in acquisition receipts even when later repairs succeed.
- Final real-source replay contains 99 original PDFs: 57 operating-window
  versions and 42 historical seeds. All seven issuers have 2019-2025 core
  operating numbers, 49 FCF proxies, 49 BVPS values, 49 annual MD&A chapters and
  the latest 2026 H1 material gate. The 28 old FY/H1 documents contribute all
  112 required seed observations to financial events. This does not certify
  every historical valuation: six old Q3 reports still need applicable currency
  evidence; exact share-event/A-H adapters and three Oct 15 price refreshes
  remain separately visible. Those three known transient attempts reached the
  daily retry limit and wait for the next bounded daily refresh.
- `cache_reuse_acceptance.json` proves both preparations reused unchanged
  packets with requests/httpx networking forbidden. Paid-token ledger snapshots
  are unchanged. Large report evidence is stored losslessly in grouped local
  HTML pages: the liquor/construction landing pages are approximately 9.94/14.63
  MB rather than 76/157 MB. Original sections, quotation anchors and cross-links
  remain available; no numerical fact is trimmed to reduce browser load.
- Final regression: 704 tests passed, including `tests/validation/`, source,
  parser, cross-page observation, retry, cache, worker and report-sidecar paths.
  Local worker PID 22592 resumed and executed real run
  `ccc19bc56a2e4072a4be0334c7024169`: both packets reused, material readiness ready,
  zero paid calls, with only `history_refresh_incomplete` retained for the three
  bounded transient price failures. Its coarse job status remains blocked/partial;
  the per-company material acceptance is ready. `delivery_receipt.json` binds the
  actual worker result, independent evidence checks, cache hashes and unchanged
  paid ledger; this is not a production-server deployment or investment approval.

### 2026-10-16: distinguish remaining evidence gaps and continue public backfill

- The three Oct 15 transient price failures now have a bounded Tencent raw-price
  fallback. The original response, query and content hash are retained; adjusted
  candles cannot replace unadjusted prices. The primary-source failures remain
  in history while a cache-only replay checks all seven issuers before clearing
  the active error. This recovery never adds vendor PE/PB as calculated facts.
- `share_announcements.py` discovers CNINFO share changes, completed cancellations
  and listing notices, keeps immutable response/PDF bytes, and independently
  replays issuer identity, publication date and the quoted share count. Only
  already-completed registration or a current disclosed snapshot can establish
  a point-in-time anchor. Proposed registration, third-party documents and bond
  listings have separate statuses. A 2021 notice recounting a 2018 event does
  not authorize a 2018 valuation. Exact counts and source-precision intervals
  remain distinct in `longitudinal-data/13`.
- Share source requests have a persistent rolling limit of 12 per 600 seconds,
  with unknown transmissions retained and no midnight reset. Cached parsing
  repairs consume no network operations. Unfinished issuers precede refreshes
  of completed scopes, with actual-attempt timestamps providing fair rotation.
- The isolated materials worker checks known public-rate deferrals every ten
  minutes and continues its supervised child only while that queue remains.
  A `--materials-only` child cannot fall through into the research service if
  configuration changes. Completed, unknown-response and parser-only tasks do
  not cause automatic repeated model or source requests. Paid campaigns and
  their ledgers are not resumed or replaced.
- Material acceptance distinguishes executable backfill, absent adapters,
  exhausted configured searches, parser/integrity failures and currency-basis
  evidence gaps. Limited searches do not prove nondisclosure. Six 2017/2018 Q3
  reports retain their raw amounts and unknown presentation currency: prior H1
  CNY declarations are shown as leads, not copied into current-period facts.
  These historical valuation seeds do not erase complete 2019-2025 operations.
- `materials/index.html` is the stable latest acceptance entry point. Dated
  snapshots link back to it; relative report/source links keep the dated base.
  A failed run replaces the latest entry with a visible failure instead of
  leaving a stale successful page. The dated original reports and old receipts
  remain available for audit.
- Actual Oct 15 replay finished `prepared`, with no active pipeline errors and
  unchanged paid-call/token ledgers. It retains 99 financial-report originals,
  49 FY core/FCF/BVPS sets and 49 annual MD&A chapters. Eight primary share
  disclosures now support 24 intervals. Strict-PIT PE coverage increased from
  44 to 58 months for Luzhou Laojiao and 20 to 21 for Sichuan Road & Bridge;
  these counts do not imply complete historical valuation coverage.
- Regression ran 763 passing cases including `tests/validation/`; 85 affected
  cases passed after the final parser/queue/latest-entry changes (overlapping
  counts). Network-forbidden preparation reused both byte-identical packets.
  `independent_material_review_data13_final_20261016.json` checked all 99 report
  hashes, 658 historical monthly prices, eight announcement originals and their
  disclosure dates, 24 share intervals and corresponding historical market-value
  equations without new model calls or network requests. It found no issues,
  while retaining the six old quarterly currency limitations and outstanding
  public-discovery/registration/A-H work. `gap_repair_delivery_receipt_20261016.json`
  binds these checks to the live local worker and the unchanged paid ledger.

### 2026-10-16 — A/H and continuous-conversion source adapters

- Public-data only: no paid model or old research budget is resumed. A/H listing
  identity comes from the original stock-information table; H quotes retain the
  raw response and body hash. Verified cache coverage drives incremental requests,
  so advancing the end date does not redownload the entire historical window.
- Class market value is `A close * all A shares + H close * H shares * fixed HKD/CNY`.
  Restricted A shares are included. Both prices must have their own references,
  match the same observed trading day and the correct class. The runtime FX proof
  is frozen into the packet and its preparation hash; this is configured constant
  FX valuation, not reconstructed historical spot FX. Future share disclosures
  can support only explicitly retrospective values, never strict PIT.
- `conversion_history` discovers quarterly conversion notices, verifies original
  PDFs and retains opening/closing counts and conversion/other-change bridges.
  Closing snapshots do not establish daily registrations. The real 2025 Q3 source
  contains both conversion and cancellation, demonstrating why monotonic share
  interpolation is invalid. Unresolved registration vintages are evidence limits,
  not absent adapters or endlessly retryable download tasks.
- CNINFO returned the same first page for several requested page numbers. Both
  share-announcement adapters now use verified date cursors, preserve the overlap
  boundary, and stop explicitly when a date boundary cannot advance. Source
  operations retain the original shared rolling ledger, including failed attempts.
- Discovery also retains cancellation implementation notices. An incentive-plan
  name is not proof of nonexecution: source wording must distinguish scheduled
  and completed registration. Cached CCCC searches contained two such notices
  that the old title filter missed. Explicit debt/perpetual-security issuance
  and archive-management policies are excluded before PDF requests, with reasons
  retained; planned GDRs and approvals cannot establish issued shares.
  The real 2025 cancellation implementation notice uses an explicit `A股代码`
  header. The identity parser recognizes it while rejecting mismatched or
  conflicting identifiers; both acquired implementation PDFs remain scheduled
  events with no post-cancellation share facts until completion is evidenced.
  Further actual replay covers completed-registration wording, share-movement
  continuation tables and identity in the new-share listing chapter rather than
  the cover. Chapter identity requires code, share abbreviation and listing
  venue/context together, not an arbitrary code found elsewhere in the PDF.
  Parser11-15 receipts record restored registration quantities, explicit review/
  meeting notices, pending registration receipts and same-day transaction phases.
  A repeated identical issuer code is accepted, while mixed issuers are rejected.
  The actual two-phase Sichuan issue reconciles asset-consideration shares plus
  accompanying financing to final registered shares; neither the largest number
  nor pending account-credit shares can substitute for this evidence. The latest
  focused source regression passed 141 cases. Financial-adviser opinion appendices
  remain secondary sources rather than issuer registration facts. Child runs replay original
  cached bytes under the updated parser, without new paid calls.
- Materials runs refresh conversion sources and A/H quotes, reprepare changed
  groups and display their coverage beside historical charts. The free worker
  continuation recognizes these public-source receipts; known cooldowns resume
  with `--materials-only`, while unknown outcomes and source limits do not loop.
- The continuation checks the persisted receipt every minute and sends nothing
  before `retry_at`; the shared 12-public-requests/10-minute ledger is unchanged.
  Source conflicts are separate from parser failures: one real share announcement
  gives incompatible cancellation quantities. Its quotes and conflicting totals
  remain visible and cannot produce a share fact. A simultaneous repairable
  parser failure still takes precedence over the source-limitation state.
  A drain request is also checked before starting any new child, so a due timer
  cannot race the next heartbeat and start another batch during repair shutdown.
- The real two-group replay retains 99 financial-report originals, 49 complete
  FY core/FCF/BVPS sets and 49 annual MD&A chapters. A/H data now includes 1,918
  raw H-price observations and 14 class intervals, yielding 79 retrospective
  monthly market values for CCCC; strict PIT remains zero where contemporary
  class counts cannot be established. Twelve conversion PDFs provide 24 opening/
  closing observations over 13 distinct snapshot dates, not daily registration.
- Independent offline review rechecks original hashes, numerical equations,
  source quotes, share-event bridges, A/H valuation and configured FX. Receipts
  are timestamped under `materials/2026-10-16/independent_special_review_*`.
  The broad regression passed 905 cases including `tests/validation/`; final
  scheduling/report/source-conflict checks are recorded separately. None of these
  material checks is presented as paid-model research or investment acceptance.
- The real public queue finished all three configured announcement queries for
  all seven issuers on 2026-10-16. It retained 86 announcement records and 77
  original PDFs, with no pending downloads. Completed bounded searches are not
  claims that no further disclosure exists. Final delivery binds the worker run,
  packet hashes, source review, offline cache reuse and unchanged paid ledger in
  `materials/2026-10-16/specialized_delivery_receipt.json`.


### 2026-10-16: latest-disclosed share valuation and event-targeted backfill

- Historical valuation now keeps three different bases: strict dated share
  coverage, retrospective reconstruction, and the last publicly disclosed share
  snapshot. The third basis applies only after publication, retains snapshot age
  and original references, and never claims to know daily registered shares.
  Known completed share changes cut off an obsolete snapshot; a newer independent
  disclosure resolves covered older events. Continuous conversion is explicitly
  reported as potential dilution rather than silently assumed absent.
- A/H market value remains A shares times unadjusted A price plus H shares times
  unadjusted H price and the configured fixed FX rate. Earlier annual/interim
  capital notes are accepted only with explicit par value and corroborating
  issued-share totals. Announcements and reports never authorize total shares
  times the A price. Publication-day prices cannot use that day's new disclosure.
- Ordinary share snapshots preserve reported rounding bounds; PE/PB and FCF-yield
  ranges are calculated locally, including correct order for negative cash flow.
  New charts and acceptance counts expose this usable reference series separately
  from exact and retrospective coverage, including unresolved dated blockers.
- The old three announcement search terms were insufficient: actual CNINFO queries
  found previously missed repurchase-completion and incentive-registration PDFs.
  A remaining dated valuation gap now generates a bounded event-topic search.
  Extra queries and page cursors persist independently of the old complete flags,
  reuse the same public-source quota/cache, and resume after cooldown. Completion
  of selected search topics is never translated into issuer nondisclosure.
- This change uses cached originals and bounded public downloads only. Paid model
  requests remain disabled; budget snapshots and dated acceptance receipts record
  actual unchanged model/token ledgers. Previous acceptance artifacts are retained
  in the dated before_disclosed_share_fix directory for before/after comparison.
- Actual registration notices can precede the vendor's listing event by one or
  two days. A proven completed-registration notice, matching publication date,
  source references and compatible reported count now binds these as the same
  action. Three real Luzhou notices exposed and reproduce this defect. The
  ordinary snapshot replay restores 94/94 months; incompatible totals and planned
  registrations remain blocked. A/H bindings additionally require explicit class
  quantities reconciling to that completed total.
- The integrated regression after the ordinary registration fix passed 296 cases,
  including tests/validation. Additional A/H adapter tests retain strict proof
  requirements for planned-versus-completed events and publication-day cutoffs.
- The final integrated suite after announcement parser/17 and class snapshot/6
  passed 322 tests. Newly acquired Sichuan notices exposed an unsupported
  "registration completed, total shares are" sentence; real-page fixtures now
  cover it. Maotai's two repurchase-result notices explicitly describe scheduled
  cancellation, so they are tracked as pending completion, not failed parsing or
  completed cancellation. Before-event share tables cannot negate a same-day
  completed event merely because their calendar dates coincide.


### 2026-10-18: event-directed share originals and purpose-specific acceptance

- The latest-disclosed share denominator now has its own actionable recovery tasks.
  Completing H-share price downloads cannot close a missing A/H share-count task.
  Strict registration coverage remains a separate, explicitly labeled contract.
- Remaining known-event gaps select bounded H1/Q3/Q1 original reports, ranked by
  marginal months potentially covered. Filing deadlines schedule searches only;
  actual source disclosure dates determine historical use. The daily public-source
  ledger persists reservations and its deadline across restarts, and no model
  provider is constructed. Successfully archived originals are reused.
- New originals acquired only to verify shares retain all original pages and a
  persisted historical_share_snapshot purpose. Their unaccepted financial fields
  do not enter the operating panel. Seven required FYs and the latest H1 cannot
  be downgraded by this marker. Share quantities still require original evidence
  and the usual date, unit, identity, class and arithmetic validation.
- A repurchase label plus compatible previously published total-share intervals
  does not establish completed cancellation. Such events preserve a specific
  risk warning and both sources; a real reduced total or confirmed cancellation
  remains a blocker. This repairs two real CCCC 2020 events without changing the
  strict series or asserting that class composition was newly disclosed.
- Known transient source failures can resume only within the same free-source
  ledger/deadline and retry policy. Unknown outcomes and unavailable evidence do
  not trigger blind retries. Paid token/model ledgers remain untouched.

- The post-prepare reconciliation recomputes remaining original searches against
  current panels, without another source request. Obsolete fallback requests move
  to the audit history; unresolved document/role validation still blocks, and
  the original journal's limits, deadline, attempts and consumption are retained.
- Source originals already present in report manifests/ledgers are checked by
  hash, cover/period, issuer and publication date and reused alongside the catalog.
  Successful purpose registration is checkpointed per source response and can be
  recovered from exact successful receipts after interruption or across days.
- latest-disclosed-shares/5 and share-announcements/21 distinguish scheduled
  cancellation from completed registration. Replayed scheduled notices become
  counterevidence, not share facts. Vendor-only repurchase reductions remain an
  unresolved-change blocker; later real snapshots can supersede the old count.
- Material reports can now be rendered from frozen panels through
  render_material_reports without acquiring data or constructing a model client.
- Real-source replay caught a semantic regression that synthetic confirmations
  missed: five completed Luzhou cancellation notices say "注销事宜已办理完成".
  The completion check now recognizes that exact completed phrase, while a
  counterfactual "预计" notice still blocks. Their SHA-verified real-page fixtures
  cover same-publication completion and next-day availability.

- Before the subsequent cross-page correction, free-source acceptance acquired/reused 12 originals (11 newly available
  unique PDFs). Operating comparisons remain ready for all seven companies and
  all seven FYs. Last-disclosed monthly valuation coverage increased 562 to 592
  of 658: CCCC 68 to 76, Sichuan 72 to 83, Shandong 56 to 67; Luzhou/Wuliangye/
  Anhui stay 94 and Maotai stays 84. The remaining 66 months retain explicit
  publication, class-count, cancellation or conversion evidence limits.
- Final integrated regression: 354 passed; scoped Ruff passed. A socket-denied
  offline replay reprocessed 85 cached notices, reused both prepared packets on
  repetition, retired obsolete source fallbacks, and preserved all source limits,
  attempts and paid token/model ledger values. The final material state is prepared
  with no pipeline errors; this is not investment-research acceptance.
- Pre-continuation-check receipts are in materials/2026-10-18: the combined
  share_snapshot_final_acceptance.json, share_snapshot_offline_replay_receipt.json,
  independent_last_disclosed_review_20261018_094959521064.json and
  independent_coverage_regression_review_20261018.json. Independent checking
  revalidated 592 usable monthly values, 1,776 ratios/bounds, 76 A/H market values
  and two completed A-increment/H-carry bridges against final frozen hashes.

- The final source-queue check exposed a genuine remaining parser defect in
  Sichuan notice 1214730643, not a missing disclosure. share-announcements/21
  now links the explicitly share-denominated capital-movement table on page 3
  to its consecutive page-4 total, validates before + issued = after and the
  limited/unlimited component sums, and persists separate original-page refs.
  The original registration date 2022-09-29 is retained as evidence; the current
  observation becomes available strictly after its 2022-10-10 publication.
  The continuation handler rejects missing headers, yuan units, inconsistent
  amounts, intervening/unrelated pages and future registration evidence.

- Final post-continuation acceptance: 366 integrated tests passed, with scoped
  Ruff passing. The independent source/formula audit revalidated 594 of 658
  monthly valuations and 1,782 ratios/bounds; Sichuan is now 85/94 after restoring
  October/November 2022. Other counts: Luzhou 94, Wuliangye 94, Maotai 84,
  Shandong 67, Anhui 94, CCCC 76. The remaining 64 months are evidence limits.
  Both material_acceptance.json and backfill_queue.json have zero download,
  queued, retry, or parser-repair tasks; remaining entries explicitly require
  source basis or record completed bounded searches, not company failure.
- Final audit: independent_last_disclosed_review_20261018_095932918737.json,
  plus independent_coverage_regression_review_20261018.json. The combined
  share_snapshot_final_acceptance.json is version 2 and binds their final hashes.
  Socket-denied replay and cache reuse still show zero paid calls/network work;
  all original acquisition limits/deadlines/attempts and paid ledgers are intact.
- The local FQ host was gracefully drained/reloaded after final replay (new PID
  33196, fresh heartbeat verified). No immediate batch or paid run was started;
  the configured public-material continuation and materials-only daily route
  remain enabled. worker_reload_receipt.json records the unchanged paid ledger.

### 2026-10-18: Question-scoped research-material acceptance (offline)

- Added versioned `MaterialPolicy`, per-question `MaterialDecision` and compact
  `ResearchInputBundle`. Seven years is a target window, with no minimum years
  or fill-rate admission rule. Original numeric, identity, date, currency,
  source and calculation errors remain quarantined at their dependencies.
- `config/material_policy.json` selects industry question metrics;
  `config/material_review_prompt.md` controls conditional upstream ambiguity
  review. The review adapter has no implicit paid transport, caches successful
  and failed attempts by relevant evidence/policy/prompt, and never overwrites
  disclosed facts with estimates. Structural receipt checks are not semantic
  accounting acceptance.
- CLI preparation freezes effective policy and writes separate material decision
  and research-input artifacts. Worker/report readiness consumes the same
  question-scoped decision. Old year/document/MD&A/fill criteria remain only in
  `legacy_diagnostics`. Research tools route new fact conflicts upstream and
  persist affected-input quarantine across checkpoints.
- New isolated `scripts/accept_research_materials.py` replays the existing seven
  companies' frozen packets and original hashes without downloads, notifications
  or model calls. Artifacts: `data/fundamental_quant/material_policy_acceptance_20261018/`.
  This is source/calculation/control acceptance, not paid model-research acceptance;
  old reports, checkpoints, paid budgets and live latest pointers are preserved.
- Methods, edge cases and the simplified flow diagram are documented in
  `docs/development/research_material_policy.md`.
- Final acceptance: 253 focused integration/validation tests and scoped Ruff
  passed. Offline replay verified all 110 original files and unchanged frozen
  inputs; both groups retain common FY2019-2025 base FCF and BVPS windows,
  658 historical valuation rows, and scoped unresolved EV/NOPAT questions.
  A socket-connect-denied replay also passed with zero paid calls/token cost.

### 2026-10-18: Automatic EV / reported-NOPAT input recovery (offline)

- Traced pending enterprise questions to two separate defects: the original FR
  field contract omitted key consolidated cash/equity/financing inputs, while
  the longitudinal panel had no source-bound EV, NOPAT or invested-capital bridge.
  Added `enterprise_sources`, `enterprise_metrics` and shared
  `enterprise_materials` orchestration; normal prepare and tool-supplement paths
  now reuse originals and recompute the same scoped material decision.
- Recover consolidated figures and note components from existing text. Optional
  Windows local OCR handles existing hash-verified consolidated statement images;
  source coordinates and OCR evidence remain available. OCR comparative values
  contradicting prior original disclosures are quarantined individually rather
  than rewriting digits or blocking valid current statements.
- Reported NOPAT uses observed tax and expensed interest, with investment-return
  exclusion shown as a separate scenario; no default tax rate, automatic
  normalization or interim annualization. Debt, cash and equity adjustments use
  documented points/ranges and aggregate statement reconciliation; blank rows
  are not disclosed zero facts. Year-end EV uses observed prices and strictly
  prior disclosed balances. All-share-class and configured fixed-FX inputs remain
  authoritative.
- Code intervals keep stable ECI identities and endpoints without a fabricated
  midpoint. Question scope preserves conditional equity-proxy restrictions.
  New fact/calculation/range recheck requests quarantine affected dependencies
  across restart. Extraction caches bind original content, existing numeric
  evidence, disclosure applicability and method versions; changing market
  prices does not reread financial reports. Runtime cache-hit telemetry is kept
  outside the deterministic research packet hash.
- `scripts/backfill_enterprise_materials.py` creates an immutable local snapshot,
  original-file audit, financial calculation/source tables and compact research
  bundles. Existing frozen packets, reports, budgets and paid ledgers remain
  unchanged. No model, downloader or notification transport is used by this
  replay; source/control acceptance is not investment-research acceptance.
- Validation: 233 focused and `tests/validation` tests passed, plus scoped Ruff.
  Coverage includes wrong arithmetic, source conflicts, current/historical date
  separation, equity and debt residuals, no fabricated zeros/midpoints, OCR
  failure visibility, cache integrity/reuse and range quarantine after restart.
- Independent real replay found an OCR subtotal contradiction in CCC's latest
  interim equity: the erroneous total is quarantined while verified parent and
  minority components remain available for a derived total. No corrected raw
  disclosure value is hand-filled. Reports count only policy-accepted results;
  rejected historical candidates stay in audit details and do not inflate
  coverage. Ranges preserve FY/interim labels even when no scalar row matches.
- Final actual artifact: `data/fundamental_quant/enterprise_materials_20261018_verified/`.
  All 110 originals pass SHA checks; socket-denied execution records zero
  connection attempts and zero paid calls. Latest EV, NOPAT and invested capital
  are available as validated points/ranges for all seven companies. Accepted
  history totals: 60 NOPAT records, 46 invested-capital records and 26 year-end
  EV records. Remaining old-period/source-scope conflicts are explicitly scoped;
  one rejected historical calculation does not enter the research input/report.
  The earlier `enterprise_materials_20261018_final` snapshot remains for audit;
  repeat execution of the corrected snapshot verifies hashes and reuses results.

### 2026-10-18: Real offline longitudinal comparison and gap discrimination

- Added `longitudinal_acceptance` and `scripts/accept_longitudinal_comparison.py`
  to consume accepted material snapshots and generate actual multi-company,
  multi-year series, SVG charts, FY/interim tables, source-linked calculations
  and complete management-discussion entries. Both liquor (three issuers) and
  construction (four issuers) use generic input specifications. Templates do not
  manufacture investment theses or represent this as paid model research.
- Comparisons use common fiscal windows, actual elapsed CAGR intervals and
  cumulative consolidated CFO / consolidated profit. Negative cash flow stays
  negative; general restatements do not hide reported comparison figures or
  become claims of organic growth. BVPS retains disclosed share precision;
  capital-return observations require comparable opening/closing capital and
  positive average-capital bounds. Interval midpoints are not invented.
  Historical EV displays its actual book-balance age rather than pretending
  differently dated balances support synchronous valuation rankings.
- Actual source replay located the missing-data cause in consolidated-note
  segmentation: nested JV/associate headings prematurely ended the notes.
  Source version 14 fixes that boundary and preserves complete finance-cost
  components with signs and blanks. Engine version 5 supports source-bound
  finance-note residuals without creating raw interest-expense zeroes, and a
  conservative total-liability range when individual debt lines are all blank.
  The retrospective selector accepts source-validated comparative policy
  receipts only for their exact metric and period (data version 18).
- Added `enterprise_gaps`: explicit source absence/non-disclosure, verified
  statement blanks, parser failure, unsuccessful localization, missing cached
  reports and unresolved source/accounting classifications are separate states.
  Source receipts bind issuer, period, original hash, actual quoted page text
  and consolidated scope. Aggregate coverage is an independent axis; context
  years are distinguished from target FYs and latest interim observations.
  Each source snapshot now provides readable gap HTML plus JSON.
- Validation: 301 focused integration and `tests/validation` tests passed;
  scoped Ruff passed. Source/comparison snapshots are immutable, hashes are
  checked before reuse, and real acceptance executes with socket connections
  forbidden. Old frozen reports, raw originals and paid authorization ledgers
  are preserved; this task does not resume paid research.
- Real final source snapshot: `enterprise_materials_20261018_longitudinal`.
  All 110 originals and snapshot artifacts pass hashes and repeat-run reuse.
  Within the requested FY2019-2025 window, all 49 company-years have accepted
  CFO, cash Capex, base FCF, BVPS and NOPAT; invested capital covers 46/49.
  Wuliangye 2021, Maotai 2021 and CCCC 2023 remain limited by inconsistent
  restatement cohorts, not absent PDFs. Two unresolved liquor bond-row parses
  are covered by accepted aggregate capital intervals. Uncached older/interim
  context documents are not reported as missing target-year originals.
- Actual comparison acceptance: `longitudinal_comparison_acceptance_20261018`.
  Both groups pass deterministic checks, with eight SVG trends per group and
  seven issuer MDA pages. Current EV covers seven companies; accepted year-end
  EV has 17 liquor and 19 construction observations. Cash conversion independently
  reproduces the seven FY baseline, including negative construction results.
  All twelve HTML local link/anchor checks pass; repeat execution reuses the
  snapshot with unchanged artifacts and source packets. Execution receipts
  record no socket connections and no paid API calls.
- Optional visual browser inspection was blocked by the browser's file-protocol
  policy; no bypass was attempted and screenshot acceptance is not claimed.

### 2026-10-18: align three restatement measurement cohorts offline

- Source recovery version 16 and enterprise engine version 6 add same-document,
  same-period, same-currency consolidated measurement receipts. Complete
  restated comparatives are preferred; Wuliangye and Maotai FY2021 instead use
  complete original annual disclosures, explicitly `original_nonlatest`, while
  CCCC FY2023 uses the restated comparative in its FY2024 annual report.
  Capital, cash/debt, tax and NOPAT never borrow missing fields from another
  publication or from parent-only statements. The source balance-sheet total
  is identified as such rather than invented as an independently read asset.
- Material decision/input schema 3 validates alternate publication inputs
  locally to their CEM/ECI measurement. Old raw IDs remain globally rejected;
  `measurement_cohort_inputs` and context-only report aliases explain accepted
  calculations without replacing the latest-panel facts or BVPS. Tampered
  receipts, known fact errors, document/page failures and review quarantine
  still block affected calculations after restart. EV/PIT cannot reuse these
  alternate current-analysis cohorts.
- Comparison version 2 displays the chosen publication and separates BVPS's
  global selected disclosure from NOPAT/capital's local basis. Available annual
  points do not imply a valid cross-version ROIC bridge or precise ranking.
  Historical EV displays balance-sheet dates, age and stale-input limitations.
- Real replay caught and repaired a source15 regression: an unreadable
  comparative date in a split H1 table header wrongly suppressed its verified
  current column. Source16 restores CCCC FY2026 H1 assets, cash and NCI from the
  same original OCR/page hashes; the erroneous OCR equity total stays rejected.
  Runner6 also matches current-price EV range receipts by explicit balance
  period, avoiding a false display failure when the host row has another date.
- Final scoped regression plus `tests/validation`: 333 passed; scoped Ruff
  passed. This work uses only frozen originals and local calculation/OCR,
  without a paid model, notification, deployment or budget reset.
- Actual final frozen materials: `enterprise_materials_20261018_cohort_aligned_final`,
  source input hash `02b5f15ad1a4bba125dbda926e768689d64b0b988079c1cbdab67552813e88fb`.
  All 110 originals pass source hashes; repeat execution reuses unchanged
  artifacts. The requested 49 company-FYs now have accepted NOPAT and capital
  measurements (points or bounded intervals), with the three publication
  exceptions exposed. CCCC current-price EV again uses its FY2026 H1 balance.
- Actual final comparison: `longitudinal_comparison_acceptance_20261018_cohort_aligned_final`,
  input hash `82fcabe6cca7728ac595c29bacd4937e40eaabd86f1c8519eb686c7e1d34b62c`.
  Both complete issuer groups pass deterministic checks and repeat-run reuse;
  all eight annual metrics expose FY2019-2025 common periods, seven current EV
  observations and 36 historical year-end EV observations. The remaining
  `limited` status preserves accounting, interval and cross-version-return
  constraints; it does not label unprocessed firms as failed. Execution
  receipts record zero socket attempts and zero paid API calls. Earlier
  intermediate snapshots are retained instead of silently overwritten.
- Independent final review passed: 128 scalar formulas, 96 interval endpoints,
  49 FY cash-flow equations, seven cumulative cash conversions, 49 CAGRs,
  23,049 local links/anchors and six cohort balance crossfoots. The durable
  review sidecar is bound to the final acceptance input hash. It confirms
  all seven current EVs use 2026-10-16 prices plus 2026-06-30 balances, and
  the six affected cross-version capital-return years remain explicitly
  restricted rather than presented as comparable ROIC observations.

### 2026-10-18: use disclosed opening/closing capital from one publication

- Diagnosis of the six remaining ROIC restrictions: mainly a missing selection
  and computation path, not six absent external accounting bridges. The former
  cohort fallback only repaired a single year's balance; it did not pair the
  ordinary comparative balance with the same publication's current earnings.
- Added `enterprise_returns` and engine7 same-publication ECR measurements for
  affected cohort years and their following FYs. Each independent record binds
  opening capital, closing capital and current NOPAT, with all formulas,
  sources, publication dates and interval endpoints. ECR components do not
  enter or replace global annual calculations; market EV/PIT is unchanged.
- Wuliangye/Maotai FY2021 use their FY2021 annuals; FY2022 use FY2022 annuals.
  CCCC FY2023 uses its FY2023 annual including restated FY2022 comparative
  capital; FY2024 uses the FY2024 annual including restated FY2023 capital.
  No fabricated full retrospective latest-vintage series is claimed.
- Wuliangye FY2022 explicitly discloses Jan1 opening balances. The source date
  stays visible despite the previous-Dec31 comparison axis. All active stock
  dependencies must share that instant; the old Dec31 lease note is excluded
  with an audit reason, not zero-filled. Financing totals still bound the
  missing detail, and old cash-flow closing cash is not treated as Jan1 cash.
  CCCC FY2024 NCI remains an E-minus-parent local crossfoot when its printed
  cell is damaged; capital uses disclosed total equity, without a new raw NCI.
- Material decision/bundle4 exposes accepted ECR records separately, plus
  scoped component source contexts. Rebuilding the exact record blocks wrong
  identities/dates/currencies, extra midpoint fields, arbitrary version labels,
  changed formulas and quarantined inputs. Endpoint rechecks survive tool
  restart. Comparison3 only replaces the previously restricted return year;
  it recomputes all four return endpoint combinations and shows three source
  components and actual opening dates. Ordinary comparatives are not labelled
  restatements. Reported proxies remain non-normalized and unranked.
- Offline regression and `tests/validation`: 382 passed; scoped Ruff passed.
  No paid API, notifications, budget changes or deployment were performed.
- Real immutable source snapshot: `enterprise_materials_20261018_roic_aligned`,
  input hash `dee670a292e035eb03ff7112ea5dfe586779715d1c8a9d6865e5d32fc01ebbbb`.
  Its six ECR records pass upstream acceptance with zero unresolved pairing
  gaps. Repeat execution verifies artifacts and reuses the source snapshot.
- Real full-group comparison: `longitudinal_comparison_acceptance_20261018_roic_aligned`,
  input hash `2959dc9a726e21ce8b3aa6f6a3f1f2f79c5dfe6ad0ae86d7cc178ccf8ad3f916`.
  Both groups pass deterministic acceptance and repeat-run reuse; the six
  previously restricted FYs now have valid same-publication reported return
  intervals. Remaining cohort-bridge periods are empty, while non-normalized
  proxy, cash/debt classification bounds and no-midpoint-ranking limits remain.
- Independent actual review validates all 18 ECR source components and twelve
  return endpoints, 110 original PDFs, 20 report artifacts and 24,339 local
  links/anchors. All 498 annual/interim panel values and bounds match the
  preceding frozen report; current and historical EVs are unchanged. A durable
  independent-review sidecar binds the actual acceptance hash. Socket attempts
  and paid API calls are zero; all previous snapshots are preserved.

### 2026-10-18: ROIC bounds, source dates and independent year-end summaries

- Source18 recovers the local CCE-subtable date header, explicitly disclosed
  Jan1 opening cash, complete liability partitions and first-lease-adoption
  balance tables using verified original PDF column geometry. Numeric `1年内`
  note headings are recognized without treating other numeric rows as headings.
- The previous source16 lease facts for Wuliangye/Maotai FY2021 confused Jan1
  adoption balances with the unadjusted previous Dec31. Old snapshots remain
  historical outputs; their source dates are corrected in a new snapshot.
  Wrong exact input IDs are isolated when present, never rewritten as zeros.
- Disclosure scope separates opening-only balances from global FY closings.
  Independent, explicitly adjusted year-end summaries remain usable: source18
  obtains actual 2022-Dec31 parent equity from Wuliangye FY2023 p7 and Shandong
  FY2023 p8. Jan1 restrictions are not relaxed to recover those values.
  The intermediate `roic_refined` snapshot revealed this completeness gap;
  `roic_refined_verified` is the intended final source/comparison replay.
- Engine8 / returns2 narrow financing upper bounds only inside verified
  same-publication measurements. Cash and debt contribute explicit independent
  widths to the capital bridge; unparsed or unclassified financing stays in
  the upper bound. Financial-subsidiary funding and numerator business scope
  remain unresolved economic choices, not an excuse to report a midpoint.
- Decision/bundle5, data19, restatement2 and offline runner8 preserve source
  identity, actual dates, correction receipts and cache bindings. Raw grouped
  amounts are validated without losing original commas or accepting malformed,
  NaN or infinite values. Comparison5 binds a frozen historical baseline and
  renders actual old/new ranges and source-date corrections, without using
  old facts as new calculation inputs or enabling interval-midpoint ranking.
- Comparison5 also consumes complete, independently accepted same-publication
  returns when the global panel lacks an input, comparable scope, or its entire
  NOPAT row. The unique measurement still passes all source/context, currency,
  period and endpoint checks; it fills no global facts, preserves the replaced
  status, respects the target FY window/cutoff and cannot override an existing
  valid or nonpositive-capital result. This fixes Shandong FY2022's early
  comparability return without fabricating its global NOPAT scope.
- Final relevant regression including `tests/validation`: 358 passed; scoped
  Ruff passed. All implementation/replay work is local: no paid API requests,
  notifications, deployment, budget reset or old-snapshot modification.
- Final source: `enterprise_materials_20261018_roic_refined_verified`, hash
  `048904f0ae5c2b59067576e3c6cd53158ffb673eaa9dacd062a4aa0f8e3207a1`.
  Final comparison: `longitudinal_comparison_acceptance_20261018_roic_refined_final`,
  hash `84dbc7b65bc0dec1b0f7920c43f412ce727a7ca1d8c1601bb235a3145c2a6e73`.
  Both groups pass actual deterministic replay and immutable repeat reuse.
  All 11 independently accepted same-publication records reach the final
  comparison; all cohort-bridge gap lists are empty. The original six records
  have five narrower ranges and one unchanged range. Remaining ranges still
  prohibit normalized-ROIC claims or exact ranking. The delivery manifest and
  separate audit sidecars retain all intermediate output limitations.
- A residual Shandong FY2021 single-sided advance cell remains a parser
  diagnostic, not a remaining ROIC width driver. Original p165 confirms the
  current-column 6,833,978.26 amount; the active financing upper is already
  14.026bn, tighter than the 60.330bn liability-partition alternative. Deducting
  that cell from the latter cannot change either published return endpoint.
  Generic one-sided-cell recovery is separately recorded in the backlog;
  neither a zero fact nor a fabricated source adjustment is introduced.

### 2026-10-18 — liability nature notes and financial-company funding

- Source19 adds original, same-publication liability nature tables with all
  numeric children, note subtotals, statement parents, actual balance dates,
  units, currencies and page locators. Explicit goods payables, pending output
  VAT and accrued promotion costs can narrow financing bounds; age tables,
  guarantees, current accounts, frozen funds and agency amounts are not blanket
  financing or operating facts. Marketing-only subtotals do not swallow the
  parent statement's dividends or unidentified residuals.
- A real integration omission was fixed: liability notes now receive the
  original first-adoption observations, not just ordinary recovered balances.
  Wuliangye FY2021 Jan1 marketing/VAT details bind to the printed Jan1 parent;
  old Dec31 observations are preserved. The regression exercises the recovery
  entrypoint with unequal old/new amounts, not just the alignment helper.
- Engine9 / returns3 pair proven consolidated external positive-rate deposits
  with the distinct interest cost within operating expenses. The finance-cost
  interest line remains separate. Classification, source tables, signed amounts,
  original subtotal consistency, company/date/currency/hash and duplicate
  components are checked before any bound changes. Missing paired evidence
  leaves the classification unresolved rather than reducing only the denominator.
- An adversarial replay exposed positive-only parsing masking a negative related
  balance when the original table also had a wrong total. Signed/parenthesized
  values now invalidate that funding proof. The unfinished first replay was
  stopped and retained with an interruption receipt; the corrected verifier
  uses a separate, code-bound snapshot. Real positive-row amounts are unchanged.
- Valid nature evidence can trigger same-publication returns beyond the old
  restatement cohorts. Comparison6 explicitly prefers a verified source-defined
  measurement over its broad global proxy when applicable, retaining the old
  basis; actual nonpositive-capital results are not rewritten. Target-FY display
  does not force out-of-window measurements into the comparison.
- Decision/bundle6 verifies and passes compact literal financial-associate and
  deposit-asset context without numeric use or automatic identity/alias joins.
  Original full pages remain upstream. Errors remain visible in rejected refs;
  unrelated research questions are not rejected for these context omissions.
- Classification sensitivities preserve each amount and source date, rebuild
  the full min-constraint tree, and recompute all return corners. Parent/child
  cases are alternatives, not additive effects. No midpoint, normalized economic
  ROIC claim, ranking, cash-interest blanket haircut, or new EV/NOPAT multiple
  is produced. EV/PIT remains under its existing definition and is labeled so.
- Extraction/freeze keys also bind the liability helper and calculation/policy
  code hashes. All work in this delivery uses local originals and socket-blocked
  replay; no paid model, budget reset, email, deployment or old-artifact rewrite.
- Actual replay also exposed repeated whole-scope reconstruction in the single
  ECR validator. Batch acceptance computes the requested source scope once for
  one frozen packet and still reconstructs every record's inputs/formulas;
  single-record callers cannot inject a preapproved scope. The interrupted slow
  snapshot and bounded CPU profile are retained, not labeled completed research.
- The amount/impact review found another propagation gap: a complete original
  nature table containing only undecided guarantees/current accounts was being
  dropped because it had no safe deduction. Such verified tables must retain
  their children and parent residual for sensitivity display without introducing
  a numerical deduction or a new research target. A numerically completed
  intermediate comparison was withheld before release for this missing detail.
- Real report acceptance then caught an optional-context boundary: empty
  classification metadata on CCCC's valid measurements is not a missing
  financial input and must not fail the comparison. Whole-parent counterfactuals
  also require original-amount decimal arithmetic: micro-yuan binary-float
  cancellation must not be labeled an impossible accounting scenario. Genuine
  source conflicts remain rejected; neither wider tolerances nor midpoints fix
  such boundaries. Failed intermediate reports remain separate from delivery.
- Final source snapshot is
  `data/fundamental_quant/enterprise_materials_20261018_liability_classified_complete`
  (`a1bbb4c4e67c2967aeca5d8102a4b1f7b28d63cc7ed849d385dc41b02c4109e2`).
  Comparison7/sensitivity2 replay is frozen separately at
  `data/fundamental_quant/longitudinal_comparison_acceptance_20261018_liability_classified_verified_final`
  (`c77704c123d73a27aa27241b6bdcc5f9c67b4c1df1e072ca886bc644fdbff324`).
  Both construction and liquor deterministic checks passed against real local
  packets; repeat execution reused the snapshot, socket attempts were empty,
  and all 620 prior artifact hashes were preserved. This is not paid-model or
  investment acceptance. Focused regression files plus `tests/validation/`:
  421 passed; scoped Ruff passed.
- The delivered liquor comparison contains 11 applicable annual classification
  records, 85 alternative item cases, and 12 accepted relationship contexts;
  construction has four applicable records and 22 item cases. All 17 accepted
  same-publication capital-return measurements remain available, including the
  two CCCC measurements without optional classification detail. MT2021 source
  amounts total CNY 3,257,245,259.42 at opening and 4,124,404,781.29 at closing
  for four unclassified guarantee/current-account children. Its reported
  cash-net capital-return proxy now spans 133.60401056%-146.53978542%, rather
  than the old 123.68219447%-277.03158838%; it is not normalized economic ROIC.
  The exact-source whole-parent alternatives hit the two current bounds; child
  cases retain their own source amounts and cannot be added to parent effects.
- Separate deterministic audit passed and is retained as
  `longitudinal_comparison_acceptance_20261018_liability_classified_verified_final_independent_audit_verified.json`
  (SHA-256 `963a8153e5b5d0fce7ce13dc7b3f54b9008f3270f5fa381253d20c3cd260884c`).
  It independently reconstructs 125 scalar formulas, 100 interval endpoints,
  34 capital bridges and 107 alternative items (192 feasible directions, 22
  genuinely infeasible), and checks 110 original PDFs, 4,214 original fact
  identities/values/refs, 35,700 local links and the 620 preserved artifacts.
  All 7 current and 37 historical source EV bounds are exactly unchanged.
  The first auditor's single quote error was a bare CR normalized to LF during
  HTML reading, not a missing quotation. Its failed sidecar remains immutable;
  the corrected auditor uses the same universal-newline transformation as the
  reader and leaves every source/report artifact unchanged. Original facts'
  restatement annotations can be enriched separately from numeric identity.
  `data/fundamental_quant/liability_classification_delivery_20261018.json`
  binds the source/report/audit/old-failure receipts and code hashes. Paid calls,
  network attempts and notifications were all zero; no deployment or visual
  browser acceptance is claimed.
  The separate `*_independent_audit_v2.json` also records the exact failing
  relationship ID, original quote, frozen-HTML byte offset and universal-newline
  correction provenance without replacing either earlier audit receipt.

## 2026-10-18 — Wuliangye capital-range attribution and conditional peer comparison

- Source20 / liability-evidence2 fixes the literal VAT alias `待转销销项税额`,
  binds one-sided note cells by physical row/column rather than globally equal
  amounts, and retains blank comparative cells as missing. Separately disclosed
  ordinary dividends identify the prior parent residual but are not classified
  as ordinary operating payables. The source code contains no issuer/year/value
  exceptions for these cases.
- Metrics10 / returns4 verifies a source-only cash-composition context on each
  same-publication capital bridge. Monetary-fund components and the exclusions
  to disclosed CCE must both reconcile against original references. CCE remains
  unchanged; restricted funds, accrued deposit interest and finance-company
  deposits are not subtracted again. Different related-party deposit disclosure
  bases remain separate rather than being forced to reconcile.
- Numerical acceptance8 / capital-policy-comparison1 attributes capital width
  to nonoverlapping payable parents, retains child amounts, and recomputes
  cash-net, no-cash-deduction and declared-distribution claim alternatives.
  Distribution claims first leave the uncertain financing upper constraint and
  are then added to capital once; the main measurement is not overwritten.
  The original NOPAT is fixed across these capital-policy sensitivities.
- Peer relations are evaluated for the same actual FY and mathematical
  definition. Global cross-publication values are limited references; excluded
  or nonpositive-capital periods remain explicit. A numerical relation reversal
  is not a business-quality ranking or normalized economic ROIC conclusion.
- Full regression found source-evidence version2 being rejected by the old
  relationship-context validator. Policy7 / bundle7 explicitly accepts supported
  evidence1 and evidence2, still verifies all original binding checks, and
  rejects unknown versions. The fix refreshes decisions on byte-identical
  source packets through the existing material replay rather than re-extracting
  statements or calling a provider. The initial prepared snapshot is preserved.
- Focused financial/material/comparison tests plus `tests/validation/` passed
  463 cases after the integration fix; scoped Ruff passed. Paid API calls remain
  zero. Actual frozen outputs and independent replay receipts are bound below
  after full artifact acceptance; tests alone are not that acceptance.
- A final completeness check found explicit comparative VAT was still being
  discarded when a different cell in its table was blank. Metrics11 / returns5
  accepts only individually verified, positive safe components as a bounded
  partial subset; unknown cells stay unknown and unlocated parent residuals
  remain conservative. Complete-table acceptance is not forged. Known unsafe
  numeric/source/currency failures or signed amounts cannot masquerade as blank
  non-safe cells. Explicit `statement_blank` remains a missing state, not zero.
- The partial derived parent residual has its own label and exact Decimal
  operator in comparison9; it is not a fabricated source fact. Three real
  derived cases were replayed, including infeasible financing assignments that
  remain infeasible. A separate cross-column dash ambiguity in an Anhui 2022
  note conservatively excludes that partial path; it is not a financial failure
  or a reason to block other companies or the rest of that company's analysis.
  The final source rebuild reuses the existing facts, observations and recovery
  records without extraction, and recreates only derived calculations and their
  material decisions. Old generated records remain in their original snapshots.


### Final offline delivery receipts — Wuliangye capital ranges (2026-10-18)

- Final derived-only source: `data/fundamental_quant/enterprise_materials_20261018_wly_width_reconciled_verified`, input `516967791a2e4c0b5df2db2b06c29eb117ad24a4d68b5b2183d03b435be7fbc8`. Original facts, observations, recovery records and PDFs remain unchanged.
- Actual two-group replay: `data/fundamental_quant/wly_capital_comparison_20261018_reconciled`, input `77a8555183a512c5a543431a7e76e00900d9d18287a1ed63a9acf64b3c29e1d0`; receipt passed and repeat reused frozen results. Liquor entry: `liquor/index.html#capital-policy`.
- Independent Decimal/source audit: `data/fundamental_quant/wly_capital_comparison_20261018_reconciled_independent_audit.json`: 5,626 checks, no errors. Independent presentation/source audit: `data/fundamental_quant/wly_capital_comparison_20261018_reconciled_presentation_independent_audit_verified.json`: 20 report artifacts, 38,598 links/anchors, 62 ratio replays, 8 cash proofs and 29 same-year pair comparisons passed. These are offline source/calculation/structural checks, not paid model or HTML visual acceptance.
- Final targeted and required validation regression: 479 passed; scoped Ruff passed. Delivery manifest `data/fundamental_quant/wly_capital_reconciliation_delivery_20261018.json` (SHA-256 `5fca52cf429da7aa5c32df5cf1349e017aaa716157f756f82e476ef7d0b70c92`) binds source/report/audit receipts and live calculation code; 717 previous artifact hashes checked unchanged. Paid model calls 0, network attempts 0; no email, deployment, budget or fee-ledger changes.
- Wuliangye FY2025 reported NOPAT held fixed: cash-net capital proxy 24.47–171.35%; no-cash-deduction 5.74–7.19%; explicitly conditional unpaid-dividend capital-claim scenario 24.47–70.57%. Cash amount width is zero. Remaining interval is financing classification, chiefly regulatory merchandise payables, other payables and AP; small net-capital denominator magnifies it. Economic cash surplus and regulatory-contract terms remain judgments; scenario results do not prove normalized economic ROIC or business quality order.


### 2026-10-18 — Common cash/payable policies across the liquor peer group

Added `scripts/compare_peer_capital_policy.py` and `peer_capital_policy.py`; new explicitly scoped publication comparison builders in `enterprise_returns.py` leave existing canonical builders and validators unchanged. No re-extraction, download, paid API, notifications or budget changes. Each company/year uses its own current FY publication and comparative opening balance. Latest eligible correction is not bypassed on failure. Raw facts and old snapshots remain immutable.

Actual offline replay: `data/fundamental_quant/liquor_unified_capital_policy_20261018_verified/index.html`, 15 source-reconstructed measurements, all three companies common FY2022–2025, 12 same-year pairs. Lao Jiao's existing FY2022–2025 evidence now has explicit single-publication measurement, replacing neither its old global-reference rows nor Moutai2023's old canonical restriction. Four scenarios retain/deduct CCE × financing lower/upper bounds, plus independent-financing envelopes; no midpoint or invented raw zero. Economic scope differences remain explicit. Repeated CLI execution reused the frozen snapshot.

Regression: 489 passed including `tests/validation/`, existing enterprise/material/comparison tests and new policy boundary tests; scoped Ruff passed. Report source pages and complete inputs available in `sources.html` and `measurements.json`; `acceptance_receipt.json` binds original PDF hashes, unchanged source artifacts and code SHA-256. This is offline deterministic/source acceptance, not new paid-model or visual acceptance.

Final independent Decimal/source audit passed: 1,890 checks, 60 policy-corner replays, 12 same-FY pairs, 404 decisive raw references and 43 original PDFs. Receipt: `data/fundamental_quant/liquor_unified_capital_policy_20261018_verified_independent_audit.json`. Root integrity check verified 241 HTML links, all 24 frozen source files and 143 earlier frozen artifacts. Active `materials/2026-10-18` files differ from the prior-turn baseline and are not this run inputs; no preservation claim or attribution is made for rolling state. Delivery manifest: `data/fundamental_quant/liquor_unified_capital_policy_20261018_delivery.json`.


### 2026-10-18 — Identified financial influence and operating-scope bridge

Added `financial_operating_scope.py`, `financial_scope_report.py` and CLI `scripts/compare_financial_operating_scope.py`. Existing frozen packets, group capital measurements, fee ledgers and previous reports are not modified. The new report consumes source-bound FY2022–25 evidence and prior accepted measurements, validates literal amount/location/company/publication/unit/period, then computes a deliberately partial identified-financial carve-out. No paid API or network calls.

Source work: 12 original PDFs checked, 488 source records and explicit standalone/elim gaps frozen in the input ledger; 24 additional entries come only from existing literal facts, same-note fund FV matching and the reserve-label alias. Equity associates use issuer carrying value plus specifically recognized earnings, never investee gross balance or total investment income. Maotai's operating financial interest addback is reversed once; fee, cash intersection, reserve allocation and matched fund asset/FV are explicit. Customer deposits are never subtracted again. Group ETR and no-second-tax equity-method endpoints are policy sensitivities, not proven allocated tax bounds. Unallocated cost, impairment, eliminations and unmatched financial items remain visible.

Offline report: `data/fundamental_quant/liquor_financial_operating_scope_20261018_verified/index.html`; 6 complete records, common FY2024–25 and 6 same-year pair comparisons. Earlier Jan1/Dec31 alignment and repo opening gaps are limited to affected incremental scopes, never called financial failures or missing-original disclosures. In 2024 the prior Wuliangye-above-Maotai group proxy becomes overlap after identified exclusions; Maotai-above-Laojiao remains within this sensitivity. No normalized industrial-ROIC or investment-quality ranking claim.

Regression: 498 passed including required `tests/validation/`, all selected enterprise/material/comparison tests and 9 new source/finance-pairing boundary cases; scoped Ruff passed. Repeated actual CLI execution reused frozen output. Two original Maotai FY2025 pages (financial interest note and CCE/reserve bridge) rendered and visually checked. HTML/source structure is checked separately; no paid Agent acceptance is claimed.

Final independent audit: 3,069 checks passed, 6 measurements/6 pairs, 512 source entries (488 original unchanged and 24 explicit additions). Receipt: `data/fundamental_quant/liquor_financial_operating_scope_20261018_verified_independent_audit.json`; root integrity sidecar verified all 29 protected source/baseline files, 12 original PDFs, code/artifact hashes and 116 local links. Final delivery manifest: `data/fundamental_quant/liquor_financial_operating_scope_20261018_delivery.json`. No paid model/network/notification/deployment calls. Lao Jiao FY2025 remaining identified trading/financial OCI holdings average book value 19.7467亿元 (not only closing 19.3445亿元), below the 134.677亿元 one-at-a-time additional-capital-removal threshold under this proxy; the inequality alone does not prove full business quality.

### 2026-10-18 — Publication-bound opening financial bridges for FY2022–23

Added `financial_opening_sources.py` with source reconstruction and isolated opening bindings; augmented the financial-scope CLI/renderer without changing canonical facts or old frozen reports. Four Lao Jiao/Wuliangye Jan1 stock/CCE/associate bindings use explicit current-publication source rows rather than equal previous-Dec31 values. Maotai repo and CCE blank columns produce separate derived-zero constraints only after complete Decimal cross-footing, physical-column verification and explicit yuan/CNY units; raw blanks survive unchanged. Opening financial source version is `/2`.

Maotai annual BS comparative dates and CF opening dates remain distinct labels. Metric-scoped same-publication policy proofs connect their normal neighboring accounting boundary; the 2023 tax/equity restatement does not authorize moving equity/tax or borrowing an earlier publication. FY2025 uses a resolved policy cross-reference from p83 to Important Matters p38 plus the separate first-adoption section. An intermediate replay exposed this format-related regression; it was not delivered. The CLI now requires all previously supported company/year records to survive before writing a new snapshot.

Final report: `data/fundamental_quant/liquor_financial_opening_bridge_20261018_final/index.html`. Common FY2022–25, 12 records/12 pairs, no unresolved incremental opening gaps; previous six records retain their numerical results. Evidence ledger has 536 entries: 488 original, 24 prior recovery entries and 24 new repo/CCE source/derived entries (9 derived constraints); 12 isolated Jan1 bindings and 4 policy-role proofs are separately preserved. Source residuals and unallocated finance-company costs/tax/eliminations remain disclosed: this is a partial identified-financial proxy, not complete industrial ROIC.

Actual full two-group longitudinal replay: `data/fundamental_quant/longitudinal_comparison_opening_bridge_20261018_verified/index.html`; deterministic checks passed with scope limits retained, no new model research. Final focused regression: 538 passed including `tests/validation/`; presentation-only title/source-page-label correction followed by 49 source/finance tests, actual CLI replay and scoped Ruff. Repeat run reused the final snapshot. Root integrity verified 55 protected files, 12 original PDFs, 39,684 local links/anchors and unchanged previous six records. No paid API, network, email, budget reset, commit or deployment.

Final independent source/Decimal audit passed 4,185 checks with no producer import and no errors: `data/fundamental_quant/liquor_financial_opening_bridge_20261018_final_independent_audit.json`. It binds the final receipt `e9e0bfaf7383cc567929fd577202c6db3937caa4d4caeb9b978a0e52216fda5d`, all 12 original PDFs, physical monetary columns, 9 derived zero proofs, four policy-role bridges and unchanged prior six numerical records. The intermediate FY2025 regression attempt remains preserved with a withdrawal sidecar; the numerical reconciliation has a presentation supersession sidecar. Final delivery manifest: `data/fundamental_quant/liquor_financial_opening_bridge_20261018_delivery.json`.

### 2026-10-18 — Financial cost, tax-stock and elimination attribution

Added `financial_attribution.py`, `financial_attribution_report.py`, and generic cached-evidence CLI `scripts/compare_financial_attribution.py`. Prior source packets, measurements, reports, notifications and fee ledgers remain unchanged. New source inventories preserve 437 literal/nonnumeric financial-cost/tax entries and 40 complete sections, plus eight associate source reviews/129 relevant pages and visual receipts. Permanent copies are `financial_source_evidence.json` and `associate_source_evidence.json` in the final report, not only temporary review files.

Only specifically identified signed financial-instrument credit P&L enters the incremental strip. MT2024 −22,397,217.30 and MT2025 +16,911,804.37 use the P&L sign, so their reference core adjustments have opposite direction. Qualified earlier “other mainly finance-company loans” remains unallocated; later reclassified comparative rows are not merged into older publications. Financial-associate Y is already based on net income and is removed once in full; issuer incremental tax is unknown, not zero. Investee expenses and dividends cannot be added a second time.

Tax stocks adjust capital only: one shared gross impairment-DTA allocation budget, matching fund DTL returned to capital only after testing its exclusion from original D bounds. FY2025 fund DTL closing is source explicit; blank opening is a 0-to-gross-DTL allocation range, never a raw zero. These are category-constrained outer envelopes and do not bound every financial tax balance. Actual tax flows, remaining costs/eliminations and capital retain null bounds where not identified; full accounting peer order is not asserted. Eight first-contact thresholds are net core earnings; the separate pretax-cost column requires fixed group ETR and only issuer consolidated costs.

Final actual replay `data/fundamental_quant/liquor_financial_cost_tax_20261018_final`: 12 FY2022–25 records, 12 pairs, 0 changed reference relations, 8 thresholds. 553 relevant tests including `tests/validation/` passed; after presentation-only unit/scope clarification, 15 targeted tests, scoped Ruff and actual CLI passed. Independent no-producer Decimal/source audit passed 2,584 checks, including four DTL debt-AST perturbations. Receipt: `liquor_financial_cost_tax_20261018_final_independent_audit.json`, final report receipt SHA `dd3edbe44017f36d1f3ae28ea8bda356d6ccd16b48e4895c402c8f06aaa5512e`. Integrity verified 61 protected files, 12 originals and 171 report links. No paid API, network, email, budget reset, commit or deployment. This remains offline accounting/sensitivity acceptance, not complete industrial ROIC or paid-agent validation.

### 2026-10-19 — First-contact thresholds versus disclosed group expense/tax scales

Added `financial_thresholds.py`, `financial_threshold_report.py`, and `scripts/compare_financial_thresholds.py`. Uses the prior immutable cost/tax comparison and 48 same-publication current source amounts (administration, current tax, signed deferred tax, total tax) from 12 FRs/24 complete sections. Lao Jiao2022 total-tax footnote superscript is preserved and separated only by verified PDF font/column geometry. No download/model/API or prior artifact mutation.

Group administration expense G is a scale comparator, never finance allocation or a finance upper bound; G*(1-g) is only a fixed-ETR unit conversion and is not added to core earnings. Investee expenses already in equity-method Y are not available again. Eight pretax contacts are 2.4588–22.6550 times the lower issuer group's G. FY2022 Maotai/Wuliangye is closest by this particular scale: net contact56.8693亿元, fixed-ETR pretax75.4382亿元, G30.6812亿元. All UI monetary tables explicitly use CNY亿元. No economic allocation follows from these scale multiples.

Explicit tax scenario applies T_F=0 to both companies' identified F, deltaN=-gF, retaining reference J tax shield and other residual conventions. It is not a worst-case bound or an actual zero-tax claim. Eight separated/four overlapping relations persist. Tax-only contacts T_F*=gF-down (higher) or gF+up (lower) preserve negative tax benefits, never infer a tax rate from partial F, and never allocate group current/deferred/total tax to finance. No new attributable reversal witness is supplied by group monetary context; full accounting order stays unidentified. Already-used credit and tax-capital corrections are not reused.

Actual frozen report: `data/fundamental_quant/liquor_threshold_context_20261019_verified/index.html`, with context evidence, full original pages, cost-scale bars, tax-only contact table and policy comparison. 569 relevant tests including `tests/validation/` passed; 16 final presentation/source tests and scoped Ruff passed. Regression includes issuer versus investee expenses, signed tax, current/comparative and parent/consolidated scope, missing/negative administration, stale ratios/contacts, and displayed higher/lower order versus original first/second labels. Repeat CLI reused snapshot. Root integrity: 67 protected files/12 originals unchanged and39 report links checked. Receipt SHA `d2838b3ebc18db3b2d7b3ecb2a6b68d765ef8c890310e757f83ce973ba05442e`. Paid API/network/email/deployment/budget changes0.

Final independent audit passed656 checks with errors[]: `data/fundamental_quant/liquor_threshold_context_20261019_verified_independent_audit.json`. No producer import; independently verified48 current source amounts (including Lao Jiao2022 superscript font/column geometry),8 cost contacts,16 implied financial-tax contacts,12 policy rows/pairs and four HTML tables with8/16/8/4 rows. The displayed higher/lower order is explicitly checked against original first/second orientation; report rounding2.46–22.66 matches independently computed2.4588–22.6550 scale multiples. Delivery manifest: `data/fundamental_quant/liquor_threshold_context_20261019_delivery.json`.

### 2026-10-19 — Stop economically immaterial source detail

The preceding ROIC-rank investigations did not answer whole-company valuation materiality. Added a configurable 5% upstream economic stop rule, `ignore` decisions, and concise downstream closure notes. No new financial classification investigation is required. A source-bound reviewer may close a bounded whole-equity-value effect below the line or a consolidated reclassification that preserves cash flows, net assets and the valuation method. The arithmetic uses the positive intrinsic-value lower bound, never a midpoint or market-cap substitution; multiple issue maxima are conservatively combined per company. New material evidence can reopen the scope.

Closure stops follow-up rather than manufacturing evidence: missing metrics remain `omitted_immaterial`, existing usable scope remains limited, and rejected facts stay quarantined. The research and independent-review prompts respect this upstream decision and focus on cash flow, value and price compensation instead of exact ROIC rank. Old frozen reports, source packets and paid ledgers remain unchanged. No paid API, download, notification, budget reset, commit or deployment is part of this change. See `docs/development/research_material_policy.md` for the contract and semantic-acceptance limits.

Validation: 259 focused regressions including `tests/validation/` passed; scoped Ruff passed. Read-only replay of the two existing real panels retained 4,570/2,786 accepted facts and 43/27 rows, respectively, with source packet hashes unchanged (`.tmp/materiality_actual_replay_20261019.json`). This compatibility replay does not invent any new economic materiality judgments or claim paid-agent acceptance.

### 2026-10-19 — Complete offline liquor cash-flow and valuation research

Delivered `data/fundamental_quant/liquor_cashflow_research_20261019/index.html`: three companies, FY2019–2025 plus 2026H1, using immutable existing materials and 2026-10-16 unadjusted cached prices. This is authored Codex offline research with a separate Codex context reviewing real evidence and independent arithmetic; it is not DeepSeek paid-pipeline acceptance. No API calls, downloads, email, budget reset, commit or deployment. The report leads with cash sustainability, scenario value and price opportunity, then long-term charts, MD&A evidence, bridges, full-capex normalization, historical PIT P/E, sensitivity and six conclusions/breakers. Earlier financial-tax and ROIC reports remain historical evidence, not publication blockers.

Added deterministic `cashflow_valuation.py`; economic assumptions stay in the authored report replay, not hardcoded financial facts in the library. All model monetary amounts use CNY100million (亿元): working normalized shareholder-cash proxies are 61/131/781亿元 for Lao Jiao/Wuliangye/Moutai. Working scenario values are about CNY75/66/1275 per share. Cash release removes foregone income before adding cash once; releases and growth are conditional assumptions, not payout rights or guaranteed recovery. Transition sensitivity changes only years one/two and gives about CNY70/60 for the first two companies. No midpoint of the three scenarios is used.

The Moutai two-principal-flow bridge is taken directly from source columns and is not a full finance carve-out: H1 comparable cash is roughly414 versus410亿元. Wuliangye regulatory-goods balances and the 2025 operating-payable increase are explicit, economically material cash risks. Full cash Capex remains deducted, and the minority Capex allocation is labelled a profit-ratio proxy. Forty-four focused calculation/validation tests and scoped Ruff passed. Independent real-report review is saved with the report; original snapshots and PDF hashes are preserved. Browser automation could not preview the local file URL under its protocol policy, so no browser visual-acceptance claim is made.

### 2026-10-19 — Chain inventory and reader-oriented report revision

Audited actual boundaries at `data/fundamental_quant/research_chain_audit_20261019/index.html` and `inventory.json`: 43 fixed FR fields, 7 accounting topics, 9 supplement categories; the real liquor panel contains 73 raw metric names and 27 company-period rows, with revenue/profit/CFO/Capex present in all rows. The 43 source files include24 research-window reports,18 historical valuation seeds and1 share snapshot; nested chapter/duplicate fact counts are not unique disclosure counts. Financial cache retains a fixed version at least365 days; prices use dated snapshots. Some historical balance-sheet values have existing valid source refs mixed with scoped-opening refs and are withheld in the general panel; this is a reference-selection integration issue, not proven non-disclosure.

The completed liquor report is explicitly an authored offline Codex case, not a successful autonomous DeepSeek run. Its economic assumptions and first prose were in the dedicated replay script; `cashflow_valuation.py` supplies arithmetic and is not yet a named scenario tool of the paid workflow. The generic workflow still embeds most research/review prompts in Python. Added configurable `config/research_report_style.md`, loaded into the actual research prompt through `report_style.py`; fresh sessions receive it and persisted sessions keep their frozen context. Financial/schema checks remain separate from soft writing guidance.

Delivered reader revision `liquor_cashflow_research_20261019_readable/index.html`, with authored prose in `article.json`. Model and source files are byte-identical to the earlier reviewed case;12 original data tables and7 charts are retained, six conclusions remain accessible, and estimates/timing/rights constraints are stated together. Independent limited editorial review retained substantive risks and requested two wording fixes: the cash-release timing and the partial financial-principal bridge scope. Both were applied. Ninety focused workflow/rendering/style/validation tests and scoped Ruff passed; no paid API, network retrieval, notification, budget reset, commit or deployment. Earlier reports remain unchanged.

### 2026-10-19 — Frozen research materials, independent Skill and shared Pi loop

Implemented the approved offline stage documented in `docs/development/research_retirement_20261024.md`（原 `fundamental_research_skill_pi.md` 已合并删除）. The final immutable baseline is `research_baseline_20261019_final`: 110 original PDF hashes checked, original facts/observations preserved, seven companies with seven FY CFO/basic-FCF/BVPS observations. Mixed valid closing and opening-only references are rebuilt from supported disclosed observations and dependent metrics recalculated; unsupported closing values and historical source-integrity rejections stay isolated.

Research/review/compaction/writing and industry methods now live under `skills/fundamental-research/`, loaded as exact content/hash snapshots. New prepared packets select the actual shared Pi Agent Core loop with native tools, separate persistent study/review histories, checkpoint recovery, 2 MiB frames/48 MiB logical messages and provider-owned budget/HTTP/receipts. Financial scenarios use reference-bound deterministic cash-flow, DCF, reverse-return, cash-release and recovery tools. New source needs queue upstream while current inputs remain frozen. Partial result revisions retain unaffected chapters/charts/bridges; publication binds the method manifests and continues to share one approved HTML/Bot/email payload.

The report renderer uses question/short-answer chapters, inline long-period charts, concrete risk links and a compact six-conclusion index. `research_skill_examples_20261019_final` contains two real-panel, unreviewed presentation templates, without new investment conclusions. `accept_fundamental_research_offline.py` reproduces the offline material/control checks with real Pi subprocesses and synthetic provider fixtures, network guards, JUnit and version receipts. These fixtures establish execution controls, not paid autonomous research quality. Paid calls, downloads, notifications, budget resets, commits and deployment remain disabled; final autonomous two-industry report acceptance needs separate authorized budget.

Offline acceptance receipt: `data/fundamental_quant/research_skill_pi_acceptance_20261019_final/validation.json` reports279 passed, zero failures/errors/skips, with Python network connections blocked and unchanged frozen input/runtime code hashes. Actual Node bridge tests:17 passed/1 optional schema integration skipped. A separate explicitly configured compatible-Node run of the unchanged Feishu Python harness passed9/9 (`legacy_feishu.xml`). Scoped Ruff and focused `git diff --check` passed. The double-industry presentation examples passed95,046 local link existence checks. These are control, data-replay and static-link checks; no browser visual or autonomous model-research acceptance is claimed.


### 2026-10-19 Pi real liquor research trial (bounded, not accepted)

- Authorization `fq-pi-peer-20261019-111946`: 60 calls, 20M input reservations, 2M output reservations, 90 minutes; original ledger and expiry preserved. Paid execution is stopped, campaign marked blocked and heartbeat automation paused. Construction was not started.
- Actual provider receipts: 32 calls, 17,235,600 input, 15,603,456 cache-hit input, 109,303 output tokens; no unknown usage calls. Returned usage priced at the frozen official off-peak tariff is CNY 2.38142512, not a provider invoice. Account balance observations are account-wide and are stored separately.
- DeepSeek produced seven complete chapters, scenarios and six claims, then an independent Pi review returned `revise`: three material and four minor issues. Issues include a factor-ten prose unit error, unsupported industry figures and mismatched ROE reference periods. No approved result or formal report was published; original draft is retained with the unresolved review warning above it. HTML failure notification received `sent` acknowledgement.
- Preflight projected the huge duplicated audit payload into a financial panel without deleting original chapters. Added source-verified frozen market records, lossless AST/wire-shape compatibility, campaign-aligned Pi limits, explicit incomplete-output diagnostics, and a higher independent-review output cap. A completed receipt recovered after checkpoint interruption was replayed without a duplicate model charge.
- Independent review continuation now preserves its original evidence prefix and reviewer messages, appends changed evidence and a result patch instead of duplicating the full panel. The research budget precheck separately sizes the actual reviewer session and reserves correction output space. Offline measurement still could not safely fit correction plus review within the remaining 1,330,239 input reservations, so no further paid request was sent.
- Focused protocol/transport/report/response regressions plus `tests/validation/`: 83 passed; scoped Ruff passed. Initial pytest default temp directory access failed; rerun used workspace `.tmp/pytest-pi-final-20261019a`. These checks do not establish research acceptance.
- Delivery: `data/fundamental_quant/pi_peer_research_20261019-111946/delivery.html`; full unapproved draft and review issues under `liquor/index.html`; per-call fees `costs.html`, ledger closure `final_receipt.json`. Research correction/re-review/publication remains unaccepted.

Additional context projection, verified market-record and calculation-contract regressions: 23 passed (106 targeted checks total across the two runs).


### 2026-10-19 explicitly authorized liquor continuation (no formal publication)

- `fq-pi-liquor-continuation-20261019-131604`, 21:16:04-21:46:04 Asia/Shanghai: 6 calls / 5M input reservations / 300K output reservations. Exact parent checkpoint and all study/reviewer histories were reused at sequence 32; parent financial packet, checkpoint and ledger remain unchanged. Construction excluded.
- Actual 6 calls: input 3,774,319; cache-hit input 3,138,688; output 43,158; frozen-tariff cost CNY 0.87103676. Account balance observation decreased CNY 0.87. Parent plus continuation: 38 calls, 21,009,919 input, 152,461 output; tariff cost CNY 3.25246188. No pending/unknown usage.
- Reviewer returned pass with three annotations, then final targeted review returned revise with 9 claim/axis checks pass. Last two issues were chapter base-DCF field names and stale ROE period references. They have been repaired offline exactly from existing records, with prose, economic parameters and valuation numbers unchanged. No final global pass was fabricated; corrected draft remains pending independent recheck. Authorization cap reached, paid execution stopped, campaign blocked, heartbeat paused; HTML terminal notification received sent acknowledgement.
- Fixed lossless response compatibility for partial revisions; replayed the already paid response without another provider request. Added missing chapter `calc_refs` validation. Rejecting reviews may identify a nonexistent submitted reference when also grounded in an existing source; this does not allow an invented source or a pass with unresolved material issues.
- Fixed baseline freezing to recompute embedded packet identity after reconstruction/runtime annotations. For this already authorized frozen legacy input, publication snapshot adapter can repair only the embedded metadata digest after checking the exact full input digest against its original authorization; source files, facts, methods and reviewed result remain untouched. Strict publication checks remain. No report was published in this continuation.
- Scoped regression plus validation: 87 passed; Ruff passed. Real generated draft has all seven chapters, a pending-review warning and no approved_result. These checks are not independent research acceptance.
- Deliverables: `data/fundamental_quant/pi_peer_research_20261019-111946/continuation_20261019-131604/delivery.html`, `liquor/index.html`, `costs.html`, `offline_reference_repair.json`, `publication_metadata_preflight.json`, `final_receipt.json`.


### 2026-10-19 independent offline reference closure and formal delivery

- User explicitly authorized an independent sub-agent to close only the two reference issues offline, reuse all results, make no paid API calls, and publish/email after pass; construction excluded. `/root/offline_reference_acceptor` independently verified final result `67fe63ef978110fdef6023699aa0e6f5fb7d65777df02546d39e3f30bb57b9c6`: 8 ROE calculations, 20 original-PDF input rows/source hashes and three base/cash-release DCF pairs. Two historical comparator refs were added without changing prose or economic inputs; reviewer rechecked the final hash.
- Independent artifact: `offline_subagent_review.json` SHA256 `f1888ae7d9796ea11a5a0b1359ab23dc49765b652d211e94b1f2af6506da903b`; verdict pass, source issues 0/1 closed. Original DeepSeek revise and nine semantic checks remain immutable. New acceptance method explicitly combines inherited DeepSeek semantic checks with separate-context Codex reference-only closure, not a new DeepSeek pass.
- `offline_reference_review.py` binds explicit user scope, source/result/packet hashes, reviewer task/artifact, original semantic review and unchanged prose/economic content. Release rejects missing authorization, changed prose/claims, unclosed source issues, mismatched artifact or reviewer, failed inherited checks and a bare independence label. Authorized metadata-only packet identity normalization now also recomputes its publication review-version binding.
- Formal publication `458fff9dcfce9000aabe1ab485f26054527b63e0b9ef162f442b610efe902de0`: HTML/Bot/email share approved result and publication ID. Full seven-chapter paragraph parity checked. Readable report uses local evidence sidecars; archival self-contained HTML preserved. Delivery page now links the formal report and scope review. Email outbox `7e80765f06fc22f901017c7c3edc2ec17bcfd64ce73c2697fa31fc9aec1b5b85` is delivered with SMTP accepted; notification dispatch scoped to this publication, leaving unrelated pending outbox items untouched.
- New paid calls: 0; all six continuation API call-state hashes and original parent checkpoint hash unchanged. Original two paid rounds remain 38 calls / CNY 3.25246188 known usage tariff total. Paid budgets remain closed and follow-up automation paused. Construction not run.
- Scoped release/report/chapter regression and `tests/validation/`: 82 passed, Ruff passed. Publication preflight empty errors; formal release used actual frozen evidence plus actual independent offline review, not mocked review output. No commit, push or deployment performed.

### 2026-10-20 calculation-bound independent reference review (offline)

- New Pi and artifact-inbox reference review jobs freeze `calculation-bound-review/1` in their request and authorization. Review explanations use `{{calc:exact-calculation-id}}`; Python resolves the existing calculation value and unit, retains company/period/currency plus record hash, and formats the display. Reviewers no longer recopy amounts or choose display conversions. The method stays in the independently frozen review skills.
- Receipt import rejects unbound numeric literals, malformed placeholders and extra numeric display fields. Consumption checks exact calculation existence, declared refs, quarantine status, finite numeric type and unit before accepting either a passing or rejecting receipt. Failures remain visible and do not trigger another provider request. No alias guessing or midpoint substitution is introduced.
- Raw model output, executor receipts and historical publications remain unchanged. The accepted review gains a separate `reference_review_view`; release recomputes that view against its bound evidence and rejects changed values or metadata. The original nine semantic checks remain inherited rather than relabeled as a new semantic review. Legacy frozen jobs retain their original contract.
- Regression evidence is the actual `pi_liquor_e2e_20261019-152921` review explanation that copied `1309051872009855500`, alongside the actual `scenario_moutai_base_v3.value` calculation `1309051872009.8555` CNY. The new contract rejects the copied literal; a bound reference renders approximately CNY 1.309 trillion. A different issuer ID exercises the Pi loop, receipt consumption, HTML and restart path with offline native receipts, without rewriting the raw response. Additional checks cover missing/wrong-field refs, unlisted refs, isolated inputs, invalid values, missing units, negative results and tampered presentation.
- This change performs no paid API calls, no construction research, no email delivery and no historical publication/ledger rewrite. Offline replay verifies control behavior; it is not a new paid model research acceptance.
- Verification: combined Pi dispatch/inbox/release/report/skill regressions and `tests/validation/` passed 146 checks. Final numeric-only suite passed 24 checks after adding Chinese financial-literal cases. Scoped Ruff, skill package validation and documentation diff checks passed.

### 2026-10-20 real-data offline liquor end-to-end replay

- Reused the approved seven-chapter liquor result `67fe63ef978110fdef6023699aa0e6f5fb7d65777df02546d39e3f30bb57b9c6`, frozen source review and calculations from `pi_liquor_e2e_20261019-152921`. No fresh research, paid provider request, construction study, production publication replacement or SMTP delivery was performed. Replay output and its publication database are isolated under `offline_liquor_e2e_20261020`.
- A separate-context Codex acceptor checked only public frozen inputs, reconciled source facts and ROE/DCF calculations, and closed the two scoped reference issues. The old semantic review remains inherited. The importer detected a reviewer-role metadata mismatch; the acceptor archived that first receipt and corrected identity metadata without changing conclusions. Actual executor identity remains in the verification artifact.
- Real replay found the scoped projection omitted a newer release scenario used by a numeric assertion. The generic projection now includes all declared numerical assertions and follows calculation, estimate and fact dependencies to original pages. Projection version participates in job identity; a new frozen job was created and independently checked, leaving the prior frozen job unchanged. Regression covers an issuer-independent newer scenario outside chapter calc_refs.
- Report and email now use the same recomputed review component and numerical index. Acceptance parses the actual HTML outputs, verifies all seven chapters, all 32 indexed records against source values/units/hashes, reviewed visible explanations, and both-channel consistency. Identically wrong visible text in both channels is also rejected. Rendering blocks stale/missing review views rather than displaying cached copied values.
- `scripts/accept_reference_review_offline.py` consumes the independent receipt and exercises release, notification projection and repeat consumption with network connections blocked. Email transport captures exact HTML locally and explicitly returns `skipped/offline_capture_no_smtp`; isolated outbox pending status does not represent SMTP acceptance. Re-reading captures preserves original CRLF bytes for exact restart comparison. Only the old immutable packet authorization binding is copied into the isolated store; no runnable campaign or fresh token budget is created.
- Actual replay: offline accepted, one local mail capture, no duplicate capture on repeated consumption, zero network attempts or paid input/output, unchanged approved result and 17 source-file hashes. Original paid usage is retained as historical usage. Deliverables include full HTML, email preview, independent review/source evidence and machine-readable acceptance.
- Restart acceptance also exposed the local capture writing a JSON-encoded string to an HTML filename. Capture now writes actual UTF-8 HTML bytes and acceptance reads the disk artifact. The failed capture is retained; it was decoded and rewritten only after exact comparison with the immutable same-version local email archive. No SMTP message was sent. A capture/restart regression checks real HTML file bytes, CRLF preservation and no repeated capture. Both-channel visible errors are compared with the bound review and source index, not merely with each other.
- Combined regression and `tests/validation/`: 147 passed; final capture/visible-number parity suite: 9 passed. Scoped Ruff and diff checks passed. These observations establish offline data/receipt/render/capture behavior, not a new DeepSeek run or mailbox-delivery acceptance.

### 2026-10-20 explicit formal publication and actual HTML email

- User explicitly authorized publishing and emailing the accepted offline liquor version, reusing existing results with no paid API or construction run. Promoted the same approved payload to the main `data/fundamental_quant/research.sqlite3` publication store: version `646e6f3a4a7a81b35d8874b2c9c16e01997e4d6fe059d506f2f38c60aaa49b8a`; approved result hash remains `67fe63ef978110fdef6023699aa0e6f5fb7d65777df02546d39e3f30bb57b9c6`.
- Initial strict byte check rejected email before any SMTP attempt: canonical database hydration reordered keys in the historical usage JSON appendix. The seven chapters and all 32 numeric records were identical. Preserved the failed pre-transport receipt, verified the formal archive against the accepted content, and requeued only that proven unsent notification. No uncertain SMTP attempt was retried.
- Real HTML mail completed one SMTP attempt: `smtp_accepted=true`, Message-ID `<179243147674.34564.4682396849729589558@DESKTOP-GPKM5TC>`, main outbox `delivered`. Actual mail equals the formal publication email archive; report, Bot projection and email retain the same research payload/version. Independent recipient-inbox arrival was not checked.
- Additional paid API/input/output: zero. Existing 17 source-file hashes are unchanged; historical budgets and paid receipts were not reset or resumed. Delivery artifacts: `data/fundamental_quant/liquor_published_20261020/delivery.html`, `index.html`, `sent_email.html`, `delivery_receipt.json`, `smtp_receipt.json` and preserved pre-SMTP rejection evidence. The original offline acceptance summary now links to this separately authorized formal delivery.

### 2026-10-20 automatic JSON-order-safe publication checks

- Fixed the publication boundary rather than weakening amount/body checks: `publish_group_report` renders the canonical payload that is actually persisted. Caller object insertion order no longer changes a new report or email. Historical published HTML, sent mail, snapshots and fees remain immutable.
- Added runtime-owned `publication_consistency.py`, shared by the automatic release/notification path and the offline acceptance tool. Strict JSON objects/lists inside audit `<pre>` blocks are compared canonically; only object-key ordering is ignored. Array order, values, keys, statuses and ordinary prose are preserved. Duplicate keys, malformed/nonfinite JSON cannot be normalized into a valid match.
- Publishing validates actual report and email against the approved projection. Before notification claim/SMTP, the dispatcher validates the archived outputs again, including the approved body and bound numeric/source index. Genuine mismatches are recorded as `blocked` with `stage=pre_transport_validation`, `smtp_attempted=false` and an event. They never become ambiguous transport failures. Existing `sending`/`unknown` transport outcomes remain non-retryable without evidence; no auto-reset of uncertain deliveries was added.
- Moved chapter/numeric HTML checks from the acceptance script into the runtime module; script import compatibility remains. Empty legacy reference-only numerical inventories remain empty rather than being treated as missing declared values; real declared inventories still require exact coverage, metadata and visible-value checks.
- The actual failing pair is frozen as `data/retired_test_fixtures_20261030/fundamental_quant/publication_json_order.json`. Tests cover reordered/nested objects, changed input/caller order, persisted-state reload, genuine prose/audit changes before SMTP, duplicate/nonfinite JSON, array changes and send deduplication. The existing reference, Pi, body and numeric regressions remain applicable.
- Real offline replay used the accepted liquor payload plus the genuinely different preview/sent audit order in a separate store. Automatic dispatch accepted it without a repair/requeue, captured one local mail, preserved seven chapters and 32 numeric records, and blocked injected amount/prose changes. No SMTP, network or paid API calls; construction excluded. Evidence: `data/fundamental_quant/publication_order_acceptance_20261020/acceptance.json`; original preview and sent-mail hashes unchanged.
- Verification: 14 new order/publication regressions; combined regression and `tests/validation/` 163 passed. Scoped Ruff, formatting and diff/whitespace checks passed. The runtime now owns these checks; the one-off historical publication script remains an audit artifact rather than a required recovery step.

### 2026-10-20 construction real-material offline end-to-end acceptance

- User authorized construction with existing materials, independent review and email preview only; no paid API, download, SMTP or production promotion. Existing construction examples were non-final chapter templates, not previously accepted research. Reused the frozen `research_baseline_20261019_verified/construction` panel and originals; Codex researcher and separate-context Codex acceptor produced and checked substantive industry research without claiming a fresh Pi/DeepSeek run.
- Deliverables: `data/fundamental_quant/offline_construction_e2e_20261020/index.html`, seven-question report under `construction/index.html`, `email_preview.html`, raw research/review artifacts, reproducible calculations and `acceptance.json`. Final result digest `3df2bf4af7681d327b900c737c3cd39dd64b6feaf054297fa243f599f6c9b149`; isolated publication version `99c04571d3b742e354944f2adcf7899c654fcb6d780980d612982609d8c03031`. Original liquor publication and paid ledgers were not touched.
- Research covers Sichuan Road & Bridge, Shandong Road & Bridge, Anhui Construction and CCCC: seven substantive chapters, twelve bridges/chart declarations, actual coverage and cached quote date. The independent reviewer checked all nine axes/claims, recomputed 353 calculations and matched eight recent original annual/interim PDF hashes. Local corrections included noncurrent PPP/retention exposure for both Anhui and CCCC, fixed HKD/CNY 0.9, and cash-interest/priority-distribution boundaries. CFO minus full cash Capex and profit-share attribution remain financing-before-distribution proxies; negative cash is retained. Acceptance supports operating comparison, cash constraints and conditional reverse-price requirements, not proven FCFE, precise ROIC ranking or unconditional target prices.
- Generalized calculation-bound review presentation to full semantic reviews: raw reviewer receipt remains unchanged, its adapter/hash and independently derived numeric view are checked. Missing/tampered views and unbound copied numbers fail closed. Legacy review contracts retain their original path. Fixed chapter-local monthly valuation charts to read frozen historical valuation rows, preserve missing points and precision intervals, and use the same plot in HTML/email.
- Actual offline replay consumed the independent receipt, used the production publication/notification projection checks in an isolated database, checked on-disk seven-chapter HTML and all 217 numeric records against email, and captured one local preview. Repeat publication/read/dispatch did not duplicate capture. Network and SMTP connections were forbidden; receipt explicitly records `skipped/offline_capture_no_smtp`, never delivered. New paid calls/fee and SMTP attempts are zero; Codex subtask usage is not metered by the DeepSeek ledger.
- Replay first caught nested bridge/index JSON key-order differences between caller objects and canonical persisted state. Preserved `projection_diagnosis.json`; replay now renders and validates every projection from the same stored approved snapshot, without loosening ordinary text/numeric checks. Original embedded packet digest was stale: a derived `publication_packet.json` corrects only `packet_hash`; independent source comparison confirms facts, rows and chapters unchanged, and all baseline source files remain byte-identical.
- Final targeted release/review/Pi-protocol/chapter/sidecar regressions plus `tests/validation/`: 186 passed. Scoped Ruff and whitespace checks passed. This acceptance verifies actual offline research, independent-context artifact consumption, report and local mail preview; native Pi provider execution and recipient delivery are explicitly outside this run. No commit, push or deployment requested or performed.

### 2026-10-20 readable summary and explicit visual-acceptance limit

- The user's screenshot exposed a real presentation failure: summary cards displayed hundreds of internal reference IDs and overflowing PDF IDs, while English status codes obscured their meaning. Previous acceptance established financial/content consistency, not readable browser layout; these boundaries are now explicit.
- Shared renderer keeps the actual axis conclusion visible, moves counterevidence/limitations/sources into closed disclosures, uses readable company/period/page or fact/calculation labels while retaining exact audit IDs and original anchors, translates conclusion status codes, and constrains grid children/links to wrap rather than clip. The seven chapters, economic assumptions, numbers, research result and independent review are unchanged. Report/email use the same renderer.
- Frozen prior presentation is retained in `offline_construction_e2e_20261020/presentation_history/99c04571d3b742e354944f2adcf7899c654fcb6d780980d612982609d8c03031/`. New isolated presentation version `18be302c6c2baa3bda535dbb767a7e69d329d25d1f7dbe4acc3bb74aa3bcecc1` retains original result digest `3df2bf4af7681d327b900c737c3cd39dd64b6feaf054297fa243f599f6c9b149`. Actual replay preserves seven chapters, 217 bound numeric records, one local capture and deduplication; no paid provider or SMTP request.
- Regressions cover the long-ID screenshot case, folded default source panels, readable source/status labels, immutable study payload and risk-only source audit availability. Combined suite plus `tests/validation/`: 187 passed; scoped Ruff and whitespace checks passed. `check_presentation.py` separately verifies both actual report/email files contain three closed summary source panels and no direct source links/raw IDs in the default summary; `presentation_acceptance.json` records actual file hashes and unchanged research/review.
- Actual browser screenshot/responsive geometry checks remain unverified: browser security rejected the `file://` URL. No localhost/protocol/alternate-browser workaround was attempted. The receipt records `browser_screenshot_verified=false`; static markup/CSS and content tests are not represented as visual acceptance. Production liquor publication and historical provider ledgers remain unchanged.

### 2026-10-20 actual desktop/narrow report and mail-preview visual acceptance

- User explicitly requested actual visual checks for both existing HTML documents at desktop and narrow widths, without paid APIs or email delivery. File-scheme browser access remains prohibited. Used the browser-supported local-development HTTP path with a materially narrower read-only preview: bind only `127.0.0.1`, whitelist exactly two HTML routes, no directory handler, no config/ledger/original-file routes, CSP disabling scripts/forms/outbound connections. Actual HTTP payloads and source SHA256 match the two HTML files byte for byte; denied routes return 404. The helper was stopped and temporary browser tabs/viewport overrides cleaned up after checks.
- Real screenshots exposed two defects missed by markup tests: a later `main` max-width rule overrode the report's intended reading width; mobile SVG scaling reduced axis labels to roughly five pixels. Shared renderer now preserves report max-width 1240px and email max-width 900px; figures use focusable, locally scrollable SVG regions with minimum readable width and a narrow-screen scroll hint. Legends remain outside the scrolling SVG. No financial assumptions, numbers, claims, source input or raw independent review changed.
- Actual in-app browser cases: report and email at 1440x1000 and 390x844 requested CSS viewports; document client widths 1425/375 after scrollbar reservation. All four have zero document-level horizontal overflow, correct three-column/one-column summaries and three default-closed source disclosures. Expanded source disclosures preserve width. Actual chart scrolling exposes the final year; narrow labels measure eleven effective pixels. All seven chapter links were clicked, browser-visible chapter text matches report/email, and the expanded mail audit table contains 217 numeric records without widening the page.
- Ten original, unedited JPEG screenshots plus browser geometry, exact output hashes and source payload checks are archived under `offline_construction_e2e_20261020/visual_acceptance_20261020/`. Gallery `index.html` links originals and `visual_acceptance.json`. New isolated presentation version `8ecfc37c95a8cc3bcf1efc8b98cf1271a9750ec05c3f8120c3afdc71ec5b78bc`; actual report/email and numeric parity replay passed. Previous presentation and false/unverified visual receipt remain in presentation history.
- `check_presentation.py` consumes the actual visual receipt only while its output hashes and original screenshot hashes match. A changed rendering invalidates that receipt instead of inheriting visual pass from static tests. Current `presentation_acceptance.json` records `browser_screenshot_verified=true`, unchanged study/review and 188 passing scoped regressions including `tests/validation/`. Scoped Ruff and diff checks passed.
- This is browser HTML visual acceptance, not Gmail/Outlook rendering or recipient-delivery acceptance. No paid API calls, no SMTP attempts, no production promotion, commit or deployment. Temporary preview process and browser tabs are closed; the local file report/mail and screenshot gallery remain available.

### 2026-10-20 authorized construction formal publication and HTML delivery

- User explicitly authorized publishing and emailing the accepted construction report using existing research, independent review and browser visual acceptance, with no paid API calls. Published version `8ecfc37c95a8cc3bcf1efc8b98cf1271a9750ec05c3f8120c3afdc71ec5b78bc` into the main research store; approved study digest remains `3df2bf4af7681d327b900c737c3cd39dd64b6feaf054297fa243f599f6c9b149`. Existing liquor publications remain unchanged.
- Publication preflight rehashed frozen source inputs, reviewed primary PDFs, independent review evidence, accepted report/email and all ten visual screenshots. Formal report and sent email are byte-identical to the visually accepted documents. Seven chapters and 217 bound numeric records match the report, mail and Bot projection. HTTP/model requests were forbidden during this publication; no new paid calls, input/output tokens or fees.
- One HTML email was accepted by the configured SMTP server; scoped main-store publication outbox is `delivered`. Message-ID `<179250128541.38260.8250256393360554545@DESKTOP-GPKM5TC>` is preserved in `construction_published_20261020/smtp_receipt.json`. Exclusive persistent attempt creation prevents automatic duplicate delivery after an ambiguous outcome. No prior isolated skipped receipt was promoted or reset.
- Delivery entry: `data/fundamental_quant/construction_published_20261020/delivery.html`; complete readable report `index.html`, exact sent body `sent_email.html`, authorization and publication/SMTP receipts retained. The original offline acceptance records remain historical; delivery page distinguishes their preview-stage wording from this real send. SMTP acceptance does not independently verify inbox arrival or Gmail/Outlook rendering. Existing product code was reused; `tests/validation/`: 36 passed. No new research, commit, push or deployment.

### 2026-10-20 reader email redesign preview after readability complaint

- Inspection of the actual sent construction HTML found 1,170,424 bytes, 564,881 text characters, 1,381 table rows, 12 inline SVGs, 13 disclosures and a JSON preformatted audit. Browser-responsive acceptance did not establish that this oversized research/audit projection was a useful email. Existing formal publication and its exact sent archive remain immutable.
- Added reusable `research_email_digest.py`: copy the approved summary/question answers and company-specific triggers without inventing new prose, resolve explicitly selected comparison values from approved numeric assertions and CNY calculation records, preserve units/periods, reject unreviewed results, mixed comparison periods and mismatched values. Mail HTML uses a narrow single-column presentation table and inline styles, no head stylesheet, SVG, script, disclosure, raw evidence catalogue or audit JSON. Excess content fails explicitly rather than being silently truncated. No local/file URL is presented as a working recipient link; an accessible HTTPS report URL is required for an email CTA.
- Real frozen construction replay produced 8,895-byte HTML and 1,587 text characters with the seven exact approved short answers and eight source-bound amounts. The cumulative cash comparison is explicitly CFO minus full cash Capex, not ordinary-share distributable cash. Its definition and accounting scope caveat remain visible. The full formal report is linked from the local preview landing page.
- Actual browser desktop and 390px narrow preview plus head-removal compatibility preview have zero document horizontal overflow; narrow table width 319px inside a 375px client viewport and body font 16px. Four unedited screenshots and source hashes are bound in `construction_email_redesign_20261020/preview_receipt.json`. Preview server stopped, temporary tab closed and viewport reset. This is not Gmail/Outlook inbox/client acceptance.
- Digest and existing publication consistency plus `tests/validation/`: 64 passed; scoped Ruff passed. First test attempt hit the Windows default pytest-temp access restriction; the exact suites passed using a workspace-local unique `--basetemp`. No paid API, no SMTP, no budget changes or publication replacement. This is a separate reader preview, not a silent migration of immutable historical mail projections; the automatic publication template has not yet been switched.

### 2026-10-20 automatic reader publication and authorized construction resend

- User explicitly authorized integrating the reader template into automatic publication and sending one engineering-group email, preserving the complete-report entry and making no paid API calls. New publications default to frozen `email_presentation` format `research_digest/1`; exact historic publications without a format retain their stored legacy projection. Restarting an old research checkpoint after a presentation-only migration resumes the current presentation rather than reverting or duplicating it. Unsupported formats fail explicitly.
- New reader publication `0415f8bc7e662bb9887bcce07b5332248ad8747e0dda1d2ced023cfb181f8a1c` preserves approved result `3df2bf4af7681d327b900c737c3cd39dd64b6feaf054297fa243f599f6c9b149`. Only email presentation changed. The original main-store report HTML and liquor publications remain unchanged. Presentation-only supersession retains old files and avoids false financial-correction notifications; real changes still use the existing withdrawal/correction controls.
- Mail is 9,001 UTF-8 bytes. With no configured recipient-accessible HTTPS report URL, the automatic notification adapter attaches `full-research-report.html` (657,962 bytes): complete approved prose, all seven chapters, twelve figures, independent review and 217 bound numeric rows. Original raw financial documents and the full extraction audit remain in the project report. Internal chapter/chart navigation is retained; unavailable source-appendix anchors become plain readable labels. The attachment is not a claim that all raw source documents were emailed.
- Reader body is rebuilt from the approved result and source-bound numeric records before claiming the outbox. Actual report body and numeric-review projection are checked against the frozen inputs; the portable attachment's actual chapter/numeric text is compared with the full report. EmailNotifier supports bounded HTML attachments via a mixed MIME container while leaving legacy callers unchanged. Outbound MIME was parsed immediately before real SMTP send: decoded body and the one attachment exactly match the frozen hashes.
- One HTML summary email with the complete research attachment was accepted by SMTP, Message-ID `<179250644937.24824.6518265527188440463@DESKTOP-GPKM5TC>`; the main-store scoped outbox is `delivered`. No duplicate resend or spurious financial correction was issued. HTTP/model requests were blocked in both prepare/send phases. No new paid calls, tokens or fees; prior budgets unchanged. SMTP acceptance does not establish inbox arrival or Gmail/Outlook layout.
- Regression suite: reader/default dispatch, portable navigation, true MIME serialization, tampered-body blocking, legacy JSON-order validation, historic-format migration/resume, release/review/report/sidecar controls, notification switches and `tests/validation/`: 137 passed. Scoped Ruff and whitespace checks passed. Delivery, exact sent body, attachment, original outbound MIME and receipts: `data/fundamental_quant/construction_reader_mail_published_20261020/delivery.html`. This connects the workspace's automatic publication path; no server deployment, commit or push was requested or performed.

### 2026-10-20 PDF attachment correction after actual recipient rendering failure

- User's Gmail screenshot showed HTML attachment source code rather than a rendered report. Prior content and MIME equality checks did not establish recipient readability; that acceptance gap is explicitly acknowledged. New publications now use `research_digest/2` with `full-research-report.pdf`; legacy HTML presentation snapshots remain historical. This change makes no new investment judgments or provider calls.
- Added deterministic ReportLab PDF renderer with an embedded Chinese TrueType font and direct vector conversion of the publication's SVG charts. All seven chapters, their full paragraphs, six conclusions, bridges, review and 217 numeric records are retained. Chart titles and colored legends are explicitly rendered; monthly intermediate labels that would collide with the final endpoint are omitted without changing any series data. Raw bridge dictionaries are presented as their value/unit and unchanged interpretation rather than JSON source IDs. No external image/font requests or HTML execution is needed by the recipient.
- Validation checks the matching research/review hash, complete extracted narrative and displayed numeric strings (450 required items), actual embedded TrueType streams, all 12 vector-chart placements within page bounds, nonblank pages, PDF format and size. A real false positive from footer text inserted between parts of a cross-page paragraph was repaired with a position-and-exact-label footer exclusion; financial numbers are not globally stripped or matched with relaxed tolerances. Regression covers long paragraphs across pages, deterministic bytes, missing/remote charts, unreviewed changes and overlapping month labels.
- Final artifact: 30 pages, 613,605 bytes, PDF SHA256 `a8d3a78177a8c4f9d06db74460833366ca88dd3e5669a73bd8568a26f01e47f9`. All 30 pages rendered with Poppler; full contact sheets plus enlarged cover/chart/cash-flow/valuation/bridge/table pages were inspected. Exact per-page PNG hashes and the reviewed PDF hash are recorded in `construction_pdf_mail_20261020/pdf_visual_acceptance.json`. Earlier failed and intermediate renders are retained separately. The email was held until this file-specific visual receipt passed.
- Current construction presentation version `9912d5e44f77ea7b5ac070bbc21a6cb51f7065b8454a6ff8ec03661d22e5ab2e` retains approved result `3df2bf4af7681d327b900c737c3cd39dd64b6feaf054297fa243f599f6c9b149`. One corrected PDF email was accepted by SMTP, Message-ID `<179250802520.42484.5987731113110396106@DESKTOP-GPKM5TC>`; main outbox is `delivered`. Immediately before real send, the actual MIME was parsed and the single `application/pdf` attachment compared byte-for-byte with the visually inspected PDF. The saved outbound MIME was independently rechecked after send. Automatic receipts now retain attachment filename, MIME type, hash, bytes, page and chart count. A non-PDF payload labeled PDF is rejected before SMTP.
- Delivery page, stable PDF, actual rendered pages, exact sent body/MIME, generation/visual/SMTP receipts: `data/fundamental_quant/construction_pdf_mail_20261020/`. No paid API calls, tokens, fees or quota changes; no commit/deployment. SMTP acceptance remains distinct from inbox arrival and Gmail's actual PDF preview, which has not been independently observed.
- PDF dependencies added to requirements: ReportLab, svglib and pypdf. On this host they were installed into a workspace-isolated `.runtime/fundamental-pdf-deps`, preserving the project's base/Pydantic environment; the first slow dependency download was stopped and the bounded install completed. Production needs an embeddable Chinese TrueType font (`RESEARCH_PDF_FONT` override or supported system path); dependency/font failures are explicit and never fall back to sending HTML. Current Windows glyph and layout acceptance does not certify an untested server font environment. Scoped suites plus `tests/validation/`: 142 passed; scoped Ruff and whitespace checks passed.


### 2026-10-21 underwriting-first research and report contract

- User identified a substantive omission in the construction report: the opening gives broad operating judgments, while reverse cash requirements stand in for an evidence-based estimate of normal ordinary-share cash and its return at the quoted price. The frozen result actually uses recent FY/TTM earnings anchors and a financing-before-distribution cash proxy; it does not complete normalized ordinary-share FCF/ROE, an underwritten value range, or a systematic management-statements versus peer/outcome comparison. A negative evidence axis does not establish that the stock is unattractive. Existing report, approval, sent MIME and publication versions remain unchanged.
- Added `skills/fundamental-research/references/underwriting.md`, loaded into research/review/writing snapshots. Method: preliminary normalized cash/ROE and price-return thesis, counterevidence and common-accounting peer checks, cross-company management statements and realized outcomes, then revised scenario returns and an explicit decision. Seven questions become supporting investigations. Sub-5% bounded valuation differences retain the existing materiality treatment; uncertainty is not a reason to reopen exhaustive classification or fabricate a midpoint. Frozen sessions keep their original method; the update does not silently rewrite or resume them.
- Added optional versioned `underwriting/1` output and a shared reference-bound reader projection for HTML, PDF and mail. Each company records a reviewed decision, dated frozen price, normalizing rationale, calculated ranges, scenario assumptions/results, peer and management assessment, risks and reconsideration conditions. Numeric cells and hurdle resolve from calculation references already bound in numeric_assertions; currency/period/company mismatches, reversed ranges and unresolved source conflicts are rejected. No history-length, scenario-count, positive-cash, or 7% investment screening gate was added. Missing estimates may be explained as unresolved; code does not promote or reject companies from prose or numeric thresholds.
- Independent semantic review must additionally cover each submitted company decision (`underwriting:<issuer_id>`); a new financial judgment cannot inherit only the old nine checks. Historical results without the sheet still render with their existing wording and review coverage. Re-rendering old results does not complete the missing research or confer approval on new assumptions.
- Offline regressions cover range preservation including negative cash, short history, estimate-versus-required-return separation, dated prices, mismatched currency/period/company, source conflicts, immutable projection, legacy compatibility, new decision-review coverage, and HTML/mail/PDF numeric parity. Fixtures validate the execution boundary, not actual investment judgment. No new paid calls, budget reset, SMTP attempt, publication, commit or deployment in this task. The existing engineering research still needs the substantive normalization and counterevidence work before being presented as completed underwriting.
- Validation for this change: 193 scoped tests passed, including tests/validation; skill frontmatter validation, scoped Ruff and whitespace checks passed. No new report visual acceptance or model-behavior acceptance is claimed.


### 2026-10-21 research returns without an assumed investor hurdle

- User clarified that this open-source project's public research must not assume an investor's minimum return. Revised the skill's research, writing, review, output, compaction and ordinary-company guidance plus the report-style configuration: present normal cash/ROE, price-implied returns, valuation sensitivity and specific risks; the reader decides personal suitability. Independent review still checks the evidence, accounting scope, calculations and interpretation.
- New `underwriting/2` keeps the stable container name but removes required `hurdle_ref` and personal `decision`. Its HTML/PDF/email opening is now “现价、常态化假设与隐含回报”; no automatically generated required-return, pass/fail, or underwriting-suitability labels. The v2 shape rejects the old investor fields. Explicit v1 snapshots retain historical reproduction; signed reports, budgets, existing mail and study results are not silently reauthored or relabeled.
- Removed implicit discount=0.07 defaults from the pure DCF, cash-release and transition-value functions. Each valuation sensitivity requires an explicit calculation rate. The Pi equity scenario tool can compute current-price cash yield and implied return with market_cap_ref and no discount_ref; it does not emit a value under an invented rate. Supplying an explicit sensitivity discount changes DCF values but not the price-implied return. Financial inputs still use evidence refs, compatible units and atomic output registration.
- Also removed the legacy adaptive researcher calculator's default investor hurdle and the return/earnings-proxy threshold comparisons from its research reference conditions. The separate mechanical-screen implementation is unchanged. When replaying old signed adaptive results, their explicitly stored historical hurdle metadata is used only to reproduce their original calculations; new calculations omit that field. Prompt revision advanced to model-led-prompts/7 to distinguish the revised research contract in caching/versioned runs.
- Offline regression coverage includes current-price returns without a discount reference, value-only sensitivities requiring an explicit rate, no suitability/hurdle in new HTML/PDF/email, range/source/currency/period controls, v1 reproduction, new independent-review coverage, and old publication recalculation. No paid API requests, SMTP attempts, budget changes, formal publication, commit or deployment.
- Validation: 307 scoped tests passed including tests/validation, plus scoped Ruff, whitespace and skill validation. These are offline code/contract checks; no new autonomous research, client-specific rendering or investment conclusion acceptance is claimed.

### 2026-10-21 construction cash-flow and current-price report continuation

- User requested a complete construction report using existing evidence and research, with no paid API, email or restart of the original research. New isolated continuation is `data/fundamental_quant/construction_cashflow_revision_20261021/`; original packets, published reports, budgets, review receipts and delivered MIME remain unchanged. This is Codex-authored offline continued research with an independent-context substantive reviewer, not new Pi/DeepSeek autonomous acceptance or formal publication.
- Reused the frozen 2019–2025 panel, 2026 interim statements, original management chapters and twelve historical charts. Eight chapters now connect ordinary-share earnings, conditional normal cash, current-price scenario returns, management statements versus outcomes, peer mechanisms and company-specific risks. Prices remain the clearly dated 2026-10-16 snapshot; sources close on 2026-10-18. New estimates are separate from reported facts. No personal hurdle or suitability decision is introduced.
- Ordinary-share scope is explicit: Sichuan uses the full annual priority coupon; Shandong uses the later-disclosed parent-profit and interim comparison bridge with a qualified distribution classification; Anhui annualizes the current priority distribution and tests a higher funding cost; CCCC uses the EPS note's ordinary-share numerator and an A-price-consistent denominator. Group financing-before-distribution cash does not become shareholder FCF. The actual financing-before-funding residual includes all investing and paid distributions; Capex and minority distributions are not deducted twice.
- Normal cash scenarios preserve economic replacement and earning principal, cash tax/interest and financing continuity, then test ordinary-share additional capital as parallel 0/1/2% of group-revenue axes. These axes have no probabilities and are not historical forecasts or a midpoint. PPP accrued interest requires collection and maintenance of earning principal. Cash yield is separate from a finite cash-path IRR; finite life, delayed receipts, explicit additional funding and discount-rate value sensitivities are shown. Present expansion deficits remain prominent counterevidence. Engineering assets are not valued by stopping all investment while retaining perpetual earnings.
- Independent reviewer read original frozen pages before the draft, checked eight PDF hashes and recomputed 228 comparisons with closed-form annuities and a separate root method. All matched. It requested seven direct source-page bindings for project equity, contract assets, priority tools and supplier financing. The correction changed references only; `review/reference_patch_verification.json` reconstructs the exact previous draft digest after reversing those changes. Final review covers thirteen axes/claims/company conclusions and passes result `79f413d4f8a7dea2912c91997a8d819d7dc7de9bee38945eaa13fd1132f9a279`; initial review/recomputation receipts remain immutable.
- Added optional reference-bound summary/chapter tables to the shared renderer and deterministic validation. Numbers resolve from the same calculation registry and assertions; company, currency, period, source conflict and range-order checks remain. HTML and PDF retain both endpoints. Units appear once per table interval to avoid wrapping. New offline delivery labels do not imply publication. Reader validation now supplies the actual state to the underwriting projection, fixing a real empty-registry failure for new reports. No SMTP path was called.
- Scoped regressions including `tests/validation`: 262 passed; the final table/PDF/offline-label presentation adjustment additionally passed its eighteen-test focused suite. Final real-document generation and file-specific visual receipts are stored beside the report; static tests alone do not confer visual acceptance. No external paid API calls/tokens/fees, SMTP attempts, publication-store writes, budget changes, commit or deployment. Codex session/subagent usage is outside this external-API ledger and is not claimed to be zero.
- Delivered reader `index.html` and PDF: 38 pages, eight chapters, twelve embedded vector charts, 394 bound numeric records and 801 required text items with none missing. Final PDF SHA256 `3af96ffdd9208206a1797375de24a880102a5d9d6204a4d35be85ba316369cdd`; all pages rendered with Poppler and inspected via full contact sheets and enlarged cover/value/timing/scope pages. A first render's repeated table units and orphan limitation page were repaired without changing research. The final visual receipt is `visual_final/visual_acceptance.json`; it binds actual PDF and HTML byte hashes and all page PNGs, and explicitly does not certify a browser or mail client. HTML writing uses exact UTF-8 bytes to avoid Windows newline translation invalidating the receipt. Delivery remains offline/unpublished and no mail was sent.

### 2026-10-21 historically grounded construction cash revision

- User correctly challenged the preceding 1%/2% revenue capital-burden axes: they were sensitivity inputs, not an estimate derived from history. The new isolated cash study `construction_cashflow_grounded_20261021/` withdraws those axes and their cash-yield/IRR/value tables from the research conclusion. It reuses the actual seven-year panel and original MD&A/notes. Original source/result/review files are preserved; `research_correction.json` records the scoped replacement of the unpublished cash revision. No paid API, mail or publication/budget mutation.
- The new cash bridge is CFO minus net investing outflow with nonrecurring PPE/subsidiary sale receipts removed, minus financing cash interest and additional financing lease payments. Cash Capex is already inside net CFI and is not deducted again. Period-aligned source records distinguish cash interest from supplementary-statement finance addbacks, cash distributions from declarations, investment principal recycling from income, and finance-company deposits/lending principal from cash earnings. The seven-year cumulative CFO-minus-cash-Capex proxies are approximately Sichuan -7.7, Shandong -151.9, Anhui -440.0 and CCCC -2828.1 in **CNY 100 million (亿元)**; they precede financing and equity cash claims and are not ordinary FCF.
- Work windows follow business evidence: Sichuan's post-disposal scope and payment timing; Shandong's actual collection improvement and continuing EOD/overseas project investment; Anhui's increasing BOT construction with actual TTM rather than twice the interim; CCCC's ongoing investments and financial-principal cash composition. Conditional group annual funding bands (亿元) are Sichuan -34.6 to +22.6, Shandong -67.2 to -27.1, Anhui -204.8 to -141.0, CCCC -628.5 to -372.0. These are research working combinations, not guaranteed/probabilistic bounds, not maintenance-only spending, and not cash belonging entirely to the listed ordinary shareholder. Sichuan payment support reverting only to the prior-year increase produces a separately displayed -76.6 stress, outside the headline band.
- Independent source-first review found a material CCCC omission and required a real model change: reported CFO contained finance-company deposit/lending principal. Period-matched neutralization now leaves normal interest but removes principal pool expansion/release from the working CFO anchors. FY2024/FY2025/TTM adjusted anchors are about 41.0/183.7/234.3亿元, rather than reported 125.1/153.3/317.7. The latest operating-payables cash contraction is also stated correctly; the latest improvement is not attributed wholesale to supplier expansion. The original material review and initial recomputation remain saved.
- Final independent review covers thirteen checks and 569 separate arithmetic comparisons, with no mismatches, bound to result `3bb175c86ddcbd88a2a4e82ac3f4bb1996282a9f24fa03b69f98f94946f2e694` and 331 numeric assertions. Acceptance is explicitly a **scoped group-funding cash study**. `final=false`, ordinary cash normalization and implied return remain insufficient where maintenance/growth and ordinary cash rights cannot be established. Negative full-growth-investment cash does not prove zero equity value; the report does not fabricate an ordinary FCF or IRR to satisfy a final-report gate.
- Updated research, review and construction skill guidance to require historical amount bridges, window reasons and jointly plausible endpoint conditions before generic sensitivities. Review checks actual cash finance/lease classification, principal and equity scope, rather than approving normal cash because formulas and citations exist. Fixed one real review-text false positive: `零股权价值` is a qualitative equity-value phrase, not a copied `零股` share count. Numeric-literal rejection still applies to actual share counts and copied amounts; regression covers both sides. Focused skill/table/underwriting and required validation suite: 70 passed; numeric-review/publication/skill suite after this fix: 55 passed, with scoped Ruff/whitespace checks. No new autonomous paid-model or mail-client visual acceptance is claimed.


### 2026-10-21 Ordinary-share economic cash continuation (offline)

- Added isolated `data/fundamental_quant/construction_ordinary_cash_20261021/` research, rebuilding ordinary-share cash from the existing frozen FY2019-25/H126 panel, notes and prior ordinary earnings. No paid API, new source download, email, publication, budget mutation or change to historical reports.
- Maintenance budgets use PPE/non-concession asset renewal evidence. Concession amortization remains deducted; lease depreciation is not freely added back. Only incremental capital adjustments use estimated asset rights, not a group-CFO/parent-profit allocation. Sichuan/Shandong retain order-sourcing capital; Anhui/CCCC reduced-investment strategies also remove linked construction contributions.
- Conditional annual CNY cash estimates: Sichuan 43.3-56.8亿元; Shandong -6.5 to 3.9亿元; Anhui -7.5 to 2.0亿元; CCCC -92.7 to 125.4亿元 as a transitional portfolio boundary, not perpetual FCF. No investor hurdle. No midpoint prediction. Stable non-PPP working capital, normal cash interest/tax/lease settlement and equal debt rollover without incremental net leverage are explicit conditions.
- Independent source-first review binds model `f7039f67802689767b456123569f724edc0ff85aaf17d45befe18c7c9661e4da`; 101 model/source checks, 34 prior DAG checks and 13 primary-unit bindings. Local deterministic checks additionally verify arithmetic, negative interval products, missing-input isolation, source hashes, citations/anchors and that the reviewed report body changes only by the review-status badge. `tests/validation/`: 36 passed. This is local conditional research review, not new autonomous Pi/DeepSeek acceptance.


### 2026-10-21 Price-first presentation and forecast horizon (offline)

- Added `construction_price_first_20261021/`, preserving the prior independently reviewed cash model. The opening table now binds frozen price, dated ordinary share count, price-equivalent equity capital, annual and per-share cash, and conditional yield. A/H total shares at A price are explicitly an A-price equivalent, not mixed market capitalization; cross-zero cash does not get a positive multiple interval.
- Price conclusions precede business detail: Sichuan is conditionally measurable but lacks an established realization time/forecast horizon; Shandong/Anhui cash does not establish cheapness; CCCC is too-hard to price from current evidence. Too-hard is neither expensive nor bad, and its transitional upper yield is excluded from the opening normalized-return table. No automatic company ranking or default investor hurdle.
- Sichuan 5/10-year price-coverage examples are undiscounted duration axes beginning at stable cash, not evidence-backed forecasts, target prices, or actual distributed dividends. Underwriting/writing skill references now require first-screen share/price/per-share presentation and separate historical windows, mechanical recovery multiples and forecast confidence horizons. Frozen research/model artifacts were not modified.
- New source-first independent review: 96 checks, `pass_with_conditions`, binding pricing artifact `acbf558803765ec6c7ff33c9fcf13782a877deafb07ebebd2b013dfafaad06ae`. Required validation plus research-skill tests: 48 passed. No paid API, mail, publication, new market fetch or production change.

### 日报权益分派实施公告漏报修复（2026-10-21）

- 实际 10 月 21 日日报仅保留神华 8 月 29 日旧方案；实时新浪公告抓取已返回
  10 月 16 日实施公告，但标题“权益分派”未命中分红识别及重要公告筛选。
- 统一分红关键词供扩展窗口、内容提取、重要公告筛选共用，补齐“权益分派”；
  对所有股票生效，不写入特定股票的生产分红数据。
- 公告明确 A 股登记日为 10 月 23 日，除息及现金发放日均为 10 月 26 日，
  每股税前 0.98 元。回归覆盖窗口、重要公告保留及提取后待派息事件。
- 后续按用户授权完成单文件定向部署。SSH 密钥及配置路径均正确，先前是受限
  执行环境不可读取用户密钥；使用现有密钥、验证已有 known_hosts 后恢复连接。
- 生产仅替换 `src/data/announcement_fetcher.py`，其余 341 个代码、配置、四份
  参考账户及策略指针文件 hash 不变；服务重启后恢复，未同步实验功能或重发邮件。
- 本地 46 项回归通过（含 tests/validation/）；实时源复查已保留实施公告。
- 服务器隔离回归 45 passed、1 deselected；排除的 Yahoo 日历测试在原生产代码
  上同样失败，本地历史分红记录导致多出一条事件。原始复现及部署回执保留于
  `data/deployment_audit/shenhua_dividend_20261021/`。
- 北京时间 20:25:52 独立验收通过：服务器实际抓到 10 月 16 日神华实施公告并
  通过日报筛选；新 PID 758235 稳定 61 秒，心跳年龄 0.97 秒，调度器、飞书线程
  及接收子进程出站 443 正常，重启计数与启动错误均为 0。验证未调用模型或发信。
- 后续用户授权重发：生产全量 21 只标的于 20:34:52–20:35:50 重跑，20:35:51
  Gmail 实收；神华实施公告及每股 0.98 元、除息/派息 10 月 26 日在正文中可见，
  登记日 10 月 23 日由生产模型提取正确，但现模板未展示该字段。实际附件存在，
  服务器保存 PDF 共 9 页；PDF 模板未包含公告事件区，不以其替代正文展示验收。
- Event based trigger 实查：服务器规则模块存在，生产入口没有评估调用，配置未启用；
  仍按既有时间计划运行。本地发送门控实现也不包含新事件监听与去重，不能称已上线。
- 重跑未改生产代码/配置/策略指针或四份参考账户；回执与具体接受边界详见
  `docs/development/shenhua_dividend_hotfix_20261021.md`。


### 2026-10-21 工程组 DeepSeek 价格优先对照试验（未通过）

- 用户独立授权 `fq-price-benchmark-20261021-122050`：12 次调用、1000 万输入
  预留、60 万输出预留、60 分钟。本轮仅本地产物，无发布、邮件或新来源下载；
  旧价格优先报告用于事后比较，不作为研究者答案输入。原授权和历史账本保留。
- `scripts/run_price_research_benchmark.py` 复用既有 Pi 工具循环、资料冻结、
  原生工具传输、持久化账本和报告渲染；仅增加已核对的原始附注事实，不输入
  旧正常化估计。已执行版本和后续修复版本分别保存于试验目录。
- 首两次高思考请求因仅返回思考而耗尽输出结束，均计入真实用量；同根因最多
  两次修复回放后，显式设本轮 `reasoning_effort=low`。LLM/传输支持可选官方
  effort 参数；未配置时保留旧行为，不修改生产默认。
- 修复无效草稿导致渲染崩溃：草稿的未绑定估值表隔离并显示具体错误；有效版本
  仍严格拒绝错误数字。原生计算的单公司/来源期间范围从实际输入签名解析，
  不改数值、不将多公司计算绑定给某一公司。无效草稿的预算预检不再先转换成
  正式审查输入，从而可以进入局部修正。新增回归覆盖这些失败路径。
- 原研究在第 11 次调用后仍未解决 `Y_000498_CASHEARN`：其中实际混用了
  安徽建工的其他权益事实和山东路桥的归母利润。属于公司归属错误，不是资料
  填充率低。没有松开核验、代写金融结论或继续重跑。
- 剩余第 12 次调用仅用于独立上下文的受阻草稿诊断审查，不恢复研究、不批准
  报告。真实返回 `revise`，保存 8 项重大问题及限制；离线代码又确认审查文字
  的中交市值及安徽收益率换算有数量级错误。审查意见保留为原始模型输出，
  没有直接当成修正答案，也不将模型同意视为事实证明。
- 最终实际输入 8,093,434、输出 247,891，缓存命中 6,061,696、未命中
  2,031,738；输入预留 8,740,328、输出预留 589,824。无未知用量。
  按本轮绑定非高峰费率计算 ¥3.14453592（约 ¥3.14，非账户账单回执）。
  共 12 次调用，已停止；原截止时间 21:20:50 不变，没有重置或追加额度。
- 已生成 `deepseek_price_benchmark_20261021/delivery.html`、完整 API 草稿、
  独立审查原文、离线核对、事后质量比较和逐调用费用。结论是当前模型、Skill
  与链路组合未达到既有报告质量；不据此推断模型能力上限。未交付正式估值报告。
- 相关回归最终 107 passed；新增入口和所改共享模块 Ruff 检查通过。桌面实际
  浏览器确认交付页及草稿失败标记，不将测试或进程完成计为研究验收通过。


### 2026-10-21 个人持仓监控、一次性提醒及飞书截图链路（本地，未部署）

- 新增独立 personal_monitor SQLite/core/runtime，个人持仓与参考组合隔离；配置阈值、实际持仓范围、暂停例外、绑定场景年化回报及持久一次性状态。
- 飞书 image/post → 认证资源下载 → DeepSeek JSON 识图 → 单位/金额/真实来源行情及交易日历核对 → revision/as_of 合并；支持跨消息/重启补充日期和手工纠错。
- 独立时间提醒支持自然日、日期默认、改期取消、停机补一次及已过期即时回执；outbox 区分明确失败与未知结果，防旧信号补发及多进程重复。
- 模型负责语义选择，程序负责身份、版本与投递边界；真实状态生成回执，避免模型二次计算时间。未加入交易建议比例、自动滚存或其他实验功能。
- 手工事实入口防止不确定OCR绕过校验；过期提醒按真实目标会话投递，逐条消息提交前校验取消/改期。新增隔离飞书测试配置、只读会话发现和专用启动器，不接入生产应用。
- 验收清单和真实资源边界见 docs/development/personal_monitor_acceptance_20261021.md；证据在 data/acceptance/personal_monitor_20261021/。真实模型及公开行情已实测；真实券商图准确率及真实飞书完整收发仍待隔离资源，不能把合成图/受控投递当作通过。


### 2026-10-21 工程组研究证据合同离线修复

- 原材料有49个完整MD&A，旧会话只收到目录；16次工具执行为12次计算和4次
  估算，阅读调用为零。新增随Skill冻结的evidence-policy，新会话自动投递最近
  年报和最新期间完整章节；真实工程组投递8章，恢复不重复。投递不冒充理解。
- company计算在入口核对发行人并返回具体引用元数据；真正同行计算使用显式
  peer_comparison，不能当单家公司现金。真实山东/安徽混用请求不再进入登记。
- 估算分假设/正常化结果，保存锚与管理层来源/限制、期间方法和跨期桥；正常化
  端点由代码重算，检查单位、公司和重述冲突，防止归母利润二次归属。假设也不
  能悄悄重标源财期。负现金、短历史和有依据的区间继续允许，不加七年或7%筛选。
- 新主审及诊断审查必须绑定计算数字；原始回执和渲染视图分存。补齐工作流视图
  留存和失败隔离；引用复核组合检查继承两份已绑定回执，未放宽发布检查。
- 扩大回归289 passed；最后期间边界定向检查106 passed（测试有重叠）。包含真实
  失败样本、实际Pi子进程离线响应、派发/消费、重启/预算、发布和邮件生成。
  通知使用测试捕获器，无真实发送；相关Ruff和Skill校验通过。
- 实际资料离线回放及完整投递原文：`research_chain_repairs_20261021/index.html`。
  新初始消息约81.4万token，含余量/输出预留仍适配1M；付费调用及网络请求为0。
  原授权、费用和研究回执hash不变，原研究仍未通过；未宣称模型理解或经济判断
  已经重验。细节见`docs/development/research_evidence_root_causes_20261021.md`。


### 2026-10-21 工程组真实修复验收与未通过边界

- 新轮唯一ID `fq-repaired-construction-20261021-142117`；按正常估算十倍上限运行，原90分钟截止不变，不发布不发邮件。
- 实际29次请求，已返回输入16468223、输出281064、缓存命中12342272 token；已知费用按费率折算¥5.49705244，末次复核用量未知，总额待对账，不计零、不重发。
- 已有完整API研究稿；有效独立审查9项收敛至4项，最新局部修订未取得最终复核回执，验收未通过。
- 固化重复面板去重、两层13工具批次恢复、无损容器适配、代码绑定数字、草稿隔离、检查点渲染解耦、引用归档压缩与独立审查明确任务；保留原回执、预算和方法版本。
- 最后请求剩余时间不足暴露后，补新请求最低90秒窗口及研究/独立审查时间预留；仅离线回归验证，未开启新付费请求。
- 本轮依赖10次离线修复/恢复及一次独立原PDF身份核对，不能称无人干预自动链路通过。详情 `docs/development/repaired_construction_paid_acceptance_20261021.md`；交付 `data/fundamental_quant/deepseek_repaired_construction_20261021/delivery.html`。
# 2026-10-22 工程组最终独立复核续接

本轮用户明确授权40次调用、3000万输入预留、150万输出预留、300分钟；
复用既有研究与审查会话、最新修订稿、冻结资料和Skill，不发布、不发邮件。
当前目录见 `data/fundamental_quant/construction_final_review_latest.json`。
旧轮账本及未知用量请求保留；新轮额度不用于清零历史成本。

实际回执暴露并修复了三项适配问题：审查数字格式纠正缺少有界续轮、
单段limitations容器差异、已登记估算区间不能直接绑定。纠正最多两次，
固定判定、问题、严重性与引用，原始回执不改；区间完整标为估算，不取中点。
修复压缩后证据差分忽略完整历史而重复投递面板的问题，并给新压缩档案加哈希。
同一授权的长截止时间可原样恢复；不能借恢复延长截止或重置用量。

真实回执离线消费、回归测试与最终研究通过是不同状态；最终结果以本轮
checkpoint、terminal和费用回执为准。实施证据见
`docs/development/construction_final_review_continuation_20261022.md`。



### 2026-10-22 研究稿编辑与图表 SOP

新增 `research_charts.py`、`build_research_charts.py` 和 `edit_research_with_charts.py`。财务面板与原文EPS导出CSV，保留披露版本；只对有依据的已执行转增还原每股单位，融资不作机械还原。四组PNG以data URI嵌入HTML；原始API输出、编辑稿、来源、实际usage和费用分别留存。编辑要求与原页选择外置，不写死公司规则。Skill入口新增charts.md；prepare离线，run每目录最多一次发送，不自动重试、发布或邮件。

工程组本次真实完成两次API编辑，合计input 1019915、output 29418、cache hit 0，按已核实非高峰费率约1.137587元（非账单）。局部更正修复四川中报少数损益、其他权益工具跨公司挪用与H1年化；撤回失去依据的现金基准，保留条件估计。结果为未独立审查研究稿，不能作为正式发布验收。实际文件：`data/fundamental_quant/direct_chart_revision_20261022/revision_02/index.html`。图表与嵌入完成本地检查；浏览器file导航被策略阻止，不宣称已做桌面/窄屏浏览器视觉验收。


### 2026-10-22 工程组独立审查（限定通过）

复用现有268份Markdown/CSV输入与图文修订稿，完成真实独立首审、局部修订、独立终审。新授权独立账本 `fq-engineering-independent-review-20261022-223747`：3次、300万输入预留、15万输出预留、45分钟；复用AcceptanceCampaign和已有单次传输，未重置旧账本。首审finish_reason=length，公开部分保留；终审完整返回“通过（限定范围）”。EPS、融资/转增区分、普通股权益PB及历史现金诊断获支持；原正常化现金点估计、利息净额及少数现金分配依据不足，相关点估计/排序撤回，不宣称普通股正常化现金已核验。普通股PB另列归母权益扣除其他权益工具的口径；已补EPS附注和永续分配原页。

真实累计3次调用、input 2835435、output 82744、cache hit 0；按当日非高峰费率估算3.166411元，非账单。持久化账本状态completed_with_limitations，后续付费停止。原API稿和终审公开文本保持不改，HTML内嵌4组图表；13项本地数值/标记核对仅属机械检查。交付目录 `data/fundamental_quant/independent_review_20261022-223747/`，未发布、未发邮件；归属/现金估计的实质限制见review.html。

### 2026-10-22 工程组普通股现金离线续研

用户要求先离线补正常化现金，禁止新增付费调用。复用原文与10月21日有来源的普通股NI桥，分别核对维护/获单资本、现金利息、少数和永续及母公司实付。新增底稿与可重算报告位于 `data/fundamental_quant/construction_cash_normalization_offline_20261022/`；旧API稿、独立审查原文及费用账本保留。

从普通股NI起算，只调整已含费用和现金的差额，不再全额扣利息/少数分派；租赁仅比较本金与已保留耗损。四川普通股2025实付约23.26亿元，不能使用宣告/实施叙述35.66亿元代替；安徽采用母权益表而非合并归母分配重构现金，中交母/集团应付股利范围分开。未收PPP利息不假定现金退税，安徽停投利润损失下沿不假定税盾；这些修正已进入主现金范围。新增审查skill说明，避免将缺少精确维护披露或H股报价机械当成A股每股现金估计的阻断。

独立离线实质复核绑定本版现金模型，通过范围是明确策略与稳定周转条件下的普通股经济现金，不是近期上划或分红承诺。14项针对性回归及36项现有validation通过；本轮DeepSeek调用0、外部付费API费用0、邮件0，未发布，未宣称付费/Pi自主研究验收。

### 2026-10-22 正常化现金方法接入Skill、Pi和完整报告

新增`ordinary-cash.md`并随普通企业研究/审查/写作角色冻结；工程专题去掉合并CFO按利润比例分配和宣告分红替代实付的路径。新增原生`calculate_ordinary_cash_bridge`，复用Pi的工具循环、估算登记及计算表；现金/每股/报价收益率由同一回执生成，不写死公司、税率或收益门槛。报告表格接受仅数字引用，Markdown与原生渲染共用计算投影。

复用现有独立复核结果，通过真实工具重算四家公司24个端点并整合到`construction_cash_integrated_20261022/index.html`；四个原有非现金章节与四图保留，旧现金缺口和相冲突结论被替换。8个空文本原页隔离，不作估计锚点，210个非空页逐页一致。独立整合复核通过。135项聚焦回归及36项validation、Skill校验和Ruff通过。付费API0次，未发布、未发邮件；没有把离线控制回放冒称新的模型自主研究验收。实现和入口见`docs/development/research_retirement_20261024.md`（原 `ordinary_cash_research_chain.md` 已合并删除）。

### 2026-10-23 工程与白酒真实研究的运行边界修复

用户新授权两组共享3000万token，本轮以2700万输入和300万输出预留绑定同一新账本；旧轮不变。资料沿用冻结时点，110份原始财报先核对哈希，研究与审查加载最新现金方法。通用入口根据授权在任何发布调用之前选择仅本地交付，禁止发布与邮件的回归覆盖无独立引用闭环的普通通过路径。

新增可选`financial_panel_format=columnar-v1`，将已有无损财务列式投影接入Pi输入；事实值、单位、币种和实际期间保留，完整引用留在冻结工具库。首次真实运行发现旧`search_sources`只按整句匹配且返回全部定位，宽词生成上千个引用，促发无效压缩。运行安全停止后加入公司/财期/文档范围及默认20、最多50的分页；模型接口说明整句匹配。旧大定位回执只在下一次请求的展示层分页，保留总数、后续位置和完整原始回执，不裁剪或摘要原文。原账本费用、失败压缩响应和旧检查点归档，相关代码变化后按原截止与剩余额度恢复，不重置预算。压缩可在本轮绑定的修复配置中关闭思考并留足结构化输出空间。

这些控制回归不证明真实报告通过；本轮最终研究、独立审查及实际费用以`data/fundamental_quant/pi_two_group_20261023-001906/`的检查点、终态和逐调用回执为准。

本轮实际未通过两组交付验收。工程组产生完整草稿后，旧“所有核心结论均支持”分支及没有字段定位的错误信息造成多轮协议返工；Pi统一采用问题范围验收，冲突状态不必改成支持，未知引用错误增加具体章节/ID，事实和计算门槛保留。相关27项工作流/诊断回归通过。工程最终独立API审查返回revise，但回执仍有未绑定数字及未知引用，未生成已批准结果；白酒在新研究发送前被预算挡下，新付费调用为0，不能称两组完成。

原3000万总预留内仅调剂一次输入/输出分配为2750万/250万，同ID、同截止、同调用上限和累计用量，调剂前后总上限均不变；只完成在途工程终审后停止。真实40次已发送调用、输入24,293,803、输出182,601、缓存命中19,187,584，全部usage已知；累计预留29,838,482。非高峰绑定费率计价约6.220375元（非发票）。保留草稿、原始审查和独立离线补充问题，未发布、邮件0；本次结果证明链路仍未完成自主研究验收，不追加预算维持循环。

### 2026-10-23 五脚本收敛设计（尚未实施）

用户要求基本面投研生产代码最多五个脚本、每个最多100个物理行。只读统计当前 `src/fundamental_quant` 为131个Python文件、71,133行；发现材料链路反向导入研究脚本，以及CLI、worker、周通知和Bot的多入口依赖。收敛职责、旧入口退役清单、上游范围及依赖闭包验收记录在 [五脚本设计（旧说明已合并）](../development/research_retirement_20261024.md)。本次仅落设计，未新增平行执行层，未删除旧实现或切换调度；未调用付费API、发布或发邮件。现有36项validation通过，不代表五脚本改造已完成或新的研究质量已验收。

### 2026-10-23 基本面链路事后复盘

按真实数据库事件、逐调用状态及专项回执离线去重，记录在 [事后复盘](../reports/fundamental_quant_postmortem_20261023.md)；本地阅读页为`data/fundamental_quant/postmortem_20261023/index.html`。首事件2026-10-03 22:13北京时间，到10月23日进入第21个自然日。已知DeepSeek输入114,157,915、输出3,262,513，共117,420,428 token；482条尝试记录中5条无完整usage（1条401、4条传输故障），另2条未发送预算拦截不计。主日账本漏计独立审查专项token，且不含3次直接调用，已在独立审计中补差但没有回写历史账本。留存费率回算已知usage约46.80—71.51元，非实付总账，未知请求、Codex订阅与其他费用另列。Codex聊天累计快照约12.33亿token，含缓存与混合话题；不是基本面独占或DeepSeek费用，不能伪造纯项目总价。核实白酒限定引用复核真自动闭环，以及两组历史发布/SMTP；未证明无人救援的完整两行业研究。发现2条旧版更正邮件仍pending，本次未补发。复盘未改生产代码、预算、发布或outbox，付费API新增0。

### 2026-10-23 财报数据三阶段输入输出约定草案

按用户新要求单独整理 [数据格式 v2 草案](../design/fundamental_data_contract_v2.md)：
第一阶段提取选定范围的完整三表、股本/每股指标、附注政策及数值、管理层披露和公司元数据；
第二阶段默认由脚本直接对齐财期、保留披露版本、精确去重，不重复要求模型抄数字；
第三阶段复用已有元数据，输出有范围和依据的同行候选、比较维度及缺口。
协议给出共用行列/期间/缺失格式、各阶段输入输出、中文排版示例及必要的局部多轮上下文。
按用户后续明确要求，草案移除来源文件/页码、证据对象、数值复核、合计/勾稽检查和正确性验收报告；
保留提取、整合及同行元数据的内容约定，不把模型自查或格式符合当成准确性保证。
示例均为虚构格式示意，未启动新提取或付费调用；首次 API 演示的冻结代码、协议和产物不改。
本项只是设计文档，不代表协议已实现、数据完整性已验收或五脚本约束已达标。

### 2026-10-23 结构化数据 v2 独立入口

按用户后续授权接通 `scripts/build_fundamental_data_v2.py`：extract由模型按固定schema提取完整三表、
股本每股指标、附注与元数据；history为纯脚本整合；peers只复用已有元数据判断同行并整理研究范围。
新schema和提示词保存在`config/fundamental_data_v2.json`，输出中文Markdown/HTML及长表CSV。
未加入来源证据、数值复核或会计勾稽；JSON/行列结构检查不代表财务准确性保证。
首次实际演示选泸州老窖FY2025，只执行一次extract；正式请求前固定新版本，旧演示全部冻结。
第二/三阶段使用模拟API及离线路径测试，未声称已有真实多年/同行结果；运行记录见独立目录。
本次功能接通不代表整个旧研究依赖闭包已收敛为五脚本。

### 2026-10-23 同份年报的四块提取

用户授权先修改代码，再用泸州老窖同一份FY2025年报重跑一次，调用后不再修改。
extract改为三表、股本每股指标、附注、元数据四个小schema，按固定顺序各调用一次；
脚本直接拼合，不重试、不修复JSON，不加入来源证据或财务数值复核。
四个完整请求及代码副本在首次调用前固定，各块独立留存原始响应、数据与阅读页；
单块失败仍显示其他有效块，主结果列出缺块，期间标签冲突则分开而不改数值。
本次新目录为`data/fundamental_data/v2_luzhou_2025_chunks_first`；结果以其原始回执及summary为准。
旧两次演示产物不改，改动前代码/配置另存`.trash/fundamental_data_v2_before_chunks_20261023/before.zip`。

### 2026-10-23 去除整块拦截并使用真实响应离线回放

用户要求链路BUG不得靠付费重试调试。本轮无新API请求：去掉schema意见导致整块被设为None的逻辑，
单元格允许百分比、区间和文本原样展示，额外字段与不齐列保留，结构差异只作日志说明。
新增同入口`replay`命令，以历史`response.body`和receipt构造本地响应，复用原解析、拼合和展示路径；
不读取PDF/tokenizer/密钥，不回退网络，拒绝`--paid`及向历史目录内部输出。
保存顺序补齐为HTTP原文、正文、parsed.json、data.json、阅读页，events.jsonl逐步留痕。
历史usage单列为historical_usage，回放新增model_calls为0。真实百分比/区间/文本摘录进入回归fixture。
旧三个运行目录全部只读；本次新阅读结果位于`data/fundamental_data/v2_luzhou_2025_replay`。
本轮代码修改前ZIP为`.trash/fundamental_data_v2_before_replay_20261023/before.zip`。


### 2026-10-24 财报数据合约3.0：定稿、离线贯通及预算

按用户确认的清单/schema/prompt/示例同步实现，沿用build_fundamental_data_v2.py入口。
数值表改为同键columns/values，模型输出明确单位、期间、统计方法、范围及调整口径；
脚本不猜单位、不换算、不匹配同义、不平移表头、不修补JSON。资本最低14项独立标签覆盖。
共同20主题＋非金融21及金融10模块；调用前显式选profile，基础8块，银行/保险各加2，其他各加1。
阶段2纯脚本按明确语义合并，原始和重述报告版本并存；未知与重复歧义隔离。
阶段3只投影已有元数据及明确选择的分部/口径上下文；不读取整份PDF或重抄三表。
请求前固定全计划、代码/config/示例和输入快照；每请求前复核，保留HTTP原文/content/parsed及日志，禁止自动重试。
未带paid仅预检；未来paid须指定正数规划预算限额，非供应商账单硬上限。
本轮HTTP/socket封锁，179项测试通过（125＋18＋36），57块规范例结构通过；新增付费调用0。
旧招行/泸州响应只读回放保留原文，明确不迁移为新合约；旧7目录355文件集合及SHA256不变。
最终阅读导航及记录：data/fundamental_data/contract_acceptance_20261024_final/。
招行350页、银行10次预检峰时全未缓存估计USD1.125236；泸州183页、制造业＋食品饮料10次约USD0.761840。
预算按本地输入估计及最大输出，不是实际费用；新prompt遵约、完整性和财务准确性未真实模型验证。
HTML结构通过；本地file浏览器策略阻止截图验收，未声称视觉通过。
完整约定和问题/分块预算见docs/design/fundamental_data_contract_v2.md及docs/development/fundamental_data_contract_acceptance_20261024.md。

### 2026-10-24 去除整表拦截和有意见分块的整体展示降级

按用户授权仅修复保存/展示边界：表级schema及语义意见不再将整表移出正常集合，原字段和行值保留，意见关联到具体表。
format_notes不再让整个分块退回JSON或导出空CSV；不合格式表按明确列键显示，未知列键原值显式保留，不推测或修复。
个别说明记录展示失败时只保留该条原始片段，不影响其他表。原始响应和parsed不动，版本派生字段不覆盖意外同名原字段。
prompt/schema/示例、分块、输出上限、预算、传输及截断行为均未改，第二阶段合并算法未改。
在网络封锁下193项回归通过，招行既有10份响应离线回放新增API调用0；旧2291文件集合及SHA256未变。
新回放保留62张表693行（原为43张369行），CFS58行恢复；所有已解析表原字段和值与响应一致。
两块旧截断仍保留，运行仍为partial，不代表完整财报提取或异常表多年合并已经验收。
新阅读页：data/fundamental_data/v3_cmb_2025_annotation_replay_20261024_final/阅读.html。
验收记录：docs/development/fundamental_data_annotation_only_acceptance_20261024.md。

### 2026-10-24 招行三年数据测试：64K准备与离线验收

用户授权新增2023、2024两份完整年报、合计US$5，2025复用原有结果不补调。
第一阶段41项job的max_tokens及统一预算上限均调为65536；prompt/schema/示例、thinking关闭、分块及完整PDF输入不变。
第二阶段修复已保留异常表导致的合并异常：不满足明确合并条件的表独立保留并标注；原始列名纳入列身份，避免同期间金额/占比列覆盖。
201项离线检查通过；2025单报告第二阶段保留693行1938单元格，与输入逐项一致，零冲突。
本轮准备目录data/fundamental_data/cmb_2023_2025_64k_20261024，预先生成20份请求、预算、代码/请求/PDF哈希冻结；不重试、不续写、不追加FY2025请求。
调用前完成全部修改；执行器只生成约定的新输出和逐步日志，旧2585文件受哈希和文件集合保护。
本段记录准备状态，真实调用结果以该目录execution_result.json、stage2_cell_preservation.json为准；不预先宣称付费验收成功。


### 2026-10-24 财报数据契约4.0：一次版本修补及冻结试验

- 用户授权实施此前修补、补全prompt要求与对照例，再试一个版本；旧产物只读，不自动追加付费。
- 配置与schema升级4.0，明确table/row/单位/主体/会计基础/列语义固定身份；原始显示字段保留。41块任务逐项清单、31行业范围和10组歧义正反例随实际请求传入。
- 主路径接收→严格JSON解析→原对象保存→展示，不调用业务validate/table_notes/item_errors；独立audit入口只生成离线问题报告。预算/冻结/防覆盖/JSON不丢键继续保留。合法JSON即使finish非stop也保留展示并标未完成。
- history只机械匹配明确身份和口径，row.period_overrides以缺省继承/显式null未知区分，升入输出有效期间列。原字段和键映射保留source_fields；局部不可读行独立原样保留。旧3.0不补码，独立保留；重复身份不按序配对、不再报19类伪财务冲突。
- 先离线回归与历史原文回放，报告放新目录；随后仅对同一招行2023完整年报notes_operations块发一次请求：128K输出、thinking启用、保守峰时未缓存估算上限US$0.50（本次单请求自限）。实际预算/请求/完整输入哈希及验收条件在调用前冻结。
- 付费结果由预先写好的experiment.py保存和离线核对，不修复返回、不修改代码/prompt/request、不重试；其通过与否见独立experiment summary/offline_acceptance。一个经营块不能代表三年或全行业验收。

### 2026-10-24 第二阶段收缩为离线模型正文整理

- 第一阶段及旧结果保持冻结；新增独立入口build_financial_context.py与model_context.py，不导入或修改冻结V2提取链，入口禁用网络。
- 用现有三家公司九份data.json离线整理；同原表及明确口径才对齐，歧义列保留原列，不再按行生成列。共同口径写一次，政策按精确文本/口径版本复用。
- 最终给模型的是Markdown正文，HTML仅预览；审计JSON不默认进入模型。未解析响应另存原字节附件，不修复或补调。
- 32,121个已有单元格全部保留，行口径、有效列期间、缺失原因及原值逐项核对；1,596个旧文件与冻结依赖哈希不变。
- 隧道正文1,015,013→311,642 tokens，泸州458,544→247,837，两家合计减少62.03%；四川原二阶段失败，本次独立保留2024身份未定记录，正文236,389 tokens。
- 三家公司总正文795,889，未解析附件286,063，拼接1,081,951 tokens；近似改写政策、第一阶段漏项/错误均未擅自改写。
- 210项离线测试及新增文件ruff通过；结果data/fundamental_data/stage2_model_context_20261024_125223_final/；完整验收边界见docs/development/financial_model_context_acceptance_20261024.md。

### 2026-10-24 退役数据流程之外的旧基本面研究

- 用户明确选择全量退役旧研究入口，保留历史文件与必要读取。第一阶段、现行第二阶段、技术面实现不变，不接统计模型、不调付费API。
- fundamental_quant从110文件58,562行收缩为4个只读文件198行（每个≤100行），物理删除107个旧模块和32个正式入口；未将旧引擎移目录继续调用。
- 旧查询会重验和写回历史，现改为SQLite mode=ro读取元数据、原状态、版本和已有报告路径；run/notify明确退役，Bot不输出历史结论作为当前建议。
- 旧调度分支和任务注册器移除，enabled旧配置不能恢复；本机旧worker已停止，其专属Windows任务备份后删除并验证不存在。未部署、未重启主交易/助手服务。
- 152个旧功能测试文件退役；8个混合数据/PIT套件保留独立测试，135项通过。主回归387项通过（与135有重叠，不相加）。
- 1,669项冻结数据/源码/旧产物哈希不变；生产源码无悬空旧导入；旧真实库6,001家公司、6报告、3会话只读可见且DB哈希不变。
- 原有未提交/未跟踪源码原字节备份于.trash/research_retirement_20261024_131928/，机器清单及JUnit在同名.tmp目录。说明见docs/development/research_retirement_20261024.md。

### 2026-10-24 单公司七问摘要契约与离线调用准备

- 第一/二阶段和旧产物冻结，新增company_summary_contract及摘要/恢复prompt、完整排版示例；沿用原七问，输出以Markdown为主，不建立大JSON业务schema或新校验门槛。
- 新增prepare_company_summary.py仅离线复制、装包、计token与预算、保护哈希及示例；不调用API、不修改旧失败状态，不做语义猜测/JSON修复/数据换算，不恢复旧研究引擎。
- 现有三公司三年准备5个固定恢复包+3个摘要模板，11失败块、7raw、7年度冻结PDF文字共2,207页、27图片全部覆盖；失败性质与可见恢复内容/执行状态一并送达，后者尚是待绑定变量。
- 输入规划上界合计2,799,866 tokens，单次279k–411k；输出额度720,896，未缓存全额预算峰时US$1.705035/非峰时US$0.852518，实际调用0。价格截至2026-10-24，未来执行前重新核价、计量和冻结，不自动追加。
- 摘要目标20–40k/公司、规划上限60k，超目标仍保留全部结果并标记；未来7年按实际消息测量，不承诺单公司永远一包。同行资格仍读取原元数据，摘要服务后续七问研究准备，不声称现有数据3已经执行估值研究。
- 69项相关离线回归通过，失败性质投递修补后43项再通过，独立复核覆盖/预算/表格及1,642保护文件哈希不变；仅证明离线准备，不证明真实模型摘要或恢复质量。
- 说明docs/design/company_summary_contract.md；最终准备目录data/fundamental_data/summary_contract_20261024_174500_release_v2，旧草案目录未覆写。未实现或运行付费调用器，未部署、未commit。

### 2026-10-24 单公司摘要US$2授权执行准备

- 用户授权现有三家公司三年按定稿方案运行，合计US$2，第一/二阶段和旧结果不动；发送前冻结，发送后不改代码/prompt/计划/模型正文，不重试、不追加。
- 新独立run_company_summary.py只执行批准的5恢复+3摘要。固定传输参数include_usage=true；HTTP/SSE原始片段、可见正文、thinking及usage分开逐步保存，模型正文不解析JSON，不以格式意见过滤。
- 54项离线回归通过，其中完整8份批准模板mock跑通依赖绑定、预算、落盘、阅读页及报告；缺DONE/断流/缺usage/重复执行均有离线检查。验证时真实模型API调用0。
- 全峰时、未缓存、用尽输出额度，并给输入估算加25%余量，整体预留约US$1.915；周末实际费用按返回usage与已核对官方价格计算，completion已含reasoning，不重复收费，usage未知仍留全额预留。
- 恢复变量仅在原模板上一次绑定完整可见content和执行状态，不递归替换。每个实际请求在发送前保存冻结哈希；任何中断/格式问题均不触发额外调用。真实结果由新运行目录的冻结执行器自行生成，本段不预先宣称付费成功或摘要质量通过。

### 2026-10-24 单公司摘要best-effort边界冻结

- 用户明确恢复捡不回即保留缺口，摘要按已有信息尽力完成；不再以恢复完整或填充率为前置门槛。设计落docs/design/company_summary_v1_best_effort.md，原契约、第一/二阶段、prompt和旧产物不回写。
- 未来再用固定分析条件的对照试验研究提取率、新数据源是否改善关键事实精度、现金解释和判断有效性；本轮不加数据源、不建设评估引擎或修补提取流程。
- 复用原已冻结执行器，只在新目录串行重试3份原已绑定摘要请求，消息与上轮发送版本逐字节相同，不重复5份恢复。36项validation通过，原冻结文件校验不变。
- 原已知费用US$0.337602738与未知预留US$0.858958200继续保留，本轮峰时全额加输入25%余量预留US$0.752444700，合并US$1.949005638低于原US$2；新设计、三份请求和调用计划已冻结1778文件，后续只生成新结果。
- 本段只记录用户授权及执行前准备，实际摘要结果见data/fundamental_data/summary_best_effort_20261024_162500，不预先宣称成功或正确。

### 2026-10-24 财报入口与第三阶段文件交接收口

- 新统一入口scripts/financial_pipeline.py提供extract/context/summary-prepare/summary-run/stage3-input/stage3-read；source/output/budget参数统一，main退役提示及README转向新入口。原第一/二阶段实现、schema、prompt和旧产物未修改。
- extract输出明确来源清单及原提取器stage1目录；context支持一个或多个运行目录，多输入仅机械复制为持久_inputs适配目录再委托原整合器，不按名称或数值猜测身份、单位或年份。
- 摘要准备只安排每公司一次best-effort摘要，沿用原七问/prompt/示例，不引入旧恢复流程；付费执行显式--paid，partial/失败保留正文并返回执行状态。预算上界与usage计价分别记录，未来真实调用须重新确认定价日期。
- handoff保留摘要原字节、执行状态、年度元数据、分部/口径表与原来源清单，区分声明身份与提取未知；第三阶段只需参数指定交接目录，不依赖.tmp或历史运行路径仍在线。未执行同行或研究分析。
- 现有三公司三年离线重跑，逐公司及合并正文原字节、对齐目录一致，32,121已有单元格保留；1,778受保护旧文件哈希不变。真实PDF预检0调用，摘要计划3次但发送0次。
- 综合293项回归（含validation）与最终19项入口专项通过，两组有重叠；新增代码ruff通过。独立checkpoint复核来源年度、执行返回码、baseline隔离和旧32k限制未继承。
- 命令及边界docs/guide/financial_pipeline.md；最终验收data/acceptance/financial_entrypoints_20261024/验收.md，交接包stage3_ready，摘要离线计划summary_canonical。未调用API、部署、commit或改写历史摘要；旧批次/临时脚本仅归档保留，不再进入推荐调用链。

### 2026-10-24 第三阶段同行与七问研究方案离线准备

- 前面的提取、整合、摘要、统一入口与旧产物继续冻结。新增docs/design/peer_research_v1.md、config/prompts/peer_research.md、docs/design/peer_research_example.md，仅准备方案和请求预览，不新增生产执行器或业务JSON解析/门槛。
- 第三阶段默认同一次回答先判断同行关系、关键口径，再沿用原七问跨公司研究。工程两公司作为限定范围的候选组；泸州独立参照，不硬凑同业排名。没有现价/要求回报不输出确定估值与安全边际，样本共变不冒充行业因果。
- 业务/主体、单位/币种、实际期间/时点/平均、版本、股本/普通股权利及披露/模型推算分别说明；保留四川2024身份歧义、隧道重述、EPS/BVPS与旧摘要缩写冲突，不写回前阶段，不触发恢复或续调。
- 已用stage3_ready三份完整摘要和9份年度元数据组装完整prompt/示例/原七问/资料，manifest中的摘要执行状态单独投递。输入本地估算148,045 tokens（含4,096余量），原正文只进入消息一次，低于500k；输出额度131,072含thinking，可见目标15–30k为软规划。
- 原1,907项前阶段文件哈希不变，摘要/元数据/状态副本原字节保留；示例7问、7张表结构检查通过，validation 36项通过。独立checkpoint复核契约/prompt/示例一致，结果仅证明离线准备与排版，不证明模型研究或财务事实正确。
- 准备目录data/acceptance/peer_research_contract_20261024，含request.preview.json、完整上下文、输出示例HTML、调用规划及离线验收。实际API调用0，无新付费授权或美元预算，不接旧peers或summary-run运行；未改第一二阶段或摘要，未部署、commit。

### 2026-10-24 第三阶段单次US$2授权执行准备

- 用户授权使用现有三家公司摘要/元数据运行一次第三阶段，US$2上限；前阶段与旧产物不动，发送前冻结代码/prompt/请求，调用后不修改、不自动追加。
- 新scripts/run_peer_research.py只准备并发送已有request.preview.json原字节，复用原run_company_summary.perform流式保存；不改统一入口、提取、整合、摘要或既有第三阶段prompt/示例，不引入业务JSON解析/验收门槛。
- 输入本地估计148,045，thinking enabled/high，输出额度131,072。已核对官方2026-10-24周末价；按1,048,576输入加全输出额度的峰时未缓存价预留US$0.4718592，不依赖缓存节省，低于US$2。usage未知保留预留，completion已含thinking，不重复收费。
- 51项离线回归含validation通过，另对最终入口4项mock复核；覆盖唯一请求、断流/非法模型JSON原文照留、无usage不按零、重复启动及篡改阻止。新增源码ruff通过。冻结前独立checkpoint只读复核调用/预算/保护边界。
- 新目录data/fundamental_data/peer_research_20261024_frozen保存冻结计划、请求与哈希，以及冻结执行器自动生成的raw/SSE、完整分析.md/html、thinking、receipt、usage/费用和执行记录。独立复核后补齐原prompt/设计/示例/tokenizer冻结闭包，再建立此最终冻结目录；之前first目录仅预检、无attempt。此段只记录执行前准备，不预先声明真实调用成功或研究正确；真实结果以新目录为准。

### 2026-10-24 正式研究统一入口（离线实现）

- 用户将正式研究定义为主题/反证/歧义与细节审查、图表呈现、结论推出、最终写作，并明确只保留一个启动入口和一份最终呈现。新增scripts/research_report.py，四项工作通过config/research_report.json引用的独立prompt配置组合到一次请求，不拆四条命令、不自动循环调用。七问沿用为覆盖清单，不强制充当最终章节。
- 新增research_inputs.py负责材料交接和请求冻结，research_render.py负责Markdown/HTML与明确数值的line/bar图表；共三份生产脚本，复用冻结的原流式传输层，不恢复退役研究引擎。正文原字节、raw、thinking、usage和图表数据归档；格式错误、范围、百分比和局部绘图失败不触发整段丢弃或猜测修复。
- 默认只离线准备；显式本轮预算与--execute/--paid才可执行一份冻结请求。无自动重试、续写、补数或追加。前面提取/整合/摘要/同行整理及旧产物不变。用户反馈可用同一入口新建下一轮，带原图表数值与本地图片，不自动灌入全部历史。
- 操作和边界见docs/guide/research_report.md；排版示例见docs/design/research_report_example.md。data/acceptance/research_report_20261024记录已有三家公司材料的离线请求、呈现样例、回归结果及受保护文件对比。本轮不产生新的付费模型研究结论，不调API。

### 2026-10-24 正式研究固定三轮修补（仅离线）

- 用户反馈单轮报告丢失规模参照、分母、历史变化且图少；授权仅修改研究层，唯一入口research_report.py内部固定draft/review/write三轮。原研究代码、配置和指南先另存v1_source_snapshot，前阶段与已付费运行目录字节不动，不重新提取或回填旧结果。
- 共通与三轮prompt分别配置；每轮携带冻结证据包。第一轮交付自足初稿/规模历史面板及图表数据；第二轮实际读取PNG和原数审查反证并返回完整修订稿；第三轮看修订图完成写作，只引用现有图。金额明确相关分母、比例、历史基准与含义，缺行情如实标示而不省略经营尺度。
- 新research_rounds.py只负责真实图片/全文交接、每轮请求冻结和最终呈现，复用未改的制图与原流式传输。三轮计划、模板和组装规则先冻结，后轮真实请求生成后逐轮冻结，不声称首轮前已有全部请求原字节。无自动重试/续写/第四轮，格式问题只局部标注，失败保留最近非空稿与原失败响应。
- 离线以实际三家公司三年材料和明确模拟响应验证12图交接、改图后第三轮拿新PNG、原始稿保留、规模分母历史不丢；样例数独立对照旧历史面板。绘图库失败残留图不发送，最终新chart块不擅自绘制，首轮空响应仍生成失败交付。
- 官方2026-10-24文档核对flash视觉载荷、每图1024token上界及48MiB请求上限；三轮按全输入容量和131,072输出额度的峰时价合计预留US$1.4155776。实际费用本轮不存在，mock usage费用仅测试记账；常规费用估计另列，不继承此前US$2授权。
- 验收与调用计划data/acceptance/research_three_rounds_20261024，指南docs/guide/research_report.md。未调用付费API、获取新行情、部署或commit；离线通过不代表真实模型已完成研究或视觉审稿。

### 2026-10-24 研究呈现改为主题横比（仅prompt/示例/离线验收）

- 用户要求主文按主题横比，同一指标、时间轴和分母集中展示，工程两家深入，白酒作为机制参照。四个生产运行脚本及前阶段保持不变；旧v2prompt、旧报告和旧图不修改，现行配置/示例/指南先快照后更新。
- 新research_basis/draft/audit/final_v3.md分别约定共同规模表、按指标分组的历史表、共轴比较图、行业机制解释、横向反证；禁止主文按公司拼接。年度方案/实际支付、存量/年度流量、CFO/收入与CFO/利润分清，负CFO不硬做正常覆盖率。
- 更新docs/design/research_report_example.md并通过现有config.prompts实际送入每轮system上下文。图表格式说明的chart要求明确只对前两轮生效，最终写作只引用实际修订PNG，不新增解析或金融语义拦截。
- 新主题模拟响应按问题组织完整正文，保留原值与分母，12图在共同时间轴对比；仅白酒特有库存/预收机制另图解释。样例不是新的模型研究结论，不能证明真实模型会遵守新版组织约定。
- data/acceptance/research_theme_compare_20261024另存三轮离线产物与完整正文，使用同一引擎和真实旧材料、禁止socket的模拟接口验证。前阶段与旧产物哈希核对，不调用付费API、获取行情、部署或commit。


### 2026-10-24 正式研究v4：七年历史与所示价格回报形式冻结

正式研究沿用一个入口固定三轮，新增独立估值prompt配置：全文七年历史、未来五年情景、现金与普通股权利、ROE与投资IRR区别、hurdle最高买价及反向条件。主题横比/工程深入/白酒机制参照保留。仅更新配置、prompt、排版示例和离线验收，不改四个运行脚本或前面阶段。当前真实材料只有三年，缺四年显式留空；独立虚构U算例验证现金/每股/终值/IRR与图表形式，不冒充真实预测。无付费请求，旧产物不变。形式冻结及边界见data/acceptance/research_valuation_form_20261024/验收.md。


### 2026-10-24 研究v5：实际年份与本地最新已知价

按用户修正，实际收到几年的历史就展示几年，不为目标七年造空列。研究准备从stage3 manifest或显式公司参数取身份，读取已有tencent_raw_day原始A股行情，按真实行情日期生成轻量快照并冻结，三轮复用；明确手动快照优先。无抓取、付费调用、新研究轮或新运行模块。研究四脚本654→746行（+92），本批价格快照首轮仅增193 tokens；旧产物及前阶段保持不动。详见data/acceptance/research_known_price_20261024/架构与验收.md。


### 2026-10-24 正式研究v5交付基线与提交边界

- 用户接受当前文本交付基线；真实报告三轮完成，API输入505,445、输出113,746（已含thinking 78,376），usage计价US$0.14237091；最终正文15,217本地tokens。图表0张是已知限制，未自动补调。
- 本次提交仅正式研究入口、直接传输/交接依赖、当前prompt/契约、便携离线测试、tokenizer和未改字节的真实报告副本，不混入技术面/回测/部署或前阶段业务改动。
- 运行源码与已付费响应不改；测试改用固定入库资料和本地行情样例，避免依赖本机data/acceptance目录。临时目录和运行证据不进Git，原始PDF、旧产物及完整付费日志保持本地原字节。
- 已清理12个可重建pytest临时目录，5,261文件、约201.98MiB。交付说明见docs/reports/research_v5_20261024/README.md。

## 2026-10-24 全A股底层来源后台补齐

用户授权独立慢速后台任务：全市场5572只，按42银行、6保险、193能源、其余5331只串行逐只补FY2019–FY2025原件，再补日线和免费财务接口。新入口 scripts/backfill_ashare_sources.py，配置与断点均在 data/fundamental_quant/source_backfill/themes_depth7_20261024。冻结的第一/二阶段及研究产物不动，不调用付费模型；失败退避后明确挂起，不伪报齐全。42项离线验收通过，真实首份资料落盘，当前聊天heartbeat负责主题完成通知。范围、完成标准及停止恢复见 docs/guide/ashare_source_backfill.md。

### 后续修订：先筛选，再深采

用户提供2026-09-27五项筛选表并明确银行/保险豁免负债率门槛，其他条件不变，待核先补字段。旧全A股批次保留STOP。初始335通过/4879未通过/358待核；通过主题42银行、5保险、30能源、258其他。原表日期不冒充实时筛选，PB/PE预设、历史中位数、TTM期间及重组待核原因保留。新增独立source_screening字段补齐模块，不影响前面数据/研究阶段；未覆盖的356只不从另一套估值分类猜路径。

归档器支持本批显式无PDF大小上限、磁盘余量控制、单公司3路任务和共享原子限速，旧调用默认行为保留。永久失败直接阻塞；暂时失败服从Retry-After和有限退避；只重开旧size-block，不清除源站状态。Baostock保持单连接。真实兰州银行FY2023 51.91MiB原件已归档，原件与解析状态分开记录。新筛选批次、规则与命令见docs/guide/ashare_source_backfill.md。本轮无付费模型请求，历史产物不改。


### 2026-10-24 现有飞书机器人单群API验收入口（未部署，权限待补）

- 用户已创建独立测试群并加入现有机器人。新增单群OpenAPI轮询入口，复用个人持仓/提醒工具、识图及发送适配器；不再为同一App启动第二条长连接，不接入传统命令、日报、研究或优化功能。
- 严格绑定单群/发送者/测试库，校验当天生产群消息过滤证据；消息采用原始时间，分页完整收集后推进边界并永久去重。只读probe在建库/模型/通知前核实权限，未通过不启动正常模式。
- 真实只读核查确认生产active/running、心跳PID一致、飞书线程存活；当前服务器对非@且非斜杠群消息返回ignored。群详情读取成功，历史API明确缺im:message.group_msg（HTTP400/code230027）；不要求额外成员列表权限，群主身份来自已可读群详情。
- 用户需在现有应用开通该权限并发布生效，再复核只读probe。尚未运行真实截图/提醒验收，未发送测试消息、未改权限/服务配置、未部署或重启。配置与核验记录仅含必要ID，不落密钥；现有私聊测试入口保持默认行为。
- 指南见docs/guide/personal_monitor_feishu_test.md，证据见data/acceptance/personal_monitor_20261024/及同级远程只读摘要。


### 2026-10-25 真实飞书截图复验及个人监控边界修复（未部署）

- 现有App单群API权限已生效；保留生产WebSocket，独立测试库接收真实截图/文字。生产只读复核PID760395、同PID新鲜心跳、Feishu线程及群过滤源码哈希不变。
- 修复视觉输出截断/超时：独立可配120秒/16384tokens/low/1次重试及逐次安全诊断；原图受控恢复A与HK均finish stop。8笔A股/ETF/REIT代码数量价格市值逐项与人工原图一致、合计304116.30元，但数量列缺单位，等待用户确认后才导入。7笔价格与真实最新完整交易日精确一致；神华49.22对50.20有1.95%差异，不改写截图或宣称完全匹配。
- 截图pending恢复与明确批次选择、持久账户绑定、跨批次操作缓存、真实图片字段来源、互补来源账户及版本递归复查；部分成功继续暴露待补事实。用户单位确认有独立声明来源，不改原数值，其他不确定字段仍拒绝；只允许本批自身revision安全补充，人工外部修改仍冲突。
- 单群GET瞬态失败指数退避，不推进失败窗口；过滤证明在读取/重试/投递边界继续验证，状态文件可见运行/退避/失败。长回复提醒按实际分段提交，保留发送成功/失败/未知分类和平台消息ID。
- 本轮实际发送一次原图恢复说明并以user_card_content读回正文；不是自然对话自动导入回执。群原17条只有补代码/日期/账户/当前声明，无真实提醒指令，提醒/outbox仍空。4笔HK代码数量及遮挡市值、2笔回购72000元期限均待用户补充，不猜仓位或到期日。
- 聚焦363项、最终识图118项、独立工具接线44项、validation36项通过（测试重叠不相加），Ruff通过。A09/A10仅本次真实样本局部通过；A20真实到期/去重/界面可见及完整FeishuAssistant正式入口仍未完成。默认最近5个待补批次的完整历史分页未实现。
- 详细证据与通过/未完成清单见docs/development/personal_monitor_acceptance_20261025.md；证据在data/acceptance/personal_monitor_20261025/。不部署、不提交、不推送，不包含日报/研究/优化实验。

### 2026-10-25 底层采集按best-effort继续，不以单家公司缺口阻断队列

- 用户重申“有多少看多少”。旧实现把西安银行2019-03-01上市、行情从2019-03-11起的10日差异抛错，并在三次尝试后退出全队列；七年PDF和财务文件其实均已保存。这是调度语义错误，不是模型提取问题。
- 仅修改backfill_ashare_sources.py：年度404/未找到、局部接口缺失及行情首末不足保留为gaps/partial，继续该公司其他来源，再顺序处理下一家公司；原件和数值不补造。源站访问受限、磁盘/文件系统错误、程序异常仍全局停止。Baostock单连接、共享限速和封禁状态不改，前面阶段业务不改。
- 状态区分processed/completed/partial及theme_partial/themes_processed；最后可为complete_with_gaps。partial中的实际文件仍有哈希，重启复用完整的已处理记录，不为已知缺口重复请求相同接口。新批次screened_best_effort_20261025复用旧screened_depth7_20261024的16家完整进度、现有西安资料、筛选响应和上市证据，旧批次全部留存。
- 实际西安文件在禁用网络的离线回放中保留七年PDF及2019-03-11至2026-10-23行情，结果partial且缺口明确。156项通过、1项跳过，含局部缺口继续下一家、重启复用、源站封禁仍阻塞及tests/validation/。证据在data/acceptance/backfill_best_effort_20261025/，无付费模型请求。


### 2026-10-25 08:37 测试群静默恢复（仅本地）

用户查询持仓无回复，实查旧本地PID39204已不存在，最后成功轮询04:25，但状态文件仍留running；终止原因没有退出日志，不能断言是网络或模型故障。08:29真实消息未入本地去重表。恢复为隐藏独立后台进程后，真实桥补处理并发送1条回复，平台ID [验收标识已脱敏]，GET user_card_content读回通过；持仓仍0，截图仍待确认，没有伪造入账。

新增只读--status检查PID、进程创建时间/脚本身份、心跳时效，进程消失/PID复用/不可访问/过期不再被读作健康；诊断依赖不可用不阻断接收。65项单群回归及36项validation共101通过，Ruff通过。未安装自动重启或离线告警；未部署、修改生产或启动第二条WebSocket。证据data/acceptance/personal_monitor_20261025/silence_recovery/，本地新进程健康复核通过。


### 2026-10-25 本地监督、退出取证和真实故障恢复（未部署）

旧接收器最后心跳04:25:33.208，04:25:34.006 Codex执行会话25002 failed/exit-1，同秒宿主thread teardown/Shutdown/Agent loop exited，确认直接机制为依附宿主销毁退出；上游触发者无证据，不把-1解释成具体信号。

安装本机用户one的Windows计划任务TradeEyesPersonalMonitorTest（Interactive、低权限、IgnoreNew、分钟触发/RestartOnFailure），OS启动器捕获监督器退出码与stdout/stderr；监督器按PID/创建时间/脚本/配置接管单群worker，退出退避拉起、崩溃风暴冷却、独占锁和processing900秒阶段预算。健康告警仅投递既有测试群，持久故障事件、failed退避、unknown不盲重发；恢复后汇总且不晚发过时掉线。计划stop持久化，证明过期failclosed，不伪造新证明。

现场强制终止worker两次、监督器两次均恢复，OS父链为svchost/services/wininit，非Codex；worker约13秒恢复，监督器下一分钟由OS重启；外层捕获实测退出码15。真实掉线/恢复消息及受控一分钟任务跨重启投递已实际读回，提醒completed/outbox sent/attempts1，不冒称人工自然语言入站。152项watchdog/单群/validation回归通过，Ruff通过。08:54:22生产只读PID760395及hash/心跳/Feishu线程仍正常，未改SSH或服务器。

详细验收docs/development/personal_monitor_watchdog_20261025.md，证据data/acceptance/personal_monitor_20261025/watchdog。后续用户两条真实消息已恢复12笔，详见下方补记；仍待HK遮挡值、repo期限与完整正式入口。整机离线期间不能本机实时告警，未接外部主机监控。未部署、提交或推送。

### 2026-10-25 底层采集公司并发与真实小批验收

- 用户授权公司间2—3路并行。backfill_ashare_sources.py 增加 prepare --company-workers 1/2/3，由冻结计划控制；生产使用一个进程内3个公司线程、各自HTTP会话和公司目录，根状态由主线程集中写入并展示 active_companies。最多派发3个在途任务，STOP或全局故障后停止派发并等待退出；局部partial继续队列。
- PDF归档共用批次协调锁，沿用批次内3路下载，总下载并发不叠加为9；避免多个下载批次互相把在途任务认作中断。Baostock整段SDK会话继续使用原跨线程/跨进程单连接锁、共享限速/预算/封禁状态，不改前面阶段。
- 真实小批复用西安银行partial，常熟、兴业、北京均完成；实际公司任务峰值3，30次TCP采样Baostock连接峰值1，实际SDK锁竞争测试峰值1。小批36个原件/行情/财务文件逐一核验存在及保存哈希；旧批次及引用文件1281个哈希未变。162项回归通过、1项跳过，含tests/validation/；仅将两个已有下载器时序测试改为确定性同步，未为此修改下载器运行逻辑。
- 新现行批次screened_parallel3_20261025复用22家已处理结果（21 complete、1 partial）以及已有PDF、行情/财务原字节文件、筛选响应和上市证据，准备阶段无网络请求，启动后这22家未重新派发。09:00核验唯一PID18212及三个活跃公司任务，后台与原监控a均已恢复；旧批次STOP保留。通过池335、待核91，来源采集完成不能当作字段齐全或研究ready。无付费模型调用、无提交推送。证据data/acceptance/backfill_company_parallel_20261025/验收.md及JSON。


### 2026-10-25 09:07 真实持仓确认跨消息恢复（仅本地）

- 收到用户08:54的单位确认/H股数量和后一条代码消息，发现模型遗漏单位标志、日期只写当天时回退零点、人工代码消息无法引用前条数量。修复认证原消息落库及有界引用（同owner/chat/account、最近10条、15分钟内），明确分别列表按顺序映射；数量时间保留原消息，模型价格/币种/市值不引入事实。同日只复用本批已接受时点，外部人工revision仍冲突保护。
- 受控来源工具恢复8笔A股/ETF/REIT及4笔港股（01816=2000、00728=2000、09999=100、01339=19000），测试账户账户revision3/12笔；相同来源再提交不增加版本。原始失败回执保留，不能冒称自然语言自动重试通过。港股未用人民币市值反推股数，两笔逆回购72000元仍待期限。修复说明实际发送并读回user_card_content。
- 原生计划任务已加载最新源码，监督器39720/worker36720身份与新鲜心跳核验通过。未改生产、未提交推送；命名文件增量清单19项和基于已捕获脏基线的patch已更新，不包含其他采集/研究实验。

最终来源修复回归195项全部通过（final_facts.junit.xml），watchdog/单群/validation152项全部通过（final.junit.xml）；各集合重叠不累加，相关Ruff通过。本地最终健康状态与实际通过/待验收清单已保存watchdog/summary.json、final_status.json及监督验收文档。

### 2026-10-25 正式研究默认模板解除历史案例绑定（仅离线）

默认v5配置同时加载带固定行业/公司角色的共通约定、估值约定、三轮prompt及真实案例排版示例；旧测试甚至要求system包含“工程两家深入”。四大行实跑使用了独立银行模板，三轮请求未携带该案例指令，但默认入口仍有同类污染风险。问题来自模板及测试契约，不应归为随机模型输出。

新增通用版本research_basis_v4、valuation/draft/audit/final_v6和research_report_example_v6，默认配置切换到research-report-v6-input-driven-scope。实际对象数量、行业、同行/参照角色、历史窗口来自本轮输入；没有明确参照关系不自行指定，单公司不虚构比较对象，行业现金方法按业务适用。固定三轮、现价/预测/IRR/hurdle及真实PNG交接保留，运行脚本和依赖不变。

替换把案例文字当system契约的断言，新增默认CLI跨对象数量/主题的完整三轮离线回归；历史案例数据/图表测试仍保留。封存批次、原请求/原文及旧版prompt不覆盖；默认源配置更新的外部哈希变化如实记录，不重写旧freeze。验收证据在data/acceptance/research_generic_prompts_20261025；无付费API、不重跑或修改银行报告、不触碰底层采集进程及冻结实现。

### 2026-10-25 全项目架构图落文档

新增[全项目架构总览](../architecture_overview.md)，保存全项目 Mermaid 总图、财报研究放大图、执行边界和源码导航；README 与原架构页增加入口。原架构页中“未来第三阶段未实现”更新为现有横评与固定三轮正式研究的显式交接，不覆盖公共运行层的候选/部署验收边界。

明确日报/会话/个人监控的公共执行器、优化独立子进程和财报独立批次是不同执行机制；研究文件不自动发送、不直接转为策略或实盘下单。源码导航覆盖数据、策略、回测、助手/Pi、个人监控、采集、提取/对齐/摘要/横评/研究、离线模型、历史只读及运维。仅文档变更，不调用模型/采集/通知，不启动或调整后台任务，不修改旧产物。

### 2026-10-25 架构补充告警与搜参子系统

按用户指出的缺项，将告警从日报总框中显式拆出，并新增告警与搜参放大图。告警图区分价格阈值/区间状态、活动策略最后有效交易日事件、Session汇总、报告触发条件及渠道回执；规则状态不是发送确认，不将其描述成端到端去重。个人提醒和运行健康事件继续各自维护状态。

搜参图覆盖按市场合同、数据预检、资源规划、ask/tell、评价缓存、统一策略/回测、排名Gate、断点/候选归档、后置稳健性和留出、同市场相对晋升、自动/人工激活及消费者版本绑定。同步更正旧架构页“仅人工激活”的简化描述，区分当前相对晋升路径与绝对留出检查。仅更新文档与索引，未运行优化、报表、通知或付费模型，也未修改业务代码及旧产物。


2026-10-28收尾：用户取消5%性能硬卡点，接受公共层持久入队、claim/结果/互斥与健康恢复的必要读写开销；原数值失败保留，不冒充优化通过。当前v16完整回归2178通过/7环境跳过/0失败、Ruff151通过。原测试Task已刷新隔离证明并恢复；电脑控制因无法可靠确认浏览器地址而终止，当前真实消息补验、阶段2源码退役及最终Root入口核对仍未完成。生产未部署。详见docs/development/shared_runtime_migration.md及closeout_20261028证据。


2026-10-28 22:11用户授权后，公共运行层已独立提交c7d7656快进推送并以38文件哈希覆盖部署；其他未发布研究/优化器提交和原本机脏源码保留。最新定向回归去重1515项通过，另生产通知overlay82项通过；Ruff功能规则通过、102历史诊断无新增。服务器隔离schema/业务行哈希/Pi兼容通过，最终单owner/3进程/4模块7任务、实际四调度时点、38源及38保护文件哈希核验通过；test-1933健康卡片单次实发并正文读回，无新增邮件或报表运行。首次部署检查读取SDK过早已实际回滚并保留证据，修正等待后成功。私人本机验收数据库未导入生产，Root旧测试源码退役及新人工入站仍保留原M13/M17/M18边界。详见deployment_summary.md及迁移文档最新部署节；旧“生产未部署”描述属于历史阶段。

### 2026-10-29 文档索引、架构图归并与退役说明清理

新增 docs/README.md 作为现行入口索引；将原架构页的公共运行层图归入 architecture_overview.md，与全项目、告警、搜参、财报研究图集中检查。architecture.md 去除重复图和逐轮验收流水，保留运行合同、模块职责、依赖债务与状态目录；详细历史仍由迁移记录保存。

根据现有源码和已有部署记录，修正公共层已部署、本机任务主动停用、旧测试源码尚未退役的区别；更新 Python 3.10 下限、唯一调度器、参考持仓版本绑定、自动/显式激活及现行财报研究交接说明。删除八份旧研究模块/中间设计说明，边界合并到 research_retirement_20261024.md，修复引用。删除前 Markdown 原文件位于 .trash/documentation_cleanup_20261029/before.zip。仅整理文档，不改变业务实现、历史数据或运行任务。

本次检查：213个本地Markdown链接及锚点无缺失，代码围栏闭合；架构独立页5张Mermaid图，原架构说明不再保留重复图。validation 36项通过，文档git diff --check通过。检查清单及变更前后范围见 .tmp/documentation_cleanup_20261029/verification.json；本轮未重新核验生产运行状态。

### 2026-10-29 架构补充飞书开关、实际路由与环境差异

独立架构页新增第六张图，展开 assistant.enabled、native/Pi 后端选择、直接查询/模板分支、thinking 模型请求及模型决定是否继续工具调用；斜杠命令独立于普通助手开关。明确通用 Pi 桥保留与旧研究专用接入退役的区别，当前财报三轮脚本没有接 Pi。

基于当日09:03线上SSH只读配置/源码/既有审计及本机配置，增加环境表：本机助手默认关闭、native默认；线上助手开启、Pi、deepseek-flash、high、16轮/48工具。harness/settings源码哈希相同，client/service不同；没有把本机研究退役范围外推到线上。本轮没有修改配置、启动任务、调用模型、发送消息或部署。脱敏快照见 .tmp/feishu_quality_audit_20261029/architecture_snapshot.json，不向文档复制原会话。

本轮文档检查：219个本地链接/锚点无缺失，代码围栏闭合，独立页6张Mermaid图；validation 36项通过，受影响文档diff检查通过。

### 2026-10-29 飞书研究问答去模板、强制Pi检索与思考分析（本地候选，未部署）

删除单股票查询直接模板返回、本机估值旧历史查询截流和相应死代码；预查只作为模型输入。资料研究问题、相关追问及持仓公司分析强制走Pi，普通闲聊保留原后端选择；持仓/提醒的原子业务回执保留。研究无本轮资料也无真实检索却准备作答时补检索一次，仍未检索则拒绝发送该结论；导航目录和旧会话不计作本轮资料。实际查询失败回到模型继续换来源或说明缺口。模型HTTP继续thinking enabled/high，检索无须研究提案确认，写配置/回测等保留原确认。

交互/validation完整回归990通过/7环境跳过；后续提示及持仓分流另做定向复验24通过/1跳过，实际Pi检查有明确Node路径的独立通过记录。Node桥18通过/0跳过，真实工具schema夹具从19项补齐当前38项；旧研究退役/调度/归档63通过。计数范围不同且有重叠，不相加。两次真实DeepSeek+Node Pi隔离回放分别为源码6轮/8工具、600150查询6轮/9工具，均有reasoning；后者搜索HTTP202后转读已归档报告，未拼模板或发送飞书。局部真实路径不等于全量投资判断质量或正式入站验收。

已只读提取生产service/harness并准备两文件哈希候选：处理函数与已测本机逻辑一致，生产其余30方法及初始化AST保持，保留_application_config等线上差异。候选先99项通过，最后分流及异常边界变化再15项定向通过，未以其他本机脏代码覆盖生产。Ruff和差异检查通过，文档221链接无缺失。未修改实际配置、启动本机测试任务、重启或部署、提交推送。详情见[修复记录](../development/feishu_research_qa_20261029.md)，完整证据在.tmp/feishu_qa_repair_20261029/verification.json。


### 2026-10-29 运行版本标识

软件版本升至1.18.0；新增部署生成的真实源码/模板构建清单，日报、简报、PDF、帮助和服务状态统一显示软件版本、审核提交与构建指纹。未知/损坏/源码不符显式显示，避免生产覆盖包误读旧Git HEAD。宿主/worker启动一次固定身份，不增加守护、调度、外部依赖或逐心跳扫描。保留所有原业务、投递和时点，未包含其他实验。命名覆盖及CI均先安装/登记再启动，失败时恢复登记。详见[版本标识](../development/build_version.md)。

### 2026-10-29 公告原件慢速归档（独立后台批次）

用户授权先低速下载招股、重组/资产购买、评估估值和交易所函件回复原件。新批次为`data/fundamental_quant/source_backfill/disclosures_335_slow_20261029`，冻结已知有效公告及244份招股附录，共3,884个链接、实际命中322家公司，来源仍是335只筛选池；3,833个PDF、51个历史HTML。未继续扩大metadata检索，不将未命中或未完成查询说成没有公告。

仅复用现有公开来源下载、共享SQLite限速和worker锁，单路、10秒调度时隙、原件无单文件大小上限、磁盘余量10GiB、文件600秒；不修改现有下载库、前面阶段或旧原件。原件按URL归档并保存来源/大小/SHA256，PDF增量登记资料目录，历史HTML单独保留网页原文且不认证其为招股全文。文本、字段与模型提取均不运行；局部404/永久缺口继续，暂时错误最多3次并遵循退避/Retry-After，源站访问限制及磁盘/完整性/程序问题停止派发，STOP持久停且保留数据。

5项离线验收及tests/validation的36项通过；真实小批4 PDF+1 HTML均保存且哈希匹配，重复执行新增请求0。一次性按需低权限Windows任务`TradeEyesDisclosureSlow20261029`在OS侧隐藏启动，任务无触发器/自动重启，不依赖聊天进程；启动身份、定义、日志及验收在批次launch.json/os_task.xml/background_acceptance.json，plan SHA为`ced7ab7b9591e71c01fc6f9e912d2f4da01af88934b06ac5e826e2e3450e257a`。聊天监控automation-3正常进展静默，仅完成或需要介入时通知；不恢复用户停止或源站封禁。每次请求日志写盘时间存在小量抖动，不冒充精确HTTP线上起点测量。无付费模型调用；未部署或改动主服务。

2026-10-29 19:59版本标识已实际部署：v1.18.0 /6a55894 /bundle b7e30404，通用清单核对276源/模板，宿主与独立进程一致，11文件覆盖/3进程。日报简报PDF/帮助/状态统一显示，无新发送或重跑。首次回滚原因已查明是既有30天保留规则清理17条旧会话，原完整库备份保留；最终严格数据检查通过。具体证据见data/acceptance/build_version_20261029/delivery.md。

### 2026-10-29 公共层旧链路正式退役

用户确认今日三报并授权立即退役。七个已发布6a55894薄入口/测试实际落盘，删除专用watchdog、安装器和专属测试，注销已停用的TradeEyesPersonalMonitorTest；三库一致性备份及前后物理/逻辑SHA全部相等。实际Root定向258通过/零跳过，含validation；七文件Ruff、24项独审通过。生产v1.18.0/6a55894/b7e30404只读核实当前一个公共宿主及SDK，无旧业务cron/timer/进程；本次没有生产改动或重启，研究/优化器和历史数据保留。health mask与部署清理兼容有意保留防复活，M13补验和旧partial/性能结果不改写。周末退役提醒automation-2已删除。详见[退役记录](../development/shared_runtime_retirement_20261029.md)。


### 2026-10-30 财报PDF压缩与服务器空间清理

原335公司FY2019—FY2025可用年报2,164份，加建行2022官网补充，共2,165份、11,078,538,924字节，逐份核对原SHA256且无内容重复。载荷只有按公司/年度命名的原PDF；本地新批次data/transfers/annual_reports_pdf_20261029，采用xz/LZMA2 9 extreme、固定1GiB拼接流及四进程，包大小7,371,683,236字节；支持多xz流的本地逐PDF复核通过后才写package_receipt.json。旧财报、研究产物及公告下载批次不改，未调用付费API。

用户明确批准清npm/pnpm/pip未引用缓存、超过168小时Docker构建缓存及journal归档保留512MiB；按操作前后df实际释放6,186,287,104字节（5.761GiB），可用由1,380,429,824升到7,566,716,928字节（7.047GiB）。Docker镜像清单、运行的3个容器及主服务PID853160均不变；主服务state=running、transport_ready=true、飞书线程存活。系统日志约504MiB；未删除业务数据、优化历史、数据库卷、swap、/tmp或旧传输中转。明细见本批server_cleanup_receipt.json及执行JSONL；validation 36项通过，传输离线7项通过。

完整包上传后仅余约186MiB，未达到512MiB保留余量，还差341,837,220字节。初始SFTP/原生Windows SSH大块传输存在停滞，未发布服务器正式包。用户随后明确选择“保持镜像，暂不上传”；本批UPLOAD_PAUSED及transfer_status.json记录人工暂停，上传脚本在联网前和每块前均检查，不自动恢复。保持所有镜像及本地原件，本地完整性校验可继续。后续须新授权才恢复上传，不可把本地归档或服务器暂存当作Pi已可读取正文。


### 2026-10-30 财报prompt缓存只读审计

核对DeepSeek当前前缀单元缓存文档、280份实际提取请求、317个已知usage回执及单公司摘要/横评/正式研究请求。提取已知输入命中82.25%，最终280正常请求81.79%；新摘要0.97%、横评0.67%、正式研究0.60%。280请求尾部12,065token目录及规范示例逐字相同，7个附注共用5,424token schema；离线零丢失重排可把冷读一次的前缀复用模型由83.44%提至89.39%，条件费用空间约0.57—0.63美元/同规模28份。另定位单独--block收缩前置ownership、摘要段号前置及研究分阶段system破坏大材料公共前缀；没有改执行器或冻结prompt。

提取已知费用10.665927126美元中输出含reasoning占8.412219美元（78.87%），不能把输入命中改进当整体同比降本。140个缺usage调用保持未知；21个未进入最终正常结果的已知回执0.798580836美元不能一概视为可省或输出截断。582份输入哈希未变，280个重排材料文字/图像守恒、计价分项及usage恒等通过；未做付费语义/命中/时延/线上Pi验收，DeepSeek调用0次。详见docs/development/financial_prompt_cache_audit_20261030.md及data/acceptance/financial_cache_audit_20261030；上传暂停和资料采集原状态不动。

### 2026-10-30 固定完整归属合同与取消下载跟进任务

按用户授权，取消本聊天公告下载监控automation-3及已停止的Windows按需启动任务TradeEyesDisclosureSlow20261029，保留原件和批次资料；取消回执位于data/acceptance/fundamental_ownership_fix_20261030/scheduled_task_cancellation.json。未恢复下载或上传。

本地scripts/build_fundamental_data_v2.py把所选profile的完整contract_names与本次执行names分离：frozen_job_ownership固定完整合同，--block只缩小实际调度、预算和冻结执行块；模型中的具体块继续位于财报后任务尾部。call_plan.json及summary.json增加execution_plan，分别记录ownership_blocks和execution_blocks。replay及既有请求、prompt、schema、示例和结果不改写。

真实工行2019 PDF离线验证：默认10份请求与改前字节一致，capital/notes_assets单块请求与全量对应请求字节一致；单块max_calls=1、完整归属10块，582份原审计输入哈希不变。新增9项回归覆盖多profile、无效块和模拟单块调度，相关回归含tests/validation共224项通过，Ruff及格式检查通过，实际模型/网络调用0次；离线证据见data/acceptance/fundamental_ownership_fix_20261030。服务商命中率、时延及语义影响未做付费实测；不将预期收益当作实测结果。

### 2026-10-30 公告原件下载隔离后恢复

按用户随后明确“做好隔离就恢复”的授权，新建disclosures_335_slow_resume_20261030续跑，旧批次612个文件和580份原件保留。仅将可选进度HTML写入改为有限重试、失败记stale继续；任务状态、源站封禁、磁盘和完整性错误仍停止。单路10秒共享限速、尝试次数、3884链接范围不变，旧成功不重下。13项隔离验收及36项validation通过，真实3份小批全成功，包括旧18MB待重试文件第3次成功；核对后台唯一PID/创建时间/命令后继续下载。新按需Windows任务无触发器、低权限、无自动重启，周期聊天监控未重建；不运行文本/字段/模型、不调用付费API、不恢复上传。详见[续跑记录](../development/disclosure_download_resume_20261030.md)及新批background_acceptance.json。

### 2026-10-30 终止旧基本面量化实验

用户明确删除旧估值特征/行业评分/消融、监控池状态回放、MOE/定价训练和三个Ke入口；原因是效果始终未正，后续不再重新尝试。27个脚本及专属实现、测试、Ke profiles移除，保留线上PB/CAPM、新财报链路和共用数据/估值组件；旧记录仅作历史，不再据此建议补样本或重试。源码备份及受影响验证见.tmp/retire_failed_experiments_20261030/，简要范围见[终止记录](../development/failed_quant_experiments_retirement_20261030.md)。未推送或部署。

### 2026-10-30 工作区整理与模块化本地提交

按用户要求删除已终止实验和失效技能命令，保留七问方法原文；数据、原件、冻结请求、验收历史和备份留在ignore目录。删除34个纯Python缓存目录27,134,941字节，3张生成图只取消Git跟踪。本机历史输入清单/会计网页移至data，旧无调用夹具归档；不把未审源码用ignore隐藏。逐文件模块审核、哈希绑定和清单精确匹配后本地commit，未push/deploy，未启动真实机器人或付费任务。

修复Windows同一时刻部署编号碰撞；补可移植测试请求、环境开关隔离、直接依赖、真实网络标记及当前数据合同夹具，保持原锁/CAS/只读/超时断言，私人持仓回归数据改明确合成。广泛离线原结果4456通过/31失败/24跳过，31个原失败节点随后全部专项复验通过；validation36、财报441、漂移41、渠道185通过（1平台环境跳过）、隔离源码51通过（1平台环境跳过）、Node24桥18通过。各gate有重叠，完整默认Ruff既有风格欠账和未做新环境安装仍明确列限，不冒充全量重跑或线上验收。详见[清理记录](../development/workspace_cleanup_20261030.md)及本机.tmp/workspace_cleanup_commit_20261030/。

### 2026-10-30 本地优先合并范围确认

按用户选择，搜索自动激活、日报/简报条件、告警、参考持仓、财务数据、进程启动、退役、对话/研究采用本地来源；旧基本面有支持链或争议的保留。对照effee64与21:10生产实际快照，253项差异逐路径形成来源决策：41修改源码、39新增源码、33研究合同/Prompt选本地，35确认退役源码及1个无消费者旧配置列删除，6混合文件需按修改块合并，其余98项保留。另明确13个模型输入/冻结/tokenizer依赖，应用前还需生产预检。

保留4个FQ历史只读兼容文件和17个基本面适配文件。55个线上独有旧文件按争议保留；其中旧手工CLI35文件闭包依赖旧reporting接口，不能盲目换本地历史格式化壳。评价期限及生产运行配置未被明确选择，保留9m/24m两侧；已只读复现本地policy loader读取生产旧约束报evaluation_horizon_policy is required。此轮只完成具体合并范围与闭包审阅，没有执行Git分支合并、改动产品源码、运行配置或部署；不宣称混合候选已通过回归。详见[合并范围](../development/local_preferred_merge_scope_20261030.md)及.tmp/production_parity_20261030/merge_scope/。

### 2026-10-30 执行本地优先源码合并

用户授权后，在codex/local-preferred-merge隔离工作树完成源码级合并：保留本地自动激活、报表条件、告警、参考持仓、财务和对话研究；未选9m评价/运维按生产真实快照保留。5混合业务文件恢复期限节点，旧接口冲突以legacy_reporting原件和两个纯legacy_service助手隔离，9个旧消费者仅改import；55争议源码保留但不注册，4个历史壳和worker blocked语义不变，未恢复3478行旧服务。

本轮全量离线4519项首轮4476通过/3失败/40跳过，原3失败（运行目录假设及2个24m测试夹具）修复后逐节点3通过，产品源码未变；最新唯一节点4479通过/40跳过，不冒充又一次全量重跑。另validation36、旧链69、混合期限123（2既有skip）、Pi Node24桥18均有新回执且有重叠。独立核对253项+13依赖无越界或缺失；Ruff功能规则18条unused诊断均来自6份逐字保留生产脚本，非新增代码。Python完整环境安装/真实通道/数据提供方/部署未验，未push/deploy/启动Bot/调用付费模型。原E工作区两份文档改动已快照保留，生产配置和业务库未写。详见[实施与证据](../development/local_preferred_merge_scope_20261030.md)。
