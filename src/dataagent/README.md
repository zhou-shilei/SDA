# dataagent 迁移版

从同级 `dataagent/src` 复制业务实现、模板及 `prompt`，保留原 LangGraph 的表格分类、
文档规划、序列提取、记录关联、构建体校验、证据核验与有限修复流程。
原文件 SHA256 记录在 `upstream_snapshot.json`，运行时不依赖同级 dataagent 仓库。

此入口复用 SDA 已有的两路 OCR，不调用表格 OCR、页面 OCR 或 PDF 文本层提取。
SDA 的 PDF 总入口、`src/graph.py` 和网页导入/重新提取默认使用此流程。
`main_pipeline.py` 将原生结果转换为 SDA schema，`pipeline.py` 保留旧 document 模式。

## 运行

在 SDA 根目录执行。当前 SDA 环境已经包含这些依赖；其他环境可以先安装：

```bash
python -m pip install -r src/dataagent/requirements.txt
```

主流程复用 `pdf2doc.py` 生成的 `structured` 目录并导出 SDA dataset：

```bash
python src/graph.py \
  --image-dir /path/to/run/structured/layout \
  --structured-dir /path/to/run/structured \
  --text-pages-jsonl /path/to/run/structured/page_ocr/pages.jsonl \
  --output-dir /path/to/new-results
```

也可仅运行原生 dataagent 导出：

```bash
PYTHONPATH=src python -m dataagent \
  --structured-dir /path/to/run/structured \
  --output-dir /path/to/new-dataagent-result
```

或者分别指定两路 OCR：

```bash
PYTHONPATH=src python -m dataagent \
  --document-id WO2026086808A1 \
  --table-ocr /path/to/run/structured/table_ocr/tables.jsonl \
  --pdf-ocr /path/to/run/structured/page_ocr/pages.jsonl \
  --output-dir /path/to/new-dataagent-result
```

`--table-ocr` 也接受 SDA 表格结果 JSON、JSON 数组或逐表结果目录。
有裁图清单时，可传 `--layout-manifest` 和 `--image-dir`，补充裁图路径、页码和区域。
`--structured-dir` 自动提供这两项。输出目录须为空，避免覆盖已有结果。

默认使用上游 Codex CLI 语义实现，需本机已安装并登录 Codex，模型及推理档位可用
`--llm-model`、`--llm-effort` 或 `DATASETAGENT_LLM_*` 环境变量配置。
表格分类与序列批次分别通过 `--llm-workers` 和 `--sequence-workers` 控制并发。

运行日志中，`llm-template-start` 从模板准备阶段开始输出；随后 `llm-request` JSON 行记录
输入表格/单元格/段落数量、字符数、模型、推理档位、每次请求开始、校验和完成情况。
等待模型响应时每 15 秒输出 `waiting`，带请求哈希、尝试次数和已等待秒数；
它只表示请求仍在等待，不能区分服务端排队与推理，也不表示完成百分比。
限流、超时或校验失败会记录错误类型、重试次数和退避秒数；完成后记录实际 token 用量。
模板还记录校验恢复时保留/拒绝的数量，以及最终实体、序列关联、实验方案和警告数量。
这些输出实时进入网页任务的 `pipeline.log`；分类和序列提取共用相同的请求日志。
已启动的旧进程不会自动加载新日志代码，后续解析进程会自动使用。

离线检查输入适配、规则关联和文件导出：

```bash
PYTHONPATH=src python -m dataagent \
  --structured-dir /path/to/run/structured \
  --output-dir /path/to/new-offline-check \
  --semantic-provider rules
```

规则模式不运行三个语义模型阶段，其结果固定标为 `partial`，不能作为 LLM 提取效果评估。

## 输入要求

| 输入 | 要求 |
| --- | --- |
| 文档身份 | 两路输入使用同一个 `document_id` |
| 全文 OCR | SDA `pages.jsonl`，成功且连续的页面、同一工作 PDF 的 `pdf_sha256`、唯一 `line_id`、原文与页码；坐标为 PDF pt |
| 表格 OCR | 唯一表格 ID、页码、原始 HTML；保留 rowspan/colspan 展开的行列与物理来源地址 |
| 表格区域 | `table_evidence.bbox` 或版面清单里的 PDF pt 区域，用于关联表题和附近正文；缺少时保留警告，不猜位置 |
| 表格裁图 | 可选，有图可让上游分类模型参考；缺少时继续使用 OCR 文字，并保留警告 |

输入已有的文本/图片哈希会被核对；提供版面清单时，还检查工作 PDF 哈希和全部表格集合。
表格 OCR 未生成可解析网格时，原始文本、裁图和页码保存在 `unstructured_tables`，
加入 `table_structure_unavailable` 待复核项；其余有效表格继续抽取，覆盖报告保留未解析表格，结果标为 `partial`。
单元格位置优先保留已有坐标；否则复用同页已有 OCR 的完整文字框，通过原文、行列和空间顺序匹配。
重复文字必须有同行锚点消歧，整表 VL 区块不作为单元格框。歧义或缺少文字框时位置保持未知，
不按行列平均分配坐标，不合成 OCR token，也不发起额外 OCR。
`bbox` / `bbox_crop` 为裁图像素，`bbox_pdf` 为方向校正后的工作 PDF pt，`bbox_source`（如有）为原始 PDF 坐标。
`precision=ocr_text` 表示实际文字范围，`ocr_cell` 表示明确提供的单元格框，`unlocated` 表示未定位。
跨行序列通过原单元格、同行名称/SEQ ID 锚点和已有 OCR 文字框定位。`ocr_line` 为行级位置，
`ocr_row` 为已确认表格行的近似区域；OCR 字符有差异时保留原文和匹配信息，不据此修改残基。
`sequence_locations` 随 SDA 实体和 Studio 的 `sequence_index.locations` 导出。
裁图变换使用 `crop_to_pdf`，兼容旧数据的 `bbox_padded` 与图片尺寸；不会用未扩边的检测框代替裁图范围。
原始表格 HTML、证据、源文件地址及适配后的数据保存在 `sda_input.json`。
指标校验支持 `IC_{50}`、`EC $ _{50} $`、HTML 下标和 `K_D` 等明确的格式标记，
识别时保留原始 OCR 文本；不为缺失下标的 `IC` / `EC` 补猜数字。`metric_recovery.py`
可对保存的模型方案重新执行完整来源校验，恢复因旧表头格式校验被拒绝的测量计划。
全文 OCR 已经读取过所有页面，检索阶段复用这些文字，不再受原来的详细页面 OCR 次数限制。

仅补充已有结果的序列定位（先自动备份，不调用 OCR 或语义模型，也不重建序列与测量）：

```bash
PYTHONPATH=src python -m dataagent.refresh_locations /path/to/results/DOCUMENT
```

生成 `sequence_locations.json`，并更新 dataset/completed 模板和单元格的派生位置。并行任务修改结果时会拒绝覆盖。

原始 PDF 不是必填。可选 `--source-pdf` 必须是 OCR 使用的工作 PDF（方向校正后），
其 SHA256 必须匹配；不能混用校正前 PDF 的坐标。
外部序列表不是必填。原生独立入口仅使用 OCR；主流程导出时可沿用同篇本地序列表，
只按 OCR 支持的显式序列编号关联，不用序列表反推分子归属。

## 输出与现有 SDA 的区别

原生独立入口的 `dataset.json` 版本为 `dataagent-migrated-1`，不是 SDA 的
`antibody-dataset-2.0`。保留上游的两类结果，避免字段重命名掩盖原有含义：

- `records` / `dataset_records.json`：上游以 IC50 为主的兼容记录，包含 VH/VL、实验信息和证据。
- `measurements` / `assay_measurements.json`：上游通用测量导出，保存指标、数值、运算符、单位、单元格引用；包括源表明确支持的 KD、EC50 等。
- `document_template.json`：实体、别名、序列关联假设及实验协议绑定。
- `document_structure.json`、`sequence_extraction.json`：序列证据索引及模型提取决定。
- `table_classifications.json`、`record_verification.json`、`construct_validations.json`：分类与校验。
- `sda_input.json`、`sda_page_index.json`、`page_retrieval.json`：输入快照及全文证据复用记录。
- `agent_run.sqlite`、CSV、审阅任务和模型调用记录：沿用上游本地运行产物。

`ExperimentAgent` 也已复制。与上游相同，首次导入由 `LLMTemplateAgent` 绑定实验，
单独的 `ExperimentAgent` 在选中记录后刷新实验信息时调用。

序列提取以表格单元格为金标准。相同链/SEQ 的页面文字与单元格不一致时，保留表格取值，
并在 `source_conflicts` 中保存两边的引用、原文片段和说明；归属、可变区边界或表格字符
本身无法确定时仍不填值。SDA 导出保留 `table_text_mismatch` 问题，Studio 在复核状态中
显示“表格和文字不一致”，支持筛选、查看两边证据和 CSV 导出。
可变区表与完整链表重复引用同一段残基时，只要输出逐字来自表格并采用金标准片段，
不要求把重复表格都拼入序列。单条链校验失败只使该链待复核，同批其他通过校验的链继续保留。
`sequence_recovery.py` 可重验保存的已完成模型响应，只使用当时已加载且内容未改变的证据，
补回此前被批次连带失败清空的链；已有通过校验的链不被覆盖。
实验标签显示每条记录的 IC50、EC50、KD 等指标；实验 keyword 只展示协议中有依据的物种
（human、老鼠、食蟹猴等）和实验体系（细胞、蛋白），原实验名称与方法仍保存在详情数据中。

当带 IgG/Fc 格式后缀的实体缺少序列归属、同名基础抗体已有可引用的序列时，流程按需调用
Codex 逐链核验“是否相同 VH/VL”。候选生成保留完整变体名；模型返回 same/different/uncertain
及双方原文引文。仅 same 且双方引用通过逐字校验时继承已有序列及冲突标记，不覆盖已有值或
绕过有具体 SEQ 编号的损坏序列。每次最多 32 个链问题、每批 8 个；核验记录保存在
`sequence_identity_review.json`。服务失败或证据不足时保留待复核，不新增人工确认记录。

ANARCI/HMMER 和 ESM-2 统一位于 [`src/eval`](../eval/README.md)。语义处理完成后自动发现
本地评估环境，批量评估最终 VH/VL；支持独立环境和本地权重，也可使用
`--no-sequence-evaluation` 关闭。未安装、未启用或执行失败会保留待复核原因。
ESM-2 默认按单个位点伪困惑度进行风险筛选（初筛阈值 1000，可配置），超阈值将 ESM-2 标红，
保留具体位置、原残基和分数。红色表示模型风险，不确认序列错误，不据此修改残基。

## 问题、回答与修改

首次记录校验后，默认执行 `QuestionAgent → AnswerAgent → 候选修改 → 重新关联与校验`。
缺失 VH/VL、表格与文字冲突、实验来源及模板诊断会汇总成问题；同一 SEQ 的同一链异常
合并，并保留全部受影响实体。QuestionAgent 只提问，AnswerAgent 按问题读取原文及原表裁图，
给出回答、逐字引用和结构化修改。证据不足或服务失败继续待审，不生成填充值。

AnswerAgent 可以补全有证据的缺失序列、修正图像证实的 OCR 转录，以及修订已有实验区域的
方法/物种/体系绑定。原表图像优先于 OCR；图中的残基下划线属于格式。图像复核结果保存为
独立 `image-review:...` 来源，记录原单元格引用、OCR 哈希、图像路径及哈希、转录哈希和问题/回答
标识。原始 HTML 与 OCR 不修改；后续每次序列关联、导出都会重验原始 OCR 和图片。

回答不会直接改最终数据。每份候选先检查作用范围、已有 SEQ 归属、引文和序列字符，再重新
组装记录、检查构建体与来源。测量值、单位、名称、指标、观察数及已有完整序列必须保持一致，
不能引入新的来源错误。失败则丢弃该份候选，保留问题及原因；成功后仍保留原有冲突标记。
其他类型的问题可以回答并说明后续处理，但不会执行任意模型指令。

每次运行先将最多 64 个去重诊断整理成问题队列，再自动回答其中最多 12 个问题；可用
`--max-answer-questions` 调整回答数量至 1–64，或通过 `--no-question-answers` 关闭。
其余诊断及未回答的问题显式保留待审；修复循环不重复无限提问。
`question_review/` 下的 `diagnostics.json`、`questions.json`、`answers.json`、`changes.json`、
`report.json` 分别记录诊断、问题、回答、修改决定和汇总，根目录 `question_review.json` 也保留汇总。

## 迁移改动

- 包内导入改为相对导入，避免与 SDA 的同名模块冲突。
- 提示词、缓存、可选模型环境和子进程入口适配新位置。
- `sda_inputs.py` 转换并校验已有 OCR；`run_sda.py` / `__main__.py` 提供新入口。
- 原图的发现、上下文、索引和页面读取节点在有 SDA 输入时复用证据，其余语义和校验节点沿用上游。
- `main_pipeline.py` 接入 PDF/网页主流程，输出 `antibody-dataset-2.0`、逐表证据、覆盖审计、缺失清单及六字段兼容记录；保留全部通用测量指标。
- 主流程原生结果位于 `<results>/<id>/dataagent/<attempt>/`；`dataagent_checkpoint.json` 固定原 OCR 输入身份，续跑保留历史尝试，通过模型缓存复用已验证回答。
- 主流程累计实际模型尝试的 token，包含失败和重试；本地缓存命中不重复计入。旧 `--template-agent-max-calls`/`--template-agent-context-chars` 的正值仅用于旧 document 模式，dataagent 的 `0` 调用预算仍可关闭序列模型。

验证：

```bash
python -m unittest discover -s tests -p test_dataagent_migration.py -v
```

测试包括完整规则图、使用脚本化模型响应运行真实输出协议及证据校验、跨文档/哈希拒绝、
合并单元格来源、缺失坐标和零表格。脚本化响应不代表真实模型的语义准确率。
