# 公告原件下载故障隔离与续跑

2026-10-30，用户明确授权“做好隔离就恢复”。旧批次保留原样，续跑目录为`data/fundamental_quant/source_backfill/disclosures_335_slow_resume_20261030`。

## 原因与修复

旧下载进程在2026-10-29北京时间22:41:31成功归档秦皇岛港公告后，替换进度页`index.html.tmp → index.html`遇到Windows拒绝访问。网页发布与下载主循环未隔离，异常退出整个进程。那份传输不完整、等待重试的公告没有阻断后续；它进入等待后另有21份下载成功。没有记录当时占用网页的进程，不能认定是浏览器或杀毒软件。

续跑仅修改批次脚本的可选HTML发布路径：最多3次写入/替换，短暂等待0.1/0.3秒；持续失败写`progress_page_status.json`为stale，下载调度继续。诊断文件本身不可写时退到stderr；页面恢复后记录current。`jobs.json`、`status.json`、原件和回执仍是可靠状态，写入或完整性错误继续停止；401/403/429、访问验证、10GiB磁盘保留量和STOP规则保持。

未增加下载线程池，仍单路、共享worker锁与10秒请求时隙、单文件600秒、临时错误最多3次及60/300秒退避。不以“90%”截断队列或补造成功；局部失败逐份记录，继续其余可运行任务。

## 保留与验收

- 原3884链接、322家公司范围不扩展，不追加metadata检索、文本、字段、摘要或付费API。
- 启动前逐份核对580份旧原件、2,758,782,127字节的保存哈希；复制状态和回执，原文件按URL/哈希复用，失败次数保留。
- 旧批次612个文件快照未变；旧冻结代码和plan不改。新批次独立冻结本批实现、启动器及旧输入来源。
- 故障注入13项通过：持续网页替换拒绝、网页及诊断均拒绝不影响队列；核心状态、源站限制、原件损坏和磁盘错误仍阻止派发。仓库`tests/validation/`36项通过。
- 真实3份小批全部成功并核对哈希：旧待重试公告18,005,562字节，第3次成功，另外2份是原未下载文件；旧成功原件新增请求0。

由按需Windows任务`TradeEyesDisclosureSlowResume20261030`隐藏承接后台，RunLevel=0、无触发器、无自动重启；不依赖聊天进程。此前取消的周期聊天监控保持取消。后台启动后已核对PID、创建时间、完整命令和唯一下载进程，并观察到小批之外继续成功归档。

进度和证据：[进度页](../../data/fundamental_quant/source_backfill/disclosures_335_slow_resume_20261030/index.html)、[逐文件状态](../../data/fundamental_quant/source_backfill/disclosures_335_slow_resume_20261030/jobs.json)、[小批验收](../../data/fundamental_quant/source_backfill/disclosures_335_slow_resume_20261030/sample_acceptance.json)、[后台验收](../../data/fundamental_quant/source_backfill/disclosures_335_slow_resume_20261030/background_acceptance.json)。文件数量是采样时刻快照，后续以根状态和回执为准。
