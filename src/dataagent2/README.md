# dataagent2

独立的专利 OCR 增量提取实验。实现、测试、运行入口和本次实验产物均在本目录；
独立 benchmark 运行只依赖 Python 3.10+ 标准库和已配置的 `codex` CLI，不导入 `dataagent` 或
`template_sequence_agent` 的实现。独立 CLI 不会更新 Studio jobs 或覆盖旧结果。

Studio 上传现在可选择 dataagent2。集成适配器 `studio_pipeline.py` 复用 SDA 的 OCR 输入、
表格分类/模板与网页导出接口，再执行本目录的 Reader 和有限问答；不会执行旧序列 Agent 或修复。
网页每次使用新运行目录，将原始新版结果保存在结果目录的 `dataagent2/`，并另外导出 SDA 审阅格式。
本次模型用量包含实际重跑的共享阶段及新版请求，不加入 benchmark 的历史基线成本。
完整 PDF 命令可指定 `src/document/run_document_pdf.py ... --extraction-mode dataagent2`；
已有 OCR 可用 `src/graph.py ... --extraction-mode dataagent2`。网页集成依赖 SDA 完整运行环境。

代码及 29 项离线测试完成。实测记录见 [STATUS.md](benchmarks/live_20261026/STATUS.md)
及 BENCHMARK_REPORT.md；续跑期间的实时进度以每篇输出目录中的 progress.json
和 token_usage.json 为准。服务曾返回 429，成功请求均已缓存。

## 流程

1. 从原始 native run 读取 `sda_input.json` 和 `document_template.json`。
   只保留模板实体、编号映射、实验方案和原始表格测量；排除其中的
   `sequence_extraction`、后续修复、人工继承和 AnswerAgent 答案。
2. 为 OCR 正文行和表格单元格建立本地整数地址。来源、页码、坐标和字符保存在
   `sources.json`。模型不复述这些字段，也不输出长解释或完整序列。
3. 按文档顺序分块，每个来源恰好属于一个主块。少量相邻上下文和跨块表头可重复。
   每块一次请求同时输出多标签、编号关系、定义片段、实验、测量和问题。
4. 模型用短的原文起止锚点定位序列；程序逐字复制，且只移除空白和 `<br>`。
   无法定位、非标准字符、重复片段或错误地址会保留在 rejected facts 中。
   原文字符校验不等于生物学归属或边界的独立验证。
5. 程序合并共享定义，保留不同抗体身份及每条映射的来源。完整链不自动裁成 VH/VL；
   矛盾表格定义不合并。表格/正文冲突保留记录，字符优先采用表格是来源策略，
   不代表表格 OCR 一定正确。
6. QuestionAgent 的问题队列由缺口确定性生成，按共享 SEQ ID 合并；AnswerAgent
   只读取预算内的定向证据，每题至多一次逻辑回答。模型传输失败至多重试一次。
   有限问题预算未覆盖的问题仍保存，不宣称全部补全。

本版自动问答只覆盖 VH/VL 映射、定义和边界缺口；实验缺口与其他模型问题会保存，
尚未实现对应的自动问答队列。完整链和可变区分别编号时，优先采用明确的域映射；
不能因编号不同误报冲突。正文与表格中的测量仅在唯一且明确的匹配下合并。

表格复识别状态通过 `source_sequence_ocr` 同时传给 Reader 和定向回答。
已映射序列的 OCR 失败明确记录为 `ocr_failure`，带来源地址和
`recovery_action=rerun_table_sequence_ocr`；这些任务不消耗文本回答预算，
也不计为已尝试的回答。回答的 `applied` 仅表示接纳响应，`resolved` 单独记录是否真正补全。
Studio 入口会将过期修订的表格序列从原图重新识别到新文件，再建立模板种子。
当前修订中仍失败的表格也会在新运行中额外重试一次；原图和裁行产物写入新目录。
完整可见表格线与物理 HTML 行列一一对应时，用原图规则定位整个单元格，
包括没有名称的跨页续表首行；下划线不能作为跨越名称列的行分隔线。
本地 OCR 仍失败的序列进入独立原图复核，单个复核失败不会丢弃同表其他有效结果。
Studio 默认对本轮所有文本问题各回答一次；独立 CLI 仍尊重 `--max-questions`。
`questions.initial.json` 保存首轮队列，`questions.json` 保存最终剩余问题。
`quality_audit.json` 逐条检查序列片段、测量值和单位的字符来源，并列出测量与 VH/VL 的关联缺口；
这些校验不代表原图逐字符的独立人工验收。相似名称只用于检索，不用于自动合并实体。
明确的 CH/CL 定义保留在实体中，经 SDA `other_sequences` 和审核 CSV 导出，
网页序列证据面板在对应 VH/VL 下显示已有的 CH/CL；恒定区不会填入 VH/VL。

## 运行

从 SDA 根目录执行：

```bash
PYTHONPATH=src python -m unittest dataagent2.test_dataagent2 -v

# 只查看 OCR 字符量、主块数和历史基线，不调用模型，也不以字符估算 token。
PYTHONPATH=src python -m dataagent2 --plan-only \
  --output src/dataagent2/benchmarks/plan_20261026

# 两篇完整实测；与原流程使用相同模型及推理档位。
PYTHONPATH=src python -m dataagent2 \
  --documents WO2026086808A1 WO2024020591A9 \
  --model gpt-6.1-sol --effort xhigh \
  --workers 1 --document-workers 1 --max-questions 8 \
  --output src/dataagent2/benchmarks/live_20261026

# 随时生成当前进度和结果表，不调用模型。
PYTHONPATH=src python -m dataagent2.summarize src/dataagent2/benchmarks/live_20261026

# 运行中重放成功响应，生成 dataset.checkpoint.json，不调用模型。
PYTHONPATH=src python -m dataagent2.replay \
  src/dataagent2/benchmarks/live_20261026/WO2026086808A1

# 限流时自动续跑：单个目录只允许一个 supervisor，最多 8 轮（包含历史轮次）。
# 首轮等待 180 秒，随后按 300、600、900 秒退避；缓存仍然复用。
PYTHONPATH=src python -m dataagent2.supervise \
  --output src/dataagent2/benchmarks/live_20261026 \
  --rounds 8 --initial-delay 180 --cooldown 300 --request-interval 90
```

重复相同命令会复用内容哈希匹配的已完成请求，实际历史调用仍保留在用量中。
输入、提示词或模型变化时需换输出目录。`--max-chunks 1` 可冒烟测试，但不生成
完整流程的节省比例。`--native-dir`、`--reference-result` 可指定其他保存的输入。
默认串行调用；模型实际尝试的开始时间至少间隔 65 秒，缓存不等待。
可通过 `--request-interval` 调整，所有文档共享间隔。服务返回 429 时等待 45 秒后
至多重试一次，并遵守请求间隔。失败原因优先记录
模型事件中的服务错误，避免被 CLI 启动警告掩盖。
持续限流或认证失败会停止本进程后续的新模型调用；重新运行即可从缓存续跑。
未读完整文档时显示 `PARTIAL` 并以非零状态退出。因熔断而未发出的块记为
`skipped_chunks`，与已调用后失败的块区分；不会虚增实际请求次数。
自动续跑的 PID、当前阶段、下次尝试时间和历史轮次保存在 `supervisor.json`。
重新启动会保留轮次历史；优先处理正文尚未读完的文档，再处理已读完文档的补全。
限流后下一轮把请求间隔乘以 1.5，自动增加到至多 300 秒，降低连续撞限流的概率；
设置值高于 300 秒时仍保留用户设置的下限。每轮实际间隔写入 supervisor.json。
遇到非限流错误时停止自动重试；已有其他 supervisor 时拒绝启动第二个。
创建输出目录下的 `supervisor.stop` 可阻止下一轮启动，当前批次会正常结束。

## 可选的本地边界可行性检查

`domain_probe.py` 使用已安装 ANARCI 的 Python 环境对完整链编号，不导入旧管线，
不调用 LLM，不改写 dataset.json。输出单独的 `computational_boundary_candidate`。
它验证编号残基与原始完整链切片一致，但不证明专利归属或边界的原文明示。

```bash
/project_c20250601/252705034/envs/ab-data-validator/bin/python \
  src/dataagent2/domain_probe.py \
  src/dataagent2/benchmarks/live_20261026/WO2026086808A1/dataset.checkpoint.json \
  src/dataagent2/benchmarks/live_20261026/WO2026086808A1/domain_feasibility.json \
  --hmmer-dir /project_c20250601/252705034/bin
```

首次独立检查得到 32 个额外计算候选，其中 29 个与后续修复参考逐字符一致，3 个不同。
这些数字只作可行性分析，不是准确率，不计入正式 VH/VL 提取量。
`augment_domains.py` 可将这些候选导出到独立文件，逐字符核对切片与原始来源，
保留字符坐标与 `boundary_method=ANARCI/IMGT`。`computed_domain` 与正式支持状态分开，
原有提取计数保持不变；完整链的 SEQ ID 明确标为 full_chain 范围。
本次另存的 dataset.with_computational_domains.json 包含 4 个正式槽位和 32 个计算槽位，
计入计算槽位后可组成 13 对。固定输入快照保存在 domain_probe_input.json。

## 按对象审核的 CSV

```bash
PYTHONPATH=src python -m dataagent2.export_rows \
  src/dataagent2/benchmarks/live_20261026/WO2026086808A1/dataset.with_computational_domains.json \
  src/dataagent2/benchmarks/live_20261026/WO2026086808A1/review
```

`items.csv` 每个对象一行，包含 VH/VL、SEQ ID 与范围、提取状态、边界方法、原文引用、
实验 ID 和缺失字段；`computed_domain` 明确表示计算界定。`observations.csv` 与
`experiments.csv` 按 ID 关联，后者保留同一实验不同来源的方案记录。
本次实际导出后读回校验了 48 行、36 个有值的链槽位和其中 32 个计算槽位。
也可以把正式 dataset.json 传给相同命令，单独审核不含计算候选的版本。
主流程在每次增量合并后自动刷新 `review.partial`，本轮结束后生成 `review.formal`。
手动导出的计算候选版本使用独立目录，不会被这些正式结果覆盖。

## 输出和对比口径

- `manifest.json` / `seed.json` / `sources.json`：输入、分块和模板种子。
- `requests/*/input.json`、各 attempt 的 `events.jsonl`：完整请求和原始模型用量。
- `calls.jsonl` / `token_usage.json`：每次实际尝试，成功、失败和重试均保留。
  只累加 `turn.completed` 的 usage；未知值不估为零。总量=input+output，
  缓存输入和推理输出分别已包含在输入与输出中。
- `dataset.first_pass.json` / `facts.first_pass.json`：正文一次阅读后的结果。
- `dataset.partial.json` / `facts.partial.json`：每块完成后按文档顺序增量落盘的结果；
  并行返回会先缓冲，确保后文不会抢在前文声明之前合并。无需额外模型请求。
- `questions.json` / `answers.json`：问题预算、回答及未解决项。
- `dataset.json` / `facts.json`：最终结果、证据、冲突及被拒绝的提议。
- `report.json` / 上级 `comparison.json`：实际新请求用量和比较结果。

`new_model_usage` 是这次新增流程的真实服务用量。
`comparable_model_total_tokens` 是新增流程用量加上复用的历史表格分类/模板阶段用量。
这是复用阶段的成本比较，不是重新实测一遍完整旧管线。
本地 OCR token 未计入，两边都沿用相同口径。

初始历史基线：WO2026086808A1 为至少 5,173,982 token（2 次用量未知），
WO2024020591A9 为 8,975,647 token。旧基线范围不同：后一篇包含后续问题回答和修复。
报告保留该差异，不能据此直接声称同等质量的加速比。

最终旧导出仅用于提取结束后的参考对比，不进入模型输入。参考包含后续修复，
也不是人工金标准。应同时比较链数、完整配对、测量数、未解决项和逐字符差异；
不能用提取更少造成的省 token 当作等质量改进。本实验不进行新 OCR、图片重新识别
或按常见抗体基序修复残基。
