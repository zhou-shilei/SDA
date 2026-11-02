Agent 数据交接调试实验

首页从整体流程开始，当前为四层架构（旧 v6 设计图保留作对照）：
A. 初始化：PDF → 预处理 → 表格检测/OCR → 输入打包 → TemplateAgent（筛选、定位与文档计划） → 固定种子
B. 全文提取：正文/表格证据 → 分块 → 并行 Reader → Action① → 完整 Dataset2 与逐实体域复核
C. 定向补全：Action 域复核 unresolved → Python 分组排队 → 定向检索 → 并行 Answer（默认 4 路）
D. 二次综合：完整固定 Dataset2 + 全部有效 Answer 建议 → Action② → 完整 Dataset3 → SDA 导出
23 个节点均可点击。节点详情先解释职责、执行者、输入字段与输出字段，
再提供本轮 JSON 示例、实际提示词、输出 Schema 和 Python 源码。
调用明细保留原来的三栏查看和字段比较，可从 Reader / Answer 等节点进入。
旧架构图也内嵌在首页下方，供展开对照。

2026-11-02 新版入口：snapshots/49-Sructure-flow-20261102/index.html。
来自 e5d3445e 本轮完整 PDF 运行：62 个 Reader、23 个 Answer；Answer 无有效新增建议，
Action② 为 not_needed，未发送模型请求。该节点展示真实状态、保留的 Dataset3 和明确标注
未调用的当前 Prompt / Schema。全部 Answer 读取同一份固定 Dataset2，不存在逐题更新循环。
必要原文整组保留，8,000 字符是可选证据的软预算。缺口和冲突继续保留。
来源 grouped_sources_v1 仅为可逆传输选项，默认仍为七列来源行；catalogue 属于独立实验。
此次仅读取已落盘文件生成独立快照，不执行 OCR 或模型，不覆盖旧运行。

只验证统一 TemplateAgent 第一次生成 Dataset1（实际调用模型，不启动 Reader/Answer）：

    SDA_CACHE_ENABLED=0 /project_c20250601/252705034/envs/sda/bin/python try/agent_handoff/run_quick.py \
      --snapshot try/agent_handoff/snapshots/49-Sructure \
      --output try/agent_handoff/runs/49-Sructure-v6-seed-20261031 --effort xhigh --seed-only

输出目录必须是新的。document/results/<文档>/dataagent2/seed.json 是交给 Reader 的原始种子；
同级 dataset1.json 是程序从种子组装的视图，dataset1.report.json 和 dataset1.quality.json
记录数量、模板警告及来源核验。seed_ready 表示产物生成成功，不代表所有序列或实验已完整。
可用 audit_seed.py <本次 document/results/<文档> 目录> --snapshot <历史快照> 对照
表格解析结果与种子测量，检查转换遗漏；历史结果只是对照，不是正确答案。

2026-10-31 的真实 v6 seed-only 请求用时约 9 分钟，共 1 次 document-template-v6 调用。
原始请求/回复保存在 runs/49-Sructure-v6-seed-20261031/document/results/49-Sructure/shared_seed/requests。
该次运行发现对照别名定义句式和种子实验绑定问题；修复后可用 revalidate_seed.py 对相同真实回复
重新校验/组装，不重新调用模型。程序强制检查提示词、Schema 和全部输入证据与原请求完全一致：

    /project_c20250601/252705034/envs/sda/bin/python try/agent_handoff/revalidate_seed.py \
      try/agent_handoff/runs/49-Sructure-v6-seed-20261031/document/results/49-Sructure \
      --output /path/to/new-revalidation-directory

本次最终派生产物在同一运行下的 revalidated-v2/：45 个实体、197 条测量、18 条序列编号关联，
16 条实验定义对应 14 个实验 ID；已解析测量在种子转换中无遗漏，实验绑定缺失为 0。
具体序列尚未生成，模板仍 needs_review。revalidation.json 保存原请求/回复哈希及本次代码哈希。

当前 Template 提示词为 v8；此前 v6 已将 LabelAgent 的表格筛选和结构定位并入 TemplateAgent，一次模型调用返回
表格路由、区域、实体、编号映射和实验绑定。输入使用完整 headers/rows 与原始字符串引用。
明确无关表跳过，背景序列表和不确定表保留；预算外表显式标为待复核。
Reader 不再读取模板明确排除的表。历史 JSON、调用记录和 Schema 保持冻结，不能当作 v6 运行结果。
“Python 源码”页展示当前实现和提示词。旧 Label 输入准备工具仅供历史对照。

仅刷新页面与当前定义（不运行模型、不重新采集历史调用）：

    python try/agent_handoff/refresh_page.py try/agent_handoff/snapshots/49-Sructure-flow-20261102

此命令只更新 index.html 和 current_contract.json；snapshot.json、flow_snapshot.json、
preview_snapshot.json 与 records/ 保持冻结。真实新版请求及结果需运行抽取后另建快照。

旧版 LabelAgent 预览曾默认显示“当前 v5 输入预览 · 未发送”，上方版本选择器可切回
“历史真实请求与结果”。当前输入通过生产代码的 semantic_table()、pack_semantic_table()
及辅助表头/上下文规则从已有 OCR 准备，结果单独保存为 current_label_inputs.json。
不执行模型结构复核或分类请求，不沿用旧分类结果。刷新当前输入再生成网页：

    /project_c20250601/252705034/envs/sda/bin/python try/agent_handoff/prepare_label_inputs.py try/agent_handoff/snapshots/49-Sructure
    python try/agent_handoff/refresh_page.py try/agent_handoff/snapshots/49-Sructure

准备当前输入需使用已安装 SDA 依赖的 Python 环境；单纯刷新页面仍只依赖标准库。

真实快速试跑（按当前流程调用模型，必须指定新的输出目录）：

    /project_c20250601/252705034/envs/sda/bin/python try/agent_handoff/run_quick.py \
      --snapshot try/agent_handoff/snapshots/49-Sructure \
      --output try/agent_handoff/runs/<新的运行名> --max-questions 2

复用冻结的 sda_input.json（包括已保存的原图样式证据），不重新识别 PDF/OCR；
重新运行 v5 分类、模板和全部 Reader，默认 gpt-6.1-sol / medium，最多回答两题。
最后的未解决问题、真实输入输出与用量分别保留；不把降低推理强度的试跑当作质量评测。
从新运行的 document/results/<文档ID>/dataagent2 再执行 build_snapshot.py，生成新快照。
含真实 v5 分类请求的新快照默认打开“本次 v5 真实请求与结果”，Template 请求完整保留，
不再仅采样一个表格/数据行。原始回复与程序整理结果继续分开显示。

表格分类节点新增“渲染预览”（进入节点后默认显示）：
- 下拉选择 28 张表，左右按 table_id 和 classifier.input_hash 配对。
- 左侧重建实际输入的完整行列和单元格，绿色是分类证据，黄色是待复核来源。
- 点击单元格或右侧证据地址，定位原文并查看四维 bbox、行列和 OCR 置信度。
- 右侧把分类、轴方向、处理决定、分区、指标和复核项渲染为可读卡片。
- 默认第一张表内嵌经 SHA-256 核对的原始裁图，可展开对照；其余表不内嵌图片。
- “本轮 JSON 与提示词”同步当前选择，保留完整 payload、程序整理后的分类结果，
  并另列该次模型原始返回，不使用之前按数组首项采样的分类示例。
分类理由与问题的原文保持原样；中文标签仅翻译字段含义，不补造事实。
URL 的 table 参数保留选择，例如 ?table=p0262-r0001#stage/classification。

本目录独立于 SDA Studio，仅使用 Python 标准库和原生 HTML/JS。
不启动或修改 Agent，不调用模型，不修改源运行产物。

已生成的样例：snapshots/49-Sructure/index.html。
用浏览器直接打开即可，所有页面数据、脚本和样式已内嵌，无需后端或联网。
VS Code 可使用 HTML 预览扩展；若扩展限制脚本，请使用普通浏览器，
或在终端启动下面的静态文件服务，通过 IDE 转发 4193 端口：

    python -m http.server 4193 --bind 127.0.0.1 --directory try/agent_handoff/snapshots

访问 http://127.0.0.1:4193/49-Sructure/ 。code-server 可用 /proxy/4193/49-Sructure/ 。
从 SDA 仓库根目录运行上述命令；与 Studio 的 4182 端口无关。

生成另一份快照（--run 是包含 seed.json、manifest.json、requests/ 的目录）：

    python try/agent_handoff/build_snapshot.py \
      --run .studio/patents/49-Sructure/runs/c5d425f1b0fe4784b692a87a4b7d5bc1/document/results/49-Sructure/dataagent2 \
      --out try/agent_handoff/snapshots/49-Sructure

输出目录必须是新目录，已有快照不会被覆盖。再次采集时请换一个 --out。
程序一次性扫描已落盘请求，独立复制文件；正在运行的源目录没有全局事务锁，
各文件记录独立读取时间与 SHA-256。需要完全一致的终态时，请等运行结束后采集。

如何使用

初始 Dataset1：打开 #stage/seed，默认进入渲染预览。
五个类别展示完整冻结 seed.json 的实体、编号映射、实验、测量和标签，支持分页、
搜索、点击记录查看完整 JSON、下载完整初始 Dataset1。refs 原文来自冻结的 Reader
请求；没有记录的来源明确标注，不重建坐标。sources.json 是单独文件。
Dataset1 是所有并行 Reader 共用的初始快照；各块返回增量由 Ledger 顺序合并，
更新结果保存到 partial / first_pass，不作为下一个 chunk 的新种子。

1. 首页先看流程图，点击要理解的节点，查看输入输出定义。
2. 切换“本轮 JSON 与提示词”或“Python 源码”，核对实际数据和代码。
3. Reader / Answer 节点可进入对应的实际调用明细；顶部“整体流程”可返回。
4. 调用明细左侧选择 c 编号或问题编号；三栏查看上游、下游输入、下游输出。
   支持逐层展开、搜索定位、复制、下载。上游可切换不同阶段的产物。
   输入可切换业务 payload、完整请求封装、提示词和输出 schema。
5. “字段比较”使用当前三栏选中的数据，支持 JSON Pointer，根节点留空；
   Reader 默认比较 /links → /relation_index/links；Answer 默认问题对象 → /question。
   可改成输入和输出之间比较。数组按下标比较，不做实体语义对齐；最多显示 300 项。
6. “转换源码”显示 Python 函数位置、当前源码、行号、文件哈希，可复制工作区相对路径。
   页面保留源码快照；实际工作区可用 VS Code Ctrl+P 粘贴路径:行号打开。

落盘布局（不用网页也能看）

    index.json                         调用索引、上游引用、Python 函数位置
    snapshot.json                      完整快照、原路径、读取时间、文件哈希
    flow_snapshot.json                 流程节点的补充结构样例、额外源码与旧架构图
    preview_snapshot.json              完整配对的表格分类请求、结果、原始返回及默认裁图
    current_contract.json              当前定义源码及提示词（与历史请求独立）
    artifacts/                         模板、种子、首轮问题/数据集、回答状态
    records/<请求哈希>/request.json     原封不动的请求封装（含 instructions/payload/schema）
    records/<请求哈希>/input.json       提取出的 payload，JSON 内容相同
    records/<请求哈希>/output.json      已保存的 validated.json，原始字节不变
    records/<请求哈希>/attempt-N.json   原始模型尝试，和通过校验的返回分开
    source/src/...                     采集时的完整源码文件副本
    index.html                         内嵌全部数据的独立网页

范围和限制

本实验采集 dataagent2 的 Reader/Answer 已保存请求，以及模板阶段的上游产物。
原 snapshot.json 的请求保持冻结。新增流程节点的上游示例单独采样保存于
flow_snapshot.json，并独立标注时间、源文件哈希、哈希范围和采样范围。
样例中每个数组最多保留首项，不改写字符串；它们不是完整调用数据。
表格分类渲染预览单独使用 preview_snapshot.json 的完整配对记录，不做首项采样。
Reader / Answer 的实际请求及响应继续使用原完整快照。
最后的数据集或导出文件尚未生成时，节点仍展示定义，数据处明确标为未记录。
当前 question_plan() 只对 Action① 的 domain_reviews 缺口排队，不调用额外的 Question 模型。
新快照保存 domain_review / question_plan、固定 Dataset2、Answer 建议、Action② 状态及 Dataset3；
若真实存在 answer_action 请求，也完整采集其 Prompt、Schema、原始返回与通过校验的业务结果。
完整 PDF 运行生成快照时添加 --full-run，复用 OCR 的试跑不要使用该标记。
不宣称记录所有 OCR/分类模型调用。没有给生产流程增加通用运行时 hook。
如果以后需要每次运行自动采集，可以在任务结束后调用同一个构建命令。
尚未写入的请求不在本次快照中，生成之后源任务继续运行不会改变本页。
旧流程没有记录逐题调用前完整 Ledger，旧快照不会重建或伪造它；
新流程保存所有 Answer 共用的 dataset2.business.json，本题实收内容仍以 request.payload 为准。
Template 原始响应可能被程序恢复后才使用，页面区分这两个版本。
所有 Reader 使用相同种子并行运行，c1 的返回不会直接交给 c2。
源码来自采集时工作区，无法证明与历史执行时版本完全相同。
差异、响应通过结构校验、回答被接纳，都不代表语义正确或问题已解决。
快照包含原始专利文字和本地路径，保留在本地；页面不加载 CDN、遥测或外部脚本。
snapshots/ 是本地生成目录，已忽略，不提交大份专利数据。

验证（不调用模型）

    python -m unittest discover -s try/agent_handoff -p 'test_*.py' -v
    node try/agent_handoff/test_core.js
    node --check try/agent_handoff/app.js
    node --check try/agent_handoff/flow.js
    node --check try/agent_handoff/preview.js

开发文件 page.html/style.css/core.js/app.js 是生成模板，不是直接浏览的入口。
