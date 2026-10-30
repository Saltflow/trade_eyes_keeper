# 三公司三年财报：一次冻结的API数据演示

用户授权：接好一个入口，输入2家工程公司、1家白酒公司各3年财报，付费API跑完三个数据模块；
API正式运行后不得修改代码、产物或流程。本文在正式请求前写好，之后不回填、不改写结果。

## 一行命令

在仓库根目录执行，输出目录必须尚不存在：

```powershell
python -X utf8 scripts/build_fundamental_data.py run --source data/fundamental_data/local_manifests/fundamental_data_demo_20261023.json --output data/fundamental_data/api_demo_20261023_first --paid
```

`--source`接受原PDF目录，或仅登记原PDF路径/公司/财期/哈希的JSON清单。
不要求先生成旧FR Pack、逐页文本或研究packet。目录模式由API识别公司和财期；
清单模式交叉核对声明的公司/财期，不允许模型错认后静默改绑。
不传`--paid`只做本地PDF准备、容量估算与冻结；不调用API。再次运行不能覆盖原输出目录。
旧extract/check/history/metadata离线命令保留，付费run不调用旧解析器或研究框架。

本次原始PDF：四川路桥600039、安徽建工600502、泸州老窖000568，2023—2025年年报，9份2771页。
来源清单为`data/fundamental_data/local_manifests/fundamental_data_demo_20261023.json`，全部原始PDF的SHA-256已从既有来源登记重新核对。
仅复用原文件及来源标识，没有把既有提取结果或旧研究结论提供给模型。

## 固定处理与格式

1. 本地PDFium读取全部物理页文字；低于80字符的页附原页JPEG。每页保留物理页编号，未截取报表章节。
2. 9次单份提取，固定三路并发：CFO、现金Capex、投资收益，管理层摘要，FCF边界，七类政策/金额附注，六类公司元数据。
3. 每家公司1次三年整合，共3次：共同口径复用、逐年变化、管理层变化与缺口；数字由程序原样输出CSV，不让模型重算。
4. 1次元数据整理：三家公司六类元数据、3组公司对、同行候选/不可比/未定、需考虑因素与缺失项。

共最多13次请求。DeepSeek Flash、JSON模式、非思考模式、每次最大输出32768 token，
每次仅尝试一次；不重试、修复JSON、补提取、切模型或换流程。超限、无usage、HTTP失败和不合格引用原样记录。
固定的格式和阶段协议在`config/fundamental_data_api.md`；没有运行投资分析、估值、报告审查或通知。
原始PDF及逐页源文本完整保留，API阅读输出是有出处的精简摘要，不冒称逐字全文或会计语义已验收。

模型输出的每条证据必须带文档ID、原物理页、连续逐字短引句；检查引用是否位于该页。
图片读取但无法在文本层重放的引句会留下核验问题，不被程序伪装成逐字验证通过。
金额要求十进制字符串、不补零，保留披露单位/币种/合并口径；简单检查金额是否出现在所引行。
三年整合候选阅读文件上限20000字符，超限仍保留首次candidate及失败标记，不截尾、不重新请求。
单份未通过检查时，后续仍按固定流程整理候选，但明确携带上游问题，不升级为事实已审定。

## 冻结与结果

正式HTTP请求前生成`freeze.json`：代码、协议、输入清单、原PDF、逐页准备资料、tokenizer、参数、依赖版本。
每次请求及最终完成前核对冻结哈希；代码快照在`frozen_code/`。所有产物使用排他新建，不覆盖已有文件。
程序按预定规则首次生成下列文件，运行后只允许读取查看：

```text
README.md                    自动生成的总入口
freeze.json / frozen_code/   正式运行前的代码、输入和规则
sources/                     完整逐页来源，含必要原页图像
extract/<doc>/               单份data.json、candidate.md、check.json
history/<issuer>/            三年首次整合候选及独立检查
metadata/companies/          元数据与同行候选及独立检查
financials.csv               原精度数值，不由模型总结重算
*/request.json               未含密钥的实际请求
*/attempt.json               请求前的尝试凭据
*/response.body/content.txt  原始HTTP响应及模型原始正文，不修改
*/receipt.json               状态、原始usage、开始/结束时间
summary.json                 整体状态与分项问题
output_hashes.json           首次生成全部结果的SHA-256
```

状态即使是needs_review也不能改代码后重跑、修正文稿或更换材料；失败本身就是这次结果。
运行后的检查只读取冻结及结果哈希、产物内容，不写回“修正版本”或额外修复回执。

费用软件上限5美元；本地文本估算约183万token，含协议/图像/边界预留的单份请求合计约193万token，
实际以每次API原始usage为准。已知token总数及按峰时全部无缓存估算的费用上限会自动写入总回执，
缺usage保持未知；不混用Codex会话token或provider控制台余额。
当前协议依据官方 [JSON输出](https://api-docs.deepseek.com/guides/json_mode/)、
[视觉输入](https://api-docs.deepseek.com/guides/vision/)、
[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)和
[模型价格](https://api-docs.deepseek.com/quick_start/pricing/)。

本次不是既有五脚本/每个100行极简目标的完成声明；旧研究区继续冻结。
新增的API流程与旧算法分开，真实调用/代码范围可由freeze.json核对，未用旧引擎作隐藏兜底。

2026-10-30 整理：上述清单含本机原件绝对路径，作为历史运行输入保留在已忽略的 `data/`，不作为可移植源配置提交；原件及来源哈希未改动。
