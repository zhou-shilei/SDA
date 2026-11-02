# CSJ117 第 25 页序列表修复

修复分列、完整取行、编号列隔离及 dataagent2 的失败状态与恒定区导出。
长序列框的左缘轻微越过列界时，以文字主体归列并继续验证行归属。
按原图中的连续竖线限制裁行扩边；合并的前置 SEQ ID 标签通过像素分隔，
不修改识别字符。长度不足与非标准字符继续拒绝。

## 原图实测

保存的 PPVL 表格结构不变，重新运行本地检测、原始像素裁行及 PP-OCRv6 medium 识别。
所有坐标自动得到，无人工裁图坐标，无新增 LLM 调用。

| SEQ ID | 区域 | 实际行数 | 识别行数 | 残基数 |
| --- | --- | ---: | ---: | ---: |
| 65 | VL | 3 | 3 | 107 |
| 66 | VH | 3 | 3 | 120 |
| 67 | CL | 3 | 3 | 106 |
| 68 | CH | 8 | 8 | 330 |

四条均通过流程、完整性和字符校验；本记录不宣称所有残基已独立人工逐字确认。
整张表其他三个定位/完整性失败单元格仍保留为失败，未借用相邻序列。

实测证据：[live/validation.json](live/validation.json)、[live/table.json](live/table.json)。
原始裁行图保存在 `live/inputs/sequence_ocr/`。
复现：使用 SDA 环境运行 `python try/validate_csj117_recovery.py --output 新目录`。

## dataagent2 校验与导出

在独立目录中，仅替换 4986、4989、4992、4995 四个来源。
依据保存的原始模型提议重新逐字定位和校验，不借用旧 benchmark 的残基。
其余已接纳序列逐条校验来源哈希与片段；原有已支持的 VH/VL 和完整链均未改变。
原有图片样式定义、48 个实体、188 条测量和 6 个实验保留。

CSJ117 四个区域全部 supported；可变链槽位从 9 增至 11，完整 VH/VL 配对从 1 增至 2。
CH/CL 经 `other_sequences` 导出，保留编号、字符和来源；不占用 VH/VL 字段。

结果：[rebuilt/recovery_report.json](rebuilt/recovery_report.json)、
[rebuilt/dataset.json](rebuilt/dataset.json)、
[审核 CSV](rebuilt/dataagent2/review.formal/items.csv)。
复现：`python try/rebuild_csj117_recovery.py --output 新目录`。
这些文件独立保存；历史 Studio 运行和登记未改写。

## 后续运行与验证

- 新 Studio 运行会先检查 OCR 修订，旧序列复识别在新目录更新，再交给共享模板和 Reader。
- Reader/回答均可见 OCR 状态；失败保留明确来源和图片重识别动作，不占文本问答预算。
- 响应接纳与问题解决分别记录；零新增回答不会被标为已补全。
- 网页字段显示具体 OCR 失败说明；已有恒定区在对应链的证据面板展示。

153 项 Python 回归通过，覆盖真实缺陷的跨列多行、合并编号行、裁图笔画保护、
相邻实体隔离、OCR 任务调度、失败元数据、恒定区导出与旧修订升级。
缓存相关测试显式设置 `SDA_CACHE_ENABLED=1`，禁用缓存测试自行设置为 0；未改项目缓存设置。
3 项 JavaScript 测试通过：审阅元数据、单元格坐标、序列证据位置。
日志：[regressions.log](regressions.log)。

## 全文恢复流程补充与新任务（2026-10-27）

序列 OCR 修订升至 5。只有完整可见的横竖表格线与 HTML 行列一一匹配时，
才使用其定位完整单元格和无名称的续表首行；横线还须跨越名称列，排除序列下划线。
过期与失败 OCR 在新图片目录重识别，仍失败的序列进入独立原图复核。
单个原图复核失败不会丢弃同表其他有效来源；旧修订的成功标记不作为重试失败后的回退。
完整链在另一页识别失败时，VH/VL 缺口也会得到对应 OCR 失败来源。

Studio 对本轮所有文本问题各处理一次，相似名称仅用作检索提示，身份转移仍需明确引用。
首轮问题另存 `questions.initial.json`，`questions.json` 更新为最终未解决队列。
新增 `quality_audit.json` 检查序列片段、测量值和单位的字符来源及测量到 VH/VL 的关联完整性。
不能确认的边界或名称关系仍保留缺失，不通过常见基序或名称相似度自动补齐。

验证：160 项 Python 与 3 项 JavaScript 测试通过；日志：[followup_tests.log](followup_tests.log)。

已通过 Studio `/api/patents/retry` 提交最后上传的 `WO2026086808`（63 页）完整 PDF 新任务，
指定 `extraction_engine=dataagent2`，HTTP 202。
源 PDF SHA256：`b7cf38b63fbffa4f1061c01cea3642e02b12a275f23f06675fc706090f35ec00`。
新运行：`937c79e37ee4407a9dac23cf3f4c4b28`。
提交凭据：[submission.json](full_rerun_20261027T071820Z/submission.json)。
本节记录的是任务提交，尚不代表完整识别或准确性验收已经结束。
