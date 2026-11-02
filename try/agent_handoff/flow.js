/* Current architecture; frozen requests remain explicitly historical. */
(() => {
  'use strict';
  const I = window.HandoffInspector, {data, tree, el, button, copy} = I;
  const F = JSON.parse(document.getElementById('flow-data').textContent), $ = id => document.getElementById(id);
  const current = JSON.parse(document.getElementById('current-contract-data').textContent);
  const C = HandoffCore, sources = {...data.sources, ...F.sources};
  const counts = Object.fromEntries(['vision','template','reader','action','answer','answer_action'].map(stage=>[stage,data.calls.filter(c=>c.stage===stage).length]));
  const selectedRequests=new Map();
  const callsFor=stage=>stage.callStage?data.calls.filter(c=>c.stage===stage.callStage):[];
  const actualCall=stage=>callsFor(stage).find(c=>c.key===selectedRequests.get(stage.callStage))||callsFor(stage).find(c=>c.current)||callsFor(stage)[0];
  const actionCall = data.calls.find(c=>c.stage==='action'&&c.key===data.artifacts.action_status?.value?.provider?.key)||data.calls.find(c=>c.stage==='action'&&c.response);
  const B = (key, pointer = '', label) => ({scope: 'bundle', key, pointer, label});
  const S = (key, pointer = '', label) => ({scope: 'flow', key, pointer, label});
  const R = (side, label) => ({scope: 'reader', side, label});
  const A = (side, label) => ({scope: 'answer', side, label});
  const X = (side, label) => ({scope: 'action', side, label});
  const Q = side => ({scope: 'classification', side});
  const stages = [
    {id:'pdf', title:'原始 PDF', subtitle:'文档入口', kind:'file', x:35,y:80,w:95,
      purpose:'从一份真实专利 PDF 开始。文档 ID 和内容哈希贯穿后续产物，避免不同运行或不同专利混用。',
      upstream:'本地 PDF 文件', downstream:'方向校正与版面处理', owner:'文件输入',
      input:[['PDF 文件','binary','原始页面、表格、图和文字'],['document_id','string','本轮文档身份']],
      output:[['source_pdf','path','源 PDF 路径'],['source_pdf_sha256','string','源文件内容哈希']],
      rules:['此节点是文件入口，没有 LLM 调用。'], inputs:[], outputs:[S('layout','/source_pdf','已记录的 PDF 路径'),S('layout','/source_pdf_sha256','PDF SHA-256')], sources:['document_run']},
    {id:'preprocess', title:'预处理', subtitle:'方向校正 · 页面准备', kind:'python', x:170,y:80,w:145,
      purpose:'校正页面方向，建立工作 PDF 和页码、坐标之间的关系。后续正文 OCR 与表格检测都读取同一轮工作文档。',
      upstream:'原始 PDF',downstream:'表格检测 / 正文 OCR',owner:'Python + 方向检测',
      input:[['source PDF','path','原始 PDF'],['device / layout options','config','设备、版面与页码选项']],
      output:[['processing_pdf','path','方向校正后的工作 PDF'],['orientation','object','页面旋转和坐标变换'],['processing_pdf_sha256','string','后续证据的工作文件身份']],
      rules:['正文 OCR 和表格 OCR 可以并行；两路完成后才组装统一文档。'],inputs:[S('layout','/source_pdf')],outputs:[S('layout')],sources:['prepare_pdf','document_run']},
    {id:'detection',title:'表格检测与裁图',subtitle:'页码 · 矩形 · 图像',kind:'ocr',x:360,y:80,w:145,
      purpose:'找到表格区域并裁图，保存裁图到 PDF 原页的坐标映射，让识别结果能回到原图检查。',
      upstream:'工作 PDF 与版面检测',downstream:'表格 OCR',owner:'版面模型 + Python 裁图',
      input:[['processing_pdf','path','经过方向校正的 PDF'],['regions / pages','array','检测区域及页面信息']],
      output:[['images[]','array<object>','表格 ID、页码、bbox、图片路径'],['images[].crop_to_pdf','number[]','裁图像素到 PDF 的转换'],['images[].sha256','string','裁图身份']],
      rules:['表格的内容识别在下一步执行；此处主要产出位置和图片。'],inputs:[S('layout','/processing_pdf')],outputs:[S('layout')],sources:['layout']},
    {id:'table_ocr',title:'表格 OCR',subtitle:'单元格与序列识别',kind:'ocr',x:550,y:80,w:145,
      purpose:'读取表格裁图，将单元格、HTML、行列关系和来源位置保存下来；序列原图复核也属于这条证据链。',
      upstream:'表格裁图与位置清单',downstream:'表格输入打包 / 统一证据',owner:'OCR 模型 + 序列裁行识别',
      input:[['image_path / image_sha256','path / string','实际裁图及身份'],['page / bbox / crop_to_pdf','object','原页定位信息']],
      output:[['text / vl_text','string','表格 HTML 或原始识别文本'],['cells[] / grid','array / object','单元格地址、行列与坐标'],['sequence_ocr','object','序列识别状态和失败信息']],
      rules:['OCR 文字是证据，不等于已建立抗体与序列的语义归属。'],inputs:[S('layout','/images')],outputs:[S('table_ocr')],sources:['document_run','assemble']},
    {id:'table_input',title:'表格输入打包',subtitle:'全部候选表 · 原始引用',kind:'python',x:740,y:80,w:145,
      purpose:'将候选表的完整单元格和匹配到的表题组织成 Template 输入，保留原始行列与来源地址；不执行独立 LabelAgent 分类。正文 OCR 不作为 passages 发送。',
      upstream:'完整表格 OCR + 匹配到的表题文本',downstream:'TemplateAgent',owner:'semantic_table() / build_document_input()',
      input:[['OCR rows','array','完整单元格与原始来源地址'],['matched caption','string?','只传匹配到该表的标题文字'],['table contexts','array','页码、几何关系和已验证续表关系；不传邻近正文段落'],['document budget','integer','整篇表格与表题输入预算']],
      output:[['caption / headers / rows','string / array','表题、完整表头和全部数据行，空列保留'],['allowed_header_table_ids','array','经过验证的跨页表头来源'],['omitted_table_ids','array','预算外表格，保留待复核']],
      rules:['TemplateAgent 只接收表格 OCR 单元格、匹配标题和结构元数据；不接收统一 OCR 正文全文。','所有候选表交给 TemplateAgent 判断用途，不按旧分类结果预先过滤。','超预算不等于无关；预算外表格仍作为待复核证据保留。'],inputs:[S('semantic','/tables')],outputs:[S('template_request','/value/payload/tables')],sources:['table_payload','template_input']},
    {id:'template',title:'TemplateAgent',subtitle:'筛选 + 结构定位 + 文档计划',kind:'llm',x:930,y:80,w:165,
      purpose:'在一次模型调用中读取表题、表头与数据行，判断每张表的用途，并为保留表定位区域、建立实体与序列映射、绑定实验方法；不读取正文全文。',
      upstream:'全部候选表 OCR + 匹配到的表题（仅标题文本）',downstream:'表格计划物化 / 种子初始化',owner:'LLMTemplateAgent.run()',
      input:[['document_id','string','本篇文档身份'],['tables[].caption','string?','由主机匹配到表格的标题文字'],['tables[].headers / rows','array','完整候选表头与全部数据行；原文不截断'],['header_row_indices / row_indices / cell_views','array / object?','原行号、空列、合并表头和已有复核值'],['allowed_header_table_ids','array','可安全继承的续表表头引用'],['assay_keyword_context','object','实验关键词背景'],['omitted_table_ids / omitted_*','array','预算外表格，不得推断或作为无关表删除']],
      output:[['tables','array','每表的标签、路由、证据和必要的区域计划'],['entities','array','实体身份、名称和来源'],['sequence_links','array','实体、链和 SEQ ID 的对应'],['assay_profiles','array','绑定同一响应中建立的实验区域'],['unresolved','array','仍需复核的问题']],
      rules:['extract、context_only、needs_review 均保留；仅明确 exclude 的表跳过后续读取。','序列定义与无表头续表是有效背景证据；不能因没有实验指标而删除。','primary_label、measurement_axis 和 detected_endpoints 由程序派生；分类兼容产物不需要独立模型调用。','下方真实 JSON 保留历史版本；当前 v6 代码和提示词在源码页查看。'],inputs:[S('template_request','/value/payload')],outputs:[B('template_raw'),B('template','/document_plan','程序恢复后的文档计划'),B('template')],sources:['table_payload','template','table_plans','template_schema'],schema:'template_request'},
    {id:'seed',title:'初始 Dataset1 / Seed',subtitle:'点击查看完整初始数据',kind:'data',x:1160,y:80,w:180,
      purpose:'主机将完整 Template 结果与统一 OCR 来源分别处理：建立全量业务 seed，并保留带地址的正文/表格原文供 Reader 分块。它是并行 Reader 的共同起点。',
      upstream:'完整 Template 结果 + 统一 OCR 来源（由主机组装）',downstream:'完整业务 seed + 每个 Reader 的 OCR chunk',owner:'load_input() / initial_dataset()',
      input:[['document_template.json','object','模板实体、实验和编号映射'],['sda_input.json','object','带来源的正文与表格 OCR；正文不回传给 TemplateAgent']],
      output:[['entities / links','array','内部实体、别名与链编号映射'],['experiments / observations','array','初步实验与表格测量'],['annotations','array','来源支持的标签'],['sources.json','object','整数来源 ID 到原文、页码和位置']],
      rules:['分类与模板中的原始字符串引用，在 load_input() 初始化时转换为整数来源 ID；来源原文与位置仍可追溯。','Reader 保留背景和待复核表，只跳过模板明确排除的表；各块携带同一份完整初始业务数据。','不继承后续修复或历史回答。各块不接收其他块的增量输出。'],inputs:[B('template'),S('semantic')],outputs:[B('seed')],sources:['load','reader_initial_dataset']},
    {id:'page_ocr',title:'正文 OCR',subtitle:'逐页文字与原页坐标',kind:'ocr',x:170,y:300,w:145,
      purpose:'识别全文页面，把正文行及其坐标保存到 pages.jsonl。和表格 OCR 共同组成可引用的证据。',
      upstream:'预处理后的工作 PDF',downstream:'统一 OCR 证据',owner:'正文 OCR 模型',
      input:[['processing_pdf','path','本轮工作 PDF'],['page selection / backend','config','页面范围与识别配置']],
      output:[['page_number / pdf_sha256','integer / string','页码与文件身份'],['lines[].text / line_id','string','原文与行地址'],['lines[].bbox / polygon','number[]','原页坐标'],['status / quality','string / object','识别状态与质量信息']],
      rules:['页面和表格可并行识别；正文失败不会被表格成功掩盖。'],inputs:[S('layout','/processing_pdf')],outputs:[S('page_ocr')],sources:['document_run','assemble']},
    {id:'evidence',title:'统一 OCR 证据',subtitle:'原文 + 来源整数地址',kind:'data',x:360,y:300,w:145,
      purpose:'统一保存正文和表格 OCR 及原页位置。主机只把匹配到的表题文字交给 TemplateAgent；正文和其余来源留给 Reader 分块及定向检索。',
      upstream:'正文 OCR + 表格 OCR',downstream:'匹配表题 → Template；全文来源 → 分块 / 定向检索',owner:'assemble_document() → prepare_sda_inputs() → load_input()',
      input:[['pages.jsonl','JSONL','逐页正文'],['tables.jsonl','JSONL','表格文字、单元格和定位'],['layout manifest','object','版面、裁图及坐标关系']],
      output:[['document.json','object','统一文档的 pages / tables 等'],['sda_input.json','object','document_blocks / tables / table_contexts'],['source_row','tuple','[来源ID, 页码, 类型, 表ID, 行, 列, 原文]']],
      rules:['refs 是来源地址，需回到原文核对，不是模型生成的解释。'],inputs:[S('page_ocr'),S('table_ocr')],outputs:[S('document'),S('semantic'),R('sources','Reader 请求内的来源行')],sources:['assemble','semantic','load']},
    {id:'chunking',title:'全文分块',subtitle:'Document chunking',kind:'python',x:550,y:300,w:145,
      purpose:'按文档顺序和字符预算分块，表格优先保留完整物理行，并附带跨页表头和边界上下文。',
      upstream:'有地址的全文 OCR 证据',downstream:'并行 Reader 请求',owner:'make_chunks()',
      input:[['data.sources','object','所有可读取来源'],['max_chars / overlap_chars','integer','本块来源预算与上下文预算']],
      output:[['chunks[].id','string','c1、c2 等稳定块编号'],['core','integer[]','只属于本块的主要来源'],['context','integer[]','允许重复携带的辅助来源'],['source_characters / oversized_source_ids','integer / array','大小与无法再拆的来源']],
      rules:['每个来源恰好属于一个 core。分块不依赖其他块的模型回答。'],inputs:[R('sources')],outputs:[B('manifest','/chunks','真实分块清单')],sources:['chunks']},
    {id:'reader',title:'ReaderAgent',subtitle:'并行提取新增事实',kind:'llm',x:930,y:300,w:165,
      purpose:'每个 chunk 携带同一份完整初始业务 seed，结合本块正文/表格 OCR 提取新增事实和冲突；各块独立并行。',
      upstream:'本块原文 + 完整初始业务 seed',downstream:'ActionAgent 跨块综合',owner:'reader_payload() → Provider.generate("reader")',
      input:[['document / chunk','string','文档与块身份'],['initial_dataset','object','全部已有实体、关联、实验、测量（含值和单位）、标签及可用的初始序列定义'],['sources','array<tuple>','本块正文和表格的实际 OCR 原文'],['context_only_sources / existing_observation_sources','integer[]','辅助上下文 / 已有测量的来源'],['tag_categories / tag_vocabulary','array / object','允许的标签维度和示例词表'],['source_text_styles / source_image_styles / source_sequence_ocr','object?','有对应证据时附带：文字样式、原图域边界、序列 OCR 状态']],
      output:[['mentions / new_items','array','来源提及与新增实体'],['links / sequences','array','编号映射与原文序列锚点'],['experiments / observations','array','实验和测量事实'],['annotations / relations / issues','array','标签、实体关系和问题']],
      rules:['所有 chunk 使用相同完整业务 seed，不按名称、SEQ ID 或引用交集筛选；有初始序列定义时完整保留。','新增事实只能引用本次 sources 中的证据，seed 中远处的引用只作背景。','输出是增量事实；通过格式校验后还要做来源检查与合并。'],inputs:[R('payload','第一个 Reader 的真实 payload')],outputs:[R('response','第一个 Reader 的真实返回')],sources:['reader','reader_initial_dataset','chunks','provider','merge'],callStage:'reader'},
    {id:'consolidation',title:'Action① · 全局综合',subtitle:'完整 Dataset2 + 域复核',kind:'llm',x:1160,y:300,w:180,
      purpose:'所有 Reader 成功完成后，将同一份初始 Dataset、各块修改建议和带地址的原文证据交给 ActionAgent。模型跨块处理互补信息与冲突，返回完整业务数据集；程序随后校验来源、序列原文和每条建议的处理记录。',
      upstream:'初始 Dataset1 + 全部 Reader 建议 + 相关 OCR 证据',downstream:'程序校验 → 首轮 Dataset2',owner:'build_action_payload() → Provider.generate("action") → validate_action_result()',
      input:[['initial_dataset','object','完整业务底稿'],['reader_actions','array','所有 Reader 建议，保留稳定 ID、块编号和来源范围问题'],['reader_coverage','object','总块数和完成块数'],['domain_review_input','object','完整链与可变域、恒定区的精确比较候选；语义归属由模型确认'],['sources / source_*','array / object?','建议和种子引用的原文及关联上下文、可用样式和 OCR 状态']],
      output:[['dataset','object','完整业务数据集，包含实体和各类事实'],['domain_reviews','array','逐实体 VH/VL 的 supported / unresolved / not_applicable、证据及具体缺口'],['action_decisions','array','每条建议归入 accept / merge / reject / conflict，并指向输出记录'],['seed_changes','array','明确记录被替换或拒绝的初始事实'],['conflicts','array','保留冲突、证据与对应建议 ID']],
      rules:['Reader 结果是候选建议；ActionAgent 返回完整数据集。','每条建议必须恰好处理一次；初始实体与有来源的事实不得无记录地丢失。','程序负责格式、来源、字符与完整性校验；失败后最多反馈重试一次。','Reader 未全部成功或 ActionAgent 校验失败时，结果保留 partial，后续文字问答不执行。','本页历史快照没有 ActionAgent 调用；下方展示当前契约，不伪造实测结果。'],
      inputs:[],outputs:[],sources:['action_payload','action_run','action_validate','run']},
    {id:'ledger',title:'首轮 Dataset2',subtitle:'完整结果 · 经程序校验',kind:'data',x:1160,y:510,w:180,
      purpose:'ActionAgent 返回的完整业务 Dataset 通过程序校验后，形成正式首轮结果。Reader 运行时按顺序累积的 Ledger 仅供临时审阅，不能代替 ActionAgent 的跨块综合。',
      upstream:'Action① 完整返回 + 程序校验',downstream:'域复核缺口排队 → 并行 Answer / Action②',owner:'validate_action_result() → Ledger.assemble()',
      input:[['dataset / action_decisions','object / array','完整结果和逐条建议处理记录'],['initial_dataset / sources','object / array','校验底稿和可引用原文']],
      output:[['dataset.first_pass.json','object','当前代码中经 ActionAgent 综合的首轮结果'],['facts.first_pass.json','object','首轮事实账本'],['action_agent.*.json','files','请求、返回、校验与综合状态']],
      rules:['无 Reader 建议时可直接保留初始 Dataset，状态为 not_needed。','下方首轮 JSON 是历史产物，采集时尚未执行当前 ActionAgent。'],inputs:[],outputs:[B('first_pass'),S('facts')],sources:['action_validate','run','assemble_ledger']},
    {id:'question',title:'缺口排队',subtitle:'Action 域复核 → Python 分组',kind:'python',x:970,y:510,w:145,
      purpose:'Action① 已给出逐实体的证据缺口。程序只对 unresolved 复核项分组排队，共享编号或同实体无编号缺口可归为一题；不调用额外的 Question 模型。',
      upstream:'固定 Dataset2 + Action① 的 domain_reviews',downstream:'定向检索 → 并行 Answer',owner:'question_plan()',
      input:[['dataset.entities','array','首轮实体和各域现状'],['domain_reviews','array','全部实体 VH/VL 的复核结论和具体 missing_evidence']],
      output:[['questions / review_context','array','按缺口分组的问题及原始复核依据'],['excluded_reviews','array','supported / not_applicable 项'],['recovery_action','string?','OCR 失败留给图片重新识别，不进入文字 Answer']],
      rules:['缺口语义来自 Action①；Python 仅负责检查覆盖和分组，不调用轻量模型。','question_plan.json 保留全部排队依据和排除项；questions.initial.json 是本轮队列。','问题预算未覆盖的项继续保存为 deferred_budget。'],inputs:[B('first_pass'),B('domain_review')],outputs:[B('question_plan'),B('questions')],sources:['question','followup']},
    {id:'answer',title:'Answer · 并行补证',subtitle:'默认 4 路 · 共享固定 Dataset2',kind:'llm',x:780,y:510,w:145,
      purpose:'所有 Answer 独立读取同一份固定 Dataset2 的定向上下文及本题原文，用专用小型 Prompt 和 Schema 回答一项具体缺口。回答在副本校验，只产生建议，不修改共享数据。',
      upstream:'具体缺口 + 固定 Dataset2 上下文 + 必要原文',downstream:'副本校验 → 全部有效建议交给 Action②',owner:'answer_payload() → Provider.generate("answer") → validate_answer()',
      input:[['question / dataset2_hash','object / string','问题、域复核依据与固定底稿哈希'],['items / known_links / known_definitions / known_relations','arrays','实体目录及本题已知事实'],['candidate_comparisons','object','完整链拆分的字面比较候选，不作为归属结论'],['sources / retrieval','array / object','原文及必要证据大小、可选证据遗漏和软预算状态']],
      output:[['question_id / status / answer','strings','对应问题、resolved / partial / unresolved / conflict 和简短回答'],['refs / missing_evidence','arrays','答案证据及具体仍缺的依据'],['actions','array','add_link / add_sequence_definition / add_relation / record_conflict']],
      rules:['默认 4 路并行，每题只提出少量必要新增建议；不重新抽取测量或完整数据集。','必要证据和完整序列不截断，可以超过 8,000 字符软预算；可选来源按预算追加。','validate_answer() 在副本检查引用与字面证据；正式 Dataset2 在全部 Answer 完成前保持相同。','resolved 是回答的主张，最终是否补全以 Action② 综合后的实体状态为准。'],inputs:[B('dataset2_business'),A('payload','实际定向输入')],outputs:[A('response','实际回答及候选建议'),B('answers')],sources:['answer','answer_validate','followup','provider'],callStage:'answer'},
    {id:'action',title:'Action② · 问答综合',subtitle:'有新建议才调用 · 完整 Dataset3',kind:'llm',x:590,y:510,w:145,
      purpose:'全部 Answer 完成并通过副本校验后，将完整 Dataset2、有效建议及对应原文一次交给 LLM 综合，返回完整 Dataset3。有冲突的建议保留审计，受支持的域不得静默覆盖。没有有效新增建议时跳过模型调用并保留 Dataset2。',
      upstream:'完整 Dataset2 + 全部有效 Answer 建议 + 原文',downstream:'完整 Dataset3 / 综合失败时保留 Dataset2',owner:'build_answer_action_payload() → run_action_payload(stage="answer_action")',
      input:[['initial_dataset','object','完整业务 Dataset2'],['reader_actions / answer_results','arrays','Answer 建议及回答和缺口；保留稳定建议 ID'],['protected_domains','array','已支持域的值，禁止静默覆盖或降级'],['sources / domain_review_input','array / object','原文与本次域复核候选']],
      output:[['dataset / domain_reviews','object / array','完整 Dataset3 与更新后的域复核'],['action_decisions / seed_changes / conflicts','arrays','逐条处理、底稿修改与冲突审计'],['answer_action.json','file','validated / not_needed / failed 状态']],
      rules:['Answer 之间没有逐题更新循环；全部有效建议汇总后最多进入一轮综合，校验反馈另行记录。','无有效建议时状态为 not_needed，不发送模型请求；Dataset3 保留 Dataset2。','综合失败时保留 Dataset2 和错误；最终缺口、冲突与历史拒绝记录继续保存。'],inputs:[B('dataset2_business'),B('answer_actions'),B('answer_action_input')],outputs:[B('answer_action_status'),B('answer_action_dataset'),B('dataset3_business')],sources:['answer_action_payload','followup','action_validate']},
    {id:'final',title:'最终 Dataset3',subtitle:'数据、冲突与剩余缺口',kind:'data',x:400,y:510,w:145,
      purpose:'Reader 和有限问答完成后，保存原生数据集、剩余问题与质量核对。没有生成最终文件时，页面明确显示未记录。',
      upstream:'Action② 完整结果，或跳过 / 失败时保留的 Dataset2',downstream:'SDA 导出适配器',owner:'Ledger.assemble() / audit_dataset()',
      input:[['当前 Ledger','runtime object','已接纳的事实和冲突']],
      output:[['entities / sequences / observations','array','实体、序列和测量'],['conflicts / issues / rejected_facts','array','审阅所需的不确定性'],['reader_coverage / status','object / string','读取范围与结果状态'],['quality_audit.json / questions.json','files','质量核对与最终剩余问题']],
      rules:['未生成最终文件时不能用首轮或 partial 数据替代。'],inputs:[B('answers')],outputs:[S('final')],sources:['assemble_ledger','audit','run']},
    {id:'export',title:'SDA 导出',subtitle:'审阅格式与来源关联',kind:'python',x:210,y:510,w:145,
      purpose:'把 dataagent2 原生实体、事实和整数来源地址转换为 SDA 审阅格式，供原网页显示和导出。',
      upstream:'最终 Reader Dataset + 原始来源',downstream:'dataset.json / 审阅网页',owner:'export_result()',
      input:[['data / final / seed_dir','objects / path','原生数据、最终结果及共享种子产物']],
      output:[['dataset.json','object','原网页使用的数据集'],['template.completed.json','object','SDA 字段槽位与来源'],['coverage.json / missing_fields.json','objects','覆盖范围与未完成项']],
      rules:['保留不同实验与条件；未知归属和缺口继续显示待复核。'],inputs:[S('final')],outputs:[S('export')],sources:['export']}
  ];
  // Request samples store the parsed original under value. Paths above remain
  // human-readable while resolve() selects from the actual sample value.
  const stageMap = new Map(stages.map(s => [s.id, s]));
  if(counts.template){
    stageMap.get('template').callStage='template';
    stageMap.get('template').rules=stageMap.get('template').rules.filter(r=>!r.startsWith('下方真实 JSON'));
    stageMap.get('template').rules.push('下方展示本轮真实请求和原始返回；恢复后的计划与模型原始结果分别保留。');
  }
  if(counts.vision){
    const stage={id:'sequence_review',title:'序列原图复核',subtitle:`${counts.vision} 次实际图片调用`,kind:'llm',x:740,y:180,w:145,h:66,
      purpose:'对选定序列表的原图进行样式和字符复核，保存图片身份、序列片段及识别范围。它与普通 OCR、Template 表格识别分别记录。',
      upstream:'表格裁图 + OCR 定位单元格',downstream:'复核值及原图域信息 → Template / Reader',owner:'sequence-style-v1 / CodexProvider',
      input:[['image / cells','image / array','本轮原始裁图和待复核单元格'],['style_annotations','array','需要复核的标记与样式']],
      output:[['cells / domains','array','实际复核返回，保留原始片段和范围']],rules:['原图复核不是读取完整 PDF；附图和返回单独保存。'],
      inputs:[{scope:'vision',side:'payload'}],outputs:[{scope:'vision',side:'response'}],sources:['shared_provider'],callStage:'vision'};
    stages.splice(stages.findIndex(s=>s.id==='table_input'),0,stage);stageMap.set(stage.id,stage);
  }
  stageMap.get('seed').outputs=[B(data.artifacts.reader_initial?'reader_initial':'seed'),B('seed')];
  stageMap.get('ledger').inputs=[B('action_dataset'),B('action_status')];
  stageMap.get('ledger').outputs=[B('first_pass'),B('dataset2_business'),B('domain_review')];
  if(counts.answer_action)stageMap.get('action').callStage='answer_action';
  const integration=data.artifacts.answer_action_status?.value;
  if(integration?.status==='not_needed')stageMap.get('action').subtitle='本轮跳过 · 无有效新增建议';
  if(integration)stageMap.get('action').rules.push(`本轮实际状态：${integration.status}${integration.status==='not_needed'?'，没有有效新增建议，未调用 Action② 模型。':integration.status==='failed'?'，保留 Dataset2 与失败记录。':'，以保存的校验状态为准。'}`);
  if(data.artifacts.final_dataset)stageMap.get('final').outputs=[B('final_dataset'),B('quality')];
  if(data.artifacts.sda_export)stageMap.get('export').outputs=[B('sda_export')];
  if(actionCall){
    const actionStage=stageMap.get('consolidation');
    actionStage.callStage='action';
    actionStage.input=actionStage.input.map(row=>row[0]==='reader_actions'?['reader_actions','array / object','全部 Reader 建议；审计文件为数组，大输入可按种类列式传输']:row);
    actionStage.output=actionStage.output.map(row=>row[0]==='dataset'?['dataset','object','完整业务结果；模型可显式引用已有记录，由程序展开后完整保存']:row);
    actionStage.inputs=[B('action_input'),X('payload','实际发送的输入表示')];
    actionStage.outputs=[B('action_dataset'),X('response','原始返回表示'),B('action_status')];
    actionStage.rules=actionStage.rules.filter(r=>!r.startsWith('本页历史快照'));
    actionStage.rules.push('大输入可使用可逆列式编码和显式记录引用；程序展开模型明确选择的记录，再校验完整数据集。');
    stageMap.get('ledger').rules=stageMap.get('ledger').rules.filter(r=>!r.startsWith('下方首轮 JSON'));
    stageMap.get('ledger').rules.push('下方首轮 JSON 是本轮 ActionAgent 通过完整校验后的真实产物。');
  }
  Object.assign(stageMap.get('evidence'),{
    title:'坐标对齐 / 原始证据',subtitle:'文字行框 ↔ 单元格 ↔ PDF',kind:'python',
    purpose:'主机组装两路 OCR：用观测到的文字框、行列约束和裁图变换定位表格单元格。这里保留原始字符串引用、原文和坐标；整数来源 ID 在后续初始化时建立。',
    upstream:'PDF OCR 文字行框 + TableOCR 行列结构 + 裁图变换',downstream:'表格输入打包 / 主机初始化来源',
    owner:'locate_grid() / assemble_document() / prepare_sda_inputs()',
    output:[['document / sda_input','objects','全文、表格和结构元数据，保留 OCR 字符串引用'],['bbox_crop / bbox_pdf / bbox_source','number[]','截图像素、工作页和原 PDF 的位置'],['precision / location_status','string','标明定位粒度；可靠性不足时坐标留空']],
    rules:['先匹配观测文字框，再检查行列关系；多行文字可使用多个 OCR 框。','R/C 是逻辑行列地址，不保证已有物理坐标。','本节点不建立抗体与 VH/VL 的业务归属，也不让 LLM 生成坐标。'],sources:['cell_geometry','coordinate_transform','assemble','semantic']
  });
  Object.assign(stageMap.get('seed'),{title:'Dataset1 初始化',subtitle:'程序转换完整业务底稿',
    upstream:'Template 结果 + 主机保存的原始 OCR 证据',downstream:'相同完整 Dataset1 → Reader / Action；来源索引 → 分块与回查',
    purpose:'load_input() 在主机内把 Template 初稿转换成完整业务底稿，同时建立独立来源索引。全文用于来源初始化，不回传给 TemplateAgent。两种输出分别传给后续步骤。'});
  Object.assign(stageMap.get('table_input'),{upstream:'坐标对齐后的表格 + 匹配表题 + 可用的原图复核值'});
  stageMap.get('reader').input.push(['source_encoding','string?','可选 grouped_sources_v1 按页 / 表 / 行可逆组织来源；默认仍使用七列来源行']);
  stageMap.get('reader').rules.push('grouped 只改变来源的传输表示，保留全部原文、来源 ID、物理行列及顺序；catalogue 等输入筛选仍是独立实验。');
  stageMap.get('reader').sources.push('reader_request','source_encode','source_decode');
  Object.assign(stageMap.get('final'),{subtitle:'可为 partial · 缺口保留',downstream:'SDA 导出 / 待复核审计 / 引用回查'});
  const extraStages=[
    {id:'source_index',title:'来源索引',subtitle:'原文 / 页码 / 框 / 来源 ID',kind:'data',
      purpose:'这是 Dataset1 主机初始化的另一类输出。每段正文或单元格获得整数来源 ID，保留原始引用和可用坐标。Agent 实收的是来源行；坐标保存在主机侧，供结果回查。',
      upstream:'load_input() 的原始 OCR 与 Template 路由结果',downstream:'全文分块 / Action 证据 / 定向检索 / 原位回查',owner:'load_input() / source_row()',
      input:[['sda_input / Template routes','objects','原文、表格及明确排除的表格计划']],
      output:[['sources.json','object','整数 ID → source_ref / 原文 / 页码 / bbox'],['source_row','tuple','Agent 实收：[ID, 页码, 类型, 表ID, 行, 列, 原文]']],
      rules:['来源索引和业务 Dataset 独立保存；全文不作为 Template 的输入。','Agent 引用整数 ID，程序从来源字典找回位置。','bbox 主要为工作页 PDF 点坐标；原始 PDF 变换保留在上游文件。'],
      inputs:[S('semantic'),B('template')],outputs:[B('source_index')],sources:['load','reader_initial_dataset']},
    {id:'retrieval',title:'本题证据检索',subtitle:'Python · 有输入预算',kind:'python',callStage:'answer',inspectCalls:true,
      purpose:'从同一份固定 Dataset2 取出本题已有关系、定义和精确比较候选。先完整保留必要引用及其表头 / 邻近来源，再按软预算加入其他匹配证据。',
      upstream:'具体缺口 + 固定 Dataset2 + 来源索引',downstream:'AnswerAgent 的本题 payload',owner:'answer_payload()',
      input:[['question / ledger','objects','目标、已知关系和定义'],['sources','object','主机保存的原文与来源字典']],
      output:[['sources / known_links / known_definitions','arrays','原文和定向业务上下文'],['candidate_comparisons','object','本题引用的比较候选及定义'],['retrieval','object','soft_source_chars / required_source_chars / omitted_optional_bundles / required_budget_exceeded']],
      rules:['这里是确定性程序检索，没有 LLM Prompt。','Reader 使用全量初始 seed；本题补全使用定向检索，两者输入策略不同。'],
      inputs:[B('questions'),B('dataset2_business'),B('source_index')],outputs:[A('payload','本题实际送给 Answer 的输入')],sources:['answer','followup']},
    {id:'provenance',title:'原位追溯',subtitle:'refs → 原文与位置',kind:'python',
      purpose:'用 Dataset 中的来源 ID 和字符片段回查原文、页码及可用位置框。截图框通过 crop_to_pdf 映射到工作 PDF；上游保存工作页到原 PDF 的变换。字符起止位置不等于逐字符像素框。',
      upstream:'最终 Dataset 的 refs / fragments + 完整来源索引',downstream:'原文证据、对应页面及裁图位置',owner:'sources.json / fragment_refs() / ground_sequence()',
      input:[['refs / fragments','arrays','引用地址与已验证的字符起止位置'],['sources / crop_to_pdf','objects','原文、工作页框与坐标变换']],
      output:[['source_ref / page / bbox','string / integer / array','实际对应的原文位置'],['start / end / quote','integer / string','序列在来源文字中的范围']],
      rules:['只显示已经保存的框，未知位置保持空。','高亮为文字行或单元格范围，不声称每个残基都有单独像素框。','表格 R/C 编号可能随 OCR 改变；追溯还需页码、位置、原文和文件身份。'],
      inputs:[B('final_dataset'),B('source_index')],outputs:[B('source_index'),B('final_dataset','/entities/0/sequences/VH/fragments','一条实际 VH 的原文片段')],sources:['load','sequence_fragments','coordinate_transform']},
    {id:'review',title:'待复核与审计',subtitle:'缺口 / 冲突 / 未采纳理由',kind:'data',
      purpose:'本轮执行结束后，未解决问题、冲突标记和拒绝理由继续保存。它们没有因为读取流程完成而变成已确认业务事实，也不表示后台仍有自动处理在运行。',
      upstream:'最终 Dataset + questions.json + 事实候选',downstream:'后续针对性复核与修订',owner:'question_plan() / Ledger.assemble() / export_result()',
      input:[['questions / conflicts / issues','arrays','缺口、冲突及问题标记']],
      output:[['questions.json','array','最终未解决问题'],['conflicts / issues / rejected_facts','arrays','证据与理由；不同清单可能重叠']],
      rules:['partial 表示仍有业务待复核项；读取和整合流程可以已经完整结束。','冲突标记需要复核，不能直接当作专利矛盾或模型错误数量。','未采纳建议保留审计；被挂起的测量候选不冒充正式测量。'],
      inputs:[B('final_dataset'),B('facts_final')],outputs:[B('remaining_questions'),B('final_dataset','/conflicts','冲突标记'),B('final_dataset','/issues','问题标记'),B('final_dataset','/rejected_facts','未采纳建议')],sources:['question','assemble_ledger','export']}
  ];
  for(const stage of extraStages){stages.push(stage);stageMap.set(stage.id,stage);}
  const graph=HandoffGraph;
  for(const stage of stages){const p=graph.positions[stage.id];if(p)[stage.x,stage.y,stage.w,stage.h]=p;}
  stages.sort((a,b)=>graph.order.indexOf(a.id)-graph.order.indexOf(b.id));
  const graphEdges=graph.edges.filter(edge=>stageMap.has(edge.from)&&stageMap.has(edge.to));
  window.HandoffFlow={nodes:stages,edges:graphEdges};
  const kindLabels = {llm:'LLM Agent',python:'Python 程序',ocr:'OCR / 版面模型',data:'结构化数据',file:'文件入口'};
  function navigate(route) { if (location.hash.slice(1) === route) routePage(); else location.hash = route; }
  function screen(name) {
    for (const id of ['overview', 'stage', 'calls']) $(`${id}-view`).hidden = id !== name;
    $('overview-nav').setAttribute('aria-current', name === 'calls' ? 'false' : 'page');
    $('calls-nav').setAttribute('aria-current', name === 'calls' ? 'page' : 'false');
  }
  function resolve(ref) {
    let item, value;
    if (ref.scope === 'bundle') item = data.artifacts[ref.key];
    if (ref.scope === 'flow') item = F.samples[ref.key];
    if (['reader','answer','action','answer_action','template','vision'].includes(ref.scope)) {
      const c = actualCall({callStage:ref.scope});
      if (c) {
        item = ref.side === 'response' ? c.response : c.request;
        if (item) value = ref.side === 'payload' ? item.value.payload : ref.side === 'sources' ? item.value.payload.sources : item.value;
      }
    }
    if (!item) return {label: ref.label || '本轮尚未记录', missing:true, scope:'本次快照没有这份产物'};
    value = value === undefined ? item.value : value;
    // Supplemental request envelopes are sampled just like other JSON files.
    const pointer = ref.scope === 'flow' && ref.pointer?.startsWith('/value/') ? ref.pointer.slice(6) : ref.pointer || '';
    try { value = C.at(value, pointer); } catch { return {label:ref.label || item.label || '字段未记录',missing:true,scope:'源快照没有这个字段'}; }
    return {value, label:ref.label || item.label || (ref.side === 'response' ? '模型返回' : '实际请求'),
      path:item.original_path, file:item.path, scope:item.scope || '原始冻结数据（本视图完整展示所选对象）', sampled:item.sampled, counts:item.counts};
  }
  function contract(title, fields) {
    const box = el('section',undefined,'contract-card'); box.append(el('p',title,'contract-label'));
    const table = el('table'), head = el('tr'); ['字段 / 文件','类型','含义'].forEach(t=>head.append(el('th',t))); const thead=el('thead');thead.append(head);table.append(thead);
    const body=el('tbody');fields.forEach(row=>{const tr=el('tr');row.forEach((v,i)=>tr.append(el('td',v,i===0?'field-name':'')));body.append(tr);});table.append(body);box.append(table);return box;
  }
  function example(container, refs, title) {
    container.replaceChildren();container.append(el('h3',title));
    const choices=refs.flatMap(ref=>ref.scope==='classification'?window.HandoffPreview.choices(ref.side):[resolve(ref)]), select=el('select');select.setAttribute('aria-label',title);
    if (!choices.length) choices.push({label:'文件 / 运行时对象，未保存 JSON',missing:true,scope:'此处没有可展示的 JSON 快照。'});
    choices.forEach((c,i)=>{const o=el('option',c.label);o.value=i;select.append(o);});
    const scope=el('p',undefined,'example-scope'), path=el('p',undefined,'example-path'), target=el('div',undefined,'json-tree');let viewer;
    const tools=el('div',undefined,'example-tools');tools.append(button('展开两层',()=>viewer.expand()),button('折叠',()=>viewer.collapse()),button('复制 JSON',()=>{const c=choices[Number(select.value)];if(!c.missing)copy(JSON.stringify(c.value,null,2));}));
    const download=el('a','打开完整冻结文件');download.target='_blank';download.rel='noopener';
    const render=()=>{const c=choices[Number(select.value)];scope.textContent=c.scope;scope.classList.toggle('sampled',Boolean(c.sampled));path.textContent=c.path||'';path.title=path.textContent;download.hidden=!c.file;download.href=c.file||'#';viewer=tree(target,c.value,c.missing);};
    select.addEventListener('change',render);container.append(select,scope,tools,download,path,target);render();
  }
  function renderCode(stage) {
    $('stage-source-content').replaceChildren();
    const render=(s,label)=>{if(!s)return;const d=el('details',undefined,'source-card');d.append(el('summary',`${label} · ${s.name} · ${s.path}${s.available?':'+s.line:'（未找到）'}`));if(s.available){const info=el('div',undefined,'source-info');info.append(button('复制源码位置',()=>copy(`${s.path}:${s.line}`)),el('span',`SHA-256 ${s.sha256}`));const pre=el('pre',undefined,'source-code');s.code.split('\n').forEach((line,i)=>{const row=el('span',undefined,'source-line');row.append(el('span',String(s.line+i),'line-number'),document.createTextNode(line+'\n'));pre.append(row);});d.append(info,pre);}$('stage-source-content').append(d);};
    stage.sources.forEach(key=>render(current.sources?.[key],'当前定义源码'));
    const prompt=current.prompts?.[stage.id];
    if(prompt){const d=el('details',undefined,'source-card');d.append(el('summary',`当前 v${current.version} 提示词 · ${prompt.path}`),el('pre',prompt.text,'current-prompt'));$('stage-source-content').append(d);}
    stage.sources.forEach(key=>render(sources[key],'历史采集源码'));
  }
  let selectedStage, modalTrigger;
  function closeStage(){
    if($('stage-dialog').open)$('stage-dialog').close();
    $('stage-view').hidden=true;
    document.querySelectorAll('.flow-node.selected').forEach(node=>node.classList.remove('selected'));
    if(modalTrigger?.isConnected)modalTrigger.focus({preventScroll:true});
  }
  function stageTab(name) {
    document.querySelectorAll('[data-stage-tab]').forEach(b=>{const active=b.dataset.stageTab===name;b.setAttribute('aria-selected',String(active));$(`stage-${b.dataset.stageTab}-content`).hidden=!active;});
  }
  function renderStageData(stage) {
    const call=actualCall(stage);
    let inputs=stage.inputs,outputs=stage.outputs;
    if(stage.kind==='llm'&&call&&stage.id!=='template')inputs=[{scope:stage.callStage,side:'payload',label:'本次实际发送的完整业务输入'},...stage.inputs];
    if(stage.id==='consolidation'&&call?.key!==actionCall?.key)outputs=[X('response','此历史请求的返回（不替代本轮完整结果）')];
    example($('stage-input-example'),inputs,'输入 JSON 示例');example($('stage-output-example'),outputs,'输出 JSON 示例');
    const pair=stage.id==='classification'?window.HandoffPreview.current():null;
    $('stage-data-note').textContent=pair?.prepared?'当前 v5 待发送输入：由已有 OCR 按当前程序打包，使用 headers 和单行 rows；未调用模型，没有新版分类输出。下方提示词与 Schema 属于当前版本。历史请求与结果可在上方单独切换。':pair?`${window.HandoffPreview.scopeNote(pair.table, pair.request?.value)} 本例按 table_id 和 input_hash 配对，保留${pair.actual?'本次':'历史'}实际输入和结果。程序整理后的结果与模型原始返回分别展示。`:stage.id==='template'?'当前 v6 已将 LabelAgent 并入 TemplateAgent。下方输入、输出、Schema 和调用提示词保持采集时的历史版本；新版实现与提示词见 Python 源码页。刷新网页不重跑模型。':'输入输出定义按当前代码更新；本栏 JSON、Schema 与提示词保留采集时的真实版本。Reader / Answer 沿用冻结请求；OCR 等上游结构采样每个数组最多保留首项。';
    const request=pair?.request?.value||(stage.kind==='llm'?call?.request.value:null);
    const contractRequest=stage.schema?F.samples[stage.schema]:null;
    const currentSchema=stage.id==='answer'?current.answer_schema:['consolidation','action'].includes(stage.id)?current.action_schema:null;
    const schema=request?.schema||currentSchema||(pair?.prepared?null:contractRequest?.schema);
    $('stage-schema-box').hidden=!schema;
    $('stage-schema-box').querySelector('summary').textContent=pair?.prepared?'当前 v5 输出 JSON Schema（未调用模型）':'实际调用使用的输出 JSON Schema（完整）';
    if(schema)tree($('stage-schema-tree'),schema);
    const instructions=request?.instructions||current.prompts?.[stage.id]?.text||(pair?.prepared?null:contractRequest?.instructions);
    $('stage-prompt-box').querySelector('summary').textContent=pair?.prepared?'当前 v5 提示词 instructions（未调用模型）':'实际调用保存的提示词 instructions（完整）';
    if(stage.id==='consolidation'&&!actionCall){
      $('stage-data-note').textContent='当前 ActionAgent 契约与提示词；历史快照未保存该阶段请求或结果。此处没有调用模型。';
      $('stage-schema-box').querySelector('summary').textContent='当前 ActionAgent 输出 JSON Schema（未调用）';
      $('stage-prompt-box').querySelector('summary').textContent='当前 ActionAgent 提示词（未调用）';
    }
    if(stage.id==='consolidation'&&actionCall)$('stage-data-note').textContent='本轮真实 ActionAgent 输入、原始返回与展开后的完整业务结果分别展示。显式记录引用只改变传输表示；完整结果已经过来源和逐条 action 校验。';
    if(!request&&currentSchema){
      $('stage-schema-box').querySelector('summary').textContent='当前输出 Schema · 本快照未记录调用';
      $('stage-prompt-box').querySelector('summary').textContent='当前提示词 · 本快照未记录调用';
    }
    if(data.actual_full_run&&stage.id!=='consolidation')$('stage-data-note').textContent='本轮完整 PDF 重跑的冻结文件。模型输入和返回完整保留；大型上游文档的结构样例另有标注，并可打开完整原文件。';
    if(call&&stage.kind==='llm'&&!call.current)$('stage-data-note').textContent='当前选择的是历史请求 / 未最终接纳的尝试，仅展示它自己的输入、返回和 Prompt；不使用最终结果替代。';
    if(stage.id==='action'&&!call)$('stage-data-note').textContent=integration?`本轮 Action② 状态：${integration.status}。${integration.status==='not_needed'?'没有有效新增建议，没有发送模型请求；Dataset3 保留 Dataset2。':'执行情况以保存的状态及产物为准。'}下方提示词与 Schema 为当前定义。`:'本快照没有 Action② 请求与状态；下方提示词与 Schema 是当前定义，不代表历史执行。';
    $('stage-prompt-box').hidden=!instructions;$('stage-prompt').textContent=instructions||'';
    HandoffDetails.prompt($('stage-prompt-content'),stage,call);
  }
  function showStage(stage) {
    selectedStage=stage;screen('overview');$('stage-view').hidden=false;
    if(!$('stage-dialog').open){modalTrigger=document.activeElement;$('stage-dialog').showModal();}
    $('stage-dialog').scrollTop=0;
    const calls=callsFor(stage),select=$('stage-request-select');select.replaceChildren();
    for(const call of calls){const option=el('option',`${call.id} · ${call.current?'本轮接纳':call.response?'历史返回':'未记录返回'} · ${call.key.slice(0,10)}`);option.value=call.key;select.append(option);}
    if(calls.length)select.value=actualCall(stage).key;
    $('stage-request-controls').hidden=(stage.kind!=='llm'&&!stage.inspectCalls)||!calls.length;
    $('stage-request-status').textContent=calls.length?`共 ${calls.length} 个保存请求；切换后，输入、输出、Prompt 和 Schema 同时切换。`:'';
    const nav=$('stage-flow-nav');nav.replaceChildren();
    const incoming=graphEdges.filter(e=>!e.visual_only&&e.to===stage.id).map(e=>e.from),outgoing=graphEdges.filter(e=>!e.visual_only&&e.from===stage.id).map(e=>e.to);
    for(const [label,ids] of [['上游',incoming],['下游',outgoing]])for(const id of new Set(ids)){nav.append(button(`${label} · ${stageMap.get(id).title}`,()=>navigate('stage/'+id)));}
    $('stage-data-action').textContent=stage.id==='consolidation'&&!actionCall?'查看当前 Schema 与提示词':'查看本轮 JSON';
    $('classification-view-controls').hidden=stage.id!=='classification';
    $('classification-view-select').querySelector('[value="actual"]').hidden=!window.HandoffPreview.hasActual;
    $('classification-view-select').querySelector('[value="current"]').disabled=!window.HandoffPreview.hasPrepared;
    $('classification-view-select').value=window.HandoffPreview.view();
    $('classification-version-message').textContent=window.HandoffPreview.hasActual?'本次 v5 分类已实际调用模型；输入、原始返回和程序整理结果按 table_id 与请求哈希配对。':'此处用于核对旧 LabelAgent / v5 口径；不代表当前 v6 TemplateAgent 输入。预览不调用模型，历史请求及分类结果单独查看。';
    $('stage-preview-content').replaceChildren();
    const hasPreview=(stage.id==='classification'&&window.HandoffPreview.data.cases?.length>0)||HandoffDetails.preview($('stage-preview-content'),stage,actualCall(stage),navigate);
    document.querySelector('[data-stage-tab="preview"]').hidden=!hasPreview;
    stageTab(hasPreview?'preview':'contract');
    $('stage-kind').textContent=kindLabels[stage.kind];$('stage-name').textContent=stage.title;$('stage-purpose').textContent=stage.purpose;
    $('stage-owner').textContent=stage.owner;$('stage-upstream').textContent=stage.upstream;$('stage-downstream').textContent=stage.downstream;
    $('stage-contract-tables').replaceChildren(contract('INPUT · 输入如何定义',stage.input),contract('OUTPUT · 输出如何定义',stage.output));
    $('stage-rules').replaceChildren(...stage.rules.map(r=>el('li',r)));
    $('stage-call-action').hidden=!stage.callStage;
    $('stage-call-action').textContent=stage.callStage?`查看 ${counts[stage.callStage]} 个${{reader:'Reader',action:'Action①',answer:'补全',answer_action:'Action②',template:'Template',vision:'原图复核'}[stage.callStage]}实际请求 →`:'';
    renderStageData(stage);
    if(hasPreview&&stage.id==='classification')window.HandoffPreview.render($('stage-preview-content'),()=>renderStageData(stage));
    renderCode(stage);document.querySelectorAll('.flow-node').forEach(b=>b.classList.toggle('selected',b.dataset.stage===stage.id));
  }
  const ns='http://www.w3.org/2000/svg';
  function svg(tag,attrs){const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;}
  function roundedWire(path,radius=12){
    const tokens=path.match(/[A-Za-z]|-?\d+(?:\.\d+)?/g)||[],points=[];let i=0,x=0,y=0;
    while(i<tokens.length){const command=tokens[i++];if(command==='M'||command==='L'){x=Number(tokens[i++]);y=Number(tokens[i++]);}else if(command==='H')x=Number(tokens[i++]);else if(command==='V')y=Number(tokens[i++]);else return path;if(!points.length||points.at(-1)[0]!==x||points.at(-1)[1]!==y)points.push([x,y]);}
    if(points.length<3)return path;
    let result=`M${points[0][0]} ${points[0][1]}`;
    for(let j=1;j<points.length-1;j++){const a=points[j-1],b=points[j],c=points[j+1],ab=Math.hypot(b[0]-a[0],b[1]-a[1]),bc=Math.hypot(c[0]-b[0],c[1]-b[1]),r=Math.min(radius,ab/2,bc/2);const before=[b[0]+(a[0]-b[0])*r/ab,b[1]+(a[1]-b[1])*r/ab],after=[b[0]+(c[0]-b[0])*r/bc,b[1]+(c[1]-b[1])*r/bc];result+=`L${before[0]} ${before[1]}Q${b[0]} ${b[1]} ${after[0]} ${after[1]}`;}
    return result+`L${points.at(-1)[0]} ${points.at(-1)[1]}`;
  }
  function drawFlow(){
    const canvas=$('flow-canvas'),wires=$('flow-wires');
    wires.setAttribute('viewBox',`0 0 ${graph.width} ${graph.height}`);
    const defs=svg('defs',{}),palette={slate:'#9aa7ad',teal:'#327c69',orange:'#b38856',evidence:'#6c98ba'};
    for(const [kind,color] of Object.entries(palette)){const marker=svg('marker',{id:`arrow-${kind}`,viewBox:'0 0 10 10',refX:8.5,refY:5,markerWidth:5,markerHeight:5,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 1 1 L 9 5 L 1 9 z',fill:color}));defs.append(marker);}wires.append(defs);
    const primary=new Set(['pdf:preprocess','preprocess:detection','preprocess:page_ocr','detection:table_ocr','table_ocr:evidence','page_ocr:evidence','evidence:table_input','sequence_review:table_input','table_input:template','template:seed','seed:source_index','source_index:chunking','seed:reader','chunking:reader','reader:reader','reader:consolidation','seed:consolidation','consolidation:ledger','ledger:question','question:retrieval','retrieval:answer','answer:action','action:question','action:final','final:export','final:review']);
    primary.delete('action:question');primary.add('ledger:action');primary.add('ledger:answer');
    const mainLabels={'table_input:template':'表格＋表题','template:seed':'初始底稿','source_index:chunking':'原文来源 · 按顺序分块','seed:reader':'同一份完整 Dataset1','reader:consolidation':`${data.artifacts.action_status?.value.action_count??'全部'} 条建议`,'consolidation:ledger':'完整 Dataset2','ledger:action':'固定 Dataset2 · Answer 只读 / Action② 综合'};
    const groups=[],cards=[];
    function fitLabel(group,text){const box=text.getBBox(),rect=group.querySelector('rect');for(const [k,v] of Object.entries({x:box.x-7,y:box.y-4,width:box.width+14,height:box.height+8,rx:5}))rect.setAttribute(k,v);}
    for(const [index,edge] of graphEdges.entries()){
      const key=edge.from+':'+edge.to,group=svg('g',{class:`edge-group ${primary.has(key)?'is-primary':'is-supporting'} ${edge.kind==='evidence'?'is-evidence':''}`,'data-from':edge.from,'data-to':edge.to});
      const path=svg('path',{d:roundedWire(edge.path),class:`flow-edge edge-${edge.kind}`,'marker-end':`url(#arrow-${edge.kind})`,'data-from':edge.from,'data-to':edge.to,'data-edge-index':index,'data-source-path':edge.path});
      if(edge.kind==='evidence')path.setAttribute('stroke-dasharray','3 6');const title=svg('title',{});title.textContent=`${stageMap.get(edge.from).title} → ${stageMap.get(edge.to).title}${edge.label?'：'+edge.label:''}`;path.append(title);group.append(path);wires.append(group);
      if(edge.label&&edge.at){const annotation=svg('g',{class:`edge-annotation ${mainLabels[key]?'main-annotation':''}`}),rect=svg('rect',{}),text=svg('text',{x:edge.at[0],y:edge.at[1],class:`edge-label label-${edge.kind}`,'text-anchor':'middle'});text.textContent=mainLabels[key]||edge.label;annotation.append(rect,text);group.append(annotation);fitLabel(annotation,text);}
      groups.push({group,edge,key});
    }
    for(const y of [755,830,905]){const g=svg('g',{class:'edge-group is-primary seed-branch','data-from':'seed','data-to':'reader'});g.append(svg('path',{d:roundedWire(`M280 ${y-5}H405V${y}`,4),class:'flow-edge edge-teal','marker-end':'url(#arrow-teal)'}));wires.append(g);groups.push({group:g,edge:{from:'seed',to:'reader',kind:'teal'}});}
    function trace(id){canvas.classList.toggle('is-tracing',Boolean(id));const neighbors=new Set([id]);if(id)for(const edge of graphEdges){if(edge.from===id)neighbors.add(edge.to);if(edge.to===id)neighbors.add(edge.from);}for(const {group,edge} of groups)group.classList.toggle('in-trace',Boolean(id)&&(edge.from===id||edge.to===id));for(const {button:card,stage} of cards)card.classList.toggle('in-trace',Boolean(id)&&neighbors.has(stage.id));}
    const typeLabels={file:'FILE',python:'PYTHON',ocr:'OCR',llm:'LLM',data:'DATA'};
    for(const stage of stages){
      const b=button('',()=>navigate('stage/'+stage.id),`flow-node kind-${stage.kind}`);const unrecorded=['final','export'].includes(stage.id)&&!F.samples[stage.id]&&!data.artifacts[stage.id==='final'?'final_dataset':'sda_export'];b.classList.toggle('unrecorded',unrecorded);b.dataset.stage=stage.id;
      Object.assign(b.style,{left:`${100*stage.x/graph.width}%`,top:`${100*stage.y/graph.height}%`,width:`${100*stage.w/graph.width}%`,height:`${100*stage.h/graph.height}%`});b.setAttribute('aria-haspopup','dialog');b.setAttribute('aria-controls','stage-dialog');b.setAttribute('aria-label',`${stage.title}：查看流向与实际内容`);
      const head=el('div',undefined,'node-topline'),kind=el('span',typeLabels[stage.kind],'flow-kind'),open=svg('svg',{viewBox:'0 0 16 16',class:'node-open',width:14,height:14,'aria-hidden':'true'});open.append(svg('path',{d:'M4 12L12 4M5 4h7v7',fill:'none',stroke:'currentColor','stroke-width':1.25,'stroke-linecap':'round','stroke-linejoin':'round'}));head.append(kind,open);
      b.append(head,el('strong',stage.title),el('span',stage.subtitle+(unrecorded?' · 未记录':''),'flow-node-subtitle'));canvas.append(b);cards.push({button:b,stage});b.addEventListener('pointerenter',()=>trace(stage.id));b.addEventListener('pointerleave',()=>{if(document.activeElement!==b)trace(null);});b.addEventListener('focus',()=>trace(stage.id));b.addEventListener('blur',()=>trace(null));
    }
    ['c1','c2',`c${counts.reader}`].forEach((name,i)=>{const b=button('',()=>{const call=data.calls.find(c=>c.stage==='reader'&&c.id===name);navigate(call?'call/'+call.key:'stage/reader');},'chunk-card');Object.assign(b.style,{left:`${100*300/graph.width}%`,top:`${100*(755+75*i)/graph.height}%`,width:`${100*210/graph.width}%`,height:`${100*50/graph.height}%`});const top=el('div',undefined,'chunk-topline');top.append(el('span',name.toUpperCase(),'chunk-id'),el('span','＋ Dataset1','chunk-seed'));b.append(top,el('span','本块原文 · 完整初始底稿','chunk-description'));canvas.append(b);});
    function note(x,y,w,title,text){const box=el('aside',undefined,'flow-callout');Object.assign(box.style,{left:`${100*x/graph.width}%`,top:`${100*y/graph.height}%`,width:`${100*w/graph.width}%`});box.append(el('p','READING NOTE','callout-eyebrow'),el('strong',title),el('p',text));canvas.append(box);}
    note(1320,100,300,'位置与归属，分层处理','OCR 和程序保存原文位置；模型判断抗体、序列与实验之间的关系。未知位置保持为空。');const final=data.artifacts.final_dataset?.value,remaining=data.artifacts.remaining_questions?.value;if(final)note(1210,1320,370,'结果与待复核项一同保存',`${final.summary.observations} 条测量 · ${final.summary.variable_chains} 个受支持 VH/VL 域。保留 ${remaining?.length??'—'} 个剩余问题与 ${final.conflicts.length} 条冲突标记。`);
    const messages={main:'主流程突出显示，辅助证据线淡显',evidence:'突出原文来源、定位与回查关系',all:'展示全部数据流与证据连线'};
    for(const button of document.querySelectorAll('[data-graph-mode]'))button.addEventListener('click',()=>{const mode=button.dataset.graphMode;canvas.dataset.viewMode=mode;trace(null);document.querySelectorAll('[data-graph-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));$('graph-view-note').textContent=messages[mode];});
  }
  function routePage(){
    const wasStage=$('stage-dialog').open;
    let hash;try{hash=decodeURIComponent(location.hash.slice(1));}catch{hash='overview';}
    if(!hash.startsWith('stage/'))closeStage();
    if(hash.startsWith('stage/')){showStage(stageMap.get(hash.slice(6))||stages[0]);}
    else if(hash.startsWith('call/')||data.calls.some(c=>c.key===hash)){screen('calls');const key=hash.startsWith('call/')?hash.slice(5):hash;I.selectCall(data.calls.find(c=>c.key===key)||data.calls[0],false);}
    else if(hash==='calls'){screen('calls');I.selectCall(I.currentCall()||data.calls[0],false);}
    else screen('overview');
    if(hash==='reader-dataset-flow'){$('reader-dataset-flow').closest('details').open=true;$('reader-dataset-flow').scrollIntoView({block:'start',behavior:'instant'});}
    else if(!hash.startsWith('stage/')&&!wasStage)window.scrollTo({top:0,behavior:'instant'});
  }
  $('overview-nav').addEventListener('click',()=>navigate('overview'));
  $('calls-nav').addEventListener('click',()=>navigate('calls'));
  $('stage-back').addEventListener('click',()=>navigate('overview'));
  $('stage-dialog').addEventListener('cancel',event=>{event.preventDefault();navigate('overview');});
  $('stage-dialog').addEventListener('click',event=>{if(event.target!==$('stage-dialog'))return;const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)navigate('overview');});
  document.querySelectorAll('[data-reader-chunk]').forEach(b=>b.addEventListener('click',()=>{
    const readers=data.calls.filter(c=>c.stage==='reader');
    const call=b.dataset.readerChunk==='last'?readers[readers.length-1]:readers.find(c=>c.id===b.dataset.readerChunk);
    navigate(call?'call/'+call.key:'stage/reader');
  }));
  $('stage-data-action').addEventListener('click',()=>stageTab('data'));
  $('classification-view-select').addEventListener('change',event=>{
    const tab=document.querySelector('[data-stage-tab][aria-selected="true"]').dataset.stageTab;
    window.HandoffPreview.setView(event.target.value);showStage(selectedStage);stageTab(tab);
  });
  $('stage-call-action').addEventListener('click',()=>{const stage=selectedStage.callStage;$('stage-filter').value=stage;$('call-filter').value='';const call=actualCall(selectedStage);if(call)navigate('call/'+call.key);});
  $('stage-request-select').addEventListener('change',event=>{selectedRequests.set(selectedStage.callStage,event.target.value);const tab=document.querySelector('[data-stage-tab][aria-selected="true"]').dataset.stageTab;showStage(selectedStage);stageTab(tab);});
  document.querySelectorAll('[data-stage-tab]').forEach(b=>b.addEventListener('click',()=>stageTab(b.dataset.stageTab)));
  $('flow-reader-count').textContent=`本轮 ${counts.reader} 个块 · 相同完整底稿独立并行`;
  const qcount=data.artifacts.questions?.value.length;$('flow-question-count').textContent=`本轮 ${qcount??'—'} 个问题 · ${data.calls.filter(c=>c.stage==='answer'&&c.current).length} 个 Answer · Action② ${integration?.status??'未记录'}`;
  if(F.reference_image){$('reference-figure').src=F.reference_image;$('reference-box').hidden=false;}
  if(data.actual_full_run){
    const run=data.run_context||{},settings=data.artifacts.run_settings?.value||{};
    $('run-version-message').textContent=`本轮从原始 PDF 重跑：${F.pages?.length??'未记录'} 页、${F.tables?.length??'未记录'} 张表、${counts.reader} 个 Reader。Template → 完整 Dataset1 → 并行 Reader → Action① / 域复核 → 固定 Dataset2 → 并行 Answer → Action② → Dataset3。Action① ${data.artifacts.action_status?.value.status??'未记录'}；Action② ${integration?.status??'未记录'}，最终结果保留待复核项。`;
    const readerRequest=actualCall({callStage:'reader'})?.request.value;
    $('contract-updated-at').textContent=`${settings.model||readerRequest?.model||'模型未记录'} · Reader ${settings.effort||readerRequest?.effort||'未记录'} / Action① ${settings.action_effort||actionCall?.request.value.effort||'未记录'} · 本页更新只读取保存文件，没有重跑模型。`;
    const guide=document.querySelector('.reader-dataset-example');if(guide)guide.textContent='本轮 C1、C2、Cn 卡片均连接真实请求；完整底稿、Prompt、原始返回与校验结果可直接查看。Reader 独立并行，由 ActionAgent 跨块组织结果。';
    if(data.document_id==='49-Sructure')for(const [label,path] of [['查看本轮效果对照','../49-Sructure/rerun-comparison.html'],['打开更新前的历史网页','../49-Sructure/index.history-before-flow-20261101.html']]){const a=el('a',label);a.href=path;$('snapshot-links').append(a);}
  }else if(data.run_context){
    const run=data.run_context;
    $('run-version-message').textContent=`历史保存的试跑 · ${run.model} / ${run.effort} · 复用 OCR，执行当时版本的共享阶段和全文 Reader；问答预算 ${run.max_questions} 题。所有输入与输出来自本次保存的调用。`;
    $('contract-updated-at').textContent=`运行状态：${run.status} · 实际用时 ${run.wall_seconds??'未记录'} 秒；未解决项仍保留待复核。`;
  }else $('contract-updated-at').textContent=current.captured_at?`当前定义采集于 ${new Date(current.captured_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（北京时间）；本次仅更新网页，未重跑模型或 OCR。`:'';
  $('flow-sample-time').textContent=F.captured_at?`流程节点的补充样例采于 ${new Date(F.captured_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（北京时间）；原 ${data.calls.length} 个请求快照保持不变。`:'';
  document.querySelectorAll('[data-flow-stage]').forEach(b=>b.addEventListener('click',()=>navigate('stage/'+b.dataset.flowStage)));
  HandoffDetails.directory($('stage-directory'),stages,navigate);
  drawFlow();window.addEventListener('hashchange',routePage);routePage();
})();
