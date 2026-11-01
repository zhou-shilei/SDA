# dataagent2

独立的专利 OCR 增量提取实验。实现、测试、运行入口和本次实验产物均在本目录；
独立 benchmark 运行只依赖 Python 3.10+ 标准库和已配置的 `codex` CLI，不导入 `dataagent` 或
`template_sequence_agent` 的实现。独立 CLI 不会更新 Studio jobs 或覆盖旧结果。

Studio 上传现在可选择 dataagent2。集成适配器 `studio_pipeline.py` 复用 SDA 的 OCR 输入、
表格分类/模板与网页导出接口，再执行本目录的 Reader 和有限问答；不会执行旧序列 Agent 或修复。
网页每次使用新运行目录，将原始新版结果保存在结果目录的 `dataagent2/`，并另外导出 SDA 审阅格式。
OCR 来源和原图样式复核固定后，先完成表格分类/模板并生成粗略 dataset，再启动全文 Reader。
Reader 默认有多少个 chunk 就同时发起多少个独立调用；不再继承旧序列 Agent 的 3 路默认值、
8 路上限或 `DATASETAGENT_SEQUENCE_WORKERS`。
每个 chunk 携带同一份 `initial_dataset` 全量业务 seed：全部已有实体、链编号映射、
实验、测量（保留数值、单位和来源）及标签；有初始序列定义时也完整携带。
不再按 chunk 中的名称、SEQ ID 或来源交集筛选业务事实，不重复发送外层名称目录。
seed 来自同一个初始快照，结合当前块来源和边界上下文，输出新增事实或冲突。
seed 中的远处引用只提供已有背景；新事实仍只能引用本次请求实际 `sources` 中的证据编号。
运行时间、token 用量、审计路径和用于评分的参考结果不进入业务 seed。
Reader 完成后，由 LLM `ActionAgent` 接收完整初始 dataset、所有 Reader actions 及其引用的
原文证据，跨块整合后直接返回完整业务 Dataset2。每条 action 必须说明接受、合并、拒绝或冲突，
替换/拒绝初始事实必须明确给出依据；程序校验来源、原文字串和数据完整性，再检查缺口。
Reader 运行中的 Ledger 只作为临时审阅结果，不能代替 ActionAgent 的正式综合。
各 chunk 不接收其他 chunk 的增量结果。单独运行 benchmark 时种子已存在，先并发读取，
再进行一次 ActionAgent 综合；语义校验失败可反馈一次，仍失败则保留 partial，不执行后续文字问答。
本次模型用量包含实际重跑的共享阶段及新版请求，不加入 benchmark 的历史基线成本。
共享阶段使用 v6 TemplateAgent，一次完成表格筛选、区域定位和文档计划，不再调用独立 LabelAgent。
所有候选表以完整 `headers` / `rows` 及 `[原始单元格引用, 原文]` 输入，保留行列、空单元格和
已有合并表头信息；预算外表格显式保留为待复核。整数来源 ID 仍在本目录初始化时生成。
Reader 输入只跳过模板明确 `exclude` 的表格，保留 `context_only` 和 `needs_review`；原始 OCR 不修改。
历史 benchmark 产物不改写。Reader 的 sources 格式和固定初始快照策略保留，
新请求使用 `reader_input_revision=2`；输入与提示词变化后使用新的输出目录。
统一模板种子的测量按 `(table_id, region_id, endpoint)` 绑定已有实验，避免同表多指标之间误选协议。
模型未能确认测量实体身份时，仍保留有名称单元格证据的测量，以该单元格为范围建立
`identity_status=unresolved` 的占位实体并报告警告；不把不同来源的同名对照自动合并或赋予序列。

## ActionAgent 的输入、输出与保存

新增模型阶段名为 `action`，与 Reader 和定向 `answer` 使用同一 Provider，但使用独立完整数据集
Schema。请求指纹包含 ActionAgent 修订和提示词；旧运行目录和旧请求不能冒充新综合结果。
`action_agent.input.json` 保存完整底稿、所有候选 actions（带稳定 ID 和来源范围问题）、
引用的原文及邻近段落/表格行/表头、可用 OCR 状态和样式。不会先按名称或编号筛掉候选 action。

返回包含 `dataset`、`action_decisions`、`domain_reviews`、`seed_changes`、`conflicts`：

- `dataset` 是完整业务数据，包含全部实体、关联、实验、测量、序列定义和标签；不包含运行日志。
- 每条 action 在 `action_decisions` 中恰好处理一次，采纳/合并必须指向实际输出记录。
- `seed_changes` 为替换或拒绝初始事实提供原文引用和理由；已有实体不能无故消失。
- 序列同时保留 literal value 和原文定位 parts，主机逐字符核对；测量值和单位也必须回到原文。

原始返回与反馈保存在 `action_agent.response.*.json` / `action_agent.validation.*.json`；
最终完整业务输出为 `action_agent.dataset.json`，状态与哈希为 `action_agent.json`。

Action 修订 2 增加域完整性复核：`domain_review_input` 从同一批原始证据和 Reader 提议中，
对可落到原文字面的完整链、VH/VL、CH/CL 定义做精确全长比较，保留所有 `完整链 = 可变域 + 恒定区`
候选及其来源。程序不确认域标签、实体归属，不按 a/b 名称继承，也不自动写入关联。
Action 必须为输出中的每个实体分别返回 VH、VL 的 `supported / unresolved / not_applicable`
结论、证据、候选 ID、实际记录指针和具体缺失证据。完整唯一分解在域标签和完整链归属确认后，
可以证明该链的域组成，无需额外要求原文再重复一次恒定区归属；相似前缀和通用恒定区定义不足以确认。
程序检查逐实体覆盖、候选是否遗漏、与实际组装结果是否一致；不接受只有 `missing_mapping`
这类笼统说明的未决结论。精确候选仍未解决时必须引用完整链归属及三个定义。
`domain_review.json` 单独保存候选索引、逐实体结论和统计，供审阅。Reader 提示词和输入不变。

业务、域复核和字面证据全部通过而仅有 action 重复归类时，调用一次小型 LLM 审计修正，
让模型选择保留哪个已有归类。程序只按该明确选择移除重复 ID，再执行所有校验；业务记录和
域复核内容保持相同。`action_accounting.*.json` 保存输入和模型选择，不由程序猜测语义结果。
大输入默认采用显式记录引用：模型选择保留的 seed/Reader 记录，并完整写出有改动的记录；
主机仅展开这些明确选择，再校验完整结果。`seed_collection` 可按原顺序保留整组初始记录，
有效 seed 路径和 action 下标受本次请求 Schema 约束。原始表示另存 `action_agent.response.raw.*.json`，
展开后的完整结果仍为上述文件，业务事实和来源不会因为压缩被筛掉。
超过 100 万字符的输入使用可逆列式编码，`action_agent.input.json` 始终保留原始完整输入。
CLI 和 Studio 的整合阶段默认 `medium`，Reader/问答沿用 `llm_effort`；CLI 可用
`--action-effort` / `--action-timeout` 覆盖。直接使用 Provider 时可通过 `action_effort` 设置。
`dataset.first_pass.json` 是通过校验后生成的审阅视图；若 ActionAgent 失败，临时结果显式保留
`partial` 和错误，不能宣称整合完成。模型 token 与耗时以 `action` 阶段单独记录，缓存回放不调用模型。

## 关系索引、区域和标签

`structure.py` 统一 Reader、Ledger 和网页的区域名。完整保留 VH/VL、H/L、CH/CL/CH1–4、
CDRH1–3/CDRL1–3、FRH1–4/FRL1–4，以及 VHH/scFv/Fab/Fc/linker 等构件。
兼容 HCDR/HFR 等原文写法并保留 `original_region`。CDR/FR 可独立披露，不填充完整 VH/VL；
本版不新增程序编号或计算裁剪。已有原图边界提取标记为 `derived_variable`，
原文明示的 VH/VL 标记为 `direct_variable`，辅助区域为 `supporting`。
`mapping_status`、`mapping_seq_ids` 与序列解析状态独立；未知 VH 的完整重链候选编号放在
`source_chain_seq_ids`，不冒充 VH 自身编号。原始 `links`、各实体 `sequence_links` 和
完整 `relation_index` 随 dataset 保留，未解析的编号和短 CDR/FR 也会进入网页证据节点。

标签覆盖 POISK 的格式、发现状态/策略、来源物种/平台/工程、靶点、表位与竞争维度，
并补充同型/价数及实验目的、抗原物种、细胞宿主物种、体系、模型、样本、读出、终点。
`TAG_VOCABULARY` 是标准写法示例，不是事实证据；允许保留有来源的新术语。
`annotations` 每项必须关联 item/experiment、category、value、refs，未知实验的标签会被拒绝。
标签依据保留到导出和网页筛选。抗体发现方法与实验测量方法是独立类别。

实验 identity 与分类标签分离：`id` 标识原文支持的实验范围，`protocol` 是可复用方法，
`region_id` 保留表格分区。只有同表唯一实验，或独立表头引用唯一指向实验时，主机才补归属；
同标签/同指标不合并实验。同一来源的显式实验归属冲突会报告并挂起；
不同 condition/donor/replicate 的结果独立保留，未知重复次数不猜测。
网页按 experiment ID 分组，条件/供者/重复号参与测量身份。

本版修改了请求与响应契约，旧版本请求缓存不能冒充新版本命中。请使用新的运行目录；
原有运行产物不被迁移或重写。Reader 并行和后续问答预算策略沿用现有配置，
此次没有宣称消除重复阅读或已验证抽取质量提升。

审阅 CSV 同步导出全部区域；`*_seq_ids` 保存域自身的编号，`*_source_chain_seq_ids`
单独保存完整链候选编号。实验 CSV 保留标签依据、方案和区域，测量 CSV 保留条件、供者和重复号。

### 此前关系索引版本的离线验证记录

148 项 Python 回归及 3 项 Node 专项检查通过，覆盖并行 Reader、缓存重放、字符校验、
短 CDR/FR、未解析映射、同表多实验、实验归属冲突和供者/条件区分。
另用 `49-WOother` 保存的 63 个旧 Reader 响应回放当前主机合并规则：逐项核对原文一致性，
不调用模型、不修改历史产物，230 条测量和 32 条已支持的 VH/VL 保持不变；
未关联实验的测量从 122 条降到 22 条。新的 SDA 导出也保留全部 230 条测量。
此前按 chunk 筛选关系索引的版本，使同一篇文档的 Reader payload JSON 总字符数
从 3,712,143 降到 2,583,410（约 30.4%）。该历史数字不适用于当前全量 seed 版本，
也不是 token 或耗时实测、人工核验的准确率。全量 seed 提示词的抽取质量需另行验证。
完整 PDF 命令可指定 `src/document/run_document_pdf.py ... --extraction-mode dataagent2`；
已有 OCR 可用 `src/graph.py ... --extraction-mode dataagent2`。网页集成依赖 SDA 完整运行环境。
两个入口均可用 `--sequence-workers 0 --llm-workers 6 --reader-chunk-chars 24000`。
`--sequence-workers 0`（默认）表示全部 chunk 同时调用；只有显式指定正数才限制 Reader 并发。
后两个参数分别调整表格种子阶段并发和来源字符预算。

历史实测记录见 [STATUS.md](benchmarks/live_20261026/STATUS.md)
及 BENCHMARK_REPORT.md；续跑期间的实时进度以每篇输出目录中的 progress.json
和 token_usage.json 为准。服务曾返回 429，成功请求均已缓存。

## 流程

1. 从原始 native run 读取 `sda_input.json` 和 `document_template.json`。
   只保留模板实体、编号映射、实验方案和原始表格测量；排除其中的
   `sequence_extraction`、后续修复、人工继承和 AnswerAgent 答案。
2. 为 OCR 正文行和表格单元格建立本地整数地址。来源、页码、坐标和字符保存在
   `sources.json`。模型不复述这些字段，也不输出长解释或完整序列。
3. 按文档顺序分块，每个来源恰好属于一个主块。少量相邻上下文和跨块表头可重复。
   默认每块来源 JSON 预算 24000 字符，包含上下文预留；小表整体保留，大表优先按完整物理行拆分，
   在已有块接近预算时优先使用页边界和实验标题边界。跨块附带预算内的表头和前后相邻正文。
   超长 OCR 来源优先在段落、换行或空白处切分，保留所有字符及 `source_offset`。
   单个不可拆来源超过用户设置的小预算时，完整保留并在 manifest 的 `oversized_source_ids` 中标出。
   每块一次请求同时输出多标签、编号关系、定义片段、实验、测量和问题。
4. 模型用短的原文起止锚点定位序列。单字母序列只移除空白和 `<br>`；
   标准三字母缩写（如 `Glu Val Gln Leu`）由程序确定性转换成 `EVQL`，
   支持空白、`<br>` 和单连字符分隔，保留原文、来源哈希和每个残基的原文字符范围。
   全大写文本沿用单字母解释；不自动将 `ALA GLY` 改成 `AG`。
   未知/歧义缩写、修饰残基、行号、OCR 错字、混合记法和不完整缩写继续拒绝；
   无法定位、重复片段或错误地址会保留在 rejected facts 中。格式转换不推断 VH/VL 边界或配对。
   原文字符校验不等于生物学归属或边界的独立验证。
5. LLM ActionAgent 综合完整初始 Dataset 与所有 Reader actions，返回完整业务 Dataset2，
   并交代每条建议的去向、初始事实修正和剩余冲突。程序核对原文、引用与完整性后生成审阅视图。
   完整链不自动裁成 VH/VL；表格/正文冲突保留记录，字符优先采用表格是来源策略，
   不代表表格 OCR 一定正确。
6. Action① 的 `domain_reviews` 同时给出具体证据缺口。程序只对 `unresolved`
   行分组排队（共享编号或同一实体的无编号缺口），排除 `supported` 和 `not_applicable`，
   不再调用额外的 Question LLM。`question_plan.json` 保存完整复核依据和排除项。
7. Answer 默认 4 路并行，读取同一份固定 Dataset2 的定向上下文及原文，使用独立的小型
   Schema/prompt，默认 medium 推理。只返回答案、证据和少量 `add_link`、
   `add_sequence_definition`、`add_relation`、`record_conflict`，不再复述测量、实验和段落 mentions。
   原文软预算默认 8000 字符；必要的完整链、域、恒定区和标签/映射证据整组保留，
   即使超预算也不截断序列，超额会显式记录。其他检索命中按完整表格行/邻近正文组加入。
8. Answer 只提出建议；程序在临时副本核对来源与字面序列，不提交业务变更。
   存在有效建议时，LLM Action② 一次接收**完整业务 Dataset2**、所有 Answer actions
   和对应原文，输出完整 Dataset3，逐条说明采纳/拒绝/冲突，并重新复核域覆盖。
   没有建议则跳过第二次模型调用，Dataset3 保留 Dataset2。失败时保留 Dataset2，不能宣称整合完成。
   Action② 使用相同的来源、事实保留和完整输出校验；已有受支持 VH/VL 不能被静默替换或降级。

本版自动问答只覆盖 VH/VL 映射、定义和边界缺口；实验缺口与其他模型问题会保存，
尚未实现对应的自动问答队列。完整链和可变区分别编号时，优先采用明确的域映射；
不能因编号不同误报冲突。正文与表格中的测量仅在唯一且明确的匹配下合并。

表格复识别状态通过 `source_sequence_ocr` 同时传给 Reader 和定向回答。
已映射序列的 OCR 失败明确记录为 `ocr_failure`，带来源地址和
`recovery_action=rerun_table_sequence_ocr`；这些任务不消耗文本回答预算，
也不计为已尝试的回答。新回答的 `proposed` 表示有待 Action② 审议的建议，
`no_change` 表示没有新建议；`resolved` 以最终 Dataset3 的实际域状态另行记录。
`deferred_budget` 保留预算未覆盖的问题。旧运行的 `applied` 不应解释为已经解决问题。
Studio 入口会将过期修订的表格序列从原图重新识别到新文件，再建立模板种子。
当前修订中仍失败的表格也会在新运行中额外重试一次；原图和裁行产物写入新目录。
完整可见表格线与物理 HTML 行列一一对应时，用原图规则定位整个单元格，
包括没有名称的跨页续表首行；下划线不能作为跨越名称列的行分隔线。
本地 OCR 仍失败的序列进入独立原图复核，单个复核失败不会丢弃同表其他有效结果。
Studio 默认对本轮所有文本问题各回答一次；独立 CLI 仍尊重 `--max-questions`。
`questions.initial.json` 保存首轮队列，`questions.json` 保存最终剩余问题。
`dataset2.business.json` 和 `dataset3.business.json` 保存两份完整业务数据，不含时间/token/日志。
`answers/<question_id>/input.json` / `response.json` 保存每题实际输入输出，`answer_actions.json`
保存待整合建议。`answer_action/` 内保存 Action② 完整请求、返回和校验，
`answer_action.json` 保存最终状态。`answer_action` 阶段的实际用量和耗时独立统计。
独立 CLI 可设置 `--answer-workers`、`--answer-effort`、`--question-chars`；Studio 使用同样默认值。
新旧流程签名不同，继续旧输出目录会拒绝混用；旧记录仍可回放，新回放直接校验和使用
已保存的 Action② 完整输出，不再将 Answer 逐条应用到业务数据。
`quality_audit.json` 逐条检查序列片段、测量值和单位的字符来源，并列出测量与 VH/VL 的关联缺口；
这些校验不代表原图逐字符的独立人工验收。相似名称只用于检索，不用于自动合并实体。
明确的 CH/CL 定义保留在实体中，经 SDA `other_sequences` 和审核 CSV 导出，
网页序列证据面板在对应 VH/VL 下显示已有的 CH/CL；恒定区不会填入 VH/VL。

## 49-Sructure 新问答流程实测

复用网页运行 `8439a5cbc001475793d73794a86deb85` 已校验的 Dataset2，独立运行新问答。
原始 PDF、OCR、Template、62 个 Reader 和 Action① 均未重跑；既有网页业务结果没有被覆盖。
模型均为 `gpt-6.1-sol`；旧 Answer 使用 xhigh 串行执行，新 Answer 使用 medium、4 路并行。
本次同时调整了问题规划、证据检索、Schema、prompt 和推理预算，数字反映整套方案的变化。

| 指标 | 旧问答 | 新问答与可选 Action② |
| --- | ---: | ---: |
| 问题数 | 24 | 21 |
| 输入 token | 506,574 | 306,557 |
| 输出 token（含推理） | 37,211 | 3,477 |
| 总 token | 543,785 | 310,034 |
| 用时 | 1,470.24 秒 | 71.77 秒 |
| 新增受支持域 / 完整配对 | 0 / 0 | 0 / 0 |
| 最终受支持域 / 完整配对 | 75 / 31 | 75 / 31 |
| 正式测量记录 | 251 | 251 |

总 token 减少约 43.0%；这里的耗时只覆盖问答与可选整合，不代表整篇 PDF 的处理耗时。
新的问题计划保留 Action① 的 29 个 unresolved 域复核，分组成 21 个问题，
排除已经标为 not_applicable 的 6 个域复核。21 个回答均为 `unresolved`、`actions=[]`，
因此 Action② 按设计跳过，Dataset3 与 Dataset2 的业务内容哈希完全一致。
这次没有额外补回 VH/VL，不能把速度改善解释为准确率提升。

完整业务底稿的 257 条测量候选全部保留；其中正式视图展示 251 条，另 6 条仍待复核。
字面来源错误为 0，既有域和测量无丢失。旧问答额外添加的两条无名称 TSLP 定义，
与 Dataset2 已有的命名定义在编号、区域、序列和引用上重复；本轮没有重复添加，
没有损失独立序列定义。真实样本未触发 Action② 模型调用；有建议时的整合、失败回退、
历史冲突/拒绝记录保留、缓存回放及篡改检测由离线回归场景验证。

复现实验与静态网页使用仓库脚本，运行数据和生成快照保存在被 Git 忽略的本地目录：

```bash
python try/agent_handoff/rerun_followup.py \
  --baseline /path/to/saved/dataagent2 \
  --output try/agent_handoff/runs/focused-followup

python try/agent_handoff/build_followup_page.py \
  --experiment try/agent_handoff/runs/focused-followup \
  --output try/agent_handoff/snapshots/focused-followup
```

实验脚本核对 Dataset2 请求/输出哈希和 OCR 来源身份，保留每题实际请求、回答、
模型用量、配置和代码快照。网页展示完整流向、原始文件、prompt、逐题缺口和原文证据，
明确区分复用阶段、真实执行阶段与本轮跳过的 Action②。

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
  --workers 0 --document-workers 1 --max-questions 8 \
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
默认每个计划读取的 chunk 使用一个 Reader worker，请求启动间隔为 0，所有 chunk 同时调用；
Studio 同样移除原有的 65 秒启动等待。`--workers 0` 为全部并发，显式指定正数才限制并发。
独立 CLI 可通过 `--workers`、`--document-workers` 和 `--request-interval` 调整并发及节流，
设置正间隔时所有文档共享该间隔，缓存不等待。服务返回 429 时等待 45 秒后
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
  `seed.json` 是所有 chunk 使用的同一份完整粗略 dataset；manifest 保存分块修订、worker 数和种子策略。
  每次请求的 `requests/*/input.json` 中可核对实际发送的 `payload.initial_dataset` 和 `payload.sources`。
  `reader_initial_dataset.json` 单独保存所有 Reader 共用的完整业务底稿，manifest 记录其哈希及输入修订。
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

ActionAgent 返回整份业务数据集，默认等待上限为 1800 秒（或更大的通用 timeout）；Reader/问答仍使用通用 timeout（默认 600 秒）。独立调用 Provider 时可用 `action_timeout` 显式覆盖。
