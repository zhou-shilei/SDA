import { JobEtaEstimator, formatEtaDuration, formatEtaRange } from "./job_eta.js?v=20261027-agent-strategy-v2";
import { CollectionReviewPanel } from "./collection_review.js?v=20261022-review-controls";
import { setupTableColumns } from "./table_columns.js";
import { deriveDocumentId, patentNumber, patentDisplayName } from "./patent_identity.js";
import { timeMetricMarkup, tokenMetricMarkup, setupMetricTooltips, completedRunUsage } from "./intake_metrics.js";

let processingMetricTooltips = null;

const jobEtaEstimator = new JobEtaEstimator({ storage: {
  getItem: (key) => window.localStorage.getItem(key),
  setItem: (key, value) => window.localStorage.setItem(key, value),
} });

// Resolve files relative to the HTTP server root instead of the domain root.
// This keeps the app working both at localhost:4180/studio/ and behind a
// code-server path proxy such as /proxy/4180/studio/.
const SERVER_ROOT = new URL("../", import.meta.url);
const serverAssetUrl = (path) =>
  new URL(String(path).replace(/^\/+/, ""), SERVER_ROOT).href;

const PATENTS_API = serverAssetUrl("api/patents");
const DATASET_RECORDS_API = serverAssetUrl("api/dataset/records");
const DATASET_FOLDERS_API = serverAssetUrl("api/dataset/folders");
const PATENT_IMPORT_API = serverAssetUrl("api/patents/import");
const PATENT_RETRY_API = serverAssetUrl("api/patents/retry");
const PATENT_PUBLISH_API = serverAssetUrl("api/patents/publish-records");
const PATENT_REPARSE_API = serverAssetUrl("api/patents/reparse-records");
const PATENT_ANALYZE_API = serverAssetUrl("api/patents/analyze");
const PATENT_COMPARE_API = serverAssetUrl("api/patents/compare");
const PATENT_DELETE_API = serverAssetUrl("api/patents/delete");
const PATENT_TEXT_API = serverAssetUrl("api/patents/check-text");
const HUMAN_REVIEWS_API = serverAssetUrl("api/reviews/human");
const PDF_FORM_KINDS = ["text", "scanned", "ocr", "mixed", "unknown"];
const cachedPdfForms = new Map();
const completedUsageReports = new Map();
const CELL_HIGHLIGHT_PADDING = Object.freeze({ top: 6, right: 4, bottom: 4, left: 8 });

const TRANSLATIONS = {
  zh: {
    "sda.unknownMetric": "指标待确认",
    "classification.assay": "实验数据表",
    "classification.sequence": "序列表",
    "classification.both": "序列与实验数据表",
    "classification.other": "其他内容",
    "ocr.extraction.complete": "提取完成",
    "ocr.extraction.failed": "提取失败",
    "sda.tableEvidence": "原表证据（未定位到具体单元格）",
    "sda.fields": "字段证据与缺失",
    "sda.status": "状态",
    "sda.present": "已有证据",
    "sda.missing": "缺少证据",
    "sda.conflict": "证据冲突",
    "sda.unavailable": "来源不可用",
    "sda.not_applicable": "不适用",
    "sda.source": "来源",
    "sda.noEvidence": "缺少明确证据",
    "table.resizeColumn": "拖动调整列宽；双击恢复默认宽度；方向键微调",
    "app.title": "SeqDataAgent",
    "app.loading": "正在加载专利数据…",
    "app.loadFailed": "数据加载失败",
    "panel.patentName": "SDA",
    "panel.annotation": "数据标注",
    "panel.recognition": "识别过程",
    "highlight.title": "高亮显示",
    "map.title": "数据关联图",
    "map.aria": "数据关联图，点击节点定位原文",
    "map.hint": "点击节点定位原文；可折叠分支、缩放，拖动左侧分隔线调整宽度。",
    "map.collapse": "收起数据关联图",
    "map.expand": "展开数据关联图",
    "map.resize": "调整数据关联图宽度",
    "map.root": "当前抗体",
    "map.parent": "母本 name",
    "map.light": "可变轻链",
    "map.heavy": "可变重链",
    "map.fullLight": "全长轻链",
    "map.fullHeavy": "全长重链",
    "map.constantLight": "固定区轻链",
    "map.constantHeavy": "固定区重链",
    "map.parameters": "参数集合",
    "map.assayUnclassified": "实验类型待确认",
    "map.missingParameters": "未找到的参数",
    "map.sourceCount": "{count} 个来源",
    "map.measurementCount": "{count} 项测量",
    "map.source": "映射体来源",
    "map.missing": "未找到",
    "map.unlocated": "未定位",
    "map.collapseAll": "收起分支",
    "map.expandAll": "展开分支",
    "map.zoomOut": "缩小关联图",
    "map.zoomIn": "放大关联图",
    "map.zoomReset": "重置关联图缩放",
    "viewer.pdfBrowse": "未找到当前字段的定位证据，可继续浏览 PDF。",
    "viewer.noRecords": "尚未提取到数据记录，可继续浏览 PDF。",
    "nav.aria": "主功能导航",
    "nav.dataset": "数据集",
    "nav.intake": "数据录入",
    "nav.review": "数据审阅",
    "action.exportDataset": "导出数据集",
    "action.exportPatents": "导出专利列表",
    "action.exportPatent": "导出当前专利",
    "action.accept": "接受",
    "action.edit": "修正",
    "action.flag": "标记问题",
    "action.cancel": "取消",
    "action.save": "保存修正",
    "dataset.eyebrow": "提取结果",
    "dataset.title": "数据集",
    "dataset.search": "搜索名称、序列或数值",
    "dataset.records": "{count} 条记录",
    "dataset.empty": "没有符合条件的记录",
    "dataset.statusFilter": "状态筛选",
    "dataset.maximize": "最大化数据表",
    "dataset.tableAria": "专利提取数据表",
    "datasetPage.title": "数据集",
    "datasetPage.search": "搜索专利、名称、序列或数值",
    "datasetPage.tableAria": "当前靶点数据集",
    "datasetPage.back": "← 数据集",
    "datasetPage.create": "＋ 新建文件夹",
    "datasetPage.folderCount": "{count} 个文件夹",
    "datasetPage.folderHint": "按靶点管理数据集，打开文件夹查看已推送的记录。",
    "datasetPage.folderStats": "{patents} 篇专利 · {records} 条记录",
    "datasetPage.openFolder": "打开 {name} 文件夹",
    "datasetPage.createTitle": "新建靶点文件夹",
    "datasetPage.createHint": "以靶点名称命名，例如 TSLP、IL-4。创建后可将审阅结果推送到此文件夹。",
    "datasetPage.targetName": "靶点名称",
    "datasetPage.namePlaceholder": "例如 IL-4",
    "datasetPage.createSubmit": "创建文件夹",
    "datasetPage.creating": "正在创建…",
    "datasetPage.created": "已创建 {name} 文件夹",
    "datasetPage.emptyFolder": "此文件夹暂无记录。请在数据审阅中选择此靶点，再推送所选记录。",
    "datasetPage.publishTarget": "推送到",
    "datasetPage.publishTargetAria": "目标数据集文件夹",
    "intake.aria": "数据录入",
    "intake.collectionTitle": "专利收集情况",
    "field.pdfForm": "PDF 形式",
    "field.patentText": "本地正文资料",
    "field.patentTextUrl": "专利文本链接",
    "patentText.help": "检查当前专利的本地正文和 PDF 文字资料。",
    "patentText.found": "已有本地正文",
    "patentText.not_found": "未找到",
    "patentText.error": "检查失败",
    "patentText.unknown": "待检查资料",
    "patentText.unchecked": "未检查",
    "patentText.checking": "检查中…",
    "patentText.check": "检查",
    "patentText.retry": "重新检查",
    "patentText.foundHint": "本地已有可读取的正文或 PDF 文字资料。检查时间：{time}",
    "patentText.not_foundHint": "当前本地资料中未找到可读取的正文；可在解析后重新检查。",
    "patentText.errorHint": "本地正文检查失败，请查看任务日志或重新检查。",
    "patentText.unknownHint": "尚未检查当前专利的本地正文资料。",
    "patentText.requestFailed": "无法检查专利文本：{error}",
    "pdfForm.help": "按文字的保存方式自动判断；表格和插图不单独作为分类。识别结果仅供参考。",
    "pdfForm.text": "原生文本型",
    "pdfForm.scanned": "扫描图片型",
    "pdfForm.ocr": "OCR 文本层型",
    "pdfForm.mixed": "混合型",
    "pdfForm.unknown": "待确认",
    "pdfForm.textHint": "文字以文本保存，通常可直接选择、复制；也可以含表格和插图。",
    "pdfForm.scannedHint": "页面主要是扫描图或图片，正文通常需要 OCR 识别。",
    "pdfForm.ocrHint": "检测到页面图片及隐藏文字层，推测为经 OCR 处理的扫描件；文字仍可能有识别错误。",
    "pdfForm.mixedHint": "同一文件或页面中混有原生文字、扫描图片或 OCR 文本层。",
    "pdfForm.unknownHint": "尚无结果，或文件加密、读取失败、页面结构不足以可靠判断。",
    "pdfForm.counts": "逐页检查：原生文本 {text} 页 · 扫描图片 {scanned} 页 · OCR 文本层 {ocr} 页 · 混合 {mixed} 页 · 待确认 {unknown} 页 · 空白 {blank} 页",
    "intake.noneSelected": "未选择专利",
    "intake.selectedCount": "已选择 {count} 篇",
    "intake.selectAll": "选择全部可删除专利",
    "intake.selectPatent": "选择专利 {patent}",
    "intake.deleteSelected": "删除所选",
    "intake.deleting": "正在删除…",
    "intake.builtinProtected": "内置示例不可删除",
    "intake.processingProtected": "解析任务完成后可删除",
    "intake.deleteConfirm": "确认删除所选的 {count} 篇专利？\n\n{names}\n\n其已发布数据和审核记录会同时移除，PDF 与解析结果将移入本地回收目录。",
    "intake.moreSelected": "另有 {count} 篇",
    "intake.deleteCompleted": "已删除 {count} 篇专利，文件已移入本地回收目录。",
    "intake.deleteFailed": "删除失败：{error}",
    "intake.addTitle": "选择专利 PDF",
    "intake.choosePdf": "点击选择或拖入 PDF",
    "intake.startAgent": "导入 PDF 并开始解析",
    "intake.patentCount": "{count} 篇专利",
    "intake.review": "审阅",
    "intake.reviewLoading": "正在加载审阅数据…",
    "intake.reviewFailed": "审阅数据加载失败：{error}",
    "intake.waiting": "等待完成",
    "intake.retry": "重新解析 PDF",
    "intake.datasetPublished": "已推送 {count} 条",
    "intake.datasetNotPublished": "未推送",
    "intake.fileSelected": "{name} · {size}",
    "intake.uploading": "正在上传 PDF…",
    "intake.engine": "解析引擎",
    "intake.engine1": "dataagent1 · 多轮检索",
    "intake.engine2": "dataagent2 · 分块提取",
    "intake.stage.reader": "分块读取全文证据",
    "intake.chunkProgress": "{current}/{total} 块",
    "intake.stage.build": "构建审阅数据",
    "intake.engineUnavailable": "后端尚未加载双引擎功能，请在现有任务结束后重启平台。",
    "intake.compare": "新建对比",
    "intake.compareQueued": "已新建 {engine} 对比任务，原任务及结果保留。",
    "intake.currentStrategy": "当前运行：{engine}",
    "intake.comparisonRun": "同 PDF 策略对比 · 复用 OCR",
    "intake.strategyHelp": "选择新任务的策略，再点击新建对比；保留当前运行和结果。",
    "intake.queued": "已加入 Agent 解析队列。",
    "intake.duplicatePatent": "专利 {patent} 已存在，请在列表中打开已有记录。",
    "intake.retryQueued": "已加入整篇 PDF 重新解析队列，旧结果会保留。",
    "intake.completed": "{patent} 解析完成，正在打开审阅页面。",
    "intake.importFailed": "专利导入失败：{error}",
    "intake.invalidPdf": "请选择有效的 PDF 文件。",
    "intake.fileTooLarge": "PDF 文件不能超过 300 MB。",
    "intake.status.completed": "已完成",
    "intake.status.partial": "处理已结束 · 需确认",
    "intake.status.queued": "等待解析",
    "intake.status.processing": "解析中",
    "intake.status.failed": "解析失败",
    "intake.templateReview": "数据待确认",
    "review.collection": "采集确认",
    "review.collectionCount": "采集确认 · {count} 个问题，{pending} 个待确认",
    "review.collectionLoading": "采集确认 · 正在读取问题数",
    "review.collectionPending": "采集确认 · 问题尚未整理完成，点击查看",
    "review.collectionError": "采集确认 · 问题数读取失败，点击重试",
    "intake.templateValidated": "模板已验证",
    "intake.stage.orientation": "页面方向校正",
    "intake.stage.layout": "表格定位",
    "intake.stage.crop": "表格裁剪",
    "intake.stage.render": "PDF 渲染",
    "intake.stage.agent": "表格识别与抽取",
    "intake.stage.ocr": "正文 / 表格 OCR",
    "intake.stage.assembly": "文档结构组装",
    "intake.stage.export": "结构化文档导出",
    "intake.ocrBranches": "全文：{text} · 表格：{table}",
    "intake.structuredDocument": "结构化文档",
    "intake.stage.extraction": "结构化数据抽取",
    "intake.stage.classify": "LLM 表格分类",
    "intake.stage.template": "Schema 模板与缺项检查",
    "intake.stage.tst": "正文检索与缺项补全",
    "intake.stage.construct": "数据关联与导出",
    "intake.stage.sequence_extract": "Agent 判断与抽取 VH/VL",
    "intake.analyze": "复用 OCR 重新解析",
    "intake.analyzeQueued": "已加入重新解析队列，将复用已有 OCR 结果。",
    "intake.log": "查看日志",
    "intake.logTitle": "解析任务日志",
    "intake.logRefresh": "刷新日志",
    "intake.logLoading": "正在读取日志…",
    "intake.logReady": "已读取当前日志；任务运行时自动刷新。",
    "intake.logEmpty": "尚无日志，任务可能仍在等待执行。",
    "intake.logFailed": "日志读取失败：{error}",
    "intake.stage.completed": "已完成",
    "intake.stage.failed": "失败",
    "intake.stage.queued": "等待解析",
    "intake.stageProgress": "第 {current}/{total} 页",
    "intake.tableProgress": "第 {current}/{total} 个表格",
    "intake.batchProgress": "已完成 {current}/{total} 个批次",
    "intake.etaElapsed": "已用 {duration}",
    "intake.etaRemaining": "预计还需 {duration}",
    "intake.etaLive": "动态估算",
    "intake.etaQueue": "含排队时间 · 前面约 {count} 个任务",
    "intake.etaRecalculating": "当前步骤耗时较长，正在重新估算",
    "intake.etaHelp": "综合当前阶段的实际速度和历史耗时；缺少历史的阶段使用初始预算，LLM 耗时可能波动。",
    "review.publishSelected": "推送所选记录",
    "review.publishSelectedTitle": "用当前所选记录覆盖目标文件夹中该专利的记录",
    "review.publishing": "正在推送…",
    "review.publishCompleted": "{folder} / {patent} 已更新：{previous} 条 → {count} 条。",
    "review.publishFailed": "推送失败：{error}",
    "review.selectAll": "选择当前显示的全部记录",
    "review.noneSelected": "未选择记录",
    "review.selectedCount": "已选择 {count} 条",
    "review.reparseFields": "选择重新解析的字段",
    "review.reparseFieldSummary": "解析字段 · {count}",
    "review.reparseSelected": "重新解析所选字段",
    "review.reparseChooseRecords": "先勾选表格左侧的记录",
    "review.reparseChooseFields": "请至少选择一个解析字段",
    "review.reparseWaiting": "请先完成专利解析",
    "review.reparseBusy": "专利正在处理，请稍候",
    "review.reparseSubmitting": "正在提交…",
    "review.reparseRunning": "正在解析…",
    "review.reparseReady": "将重新解析所选 {count} 条记录的{fields}。",
    "review.reparseUnavailable": "逐字段重解析尚未接入 SDA，可继续审阅与导出",
    "review.reparseQueued": "已提交 {count} 条记录，将重新解析 {fields}。",
    "review.reparseCompleted": "{patent} 的所选记录已重新解析完成。",
    "review.reparseFailed": "重新解析失败：{error}",
    "filter.all": "全部状态",
    "filter.experimentKeyword": "实验 keyword 筛选",
    "filter.experimentLabel": "实验标签筛选",
    "filter.unlabeledExperiment": "未标注",
    "filter.allExperimentKeywords": "全部实验 keyword",
    "experiment.keywordReview": "标签待复核",
    "experiment.reviewReasons": "待复核原因",
    "experiment.factEvidence": "标签的原文依据",
    "experiment.dimension.target": "实验靶点",
    "experiment.dimension.species": "靶蛋白物种",
    "experiment.dimension.antigen_species": "靶蛋白物种",
    "experiment.dimension.cell_host_species": "细胞来源物种",
    "experiment.dimension.method": "实验方法",
    "experiment.dimension.system": "实验体系",
    "experiment.dimension.cell_model": "细胞模型",
    "experiment.dimension.sample": "样本",
    "experiment.dimension.readout": "读数",
    "experiment.dimension.endpoint": "测量指标",
    "experiment.dimension.evidence": "原文证据",
    "experiment.dimension.experiment": "实验关联",
    "experiment.reason.missing_evidence": "缺少明确原文依据",
    "experiment.reason.unknown_label": "原文信息尚未匹配到标准标签",
    "experiment.reason.ambiguous_label": "存在多个候选，尚未消除歧义",
    "experiment.reason.missing_target": "尚未确定当前实验的靶点",
    "experiment.reason.ambiguous_target": "当前实验涉及多个候选靶点",
    "experiment.reason.missing_library": "缺少该靶点的标签词库",
    "experiment.reason.protocol_target_conflict": "实验方法与测量表头的靶点冲突",
    "experiment.reason.method_system_conflict": "实验方法与体系冲突",
    "experiment.reason.retrieval_incomplete": "仍有实验原文需要补充核查",
    "filter.reviewStatus": "复核状态筛选",
    "filter.humanReviewer": "人工审核人筛选",
    "filter.allOptions": "全部",
    "filter.multiple": "可多选",
    "filter.selected": "已选 {count} 项",
    "sort.unit": "按单位排序",
    "sort.unitState": "按单位排序：{state}",
    "sort.none": "默认顺序",
    "sort.ascending": "升序",
    "sort.descending": "降序",
    "status.linked": "已关联",
    "status.review": "待复核",
    "status.conflict": "有冲突",
    "status.missing": "缺失",
    "status.draft": "已修改",
    "status.accepted": "已接受",
    "status.flagged": "有问题",
    "status.browse": "浏览",
    "reviewCheck.agent": "Agent",
    "reviewCheck.anarci": "ANARCI",
    "reviewCheck.esm2": "ESM-2",
    "reviewCheck.record": "记录一致性",
    "reviewCheck.table_text_mismatch": "表格和文字不一致",
    "reviewCheck.table_text_match": "表文一致",
    "reviewCheck.image_corrected": "原图复核已修正",
    "reviewCheck.question_answer": "问题与回答",
    "reviewCheck.question": "问题",
    "reviewCheck.answer": "回答",
    "reviewCheck.applied": "已应用",
    "reviewCheck.answered_without_change": "已回答，无需修改",
    "reviewCheck.needs_review": "待复核",
    "reviewCheck.rejected": "修改未通过校验",
    "reviewCheck.table_gold_standard": "采用表格序列复识别结果；正文差异仅作提示",
    "reviewCheck.table_unresolved": "表格原文尚不能输出有效序列，差异保留待复核",
    "reviewCheck.table_value": "表格取值",
    "reviewCheck.text_value": "文字取值",
    "reviewCheck.passed": "通过",
    "reviewCheck.error": "错误",
    "reviewCheck.manual_review": "待人工审核",
    "reviewCheck.title": "序列复核",
    "reviewCheck.details": "审核详情",
    "reviewCheck.vhAnarci": "VH ANARCI",
    "reviewCheck.vlAnarci": "VL ANARCI",
    "reviewCheck.esm2Scored": "ESM-2 已评分",
    "reviewCheck.sitePerplexity": "最高位点伪困惑度",
    "reviewCheck.siteThreshold": "位点风险阈值",
    "reviewCheck.flaggedSites": "高困惑度位点",
    "reviewCheck.risk": "异常风险",
    "reviewCheck.siteRiskNote": "超过阈值的位点需要回查原图；模型风险提示不等同于确认序列错误。",
    "reviewCheck.esm2Metrics": "ESM-2 序列评分",
    "reviewCheck.meanPll": "平均掩码对数概率",
    "reviewCheck.pseudoPerplexity": "伪困惑度",
    "reviewCheck.minLogProbability": "最低位点对数概率",
    "reviewCheck.bottomDecile": "最低 10% 位点均值",
    "reviewCheck.noThresholds": "已评分，尚未配置通过/失败阈值",
    "reviewCheck.anarciDomains": "ANARCI · IMGT 编号",
    "reviewCheck.chainType": "链型",
    "reviewCheck.domainCoverage": "编号覆盖率",
    "reviewCheck.message": "说明",
    "field.antibody_id": "原文名称",
    "field.vh": "VH",
    "field.vl": "VL",
    "field.ic50": "数值",
    "field.ic50_unit": "单位",
    "field.experiment_group": "实验标签",
    "field.experiment_name": "实验名称",
    "field.experiment_keywords": "实验 keyword",
    "field.link_status": "复核状态",
    "field.humanReviewer": "人工审核",
    "field.evidence": "证据",
    "field.patent": "专利",
    "field.patentName": "专利号",
    "field.pdfFile": "PDF 文件",
    "field.collectedCount": "已收集条数",
    "field.pdfPages": "PDF 页数",
    "field.processingTime": "处理用时",
    "field.tokenUsage": "Token 用量",
    "field.agentStatus": "Agent 状态",
    "field.agentStrategy": "Agent 策略",
    "field.datasetMembership": "数据库记录",
    "field.action": "操作",
    "field.generic": "字段",
    "cell.edited": "已人工编辑",
    "cell.flagged": "已标记问题",
    "cell.editedFlagged": "已人工编辑，仍标记为问题",
    "reviewer.current": "人工审核 · {name}",
    "reviewer.unset": "未设置",
    "reviewer.change": "修改当前人工审核人",
    "reviewer.dialogEyebrow": "操作者身份",
    "reviewer.dialogTitle": "请输入您的真实姓名",
    "reviewer.dialogDescription": "人工修正、接受和标记问题都会记录此姓名。",
    "reviewer.name": "真实姓名",
    "reviewer.namePlaceholder": "例如：周仕磊",
    "reviewer.nameRequired": "请输入至少两个文字的真实姓名",
    "reviewer.confirm": "确认身份",
    "reviewer.none": "未人工审核",
    "reviewer.syncFailed": "审核人记录同步失败：{error}",
    "viewer.eyebrow": "原始证据",
    "viewer.page": "连续 PDF",
    "viewer.pdfPages": "PDF · {count} 页",
    "viewer.context": "上下文",
    "viewer.table": "表格",
    "viewer.titleEvidence": "第 {page} 页 · {table}",
    "viewer.titlePage": "PDF 第 {page} 页",
    "viewer.location": "第 {page} 页 · {table} · {target}",
    "viewer.browseLocation": "第 {page} 页 · 浏览模式",
    "viewer.browseHint": "点击珊瑚色表格框可跳转到关联记录；选择“框选”可添加人工复核区域。",
    "viewer.noneTitle": "尚未选择记录",
    "viewer.noneEvidence": "未选择证据",
    "viewer.altPage": "专利 PDF 第 {page} 页标注画布",
    "viewer.altCrop": "{table}，第 {page} 页裁剪",
    "viewer.imageAlt": "专利证据图",
    "viewer.row": "第 {row} 行",
    "viewer.currentField": "{name}-{field}",
    "viewer.unnamedRecord": "未命名记录",
    "viewer.tableNumber": "表 {number}",
    "viewer.tableTarget": "表格",
    "viewer.noEvidence": "当前字段没有可定位的原文证据",
    "viewer.residueRange": "残基 {start}–{end} / {length}",
    "viewer.sequenceExact": "按 PDF 字符坐标定位序列残基。",
    "viewer.sequenceApproximate": "部分区域按 OCR 行框定位，框内可能包含额外文字；不是逐字符精确边界。",
    "viewer.sequenceUnlocated": "当前序列尚未完整定位；这里只展示同链来源证据，不代表精确残基高亮。",
    "viewer.sequenceMissing": "当前链序列缺失或有冲突；仅展示同链映射证据，不展示另一条链作为替代。",
    "viewer.sequenceChanged": "序列已被人工修改；保留原提取结果的来源，不能视为修改后每个残基均已核实。",
    "viewer.derivedFrom": "由{scope}提取可变区",
    "viewer.mapping": "构建体映射",
    "viewer.scope.variable_heavy": "可变重链",
    "viewer.scope.variable_light": "可变轻链",
    "viewer.scope.full_heavy_chain": "完整重链",
    "viewer.scope.full_light_chain": "完整轻链",
    "viewer.scope.heavy_cdr": "重链 CDR",
    "viewer.scope.light_cdr": "轻链 CDR",
    "viewer.scope.constant_region": "恒定区",
    "viewer.workspaceAria": "PDF 与 Agent 证据",
    "viewer.modeAria": "证据显示模式",
    "viewer.navigationAria": "PDF 翻页",
    "viewer.previous": "上一页",
    "viewer.next": "下一页",
    "viewer.canvasAria": "画布工具",
    "viewer.selectTitle": "选择标注框",
    "viewer.rectangleTitle": "绘制人工复核框",
    "viewer.clearTitle": "清除本页人工框",
    "viewer.zoomOut": "缩小",
    "viewer.zoomIn": "放大",
    "viewer.zoomFit": "适应宽度",
    "viewer.fit": "适应",
    "viewer.evidenceAria": "当前字段的证据列表",
    "viewer.annotationAria": "PDF 标注框",
    "viewer.legendAria": "标注框图例",
    "tool.select": "选择",
    "tool.rectangle": "框选",
    "empty.title": "选择上方任意字段",
    "empty.description": "这里将显示对应的页面和证据框。",
    "legend.table": "表格",
    "legend.cell": "当前单元格",
    "legend.context": "上下文",
    "legend.manual": "人工框选",
    "region.caption": "表格标题",
    "region.nearby": "相邻正文",
    "region.reference": "引用句",
    "region.footnote": "脚注",
    "region.manual": "人工复核区域",
    "region.manualNamed": "人工复核框 {number}",
    "region.new": "新框选",
    "inspector.eyebrow": "识别详情",
    "inspector.title": "字段证据",
    "inspector.empty": "选择数据字段后显示 Agent 识别过程。",
    "inspector.tabsAria": "Agent 识别阶段",
    "tab.detection": "表格检测",
    "tab.ocr": "文字识别",
    "tab.experiment": "实验过程",
    "experiment.summaryTitle": "实验总结",
    "experiment.evidenceTitle": "实验证据",
    "experiment.codex": "Codex 综合",
    "experiment.unknownMethod": "关联实验",
    "experiment.fallbackSummary": "该数据通过{method}获得。Codex 综合了与结果表关联的实验正文和引用句；具体实验条件请结合下方证据复核。",
    "experiment.none": "当前数据没有关联的实验过程正文。",
    "detect.none": "当前字段没有表格定位证据。",
    "detect.location": "表格定位",
    "detect.caption": "表格标题",
    "detect.content": "表格内容",
    "detect.nearby": "相邻正文",
    "detect.page": "页面",
    "detect.logicalTable": "逻辑表格",
    "detect.tableNumber": "表号",
    "detect.confidence": "检测置信度",
    "detect.contextStatus": "上下文状态",
    "detect.noNearby": "未提取到相邻正文",
    "detect.pageValue": "第 {page} 页",
    "detect.precedingParagraph": "前置正文",
    "detect.followingParagraph": "后置正文",
    "detect.nearbyParagraph": "相邻正文",
    "detect.explicitReference": "显式引用句",
    "detect.implicitReference": "隐式引用句",
    "detect.referenceSentence": "引用句",
    "detect.footnote": "脚注",
    "detect.noCaption": "没有识别到明确标题",
    "detect.noContent": "没有可显示的表格 OCR 内容",
    "detect.unconfirmed": "未确认",
    "ocr.original": "原始 OCR",
    "ocr.confidence": "OCR 置信度",
    "ocr.none": "当前记录没有可直接定位的 OCR 单元格",
    "ocr.result": "字段结果",
    "ocr.sequenceResult": "{field} 解析结果",
    "ocr.processing": "处理信息",
    "ocr.backend": "OCR 后端",
    "ocr.classification": "表格分类",
    "ocr.extractionStatus": "字段抽取",
    "ocr.extraction.ready": "结构已确认",
    "ocr.extraction.partial": "部分可抽取",
    "ocr.extraction.needs_review": "待核对结构",
    "ocr.allMeasurements": "本表测量值",
    "ocr.comparisons": "比较比值",
    "ocr.measurementNote": "测量值与原始来源对应展示；实验方法和序列关联另行核实。",
    "ocr.measurementReview": "待核实",
    "ocr.assayCondition": "实验条件",
    "ocr.metric": "指标",
    "ocr.value": "数值",
    "ocr.classificationConfidence": "分类置信度",
    "ocr.orientation": "表格方向",
    "ocr.orientationValue": "实体在{entity}，指标在{measurement}",
    "ocr.classifierModel": "分类 Agent",
    "ocr.templateModel": "文档 Agent",
    "ocr.classificationReason": "分类依据",
    "ocr.classificationIssues": "待确认项",
    "ocr.rulesMode": "规则模式",
    "ocr.proseEvidence": "正文证据（非表格）",
    "ocr.sequenceScope": "序列证据类型",
    "ocr.length": "字符长度",
    "ocr.correction": "人工修正",
    "ocr.reason": "错误类型",
    "ocr.note": "补充说明",
    "ocr.saved": "本地草稿已保存",
    "ocr.noCorrection": "无",
    "classification.assay_ic50": "IC50 实验表",
    "classification.assay_ec50": "EC50 实验表",
    "classification.binding_kd": "KD 结合表",
    "classification.sequence_mapping": "序列映射表",
    "classification.sequence_definition": "序列定义表",
    "classification.construct_variant": "构建体/变体表",
    "classification.formulation": "制剂表（不进入数据集）",
    "classification.other_biological_table": "其他生物学表格",
    "classification.non_target_table": "非目标表格",
    "classification.not_a_table": "非表格内容",
    "classification.ambiguous": "待确认",
    "axis.rows": "行",
    "axis.columns": "列",
    "axis.unknown": "未知方向",
    "speech.play": "字母朗读",
    "speech.stop": "停止",
    "speech.playLabel": "朗读{field}解析结果",
    "speech.stopLabel": "停止朗读{field}解析结果",
    "speech.chunkLabel": "从第 {start} 位开始朗读：{letters}",
    "speech.progress": "正在朗读 {start}–{end} / {total}",
    "speech.unsupported": "当前浏览器不支持语音播放。",
    "speech.empty": "当前序列没有可播放的字母。",
    "speech.failed": "语音播放失败，请检查浏览器语音设置。",
    "dialog.eyebrow": "人工修正",
    "dialog.title": "修改字段",
    "dialog.editTitle": "修改：{field}",
    "dialog.original": "原始识别结果",
    "dialog.corrected": "修正值",
    "dialog.reason": "修改原因（可多选）",
    "dialog.reasonRequired": "请至少选择一个修改原因",
    "dialog.note": "补充说明（可选）",
    "dialog.notePlaceholder": "例如：OCR 将 Q 识别为 O",
    "correctionReason.character": "字符识别错误",
    "correctionReason.sequence": "序列拼接错误",
    "correctionReason.index": "索引错误",
    "correctionReason.numeric": "数值识别错误",
    "correctionReason.unit": "单位识别错误",
    "correctionReason.relation": "字段关联错误",
    "correctionReason.missing": "内容缺失",
    "correctionReason.other": "其他错误",
    "dialog.close": "关闭",
    "toast.loadFailed": "无法读取运行产物。请从项目根目录启动本地服务器。",
    "toast.cellSelected": "已选中当前字段对应的 PDF 单元格。",
    "toast.contextSelected": "已选中标题或上下文证据。",
    "toast.manualExists": "人工复核框已保存在浏览器本地草稿。",
    "toast.tableUnlinked": "该表格尚未关联到最终数据记录。",
    "toast.manualSaved": "人工复核框已保存到浏览器本地草稿。",
    "toast.noManual": "当前页没有人工复核框。",
    "toast.manualCleared": "已清除当前页的人工复核框。",
    "toast.notEditable": "该列不是可编辑业务字段。",
    "toast.correctionSaved": "人工修正已保存到浏览器本地草稿；原始 Agent 结果未被覆盖。",
    "toast.accepted": "字段已接受。",
    "toast.flagged": "字段已加入问题队列。",
    "toast.exported": "已导出 Excel 可直接打开的 UTF-8 CSV。",
  },
  en: {
    "sda.unknownMetric": "Unconfirmed metric",
    "classification.assay": "Assay table",
    "classification.sequence": "Sequence table",
    "classification.both": "Sequence and assay table",
    "classification.other": "Other content",
    "ocr.extraction.complete": "Extraction completed",
    "ocr.extraction.failed": "Extraction failed",
    "sda.tableEvidence": "Table-level source; cell coordinates were not inferred",
    "sda.fields": "Field evidence and missing values",
    "sda.status": "Status",
    "sda.present": "Evidence available",
    "sda.missing": "Missing evidence",
    "sda.conflict": "Conflicting evidence",
    "sda.unavailable": "Source unavailable",
    "sda.not_applicable": "Not applicable",
    "sda.source": "Source",
    "sda.noEvidence": "No explicit evidence",
    "table.resizeColumn": "Drag to resize; double-click to reset; use arrow keys to adjust",
    "highlight.title": "Highlight visibility",
    "map.title": "Data relationship map",
    "map.aria": "Data relationship map; select a node to locate its source",
    "map.hint": "Select a node to locate its source. Collapse branches, zoom, or drag the left divider to resize.",
    "map.collapse": "Collapse relationship map",
    "map.expand": "Expand relationship map",
    "map.resize": "Resize relationship map",
    "map.root": "Current antibody",
    "map.parent": "Parent name",
    "map.light": "Variable light chain",
    "map.heavy": "Variable heavy chain",
    "map.fullLight": "Full light chain",
    "map.fullHeavy": "Full heavy chain",
    "map.constantLight": "Constant light chain",
    "map.constantHeavy": "Constant heavy chain",
    "map.parameters": "Parameters",
    "map.assayUnclassified": "Assay type unconfirmed",
    "map.missingParameters": "Unavailable parameters",
    "map.sourceCount": "{count} source(s)",
    "map.measurementCount": "{count} measurement(s)",
    "map.source": "Mapping sources",
    "map.missing": "Not found",
    "map.unlocated": "Not located",
    "map.collapseAll": "Collapse branches",
    "map.expandAll": "Expand branches",
    "map.zoomOut": "Zoom map out",
    "map.zoomIn": "Zoom map in",
    "map.zoomReset": "Reset map zoom",
    "viewer.pdfBrowse": "No location evidence for this field. You can continue browsing the PDF.",
    "viewer.noRecords": "No data records have been extracted. You can continue browsing the PDF.",
    "app.title": "SeqDataAgent",
    "app.loading": "Loading patent data…",
    "app.loadFailed": "Data load failed",
    "panel.patentName": "SDA",
    "panel.annotation": "Data Annotation",
    "panel.recognition": "Recognition Process",
    "nav.aria": "Primary navigation",
    "nav.dataset": "Dataset",
    "nav.intake": "Data Entry",
    "nav.review": "Data Review",
    "action.exportDataset": "Export dataset",
    "action.exportPatents": "Export patent list",
    "action.exportPatent": "Export current patent",
    "action.accept": "Accept",
    "action.edit": "Correct",
    "action.flag": "Flag issue",
    "action.cancel": "Cancel",
    "action.save": "Save correction",
    "dataset.eyebrow": "Extraction results",
    "dataset.title": "Dataset",
    "dataset.search": "Search name, sequence, or value",
    "dataset.records": "{count} records",
    "dataset.empty": "No matching records",
    "dataset.statusFilter": "Status filter",
    "dataset.maximize": "Maximize data table",
    "dataset.tableAria": "Patent extraction dataset",
    "datasetPage.title": "Dataset",
    "datasetPage.search": "Search patent, name, sequence, or value",
    "datasetPage.tableAria": "Current target dataset",
    "datasetPage.back": "← Datasets",
    "datasetPage.create": "＋ New folder",
    "datasetPage.folderCount": "{count} folders",
    "datasetPage.folderHint": "Organize datasets by target. Open a folder to view published records.",
    "datasetPage.folderStats": "{patents} patents · {records} records",
    "datasetPage.openFolder": "Open {name} folder",
    "datasetPage.createTitle": "New target folder",
    "datasetPage.createHint": "Name the folder after a target, such as TSLP or IL-4. Publish reviewed records to this folder.",
    "datasetPage.targetName": "Target name",
    "datasetPage.namePlaceholder": "e.g. IL-4",
    "datasetPage.createSubmit": "Create folder",
    "datasetPage.creating": "Creating…",
    "datasetPage.created": "Created {name} folder",
    "datasetPage.emptyFolder": "No records in this folder yet. Select this target in Data review and publish selected records.",
    "datasetPage.publishTarget": "Publish to",
    "datasetPage.publishTargetAria": "Destination dataset folder",
    "intake.aria": "Data entry",
    "intake.collectionTitle": "Patent Collection",
    "field.pdfForm": "PDF form",
    "field.patentText": "Local text sources",
    "field.patentTextUrl": "Patent text URL",
    "patentText.help": "Checks this patent's local text and PDF text sources.",
    "patentText.found": "Local text available",
    "patentText.not_found": "Not found",
    "patentText.error": "Check failed",
    "patentText.unknown": "Sources not checked",
    "patentText.unchecked": "Not checked",
    "patentText.checking": "Checking…",
    "patentText.check": "Check",
    "patentText.retry": "Check again",
    "patentText.foundHint": "Readable local text or PDF text is available. Checked: {time}",
    "patentText.not_foundHint": "No readable text found in the current local sources. Check again after parsing.",
    "patentText.errorHint": "The local text check failed. View the task log or try again.",
    "patentText.unknownHint": "This patent's local text sources have not been checked.",
    "patentText.requestFailed": "Unable to check patent text: {error}",
    "pdfForm.help": "Automatically inferred from how text is stored; tables and illustrations are not separate forms. Results are indicative.",
    "pdfForm.text": "Native text",
    "pdfForm.scanned": "Scanned image",
    "pdfForm.ocr": "OCR text layer",
    "pdfForm.mixed": "Mixed",
    "pdfForm.unknown": "Unconfirmed",
    "pdfForm.textHint": "Text is stored as text and can usually be selected and copied; tables and illustrations may also be present.",
    "pdfForm.scannedHint": "Pages are primarily scans or images; body text usually requires OCR.",
    "pdfForm.ocrHint": "Page images with hidden text suggest an OCR-processed scan; recognition errors may remain.",
    "pdfForm.mixedHint": "Native text, scanned images or OCR text layers coexist within the document or a page.",
    "pdfForm.unknownHint": "No result yet, or encryption, a read error or ambiguous page structure prevents classification.",
    "pdfForm.counts": "Page inspection: {text} native text · {scanned} scanned · {ocr} OCR · {mixed} mixed · {unknown} unconfirmed · {blank} blank",
    "intake.noneSelected": "No patents selected",
    "intake.selectedCount": "{count} patents selected",
    "intake.selectAll": "Select all deletable patents",
    "intake.selectPatent": "Select patent {patent}",
    "intake.deleteSelected": "Delete selected",
    "intake.deleting": "Deleting…",
    "intake.builtinProtected": "The built-in example cannot be deleted",
    "intake.processingProtected": "Available after processing finishes",
    "intake.deleteConfirm": "Delete the {count} selected patents?\n\n{names}\n\nTheir published data and review history will also be removed. PDFs and processing results will move to local trash.",
    "intake.moreSelected": "{count} more patents",
    "intake.deleteCompleted": "Deleted {count} patents. Files moved to local trash.",
    "intake.deleteFailed": "Deletion failed: {error}",
    "intake.addTitle": "Choose patent PDF",
    "intake.choosePdf": "Click or drop a PDF",
    "intake.startAgent": "Import PDF and start parsing",
    "intake.patentCount": "{count} patents",
    "intake.review": "Review",
    "intake.reviewLoading": "Loading review data…",
    "intake.reviewFailed": "Unable to load review data: {error}",
    "intake.waiting": "Waiting",
    "intake.retry": "Reparse PDF",
    "intake.datasetPublished": "{count} published",
    "intake.datasetNotPublished": "Not published",
    "intake.fileSelected": "{name} · {size}",
    "intake.uploading": "Uploading PDF…",
    "intake.engine": "Extraction engine",
    "intake.engine1": "dataagent1 · Multi-turn retrieval",
    "intake.engine2": "dataagent2 · Chunk extraction",
    "intake.stage.reader": "Reading document evidence",
    "intake.chunkProgress": "{current}/{total} chunks",
    "intake.stage.build": "Building review data",
    "intake.engineUnavailable": "The backend needs to restart after active tasks finish to load engine selection.",
    "intake.compare": "New comparison",
    "intake.compareQueued": "Created a {engine} comparison. The original run and results are preserved.",
    "intake.currentStrategy": "Current run: {engine}",
    "intake.comparisonRun": "Same PDF comparison · OCR reused",
    "intake.strategyHelp": "Select a strategy and create a comparison; the current run and results are preserved.",
    "intake.queued": "Added to the Agent processing queue.",
    "intake.duplicatePatent": "Patent {patent} already exists. Open its existing entry in the list.",
    "intake.retryQueued": "Queued full PDF parsing. Previous results will be retained.",
    "intake.completed": "{patent} is complete. Opening its review workspace.",
    "intake.importFailed": "Patent import failed: {error}",
    "intake.invalidPdf": "Choose a valid PDF file.",
    "intake.fileTooLarge": "The PDF must be no larger than 300 MB.",
    "intake.status.completed": "Completed",
    "intake.status.partial": "Processing ended · review needed",
    "intake.status.queued": "Queued",
    "intake.status.processing": "Processing",
    "intake.status.failed": "Failed",
    "intake.templateReview": "Data review required",
    "review.collection": "Collection review",
    "review.collectionCount": "Collection review · {count} questions, {pending} pending",
    "review.collectionLoading": "Collection review · Loading question count",
    "review.collectionPending": "Collection review · Questions are not ready yet. Click to view.",
    "review.collectionError": "Collection review · Could not load question count. Click to retry.",
    "intake.templateValidated": "Template validated",
    "intake.stage.orientation": "Page orientation",
    "intake.stage.layout": "Table localization",
    "intake.stage.crop": "Table cropping",
    "intake.stage.render": "PDF rendering",
    "intake.stage.agent": "Table recognition and extraction",
    "intake.stage.ocr": "Text / table OCR",
    "intake.stage.assembly": "Document assembly",
    "intake.stage.export": "Structured document export",
    "intake.ocrBranches": "Text: {text} · Tables: {table}",
    "intake.structuredDocument": "Structured document",
    "intake.stage.extraction": "Structured data extraction",
    "intake.stage.classify": "LLM table classification",
    "intake.stage.template": "Schema and missing-field checks",
    "intake.stage.tst": "Text retrieval and completion",
    "intake.stage.construct": "Data linking and export",
    "intake.stage.sequence_extract": "Agent VH/VL extraction",
    "intake.analyze": "Reparse using existing OCR",
    "intake.analyzeQueued": "Queued parsing using the existing OCR results.",
    "intake.log": "View log",
    "intake.logTitle": "Parsing task log",
    "intake.logRefresh": "Refresh log",
    "intake.logLoading": "Loading log…",
    "intake.logReady": "Current log loaded. Updates refresh automatically while the task runs.",
    "intake.logEmpty": "No log yet. The task may still be queued.",
    "intake.logFailed": "Unable to load log: {error}",
    "intake.stage.completed": "Completed",
    "intake.stage.failed": "Failed",
    "intake.stage.queued": "Queued",
    "intake.stageProgress": "Page {current}/{total}",
    "intake.tableProgress": "Table {current}/{total}",
    "intake.batchProgress": "Completed {current}/{total} batches",
    "intake.etaElapsed": "Elapsed {duration}",
    "intake.etaRemaining": "About {duration} remaining",
    "intake.etaLive": "Updating estimate",
    "intake.etaQueue": "Includes queue · about {count} jobs ahead",
    "intake.etaRecalculating": "This step is taking longer; recalculating",
    "intake.etaHelp": "Uses measured stage speed and historical durations. Stages without history use initial budgets; LLM timing may vary.",
    "review.publishSelected": "Publish selected records",
    "review.publishSelectedTitle": "Replace this patent's records in the destination folder with the current selection",
    "review.publishing": "Pushing…",
    "review.publishCompleted": "{folder} / {patent} updated: {previous} → {count} records.",
    "review.publishFailed": "Push failed: {error}",
    "review.selectAll": "Select all visible records",
    "review.noneSelected": "No records selected",
    "review.selectedCount": "{count} selected",
    "review.reparseFields": "Choose fields to reparse",
    "review.reparseFieldSummary": "Fields · {count}",
    "review.reparseSelected": "Reparse selected fields",
    "review.reparseChooseRecords": "Select records using the leftmost checkboxes",
    "review.reparseChooseFields": "Choose at least one field",
    "review.reparseWaiting": "Complete patent parsing first",
    "review.reparseBusy": "Patent processing in progress",
    "review.reparseSubmitting": "Submitting…",
    "review.reparseRunning": "Parsing…",
    "review.reparseReady": "Reparse {fields} for {count} selected records.",
    "review.reparseUnavailable": "Field reparse is not connected to SDA; review and export are available",
    "review.reparseQueued": "Queued {count} records to reparse {fields}.",
    "review.reparseCompleted": "Selected records in {patent} were reparsed.",
    "review.reparseFailed": "Reparse failed: {error}",
    "filter.all": "All statuses",
    "filter.experimentKeyword": "Experiment keyword filter",
    "filter.experimentLabel": "Experiment label filter",
    "filter.unlabeledExperiment": "Unlabeled",
    "filter.allExperimentKeywords": "All experiment keywords",
    "experiment.keywordReview": "Labels need review",
    "experiment.reviewReasons": "Reasons for review",
    "experiment.factEvidence": "Source evidence for labels",
    "experiment.dimension.target": "Assayed antigen",
    "experiment.dimension.species": "Antigen species",
    "experiment.dimension.antigen_species": "Antigen species",
    "experiment.dimension.cell_host_species": "Cell host species",
    "experiment.dimension.method": "Assay method",
    "experiment.dimension.system": "Assay system",
    "experiment.dimension.cell_model": "Cell model",
    "experiment.dimension.sample": "Sample",
    "experiment.dimension.readout": "Readout",
    "experiment.dimension.endpoint": "Endpoint",
    "experiment.dimension.evidence": "Source evidence",
    "experiment.dimension.experiment": "Experiment binding",
    "experiment.reason.missing_evidence": "Explicit source evidence is missing",
    "experiment.reason.unknown_label": "Source wording has no matching standard label",
    "experiment.reason.ambiguous_label": "Multiple candidates remain ambiguous",
    "experiment.reason.missing_target": "The assayed antigen is unresolved",
    "experiment.reason.ambiguous_target": "Several candidate antigens remain",
    "experiment.reason.missing_library": "The antigen keyword library is missing",
    "experiment.reason.protocol_target_conflict": "Protocol and measurement targets conflict",
    "experiment.reason.method_system_conflict": "Method and assay system conflict",
    "experiment.reason.retrieval_incomplete": "Additional experiment evidence needs review",
    "filter.reviewStatus": "Review status filter",
    "filter.humanReviewer": "Human reviewer filter",
    "filter.allOptions": "All",
    "filter.multiple": "Select multiple",
    "filter.selected": "{count} selected",
    "sort.unit": "Sort by unit",
    "sort.unitState": "Sort by unit: {state}",
    "sort.none": "Default order",
    "sort.ascending": "Ascending",
    "sort.descending": "Descending",
    "status.linked": "Linked",
    "status.review": "Needs review",
    "status.conflict": "Conflict",
    "status.missing": "Missing",
    "status.draft": "Corrected",
    "status.accepted": "Accepted",
    "status.flagged": "Flagged",
    "status.browse": "Browse",
    "reviewCheck.agent": "Agent",
    "reviewCheck.anarci": "ANARCI",
    "reviewCheck.esm2": "ESM-2",
    "reviewCheck.record": "Record evidence",
    "reviewCheck.table_text_mismatch": "Table/text mismatch",
    "reviewCheck.table_text_match": "Table/text consistent",
    "reviewCheck.image_corrected": "Corrected from original image",
    "reviewCheck.question_answer": "Questions and answers",
    "reviewCheck.question": "Question",
    "reviewCheck.answer": "Answer",
    "reviewCheck.applied": "Applied",
    "reviewCheck.answered_without_change": "Answered; no change needed",
    "reviewCheck.needs_review": "Needs review",
    "reviewCheck.rejected": "Change rejected by validation",
    "reviewCheck.table_gold_standard": "Using the table sequence transcription; page-text differences are advisory",
    "reviewCheck.table_unresolved": "The table source cannot yet yield a valid sequence; differences retained for review",
    "reviewCheck.table_value": "Table value",
    "reviewCheck.text_value": "Text value",
    "reviewCheck.passed": "Passed",
    "reviewCheck.error": "Error",
    "reviewCheck.manual_review": "Manual review",
    "reviewCheck.title": "Sequence review",
    "reviewCheck.details": "Review details",
    "reviewCheck.vhAnarci": "VH ANARCI",
    "reviewCheck.vlAnarci": "VL ANARCI",
    "reviewCheck.esm2Scored": "ESM-2 scored",
    "reviewCheck.sitePerplexity": "Maximum site pseudo-perplexity",
    "reviewCheck.siteThreshold": "Site risk threshold",
    "reviewCheck.flaggedSites": "High-perplexity positions",
    "reviewCheck.risk": "Risk flag",
    "reviewCheck.siteRiskNote": "Check flagged positions against the original source; a model risk flag does not confirm a sequence error.",
    "reviewCheck.esm2Metrics": "ESM-2 sequence scores",
    "reviewCheck.meanPll": "Mean masked log probability",
    "reviewCheck.pseudoPerplexity": "Pseudo-perplexity",
    "reviewCheck.minLogProbability": "Lowest position log probability",
    "reviewCheck.bottomDecile": "Bottom 10% mean",
    "reviewCheck.noThresholds": "Scored; pass/fail thresholds are not configured",
    "reviewCheck.anarciDomains": "ANARCI · IMGT numbering",
    "reviewCheck.chainType": "Chain type",
    "reviewCheck.domainCoverage": "Numbering coverage",
    "reviewCheck.message": "Notes",
    "field.antibody_id": "Source name",
    "field.vh": "VH",
    "field.vl": "VL",
    "field.ic50": "Value",
    "field.ic50_unit": "Unit",
    "field.experiment_group": "Experiment label",
    "field.experiment_name": "Experiment name",
    "field.experiment_keywords": "Experiment keywords",
    "field.link_status": "Review status",
    "field.humanReviewer": "Human review",
    "field.evidence": "Evidence",
    "field.patent": "Patent",
    "field.patentName": "Patent number",
    "field.pdfFile": "PDF file",
    "field.collectedCount": "Collected records",
    "field.pdfPages": "PDF pages",
    "field.processingTime": "Processing time",
    "field.tokenUsage": "Token usage",
    "field.agentStatus": "Agent status",
    "field.agentStrategy": "Agent strategy",
    "field.datasetMembership": "Database records",
    "field.action": "Action",
    "field.generic": "Field",
    "cell.edited": "Manually edited",
    "cell.flagged": "Flagged for review",
    "cell.editedFlagged": "Manually edited and still flagged",
    "reviewer.current": "Reviewer · {name}",
    "reviewer.unset": "Not set",
    "reviewer.change": "Change the current human reviewer",
    "reviewer.dialogEyebrow": "Operator identity",
    "reviewer.dialogTitle": "Enter your real name",
    "reviewer.dialogDescription": "Corrections, acceptances, and issue flags will be attributed to this name.",
    "reviewer.name": "Real name",
    "reviewer.namePlaceholder": "For example: Zhou Shilei",
    "reviewer.nameRequired": "Enter a real name containing at least two letters",
    "reviewer.confirm": "Confirm identity",
    "reviewer.none": "Not manually reviewed",
    "reviewer.syncFailed": "Could not sync the reviewer record: {error}",
    "viewer.eyebrow": "Source evidence",
    "viewer.page": "Continuous PDF",
    "viewer.pdfPages": "PDF · {count} pages",
    "viewer.context": "Context",
    "viewer.table": "Table",
    "viewer.titleEvidence": "Page {page} · {table}",
    "viewer.titlePage": "PDF page {page}",
    "viewer.location": "Page {page} · {table} · {target}",
    "viewer.browseLocation": "Page {page} · Browse mode",
    "viewer.browseHint": "Click a coral table box to jump to its record; choose Rectangle to add a review region.",
    "viewer.noneTitle": "No record selected",
    "viewer.noneEvidence": "No evidence selected",
    "viewer.altPage": "Annotated patent PDF page {page}",
    "viewer.altCrop": "{table}, page {page} crop",
    "viewer.imageAlt": "Patent evidence image",
    "viewer.row": "Row {row}",
    "viewer.currentField": "{name}-{field}",
    "viewer.unnamedRecord": "Unnamed record",
    "viewer.tableNumber": "Table {number}",
    "viewer.tableTarget": "Table",
    "viewer.noEvidence": "No locatable source evidence for this field",
    "viewer.residueRange": "Residues {start}–{end} / {length}",
    "viewer.sequenceExact": "Sequence residues located using PDF character coordinates.",
    "viewer.sequenceApproximate": "Some regions use OCR row boxes and may include extra text; these are not exact character boundaries.",
    "viewer.sequenceUnlocated": "The sequence is not fully located. Same-chain source evidence is shown, not exact residue highlights.",
    "viewer.sequenceMissing": "This chain is missing or conflicting. Only same-chain mapping evidence is shown; the other chain is not substituted.",
    "viewer.sequenceChanged": "This sequence was manually edited. Source locations refer to the original extraction, not verification of every edited residue.",
    "viewer.derivedFrom": "Variable region extracted from {scope}",
    "viewer.mapping": "Construct mapping",
    "viewer.scope.variable_heavy": "Variable heavy",
    "viewer.scope.variable_light": "Variable light",
    "viewer.scope.full_heavy_chain": "Full heavy chain",
    "viewer.scope.full_light_chain": "Full light chain",
    "viewer.scope.heavy_cdr": "Heavy-chain CDR",
    "viewer.scope.light_cdr": "Light-chain CDR",
    "viewer.scope.constant_region": "Constant region",
    "viewer.workspaceAria": "PDF and Agent evidence",
    "viewer.modeAria": "Evidence view mode",
    "viewer.navigationAria": "PDF pagination",
    "viewer.previous": "Previous page",
    "viewer.next": "Next page",
    "viewer.canvasAria": "Canvas tools",
    "viewer.selectTitle": "Select annotation box",
    "viewer.rectangleTitle": "Draw manual review box",
    "viewer.clearTitle": "Clear manual boxes on this page",
    "viewer.zoomOut": "Zoom out",
    "viewer.zoomIn": "Zoom in",
    "viewer.zoomFit": "Fit to width",
    "viewer.fit": "Fit",
    "viewer.evidenceAria": "Evidence for current field",
    "viewer.annotationAria": "PDF annotations",
    "viewer.legendAria": "Annotation legend",
    "tool.select": "Select",
    "tool.rectangle": "Rectangle",
    "empty.title": "Select any field above",
    "empty.description": "The matching page and evidence boxes will appear here.",
    "legend.table": "Table",
    "legend.cell": "Current cell",
    "legend.context": "Context",
    "legend.manual": "Manual box",
    "region.caption": "Table caption",
    "region.nearby": "Nearby text",
    "region.reference": "Reference",
    "region.footnote": "Footnote",
    "region.manual": "Manual review region",
    "region.manualNamed": "Manual review box {number}",
    "region.new": "New box",
    "inspector.eyebrow": "Recognition details",
    "inspector.title": "Field evidence",
    "inspector.empty": "Select a data field to view the Agent recognition details.",
    "inspector.tabsAria": "Agent recognition stage",
    "tab.detection": "Table detection",
    "tab.ocr": "OCR recognition",
    "tab.experiment": "Experiment",
    "experiment.summaryTitle": "Experiment summary",
    "experiment.evidenceTitle": "Experiment evidence",
    "experiment.codex": "Codex synthesis",
    "experiment.unknownMethod": "Linked experiment",
    "experiment.fallbackSummary": "This data was collected using {method}. Codex synthesized the experimental text and references linked to the result table; verify detailed conditions against the evidence below.",
    "experiment.none": "No experimental-process text is linked to this record.",
    "detect.none": "No table-location evidence for this field.",
    "detect.location": "Table location",
    "detect.caption": "Table caption",
    "detect.content": "Table content",
    "detect.nearby": "Nearby text",
    "detect.page": "Page",
    "detect.logicalTable": "Logical table",
    "detect.tableNumber": "Table number",
    "detect.confidence": "Detection confidence",
    "detect.contextStatus": "Context status",
    "detect.noNearby": "No nearby text extracted",
    "detect.pageValue": "Page {page}",
    "detect.precedingParagraph": "Preceding text",
    "detect.followingParagraph": "Following text",
    "detect.nearbyParagraph": "Nearby text",
    "detect.explicitReference": "Explicit reference",
    "detect.implicitReference": "Implicit reference",
    "detect.referenceSentence": "Reference sentence",
    "detect.footnote": "Footnote",
    "detect.noCaption": "No explicit caption detected",
    "detect.noContent": "No table OCR content available",
    "detect.unconfirmed": "Unconfirmed",
    "ocr.original": "Raw OCR",
    "ocr.confidence": "OCR confidence",
    "ocr.none": "No directly locatable OCR cell for this record",
    "ocr.result": "Field result",
    "ocr.sequenceResult": "{field} parsed result",
    "ocr.processing": "Processing details",
    "ocr.backend": "OCR backend",
    "ocr.classification": "Table classification",
    "ocr.extractionStatus": "Field extraction",
    "ocr.extraction.ready": "Structure confirmed",
    "ocr.extraction.partial": "Partially extractable",
    "ocr.extraction.needs_review": "Structure needs review",
    "ocr.allMeasurements": "Table measurements",
    "ocr.comparisons": "Comparison ratios",
    "ocr.measurementNote": "Measurements retain their original sources; protocols and sequence links are reviewed separately.",
    "ocr.measurementReview": "Needs review",
    "ocr.assayCondition": "Assay condition",
    "ocr.metric": "Metric",
    "ocr.value": "Value",
    "ocr.classificationConfidence": "Classification confidence",
    "ocr.orientation": "Table orientation",
    "ocr.orientationValue": "Entities in {entity}; metrics in {measurement}",
    "ocr.classifierModel": "Classification agent",
    "ocr.templateModel": "Document agent",
    "ocr.classificationReason": "Classification evidence",
    "ocr.classificationIssues": "Needs review",
    "ocr.rulesMode": "Rule-based",
    "ocr.proseEvidence": "Prose evidence (not a table)",
    "ocr.sequenceScope": "Sequence evidence type",
    "ocr.length": "Character count",
    "ocr.correction": "Manual correction",
    "ocr.reason": "Error type",
    "ocr.note": "Additional note",
    "ocr.saved": "Local draft saved",
    "ocr.noCorrection": "None",
    "classification.assay_ic50": "IC50 assay table",
    "classification.assay_ec50": "EC50 assay table",
    "classification.binding_kd": "KD binding table",
    "classification.sequence_mapping": "Sequence mapping table",
    "classification.sequence_definition": "Sequence definition table",
    "classification.construct_variant": "Construct/variant table",
    "classification.formulation": "Formulation table (excluded)",
    "classification.other_biological_table": "Other biological table",
    "classification.non_target_table": "Non-target table",
    "classification.not_a_table": "Not a table",
    "classification.ambiguous": "Needs review",
    "axis.rows": "rows",
    "axis.columns": "columns",
    "axis.unknown": "unknown",
    "speech.play": "Read letters",
    "speech.stop": "Stop",
    "speech.playLabel": "Read the {field} parsed result",
    "speech.stopLabel": "Stop reading the {field} parsed result",
    "speech.chunkLabel": "Start at position {start}: {letters}",
    "speech.progress": "Reading {start}–{end} / {total}",
    "speech.unsupported": "Speech playback is not supported by this browser.",
    "speech.empty": "The current sequence has no letters to read.",
    "speech.failed": "Speech playback failed. Check the browser speech settings.",
    "dialog.eyebrow": "Manual correction",
    "dialog.title": "Edit field",
    "dialog.editTitle": "Edit: {field}",
    "dialog.original": "Original recognition",
    "dialog.corrected": "Corrected value",
    "dialog.reason": "Reasons for change (select all that apply)",
    "dialog.reasonRequired": "Select at least one reason",
    "dialog.note": "Additional note (optional)",
    "dialog.notePlaceholder": "Example: OCR read Q as O",
    "correctionReason.character": "Character recognition error",
    "correctionReason.sequence": "Sequence assembly error",
    "correctionReason.index": "Indexing error",
    "correctionReason.numeric": "Numeric recognition error",
    "correctionReason.unit": "Unit recognition error",
    "correctionReason.relation": "Field association error",
    "correctionReason.missing": "Missing content",
    "correctionReason.other": "Other error",
    "dialog.close": "Close",
    "toast.loadFailed": "Could not read the run artifacts. Start the local server from the project root.",
    "toast.cellSelected": "Selected the PDF cell for the current field.",
    "toast.contextSelected": "Selected caption or context evidence.",
    "toast.manualExists": "The manual review box is saved in the local browser draft.",
    "toast.tableUnlinked": "This table is not linked to a final data record yet.",
    "toast.manualSaved": "Manual review box saved to the local browser draft.",
    "toast.noManual": "There are no manual review boxes on this page.",
    "toast.manualCleared": "Cleared manual review boxes on this page.",
    "toast.notEditable": "This column is not an editable data field.",
    "toast.correctionSaved": "Correction saved as a local browser draft; the original Agent result is unchanged.",
    "toast.accepted": "Field accepted.",
    "toast.flagged": "Field added to the issue queue.",
    "toast.exported": "Exported a UTF-8 CSV that opens directly in Excel.",
  },
};

const FIELD_LABEL_KEYS = {
  antibody_id: "field.antibody_id",
  vh: "field.vh",
  vl: "field.vl",
  ic50: "field.ic50",
  ic50_unit: "field.ic50_unit",
  experiment_group: "field.experiment_group",
  assay_keywords: "field.experiment_keywords",
  link_status: "field.link_status",
};

const CORRECTION_REASON_KEYS = {
  ocr_character: "correctionReason.character",
  sequence_assembly: "correctionReason.sequence",
  index: "correctionReason.index",
  numeric: "correctionReason.numeric",
  unit: "correctionReason.unit",
  relation: "correctionReason.relation",
  missing: "correctionReason.missing",
  other: "correctionReason.other",
};

const BUILTIN_CODEX_EXPERIMENT_SUMMARIES = {};

const DEFAULT_TAB_BY_FIELD = {
  antibody_id: "detection",
  vh: "ocr",
  vl: "ocr",
  ic50: "ocr",
  ic50_unit: "ocr",
  experiment_group: "experiment",
  assay_keywords: "experiment",
  link_status: "detection",
};

const startupParams = new URLSearchParams(location.search);
const startupDocumentId = startupParams.get("document_id") || null;
const startupPage = startupParams.get("view") || (startupDocumentId ? "review" : loadText("sda.studio.appPage", "review"));

const state = {
  appPage: ["dataset", "intake", "review"].includes(startupPage) ? startupPage : "review",
  patents: [],
  selectedPatentIds: new Set(),
  patentDeleteInProgress: false,
  patentEngineChoices: {},
  pendingPatentComparisons: new Set(),
  patentMutationVersion: 0,
  patentActivationVersion: 0,
  allRecords: [],
  datasetFolders: [],
  activeDatasetId: null,
  publishDatasetId: "SDA",
  datasetCreateInProgress: false,
  datasetMutationVersion: 0,
  activePatentId: startupDocumentId,
  activePatent: null,
  pageAssetBase: SERVER_ROOT.href,
  datasetSearch: "",
  datasetStatus: [],
  datasetKeywordFilter: [],
  datasetExperimentLabelFilter: [],
  datasetReviewerFilter: [],
  datasetUnitSort: "none",
  importFile: null,
  logPatentId: null,
  logRequestVersion: 0,
  pendingReviewPatentId: null,
  pendingReviewAction: null,
  publishInProgress: false,
  reparseSubmitting: false,
  patentPollTimer: null,
  records: [],
  contexts: [],
  candidates: [],
  classifications: [],
  measurements: [],
  comparisons: [],
  template: {},
  runtime: {},
  validations: [],
  ocr: [],
  pages: { page_count: 0, pages: [] },
  contextByTable: new Map(),
  candidateByTable: new Map(),
  classificationByTable: new Map(),
  validationByConstruct: new Map(),
  ocrByTable: new Map(),
  selectedRecordId: null,
  selectedRecordIds: new Set(),
  selectedField: "ic50",
  evidence: [],
  selectedEvidenceIndex: 0,
  highlightVisibility: loadJson("sda.studio.highlightVisibility", {}),
  mapCollapsedGroups: new Set(),
  reviewMapCollapsed: loadText("sda.studio.reviewMapCollapsed", "false") === "true",
  mapZoom: 1,
  tab: "ocr",
  viewMode: "page",
  currentPage: 1,
  pendingPageScroll: true,
  scrollSyncFrame: null,
  canvasTool: "select",
  drawingBox: null,
  drawingSurface: null,
  manualBoxes: loadJson("sda.studio.manualBoxes", []),
  zoom: 1,
  search: "",
  status: [],
  keywordFilter: [],
  experimentLabelFilter: [],
  reviewerFilter: [],
  unitSort: "none",
  corrections: loadJson("sda.studio.corrections", {}),
  reviews: loadJson("sda.studio.reviews", {}),
  humanReviews: [],
  reviewerName: sessionStorage.getItem("sda.studio.reviewerName") || "",
  locale: loadText("sda.studio.locale", "zh") === "en" ? "en" : "zh",
  speech: {
    status: "idle",
    recordId: null,
    field: null,
    chunks: [],
    chunkIndex: -1,
    utterance: null,
    timer: null,
    runId: 0,
  },
};

const dom = {};
let collectionReview;
let collectionSummary = { key: null, data: null, error: false };
let collectionSummaryRequest = 0;
let collectionSummaryTimer;

function collectionReviewAvailable() {
  return state.appPage === "review" && state.activePatent && state.activePatent.collection_review_available === true;
}

function renderCollectionReviewLauncher() {
  const button = document.getElementById("open-collection-review");
  button.hidden = !collectionReviewAvailable();
  button.setAttribute("aria-expanded", String(Boolean(collectionReview && !collectionReview.element.hidden)));
  const data = collectionSummary.data;
  const ready = data && (data.questions_available || data.question_agent?.status === "ready" || !data.tasks.length);
  const count = ready ? data.tasks.length : null;
  const pending = ready ? data.tasks.filter(task => !task.decision_current || task.decision?.answer === "unsure" || task.decision?.action === "unsure").length : null;
  const badge = document.getElementById("collection-question-count");
  badge.textContent = collectionSummary.error ? "!" : count ?? "…";
  badge.classList.toggle("is-empty", count === 0 && !collectionSummary.error);
  const label = t(collectionSummary.error ? "review.collectionError" : ready ? "review.collectionCount"
    : data ? "review.collectionPending" : "review.collectionLoading", {count, pending});
  button.title = label;
  button.setAttribute("aria-label", label);
}

function updateCollectionSummary(data) {
  if (!collectionReviewAvailable() || data.document_id !== state.activePatentId) return;
  collectionSummaryRequest++;
  collectionSummary.data = data;
  collectionSummary.error = false;
  renderCollectionReviewLauncher();
  scheduleCollectionSummary();
}

function scheduleCollectionSummary() {
  clearTimeout(collectionSummaryTimer);
  if (!collectionReviewAvailable() || (collectionReview && !collectionReview.element.hidden)) return;
  const data = collectionSummary.data;
  if (collectionSummary.error || data?.busy || ["queued", "running"].includes(data?.question_agent?.status)) {
    collectionSummaryTimer = setTimeout(refreshCollectionSummary, 5000);
  }
}

async function refreshCollectionSummary() {
  clearTimeout(collectionSummaryTimer);
  if (!collectionReviewAvailable() || (collectionReview && !collectionReview.element.hidden)) return;
  const request = ++collectionSummaryRequest;
  const documentId = state.activePatentId;
  try {
    const data = await fetchJson(`${serverAssetUrl("api/patents/collection-review")}?document_id=${encodeURIComponent(documentId)}`);
    if (request !== collectionSummaryRequest || documentId !== state.activePatentId || !collectionReviewAvailable()) return;
    updateCollectionSummary(data);
  } catch (error) {
    if (request !== collectionSummaryRequest || documentId !== state.activePatentId || !collectionReviewAvailable()) return;
    collectionSummary.error = true;
    renderCollectionReviewLauncher();
    scheduleCollectionSummary();
  }
}

function syncCollectionReview() {
  const available = collectionReviewAvailable();
  if (collectionReview && !collectionReview.element.hidden && (!available || collectionReview.documentId !== state.activePatentId)) collectionReview.close();
  const key = available ? `${state.activePatentId}:${state.patentActivationVersion}` : null;
  if (key !== collectionSummary.key) {
    collectionSummaryRequest++;
    clearTimeout(collectionSummaryTimer);
    collectionSummary = { key, data: null, error: false };
    if (available) {
      if (collectionReview && !collectionReview.element.hidden && collectionReview.data?.document_id === state.activePatentId) {
        updateCollectionSummary(collectionReview.data);
      } else {
        refreshCollectionSummary();
      }
    }
  }
  renderCollectionReviewLauncher();
}

function openCollectionReview() {
  if (!collectionReviewAvailable()) return;
  if (collectionReview && !collectionReview.element.hidden) return collectionReview.close();
  collectionSummaryRequest++;
  clearTimeout(collectionSummaryTimer);
  if (!collectionReview) {
    collectionReview = new CollectionReviewPanel(document.getElementById("collection-review-panel"), {
      url: serverAssetUrl("api/patents/collection-review"),
      locale: () => state.locale,
      patentLabel: (id) => patentLabel(id),
      reviewer: () => state.reviewerName,
      requestIdentity: openReviewerDialog,
      onData: updateCollectionSummary,
      onClose: () => {
        renderCollectionReviewLauncher();
        if (collectionReviewAvailable() && collectionReview.documentId === state.activePatentId) {
          document.getElementById("open-collection-review").focus({preventScroll: true});
          refreshCollectionSummary();
        }
      },
      navigate: (task, ref) => {
        const reference = ref || task.evidence_refs.find(r => /:R\d+C\d+$/.test(r)) || task.table_ids[0];
        const match = String(reference || "").match(/^(.*):R(\d+)C(\d+)$/);
        const tableId = match ? match[1] : String(reference || task.table_ids[0] || "").replace(/:image$/, "");
        state.selectedRecordId = null;
        state.selectedField = "ic50";
        state.evidence = tableId ? [{key: `collection:${reference}`, kind: "assay", table_id: tableId,
          row_index: match ? Number(match[2]) : undefined, col_index: match ? Number(match[3]) : undefined,
          page: state.ocrByTable.get(tableId)?.page || task.page, label: task.title}] : [];
        state.selectedEvidenceIndex = 0;
        state.currentPage = evidencePageNumber(state.evidence[0]) || task.page || state.currentPage;
        state.viewMode = "page";
        state.pendingPageScroll = true;
        switchAppPage("review");
        renderAllLinkedViews();
      },
      onApplied: async (documentId) => {
        state.patents = await fetchPatentRegistry();
        await activatePatent(documentId);
        switchAppPage("review");
      },
    });
  }
  collectionReview.open(state.activePatentId);
  renderCollectionReviewLauncher();
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    cacheDom();
    processingMetricTooltips = setupMetricTooltips(dom.patentRows);
    bindEvents();
    document.getElementById("open-collection-review").addEventListener("click", openCollectionReview);
    setupTableColumns();
    applyLocale();
    ensureReviewerIdentity();
    loadData();
  } catch (error) {
    reportFatalError(error);
  }
});

window.addEventListener("unhandledrejection", (event) => {
  reportFatalError(event.reason || new Error("Unhandled promise rejection"));
});

window.addEventListener("beforeunload", () => {
  if (speechPlaybackSupported()) window.speechSynthesis.cancel();
});

function cacheDom() {
  const ids = [
    "app",
    "language-toggle",
    "reviewer-identity-button",
    "reviewer-identity-label",
    "dataset-export-button",
    "patent-export-button",
    "review-export-button",
    "dataset-page",
    "dataset-page-title",
    "dataset-back",
    "dataset-create-button",
    "dataset-record-tools",
    "dataset-folders-panel",
    "dataset-folder-list",
    "dataset-records-panel",
    "dataset-empty-state",
    "dataset-overview-table",
    "dataset-folder-dialog",
    "dataset-folder-form",
    "dataset-folder-name",
    "dataset-folder-error",
    "dataset-folder-cancel",
    "dataset-folder-submit",
    "review-dataset-select",
    "intake-page",
    "review-page",
    "dataset-total-count",
    "dataset-search",
    "dataset-status-filter",
    "dataset-keyword-filter",
    "dataset-experiment-label-filter",
    "dataset-reviewer-filter",
    "dataset-unit-sort",
    "dataset-overview-rows",
    "patent-count",
    "patent-rows",
    "patent-selection-count",
    "select-all-patents",
    "delete-selected-patents",
    "patent-import-form",
    "patent-name-input",
    "pdf-drop-zone",
    "selected-pdf-name",
    "patent-pdf-input",
    "patent-engine-select",
    "start-agent-button",
    "patent-log-dialog",
    "patent-log-title",
    "patent-log-document",
    "patent-log-status",
    "patent-log-content",
    "patent-log-refresh",
    "patent-log-close",
    "record-count",
    "record-search",
    "status-filter",
    "keyword-filter",
    "experiment-label-filter",
    "reviewer-filter",
    "unit-sort",
    "review-publish-button",
    "review-selection-count",
    "reparse-field-menu",
    "reparse-field-summary",
    "reparse-field-hint",
    "reparse-selected-records",
    "select-all-records",
    "toggle-grid",
    "dataset-title",
    "record-rows",
    "horizontal-splitter",
    "vertical-splitter",
    "viewer-title",
    "viewer-panel",
    "annotation-workspace",
    "map-splitter",
    "toggle-review-map",
    "review-map-body",
    "highlight-controls",
    "map-toggle-branches",
    "map-zoom-out",
    "map-zoom-in",
    "map-zoom-reset",
    "pdf-page-count",
    "evidence-strip",
    "viewer-stage",
    "empty-viewer",
    "continuous-pages",
    "image-frame",
    "evidence-image",
    "annotation-layer",
    "page-previous",
    "page-next",
    "page-indicator",
    "canvas-select",
    "canvas-rectangle",
    "clear-manual-boxes",
    "zoom-out",
    "zoom-in",
    "zoom-fit",
    "zoom-level",
    "viewer-location",
    "viewer-caption",
    "field-kind",
    "inspector-tabs",
    "inspector-content",
    "accept-field",
    "edit-field",
    "flag-field",
    "toast",
    "reviewer-dialog",
    "reviewer-form",
    "reviewer-name-input",
    "reviewer-name-error",
    "cancel-reviewer-name",
    "edit-dialog",
    "close-edit-dialog",
    "cancel-edit",
    "edit-dialog-title",
    "original-value",
    "corrected-value",
    "correction-reasons",
    "correction-reason-error",
    "correction-note",
    "save-correction",
  ];
  ids.forEach((id) => {
    dom[toCamelCase(id)] = document.getElementById(id);
  });
}

function bindEvents() {
  dom.languageToggle.addEventListener("click", toggleLocale);
  dom.reviewerIdentityButton.addEventListener("click", openReviewerDialog);
  dom.reviewerForm.addEventListener("submit", saveReviewerIdentity);
  dom.reviewerNameInput.addEventListener("input", () => {
    dom.reviewerNameError.hidden = true;
  });
  dom.cancelReviewerName.addEventListener("click", () => dom.reviewerDialog.close());
  dom.reviewerDialog.addEventListener("cancel", (event) => {
    if (!state.reviewerName) event.preventDefault();
  });
  document.querySelector(".primary-nav").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-app-page]");
    if (button) switchAppPage(button.dataset.appPage);
  });
  dom.datasetSearch.addEventListener("input", (event) => {
    state.datasetSearch = event.target.value.trim().toLowerCase();
    renderDatasetOverview();
  });
  dom.datasetBack.addEventListener("click", () => openDatasetFolder(null));
  dom.datasetFolderList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-dataset-id]");
    if (button) openDatasetFolder(button.dataset.datasetId);
  });
  dom.datasetCreateButton.addEventListener("click", () => {
    dom.datasetFolderForm.reset();
    dom.datasetFolderError.hidden = true;
    dom.datasetFolderDialog.showModal();
    dom.datasetFolderName.focus();
  });
  dom.datasetFolderCancel.addEventListener("click", () => dom.datasetFolderDialog.close());
  dom.datasetFolderDialog.addEventListener("cancel", (event) => {
    if (state.datasetCreateInProgress) event.preventDefault();
  });
  dom.datasetFolderForm.addEventListener("submit", createDatasetFolder);
  dom.reviewDatasetSelect.addEventListener("change", () => {
    state.publishDatasetId = dom.reviewDatasetSelect.value;
    renderReviewControls();
  });
  bindColumnFilter(dom.datasetStatusFilter, "datasetStatus", renderDatasetOverview);
  bindColumnFilter(dom.datasetKeywordFilter, "datasetKeywordFilter", renderDatasetOverview);
  bindColumnFilter(dom.datasetExperimentLabelFilter, "datasetExperimentLabelFilter", renderDatasetOverview);
  bindColumnFilter(dom.datasetReviewerFilter, "datasetReviewerFilter", renderDatasetOverview);
  dom.datasetUnitSort.addEventListener("change", (event) => {
    state.datasetUnitSort = event.target.value;
    renderDatasetOverview();
  });
  dom.datasetOverviewRows.addEventListener("click", onDatasetOverviewClick);
  dom.patentRows.addEventListener("click", onPatentTableClick);
  dom.patentRows.addEventListener("change", onPatentSelectionChange);
  dom.selectAllPatents.addEventListener("change", onSelectAllPatents);
  dom.deleteSelectedPatents.addEventListener("click", deleteSelectedPatents);
  dom.pdfDropZone.addEventListener("click", () => dom.patentPdfInput.click());
  dom.patentPdfInput.addEventListener("change", () => setImportFile(dom.patentPdfInput.files?.[0] || null));
  dom.pdfDropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dom.pdfDropZone.classList.add("dragging");
  });
  dom.pdfDropZone.addEventListener("dragleave", () => dom.pdfDropZone.classList.remove("dragging"));
  dom.pdfDropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dom.pdfDropZone.classList.remove("dragging");
    setImportFile(event.dataTransfer?.files?.[0] || null);
  });
  dom.patentImportForm.addEventListener("submit", startPatentImport);
  dom.patentLogClose.addEventListener("click", () => dom.patentLogDialog.close());
  dom.patentLogRefresh.addEventListener("click", refreshPatentLog);
  dom.patentLogDialog.addEventListener("close", () => {
    state.logPatentId = null;
    state.logRequestVersion += 1;
  });
  dom.recordSearch.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    renderGrid();
  });
  bindColumnFilter(dom.statusFilter, "status", renderGrid);
  bindColumnFilter(dom.keywordFilter, "keywordFilter", renderGrid);
  bindColumnFilter(dom.experimentLabelFilter, "experimentLabelFilter", renderGrid);
  bindColumnFilter(dom.reviewerFilter, "reviewerFilter", renderGrid);
  dom.unitSort.addEventListener("change", (event) => {
    state.unitSort = event.target.value;
    renderGrid();
  });
  dom.reviewPublishButton.addEventListener("click", publishSelectedRecords);
  dom.selectAllRecords.addEventListener("change", (event) => {
    const visibleIds = filteredRecords().map((record) => record.record_id);
    visibleIds.forEach((recordId) => {
      if (event.target.checked) state.selectedRecordIds.add(recordId);
      else state.selectedRecordIds.delete(recordId);
    });
    renderGrid();
  });
  dom.recordRows.addEventListener("change", onRecordSelectionChange);
  dom.reparseFieldMenu.addEventListener("change", renderReviewControls);
  dom.reparseSelectedRecords.addEventListener("click", reparseSelectedRecords);
  dom.toggleGrid.addEventListener("click", () => {
    document.body.classList.toggle("grid-expanded");
    dom.toggleGrid.firstElementChild.textContent = document.body.classList.contains("grid-expanded")
      ? "↙"
      : "↗";
  });
  dom.recordRows.addEventListener("click", onGridClick);
  dom.inspectorContent.addEventListener("click", onInspectorSpeechClick);
  dom.inspectorTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    state.tab = button.dataset.tab;
    renderInspector();
  });
  document.querySelectorAll("[data-view-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      state.viewMode = button.dataset.viewMode;
      if (state.viewMode === "page") state.pendingPageScroll = true;
      if (state.viewMode !== "page") state.canvasTool = "select";
      renderViewer();
    });
  });
  dom.evidenceStrip.addEventListener("click", onReviewMapClick);
  dom.highlightControls.addEventListener("change", (event) => {
    const input = event.target.closest("input[data-highlight]");
    if (input) setHighlightVisibility(input.dataset.highlight, input.checked);
  });
  dom.mapToggleBranches.addEventListener("click", () => {
    const groups = ["parent", "light", "heavy", "parameters", "source"];
    state.mapCollapsedGroups = new Set(state.mapCollapsedGroups.size === groups.length ? [] : groups);
    renderEvidenceStrip();
  });
  dom.mapZoomOut.addEventListener("click", () => setMapZoom(state.mapZoom - 0.1));
  dom.mapZoomIn.addEventListener("click", () => setMapZoom(state.mapZoom + 0.1));
  dom.mapZoomReset.addEventListener("click", () => setMapZoom(1));
  dom.toggleReviewMap.addEventListener("click", () => {
    const page = state.currentPage;
    state.reviewMapCollapsed = !state.reviewMapCollapsed;
    saveText("sda.studio.reviewMapCollapsed", String(state.reviewMapCollapsed));
    renderReviewMapLayout();
    if (state.viewMode === "page") requestAnimationFrame(() => scrollToPdfTarget(page));
  });
  dom.pagePrevious.addEventListener("click", () => setCurrentPage(state.currentPage - 1));
  dom.pageNext.addEventListener("click", () => setCurrentPage(state.currentPage + 1));
  dom.canvasSelect.addEventListener("click", () => setCanvasTool("select"));
  dom.canvasRectangle.addEventListener("click", () => setCanvasTool("rectangle"));
  dom.clearManualBoxes.addEventListener("click", clearManualBoxesForCurrentPage);
  dom.annotationLayer.addEventListener("click", onCanvasAnnotationClick);
  dom.continuousPages.addEventListener("click", onCanvasAnnotationClick);
  dom.continuousPages.addEventListener("pointerdown", onCanvasPointerDown);
  dom.continuousPages.addEventListener("pointermove", onCanvasPointerMove);
  dom.continuousPages.addEventListener("pointerup", onCanvasPointerUp);
  dom.continuousPages.addEventListener("pointercancel", cancelCanvasDrawing);
  dom.viewerStage.addEventListener("scroll", onViewerStageScroll, { passive: true });
  dom.zoomIn.addEventListener("click", () => setZoom(state.zoom + 0.15));
  dom.zoomOut.addEventListener("click", () => setZoom(state.zoom - 0.15));
  dom.zoomFit.addEventListener("click", () => setZoom(1));
  dom.datasetExportButton.addEventListener("click", () => exportCsv("dataset"));
  dom.patentExportButton.addEventListener("click", () => exportCsv("patents"));
  dom.reviewExportButton.addEventListener("click", () => exportCsv("review"));
  dom.acceptField.addEventListener("click", () => setReviewStatus("accepted"));
  dom.flagField.addEventListener("click", () => setReviewStatus("flagged"));
  dom.editField.addEventListener("click", openEditDialog);
  dom.saveCorrection.addEventListener("click", saveCorrection);
  dom.closeEditDialog.addEventListener("click", closeEditDialog);
  dom.cancelEdit.addEventListener("click", closeEditDialog);
  dom.correctionReasons.addEventListener("change", () => {
    dom.correctionReasonError.hidden = selectedCorrectionReasons().length > 0;
  });
  document.addEventListener("click", closeOtherColumnFilters);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeColumnFilters();
  });
  setupSplitters();
}

function bindColumnFilter(menu, stateKey, render) {
  menu.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[type="checkbox"][data-filter-value]');
    if (!checkbox) return;
    const selected = new Set(Array.isArray(state[stateKey]) ? state[stateKey] : []);
    if (checkbox.dataset.filterValue === "all") {
      selected.clear();
    } else if (checkbox.checked) {
      selected.add(checkbox.dataset.filterValue);
    } else {
      selected.delete(checkbox.dataset.filterValue);
    }
    state[stateKey] = [...selected];
    window.requestAnimationFrame(render);
  });
}

function closeOtherColumnFilters(event) {
  const current = event.target.closest(".column-filter-menu");
  document.querySelectorAll(".column-filter-menu[open]").forEach((menu) => {
    if (menu !== current) menu.removeAttribute("open");
  });
}

function closeColumnFilters() {
  document.querySelectorAll(".column-filter-menu[open]").forEach((menu) => menu.removeAttribute("open"));
}

async function loadData() {
  try {
    state.patents = await fetchPatentRegistry();
    try {
      const reviewPayload = await fetchJson(HUMAN_REVIEWS_API, 12000);
      state.humanReviews = Array.isArray(reviewPayload?.reviews) ? reviewPayload.reviews : [];
    } catch (error) {
      console.warn("Human review history unavailable", error);
      state.humanReviews = [];
    }
    state.activePatent = state.patents.find((item) => item.document_id === state.activePatentId)
      || state.activePatent;
    await refreshAllRecords();
    const initialPatent = state.patents.find((item) => item.document_id === state.activePatentId)
      || state.patents.find((item) => item.status === "completed")
      || state.patents[0];
    if (initialPatent && hasReviewableResult(initialPatent)) {
      await activatePatent(initialPatent.document_id);
    } else {
      renderApp();
    }
    switchAppPage(state.appPage, false);
    schedulePatentPolling();
    dom.app.setAttribute("aria-busy", "false");
  } catch (error) {
    console.error(error);
    dom.app.setAttribute("aria-busy", "false");
    dom.recordRows.innerHTML = `<tr class="empty-row"><td colspan="12">${escapeHtml(t("app.loadFailed"))}: ${escapeHtml(error.message)}</td></tr>`;
    showToast(t("toast.loadFailed"), 6000);
  }
}

async function fetchPatentRegistry() {
  try {
    const payload = await fetchJson(PATENTS_API, 12000);
    const patents = Array.isArray(payload?.patents) && payload.patents.length
      ? payload.patents
      : [];
    // Cached artifacts also work while an older server finishes an active job.
    return await Promise.all(patents.map(async (patent) => {
      if (!["queued", "processing"].includes(patent.status)
          && patent.processing_metrics?.time?.scope === "cumulative"
          && patent.processing_metrics?.tokens?.totals?.requests === 0) {
        const usageKey = `${patent.document_id}:${patent.run_id || ""}:${patent.updated_at || ""}`;
        try {
          let report = completedUsageReports.get(usageKey);
          if (!report) {
            report = await fetchJson(serverAssetUrl(`api/patents/${encodeURIComponent(patent.document_id)}/run.json`), 5000);
            if (report.document_id === patent.document_id) completedUsageReports.set(usageKey, report);
          }
          if (report.document_id === patent.document_id) {
            patent = { ...patent, processing_metrics: completedRunUsage(patent.processing_metrics, report) };
          }
        } catch { /* Keep the available statistics if a legacy report is unavailable. */ }
      }
      if (patent.pdf_form) return patent;
      const key = `${patent.document_id}:${patent.created_at || ""}`;
      try {
        let profile = cachedPdfForms.get(key);
        if (!profile) {
          const cached = await fetchJson(serverAssetUrl(`.studio/pdf_forms/${encodeURIComponent(patent.document_id)}.json`), 3000);
          profile = cached.profile;
          if (!profile || (patent.created_at && Date.parse(profile.detected_at) < Date.parse(patent.created_at))) return patent;
          cachedPdfForms.set(key, profile);
        }
        return { ...patent, pdf_form: profile };
      } catch {
        return patent;
      }
    }));
  } catch (error) {
    throw error;
  }
}

async function fetchJson(url, timeoutMs = 20000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function resolveArtifactUrl(value) {
  if (!value) return "";
  if (/^https?:\/\//i.test(String(value))) return String(value);
  return serverAssetUrl(String(value));
}

function patentDataUrls(patent) {
  const artifacts = patent?.artifacts || {};
  return {
    records: resolveArtifactUrl(artifacts.records),
    contexts: resolveArtifactUrl(artifacts.contexts),
    candidates: resolveArtifactUrl(artifacts.candidates),
    classifications: resolveArtifactUrl(artifacts.classifications),
    measurements: resolveArtifactUrl(artifacts.measurements),
    comparisons: resolveArtifactUrl(artifacts.comparisons),
    template: resolveArtifactUrl(artifacts.template),
    runtime: resolveArtifactUrl(artifacts.runtime),
    validations: resolveArtifactUrl(artifacts.validations),
    ocr: resolveArtifactUrl(artifacts.semantic_ocr || artifacts.ocr),
    pages: resolveArtifactUrl(artifacts.pages),
  };
}

function hasReviewableResult(patent) {
  return Boolean(patent && (patent.status === "completed" ||
    patent.artifacts?.pages ||
    (patent.artifacts?.records && Number(patent.record_count || 0) > 0)));
}

async function activatePatent(documentId) {
  const patent = state.patents.find((item) => item.document_id === documentId);
  if (!hasReviewableResult(patent)) return false;
  const activationVersion = ++state.patentActivationVersion;
  const urls = patentDataUrls(patent);
  if (!urls.pages) throw new Error(`${documentId}: missing pages artifact`);
  const artifactDefaults = {
    records: [],
    contexts: [],
    candidates: [],
    classifications: [],
    measurements: [],
    comparisons: [],
    template: {},
    runtime: {},
    validations: [],
    ocr: [],
  };
  let entries;
  if (patent.artifacts?.review) {
    const review = await fetchJson(resolveArtifactUrl(patent.artifacts.review));
    if (!Array.isArray(review.records) || !review.pages) throw new Error(`${documentId}: incomplete review data`);
    entries = Object.entries(urls).map(([key]) => [key, review[key] ?? artifactDefaults[key]]);
  } else {
    entries = await Promise.all(
    Object.entries(urls).map(async ([key, url]) => {
      let value;
      if (!url && key in artifactDefaults) {
        value = artifactDefaults[key];
      } else {
        try {
          value = await fetchJson(url);
        } catch (error) {
          if (key === "records" || !(key in artifactDefaults)) throw error;
          console.warn(`Optional artifact unavailable for ${documentId}: ${key}`, error);
          value = artifactDefaults[key];
        }
      }
      return [key, value];
    }),
    );
  }
  if (activationVersion !== state.patentActivationVersion || !state.patents.some((item) => item.document_id === documentId)) return false;
  entries.forEach(([key, value]) => {
    state[key] = key === "records"
      ? value.map((record) => ({ ...record, _patent_id: documentId }))
      : value;
  });
  state.activePatentId = documentId;
  state.activePatent = patent;
  state.pageAssetBase = new URL("./", urls.pages).href;
  state.currentPage = 1;
  state.viewMode = "page";
  state.selectedRecordIds.clear();
  state.pendingPageScroll = true;
  state.selectedEvidenceIndex = 0;
  state.continuousPagesReady = false;
  dom.continuousPages.innerHTML = "";
  dom.evidenceImage.dataset.src = "";
  buildIndexes();
  const firstLinked = state.records.find((record) => record.link_status === "linked") || state.records[0];
  state.selectedRecordId = firstLinked?.record_id || null;
  state.selectedField = "ic50";
  state.tab = "ocr";
  selectEvidenceForCurrentField();
  renderApp();
  return true;
}

async function refreshAllRecords() {
  const mutationVersion = state.patentMutationVersion;
  const datasetVersion = state.datasetMutationVersion;
  const payload = await fetchJson(DATASET_RECORDS_API);
  if (mutationVersion !== state.patentMutationVersion || datasetVersion !== state.datasetMutationVersion) return;
  state.allRecords = Array.isArray(payload?.records) ? payload.records : [];
  state.datasetFolders = Array.isArray(payload?.folders) ? payload.folders : [{dataset_id: "SDA", name: "SDA"}];
  renderDatasetOverview();
  renderReviewControls();
}

function schedulePatentPolling() {
  if (state.patentPollTimer) clearTimeout(state.patentPollTimer);
  const activeJobs = state.patents.some((item) => ["queued", "processing"].includes(item.status)
    || item.patent_text?.status === "checking");
  if (!activeJobs) return;
  state.patentPollTimer = setTimeout(pollPatentJobs, 3500);
}

async function pollPatentJobs() {
  try {
    const mutationVersion = state.patentMutationVersion;
    const previousStatus = new Map(state.patents.map((item) => [item.document_id, item.status]));
    const patents = await fetchPatentRegistry();
    if (mutationVersion !== state.patentMutationVersion) return;
    state.patents = patents;
    if (dom.patentLogDialog.open && !dom.patentLogRefresh.disabled
        && ["queued", "processing"].includes(previousStatus.get(state.logPatentId))) {
      void refreshPatentLog();
    }
    const newlyCompleted = state.patents.some(
      (item) => item.status === "completed" && previousStatus.get(item.document_id) !== "completed",
    );
    renderPatentTable();
    const pending = state.pendingReviewPatentId
      ? state.patents.find((item) => item.document_id === state.pendingReviewPatentId)
      : null;
    if (newlyCompleted) await refreshAllRecords();
    if (pending?.status === "completed") {
      const completedAction = state.pendingReviewAction;
      state.pendingReviewPatentId = null;
      state.pendingReviewAction = null;
      await activatePatent(pending.document_id);
      switchAppPage("review");
      showToast(
        t(completedAction === "reparse" ? "review.reparseCompleted" : "intake.completed", {
          patent: pending.document_id,
        }),
        4500,
      );
    } else if (pending?.status === "failed") {
      state.pendingReviewPatentId = null;
      const failedAction = state.pendingReviewAction;
      state.pendingReviewAction = null;
      showToast(
        t(failedAction === "reparse" ? "review.reparseFailed" : "intake.importFailed", {
          error: pending.error || t("intake.status.failed"),
        }),
        6500,
      );
    }
    renderReviewControls();
  } catch (error) {
    console.warn("Unable to refresh patent jobs", error);
  } finally {
    schedulePatentPolling();
  }
}

function t(key, variables = {}) {
  const dictionary = TRANSLATIONS[state.locale] || TRANSLATIONS.zh;
  const fallback = TRANSLATIONS.zh[key] || key;
  return String(dictionary[key] || fallback).replace(/\{(\w+)\}/g, (_, name) =>
    variables[name] == null ? `{${name}}` : String(variables[name]),
  );
}

function fieldLabel(field) {
  return t(FIELD_LABEL_KEYS[field] || "field.generic");
}

function localizedStatus(status) {
  return t(`status.${String(status || "missing").toLowerCase()}`);
}

function classificationLabel(label) {
  return label ? t(`classification.${label}`) : "—";
}

function classificationOrientation(classification) {
  if (!classification) return "—";
  return t("ocr.orientationValue", {
    entity: t(`axis.${classification.entity_axis || "unknown"}`),
    measurement: t(`axis.${classification.measurement_axis || "unknown"}`),
  });
}

function semanticAgentLabel(agent) {
  if (!agent) return "—";
  if (agent.mode === "llm") {
    return `${agent.model || agent.name || "—"} · ${agent.reasoning_effort || "—"}${agent.cache_hit ? " · cache" : ""}`;
  }
  return agent.name || agent.mode || t("ocr.rulesMode");
}

function applyLocale() {
  collectionReview?.render();
  document.documentElement.lang = state.locale === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.title = t(element.dataset.i18nTitle);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  });
  document.querySelectorAll("[data-i18n-alt]").forEach((element) => {
    element.alt = t(element.dataset.i18nAlt);
  });
  dom.languageToggle.textContent = state.locale === "zh" ? "EN" : "中文";
  dom.languageToggle.setAttribute(
    "aria-label",
    state.locale === "zh" ? "Switch to English" : "切换到中文",
  );
  renderReviewerIdentity();
  renderCollectionReviewLauncher();
  if (dom.selectedPdfName) {
    dom.selectedPdfName.textContent = state.importFile
      ? t("intake.fileSelected", { name: state.importFile.name, size: formatFileSize(state.importFile.size) })
      : t("intake.choosePdf");
  }
}

function renderReviewerIdentity() {
  if (!dom.reviewerIdentityLabel) return;
  const name = state.reviewerName || t("reviewer.unset");
  dom.reviewerIdentityLabel.textContent = t("reviewer.current", { name });
  dom.reviewerIdentityButton.title = t("reviewer.change");
  dom.reviewerIdentityButton.classList.toggle("is-set", Boolean(state.reviewerName));
}

function ensureReviewerIdentity() {
  renderReviewerIdentity();
  if (!state.reviewerName) openReviewerDialog();
}

function openReviewerDialog() {
  const suggestedName = state.reviewerName || loadText("sda.studio.lastReviewerName", "");
  dom.reviewerNameInput.value = suggestedName;
  dom.reviewerNameError.hidden = true;
  dom.cancelReviewerName.hidden = !state.reviewerName;
  if (!dom.reviewerDialog.open) dom.reviewerDialog.showModal();
  window.requestAnimationFrame(() => {
    dom.reviewerNameInput.focus();
    dom.reviewerNameInput.select();
  });
}

function saveReviewerIdentity(event) {
  event.preventDefault();
  const name = dom.reviewerNameInput.value.trim().replace(/\s+/g, " ");
  const letterCount = [...name].filter((character) => /\p{L}/u.test(character)).length;
  if (name.length > 40 || letterCount < 2) {
    dom.reviewerNameError.hidden = false;
    dom.reviewerNameInput.focus();
    return;
  }
  state.reviewerName = name;
  sessionStorage.setItem("sda.studio.reviewerName", name);
  saveText("sda.studio.lastReviewerName", name);
  dom.reviewerDialog.close();
  renderReviewerIdentity();
}

function toggleLocale() {
  state.locale = state.locale === "zh" ? "en" : "zh";
  saveText("sda.studio.locale", state.locale);
  applyLocale();
  if (state.records.length) {
    selectEvidenceForCurrentField();
    renderApp();
  }
}

function buildIndexes() {
  state.contextByTable = new Map(state.contexts.map((item) => [String(item.table_id), item]));
  state.candidateByTable = new Map(state.candidates.map((item) => [String(item.table_id), item]));
  state.classificationByTable = new Map(
    state.classifications.map((item) => [String(item.table_id), item]),
  );
  state.validationByConstruct = new Map(
    state.validations.map((item) => [String(item.construct_key), item]),
  );
  state.ocrByTable = new Map(
    state.ocr.map((item) => [String(item.source_id || item.image), item]),
  );
}

function patentLabel(documentId) {
  return patentDisplayName(state.patents.find(patent => patent.document_id === documentId) || {document_id: documentId});
}

function renderApp() {
  syncCollectionReview();
  dom.datasetTitle.textContent = patentLabel(state.activePatentId) || "—";
  dom.pdfPageCount.textContent = t("viewer.pdfPages", { count: getPageCount() });
  renderGrid();
  renderReviewControls();
  renderDatasetOverview();
  renderPatentTable();
  if (!dom.reviewPage.hidden) renderAllLinkedViews();
}

function switchAppPage(page, persist = true) {
  const next = ["dataset", "intake", "review"].includes(page) ? page : "dataset";
  if (next !== "review") stopSequenceSpeech({ updateUi: false });
  state.appPage = next;
  syncCollectionReview();
  if (persist) saveText("sda.studio.appPage", next);
  document.body.classList.remove("grid-expanded");
  dom.datasetPage.hidden = next !== "dataset";
  dom.intakePage.hidden = next !== "intake";
  dom.reviewPage.hidden = next !== "review";
  document.querySelectorAll(".primary-nav button[data-app-page]").forEach((button) => {
    const active = button.dataset.appPage === next;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  if (next === "review") {
    syncPageToEvidence();
    state.pendingPageScroll = true;
    renderAllLinkedViews();
  }
}

function openDatasetFolder(datasetId) {
  if (datasetId !== null && !state.datasetFolders.some(folder => folder.dataset_id === datasetId)) return;
  state.activeDatasetId = datasetId;
  if (datasetId) state.publishDatasetId = datasetId;
  state.datasetSearch = "";
  state.datasetStatus = [];
  state.datasetKeywordFilter = [];
  state.datasetExperimentLabelFilter = [];
  state.datasetReviewerFilter = [];
  state.datasetUnitSort = "none";
  dom.datasetSearch.value = "";
  closeColumnFilters();
  renderDatasetOverview();
  renderReviewControls();
  if (datasetId) dom.datasetBack.focus();
  else dom.datasetFolderList.querySelector("button")?.focus();
}

async function createDatasetFolder(event) {
  event.preventDefault();
  if (state.datasetCreateInProgress) return;
  state.datasetCreateInProgress = true;
  dom.datasetFolderError.hidden = true;
  dom.datasetFolderSubmit.disabled = true;
  dom.datasetFolderCancel.disabled = true;
  dom.datasetFolderName.disabled = true;
  dom.datasetFolderSubmit.textContent = t("datasetPage.creating");
  try {
    const response = await fetch(DATASET_FOLDERS_API, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: dom.datasetFolderName.value.trim()}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.datasetMutationVersion += 1;
    state.datasetFolders = [...state.datasetFolders.filter(folder => folder.dataset_id !== payload.folder.dataset_id), payload.folder];
    dom.datasetFolderDialog.close();
    openDatasetFolder(payload.folder.dataset_id);
    showToast(t("datasetPage.created", {name: payload.folder.name}));
  } catch (error) {
    dom.datasetFolderError.textContent = error.message;
    dom.datasetFolderError.hidden = false;
  } finally {
    state.datasetCreateInProgress = false;
    dom.datasetFolderSubmit.disabled = false;
    dom.datasetFolderCancel.disabled = false;
    dom.datasetFolderName.disabled = false;
    dom.datasetFolderSubmit.textContent = t("datasetPage.createSubmit");
    if (dom.datasetFolderDialog.open) dom.datasetFolderName.focus();
  }
}

function currentDatasetRecords() {
  return state.allRecords.filter(record => (record._dataset_id || "SDA") === state.activeDatasetId);
}

function renderDatasetFolders() {
  const folder = state.datasetFolders.find(item => item.dataset_id === state.activeDatasetId);
  if (!folder) state.activeDatasetId = null;
  dom.datasetPageTitle.textContent = folder ? folder.name : t("datasetPage.title");
  dom.datasetPageTitle.title = folder?.name || "";
  dom.datasetBack.hidden = !folder;
  dom.datasetRecordTools.hidden = !folder;
  dom.datasetRecordsPanel.hidden = !folder;
  dom.datasetFoldersPanel.hidden = Boolean(folder);
  if (folder) return;
  const counts = new Map();
  for (const record of state.allRecords) {
    const id = record._dataset_id || "SDA";
    if (!counts.has(id)) counts.set(id, {records: 0, patents: new Set()});
    const count = counts.get(id);
    count.records += 1;
    count.patents.add(record._patent_id || record.document_id);
  }
  dom.datasetTotalCount.textContent = t("datasetPage.folderCount", {count: state.datasetFolders.length});
  dom.datasetFolderList.innerHTML = state.datasetFolders.map(item => {
    const count = counts.get(item.dataset_id);
    return `<button type="button" class="dataset-folder-card" data-dataset-id="${escapeAttr(item.dataset_id)}"
      aria-label="${escapeAttr(t("datasetPage.openFolder", {name: item.name}))}">
      <svg class="dataset-folder-icon" viewBox="0 0 48 40" aria-hidden="true"><path d="M3 10a4 4 0 0 1 4-4h11l5 5h18a4 4 0 0 1 4 4v18a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4Z"/><path d="M3 17h42"/></svg>
      <span class="dataset-folder-name">${escapeHtml(item.name)}</span>
      <span class="dataset-folder-stats">${escapeHtml(t("datasetPage.folderStats", {patents: count?.patents.size || 0, records: count?.records || 0}))}</span>
    </button>`;
  }).join("");
}

function renderDatasetPublishTarget() {
  if (!dom.reviewDatasetSelect) return;
  if (!state.datasetFolders.some(folder => folder.dataset_id === state.publishDatasetId)) {
    state.publishDatasetId = state.datasetFolders[0]?.dataset_id || "";
  }
  const options = state.datasetFolders.map(folder => `<option value="${escapeAttr(folder.dataset_id)}">${escapeHtml(folder.name)}</option>`).join("");
  if (dom.reviewDatasetSelect.innerHTML !== options) dom.reviewDatasetSelect.innerHTML = options;
  dom.reviewDatasetSelect.value = state.publishDatasetId;
  dom.reviewDatasetSelect.disabled = state.publishInProgress || !state.datasetFolders.length;
}

function filteredOverviewRecords() {
  const records = currentDatasetRecords().filter((record) => {
    if (!recordMatchesReviewChecks(record, state.datasetStatus)) return false;
    if (!recordMatchesAssayKeywords(record, state.datasetKeywordFilter)) return false;
    if (!recordMatchesExperimentLabels(record, state.datasetExperimentLabelFilter)) return false;
    if (!recordMatchesHumanReviewers(record, state.datasetReviewerFilter)) return false;
    if (!state.datasetSearch) return true;
    const haystack = [
      record._patent_id,
      patentLabel(record._patent_id || record.document_id),
      record.antibody_id,
      record.vh,
      record.vl,
      record.ic50,
      record.experiment_group,
      assayKeywordText(record),
      recordReviewerNames(record).join(" "),
    ].join(" ").toLowerCase();
    return haystack.includes(state.datasetSearch);
  });
  return sortRecordsByUnit(records, state.datasetUnitSort);
}

function renderDatasetOverview() {
  if (!dom.datasetOverviewRows) return;
  renderDatasetFolders();
  if (!state.activeDatasetId) return;
  const folderRecords = currentDatasetRecords();
  state.datasetExperimentLabelFilter = renderExperimentLabelFilterMenu(
    dom.datasetExperimentLabelFilter, folderRecords, state.datasetExperimentLabelFilter,
  );
  dom.datasetEmptyState.hidden = folderRecords.length > 0;
  dom.datasetOverviewTable.hidden = folderRecords.length === 0;
  state.datasetKeywordFilter = renderKeywordFilterMenu(
    dom.datasetKeywordFilter,
    folderRecords,
    state.datasetKeywordFilter,
  );
  state.datasetStatus = renderStatusFilterMenu(
    dom.datasetStatusFilter,
    folderRecords,
    state.datasetStatus,
  );
  state.datasetReviewerFilter = renderReviewerFilterMenu(
    dom.datasetReviewerFilter,
    folderRecords,
    state.datasetReviewerFilter,
  );
  updateUnitSortControl(dom.datasetUnitSort, state.datasetUnitSort);
  const records = filteredOverviewRecords();
  dom.datasetExportButton.disabled = !records.length;
  dom.datasetTotalCount.textContent = t("dataset.records", { count: records.length });
  if (!records.length) {
    dom.datasetOverviewRows.innerHTML = `<tr class="empty-row"><td colspan="11">${escapeHtml(t(folderRecords.length ? "dataset.empty" : "datasetPage.emptyFolder"))}</td></tr>`;
    return;
  }
  dom.datasetOverviewRows.innerHTML = records.map((record) => {
    const evidenceCount = overviewEvidenceCount(record);
    return `<tr data-patent-id="${escapeAttr(record._patent_id)}" data-record-id="${escapeAttr(record.record_id)}">
      ${overviewCell(record, patentLabel(record._patent_id || record.document_id), "antibody_id", "patent-cell")}
      ${overviewCell(record, record.antibody_id, "antibody_id")}
      ${overviewCell(record, record.vh, "vh", "sequence-cell")}
      ${overviewCell(record, record.vl, "vl", "sequence-cell")}
      ${overviewCell(record, `${record.ic50_operator === "=" ? "" : record.ic50_operator || ""}${record.ic50 ?? "—"}`, "ic50", "numeric")}
      ${overviewCell(record, record.ic50_unit, "ic50_unit")}
      ${overviewCell(record, record.experiment_group, "experiment_group")}
      ${overviewKeywordCell(record)}
      ${overviewReviewStatusCell(record)}
      ${humanReviewerCell(record)}
      ${overviewCell(record, evidenceCount, "antibody_id", "numeric")}
    </tr>`;
  }).join("");
}

function overviewEvidenceCount(record) {
  return record._patent_id === state.activePatentId
    ? buildEvidence(record, null).length
    : (Array.isArray(record.evidence) ? record.evidence.length : 0);
}

function overviewCell(record, value, field, className = "") {
  const display = value ?? "";
  const conflictClass = sequenceConflictClass(record, field, display);
  const title = [typeof display === "string" && display.length > 24 ? display : "",
    conflictClass ? t(display ? "reviewCheck.table_gold_standard" : "reviewCheck.table_unresolved") : ""].filter(Boolean).join(" · ");
  return `<td><button type="button" class="data-cell ${className} ${conflictClass}"
    data-patent-id="${escapeAttr(record._patent_id)}" data-record-id="${escapeAttr(record.record_id)}"
    data-field="${escapeAttr(field)}" title="${escapeAttr(title)}">${escapeHtml(display)}</button></td>`;
}

function experimentLabelValue(record) {
  return String(record.experiment_group || record.assay_endpoint || "").trim();
}

function recordMatchesExperimentLabels(record, selectedValues) {
  return !Array.isArray(selectedValues) || !selectedValues.length
    || selectedValues.includes(`label:${experimentLabelValue(record)}`);
}

function renderExperimentLabelFilterMenu(menu, records, selectedValues) {
  const counts = new Map();
  records.forEach((record) => {
    const label = experimentLabelValue(record);
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  const options = [...counts].sort(([left], [right]) => left.localeCompare(right)).map(([label, count]) => ({
    value: `label:${label}`, label: label || t("filter.unlabeledExperiment"), count,
  }));
  return renderColumnFilterMenu(menu, options, selectedValues, t("filter.experimentLabel"));
}

function localizedAssayKeyword(keyword) {
  if (!keyword || typeof keyword !== "object") return String(keyword || "");
  return state.locale === "en"
    ? keyword.label_en || keyword.label_zh || keyword.key || ""
    : keyword.label_zh || keyword.label_en || keyword.key || "";
}

function assayKeywordValues(record) {
  return Array.isArray(record?.assay_keywords)
    ? record.assay_keywords.filter((item) => localizedAssayKeyword(item))
    : [];
}

function assayKeywordIdentity(keyword) {
  if (!keyword || typeof keyword !== "object") {
    return `other:${String(keyword || "").trim().toLowerCase()}`;
  }
  const category = String(keyword.category || "other").trim().toLowerCase();
  const key = String(keyword.key || keyword.label_en || keyword.label_zh || "").trim().toLowerCase();
  const target = String(keyword.target_id || "").trim().toLowerCase();
  return target ? `${category}:${target}:${key}` : `${category}:${key}`;
}

function recordMatchesAssayKeywords(record, selectedValues) {
  if (!Array.isArray(selectedValues) || !selectedValues.length) return true;
  const recordValues = new Set(assayKeywordValues(record).map(assayKeywordIdentity));
  const filtersByCategory = new Map();
  selectedValues.forEach((value) => {
    const category = String(value).split(":", 1)[0] || "other";
    if (!filtersByCategory.has(category)) filtersByCategory.set(category, []);
    filtersByCategory.get(category).push(value);
  });
  return [...filtersByCategory.values()].every((filters) => filters.some((value) => recordValues.has(value)));
}

function keywordFilterOptions(records) {
  const keywords = new Map();
  records.forEach((record) => {
    const seen = new Set();
    assayKeywordValues(record).forEach((keyword) => {
      const value = assayKeywordIdentity(keyword);
      if (!value || value === "other:" || seen.has(value)) return;
      seen.add(value);
      const current = keywords.get(value);
      if (current) current.count += 1;
      else keywords.set(value, {
        value,
        category: typeof keyword === "object"
          ? String(keyword.category || "other").trim().toLowerCase()
          : "other",
        label: keyword.target_id ? `${keyword.target_id} · ${localizedAssayKeyword(keyword)}` : localizedAssayKeyword(keyword),
        count: 1,
      });
    });
  });
  const categoryOrder = new Map(["species", "system", "method", "endpoint", "cell_model", "readout", "sample", "other"].map((name, index) => [name, index]));
  const options = [...keywords.values()].sort((left, right) => {
    const categoryDifference = (categoryOrder.get(left.category) ?? 99) - (categoryOrder.get(right.category) ?? 99);
    return categoryDifference || left.label.localeCompare(right.label, state.locale === "zh" ? "zh-CN" : "en");
  });
  return options;
}

function renderKeywordFilterMenu(menu, records, selectedValues) {
  return renderColumnFilterMenu(
    menu,
    keywordFilterOptions(records),
    selectedValues,
    t("filter.experimentKeyword"),
  );
}

const REVIEW_CHECK_SOURCES = ["agent", "anarci", "esm2", "record"];
const REVIEW_CHECK_STATUSES = ["passed", "error", "manual_review"];

function normalizedReviewCheckStatus(value) {
  return REVIEW_CHECK_STATUSES.includes(value) ? value : "manual_review";
}

function reviewCheckStatus(record, source) {
  if (source === "record") {
    const status = normalizedReviewCheckStatus(record?.record_verification_status);
    return status === "passed" && record?.sequence_coverage_status === "review_required" ? "manual_review" : status;
  }
  const direct = record?.[`${source}_review_status`] || record?.[`${source}_review`]?.status;
  if (direct) return normalizedReviewCheckStatus(direct);
  const validationEvidence = [...(record?.evidence || [])]
    .reverse()
    .find((item) => item?.type === "construct_validation");
  const evidenceStatus = validationEvidence?.[`${source}_review`]?.status;
  if (evidenceStatus) return normalizedReviewCheckStatus(evidenceStatus);
  if (source === "agent") {
    const constructStatus = record?.construct_validation_status;
    if (constructStatus === "consistent") return "passed";
    if (["conflict", "incomplete", "invalid"].includes(constructStatus)) return "error";
  }
  return "manual_review";
}

function reviewCheckValue(record, source) {
  return `${source}:${reviewCheckStatus(record, source)}`;
}

function recordMatchesReviewChecks(record, selectedValues) {
  if (!Array.isArray(selectedValues) || !selectedValues.length) return true;
  if (selectedValues.includes("source:table_text_mismatch") && !record.sequence_source_conflicts?.length) return false;
  if (selectedValues.includes("source:image_corrected") && !record.sequence_image_reviews?.length) return false;
  return REVIEW_CHECK_SOURCES.every((source) => {
    const filters = selectedValues.filter((value) => value.startsWith(`${source}:`));
    return !filters.length || filters.includes(reviewCheckValue(record, source));
  });
}

function renderStatusFilterMenu(menu, records, selectedValues) {
  const counts = new Map(
    REVIEW_CHECK_SOURCES.flatMap((source) =>
      REVIEW_CHECK_STATUSES.map((status) => [`${source}:${status}`, 0])
    ),
  );
  records.forEach((record) => {
    REVIEW_CHECK_SOURCES.forEach((source) => {
      const value = reviewCheckValue(record, source);
      counts.set(value, (counts.get(value) || 0) + 1);
    });
  });
  const options = REVIEW_CHECK_SOURCES.flatMap((source) =>
    REVIEW_CHECK_STATUSES.map((status) => ({
      value: `${source}:${status}`,
      label: `${t(`reviewCheck.${source}`)} · ${t(`reviewCheck.${status}`)}`,
      count: counts.get(`${source}:${status}`) || 0,
    }))
  );
  options.push({value: "source:table_text_mismatch", label: t("reviewCheck.table_text_mismatch"),
    count: records.filter((record) => record.sequence_source_conflicts?.length).length});
  options.push({value: "source:image_corrected", label: t("reviewCheck.image_corrected"),
    count: records.filter((record) => record.sequence_image_reviews?.length).length});
  return renderColumnFilterMenu(menu, options, selectedValues, t("filter.reviewStatus"));
}

const UNREVIEWED_FILTER_VALUE = "__unreviewed__";

function recordDocumentId(record) {
  return String(record?._patent_id || record?.document_id || state.activePatentId || "");
}

function reviewEntryMatchesRecord(entry, record) {
  if (!entry || String(entry.record_id || "") !== String(record?.record_id || "")) return false;
  const entryDocumentId = String(entry.document_id || "");
  return !entryDocumentId || entryDocumentId === recordDocumentId(record);
}

function entryReviewerNames(entry) {
  return [
    ...(Array.isArray(entry?.reviewer_history) ? entry.reviewer_history : []),
    entry?.reviewer_name,
  ].map((name) => String(name || "").trim()).filter(Boolean);
}

function updatedReviewerHistory(previousEntry) {
  return [...new Set([...entryReviewerNames(previousEntry), state.reviewerName])];
}

function registerHumanReview(record, reviewAction) {
  const entry = {
    document_id: recordDocumentId(record),
    record_id: String(record.record_id || ""),
    reviewer_name: state.reviewerName,
    review_action: reviewAction,
    reviewed_at: new Date().toISOString(),
  };
  state.humanReviews = [
    ...state.humanReviews.filter((item) => !(
      item.document_id === entry.document_id
      && item.record_id === entry.record_id
      && item.reviewer_name === entry.reviewer_name
    )),
    entry,
  ];
  fetch(HUMAN_REVIEWS_API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(entry),
  }).then(async (response) => {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const stored = payload.review;
    state.humanReviews = [
      ...state.humanReviews.filter((item) => !(
        item.document_id === stored.document_id
        && item.record_id === stored.record_id
        && item.reviewer_name === stored.reviewer_name
      )),
      stored,
    ];
  }).catch((error) => {
    console.warn("Unable to persist human review", error);
    showToast(t("reviewer.syncFailed", { error: error.message }), 5200);
  });
}

function recordReviewerNames(record) {
  const names = new Set(
    Array.isArray(record?.human_reviewers)
      ? record.human_reviewers.map((name) => String(name || "").trim()).filter(Boolean)
      : [],
  );
  [state.corrections, state.reviews].forEach((collection) => {
    Object.values(collection).forEach((entry) => {
      if (!reviewEntryMatchesRecord(entry, record)) return;
      entryReviewerNames(entry).forEach((name) => names.add(name));
    });
  });
  state.humanReviews.forEach((entry) => {
    if (!reviewEntryMatchesRecord(entry, record)) return;
    entryReviewerNames(entry).forEach((name) => names.add(name));
  });
  return [...names].sort((left, right) =>
    left.localeCompare(right, state.locale === "zh" ? "zh-CN" : "en"),
  );
}

function recordMatchesHumanReviewers(record, selectedValues) {
  if (!Array.isArray(selectedValues) || !selectedValues.length) return true;
  const names = recordReviewerNames(record);
  return selectedValues.some((value) =>
    value === UNREVIEWED_FILTER_VALUE ? names.length === 0 : names.includes(value),
  );
}

function renderReviewerFilterMenu(menu, records, selectedValues) {
  const counts = new Map();
  let unreviewedCount = 0;
  records.forEach((record) => {
    const names = recordReviewerNames(record);
    if (!names.length) unreviewedCount += 1;
    names.forEach((name) => counts.set(name, (counts.get(name) || 0) + 1));
  });
  const options = [
    { value: UNREVIEWED_FILTER_VALUE, label: t("reviewer.none"), count: unreviewedCount },
    ...[...counts.entries()]
      .sort(([left], [right]) => left.localeCompare(right, state.locale === "zh" ? "zh-CN" : "en"))
      .map(([name, count]) => ({ value: name, label: name, count })),
  ];
  return renderColumnFilterMenu(menu, options, selectedValues, t("filter.humanReviewer"));
}

function humanReviewerBadges(record) {
  const names = recordReviewerNames(record);
  if (!names.length) return `<span class="human-reviewer-empty">—</span>`;
  return names.map((name) =>
    `<span class="human-reviewer-badge">${escapeHtml(name)}</span>`,
  ).join("");
}

function humanReviewerCell(record) {
  const names = recordReviewerNames(record);
  return `<td><div class="human-reviewer-cell" title="${escapeAttr(names.join(" · "))}">${humanReviewerBadges(record)}</div></td>`;
}

function renderColumnFilterMenu(menu, options, selectedValues, label) {
  if (!menu) return Array.isArray(selectedValues) ? selectedValues : [];
  const available = new Set(options.map((option) => option.value));
  const selected = (Array.isArray(selectedValues) ? selectedValues : []).filter((value) => available.has(value));
  const selectedSet = new Set(selected);
  const popover = menu.querySelector(".column-filter-popover");
  popover.innerHTML = `
    <div class="column-filter-caption">${escapeHtml(t("filter.multiple"))}</div>
    <label class="column-filter-option column-filter-all">
      <input type="checkbox" data-filter-value="all" ${selected.length ? "" : "checked"} />
      <span>${escapeHtml(t("filter.allOptions"))}</span>
    </label>
    ${options.map((option) => `<label class="column-filter-option">
      <input type="checkbox" data-filter-value="${escapeAttr(option.value)}" ${selectedSet.has(option.value) ? "checked" : ""} />
      <span>${escapeHtml(option.label)}</span><small>${option.count}</small>
    </label>`).join("")}`;
  const count = menu.querySelector(".column-filter-count");
  count.hidden = selected.length === 0;
  count.textContent = String(selected.length);
  menu.classList.toggle("is-active", selected.length > 0);
  const summary = menu.querySelector("summary");
  const accessibleLabel = selected.length
    ? `${label}，${t("filter.selected", { count: selected.length })}`
    : label;
  summary.title = accessibleLabel;
  summary.setAttribute("aria-label", accessibleLabel);
  return selected;
}

function updateUnitSortControl(control, sortState) {
  if (!control) return;
  control.value = sortState;
  control.classList.toggle("is-active", sortState !== "none");
  const stateLabel = t(`sort.${sortState}`);
  control.title = t("sort.unitState", { state: stateLabel });
  control.setAttribute("aria-label", control.title);
  control.closest("th")?.setAttribute("aria-sort", sortState === "none" ? "none" : sortState);
}

const UNIT_SORT_ORDER = new Map([
  "fm", "pm", "nm", "um", "mm", "m",
  "fg/ml", "pg/ml", "ng/ml", "ug/ml", "mg/ml", "g/l",
  "%",
].map((unit, index) => [unit, index]));

function normalizedUnit(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[μµ]/g, "u")
    .replace(/\s+/g, "")
    .replace(/per/g, "/");
}

function compareUnits(left, right) {
  const leftUnit = normalizedUnit(effectiveValue(left, "ic50_unit"));
  const rightUnit = normalizedUnit(effectiveValue(right, "ic50_unit"));
  if (!leftUnit && !rightUnit) return 0;
  if (!leftUnit) return 1;
  if (!rightUnit) return -1;
  const leftRank = UNIT_SORT_ORDER.get(leftUnit);
  const rightRank = UNIT_SORT_ORDER.get(rightUnit);
  if (leftRank != null || rightRank != null) {
    if (leftRank == null) return 1;
    if (rightRank == null) return -1;
    if (leftRank !== rightRank) return leftRank - rightRank;
  }
  return leftUnit.localeCompare(rightUnit, "en", { numeric: true });
}

function sortRecordsByUnit(records, sortState) {
  if (sortState === "none") return records;
  return records
    .map((record, index) => ({ record, index }))
    .sort((left, right) => {
      const leftUnit = normalizedUnit(effectiveValue(left.record, "ic50_unit"));
      const rightUnit = normalizedUnit(effectiveValue(right.record, "ic50_unit"));
      if (!leftUnit && rightUnit) return 1;
      if (leftUnit && !rightUnit) return -1;
      const compared = compareUnits(left.record, right.record);
      if (!compared) return left.index - right.index;
      return sortState === "descending" ? -compared : compared;
    })
    .map(({ record }) => record);
}

function assayKeywordText(record) {
  return assayKeywordValues(record).map(localizedAssayKeyword).join(" · ");
}

function assayKeywordReviewReasons(record) {
  const issues = record.assay_keyword_unmatched || [];
  const dimension = issue => issue.category === "species" ? "antigen_species" : issue.category;
  const detailedDimensions = new Set(issues.filter(issue => issue.detail_zh || issue.detail_en).map(dimension));
  return [...new Set(issues.filter(issue => issue.detail_zh || issue.detail_en || !detailedDimensions.has(dimension(issue))).map((issue) => {
    const categoryKey = `experiment.dimension.${issue.category}`;
    const category = TRANSLATIONS[state.locale]?.[categoryKey] ? t(categoryKey) : issue.category;
    const reasonKey = `experiment.reason.${issue.reason}`;
    const detail = state.locale === "en" ? issue.detail_en || issue.detail_zh : issue.detail_zh || issue.detail_en;
    const reason = detail || (TRANSLATIONS[state.locale]?.[reasonKey] ? t(reasonKey) : issue.reason);
    const value = Array.isArray(issue.value) ? issue.value.join(", ") : String(issue.value || "");
    return `${category}：${reason}${value ? ` (${value})` : ""}`;
  }))];
}

function experimentFactLabel(record, fact) {
  const category = fact.field === "antigen_species" ? "species" : fact.field;
  const keyword = assayKeywordValues(record).find(item => item.category === category && item.key === fact.value);
  return keyword ? localizedAssayKeyword(keyword).replace(/^(靶蛋白|细胞来源|Antigen|Cell host)[：:]\s*/, "") : fact.value;
}

function assayKeywordBadges(record) {
  const values = assayKeywordValues(record);
  const review = record.assay_keyword_status === "needs_review"
    ? `<span class="experiment-keyword-badge keyword-review" title="${escapeAttr(assayKeywordReviewReasons(record).join("\n"))}">${escapeHtml(t("experiment.keywordReview"))}</span>` : "";
  if (!values.length) return review || `<span class="experiment-keyword-empty">—</span>`;
  return values.map((item) => `<span class="experiment-keyword-badge keyword-${escapeAttr(item.category || "other")}" title="${escapeAttr(item.target_id ? `${item.target_id} · v${item.library_version}` : "")}">${escapeHtml(localizedAssayKeyword(item))}</span>`).join("") + review;
}

function overviewKeywordCell(record) {
  return `<td><button type="button" class="data-cell experiment-keyword-cell"
    data-patent-id="${escapeAttr(record._patent_id)}" data-record-id="${escapeAttr(record.record_id)}"
    data-field="assay_keywords" title="${escapeAttr(assayKeywordText(record))}">${assayKeywordBadges(record)}</button></td>`;
}

function overviewReviewStatusCell(record) {
  return `<td><button type="button" class="data-cell review-check-cell"
    data-patent-id="${escapeAttr(record._patent_id)}" data-record-id="${escapeAttr(record.record_id)}"
    data-field="link_status" title="${escapeAttr(reviewCheckTitle(record))}">${reviewCheckBadges(record)}</button></td>`;
}

async function onDatasetOverviewClick(event) {
  const cell = event.target.closest("button[data-patent-id][data-record-id]");
  if (!cell) return;
  const patentId = cell.dataset.patentId;
  if (patentId !== state.activePatentId) {
    const loaded = await activatePatent(patentId);
    if (!loaded) return;
  }
  state.selectedRecordId = cell.dataset.recordId;
  state.selectedField = cell.dataset.field || "antibody_id";
  state.tab = DEFAULT_TAB_BY_FIELD[state.selectedField] || "ocr";
  state.viewMode = "page";
  state.selectedEvidenceIndex = 0;
  selectEvidenceForCurrentField();
  renderApp();
  switchAppPage("review");
}

function canDeletePatent(patent) {
  return !patent.builtin && ["completed", "failed"].includes(patent.status);
}

function renderPatentSelection() {
  const selectable = state.patents.filter(canDeletePatent);
  const selectableIds = new Set(selectable.map((patent) => patent.document_id));
  state.selectedPatentIds = new Set([...state.selectedPatentIds].filter((id) => selectableIds.has(id)));
  const count = state.selectedPatentIds.size;
  dom.patentSelectionCount.textContent = t(count ? "intake.selectedCount" : "intake.noneSelected", { count });
  dom.deleteSelectedPatents.disabled = !count || state.patentDeleteInProgress;
  dom.deleteSelectedPatents.textContent = t(state.patentDeleteInProgress ? "intake.deleting" : "intake.deleteSelected");
  dom.selectAllPatents.disabled = !selectable.length || state.patentDeleteInProgress;
  dom.selectAllPatents.checked = selectable.length > 0 && count === selectable.length;
  dom.selectAllPatents.indeterminate = count > 0 && count < selectable.length;
  dom.patentRows.querySelectorAll("input[data-patent-select]").forEach((input) => {
    input.checked = state.selectedPatentIds.has(input.dataset.patentSelect);
    input.closest("tr").classList.toggle("is-selected", input.checked);
  });
}

function onPatentSelectionChange(event) {
  const strategy = event.target.closest("select[data-patent-strategy]");
  if (strategy) {
    state.patentEngineChoices[strategy.dataset.patentStrategy] = strategy.value;
    renderPatentTable();
    return;
  }
  const input = event.target.closest("input[data-patent-select]");
  if (!input || input.disabled || state.patentDeleteInProgress) return;
  const patent = state.patents.find((item) => item.document_id === input.dataset.patentSelect);
  if (!patent || !canDeletePatent(patent)) return;
  if (input.checked) state.selectedPatentIds.add(patent.document_id);
  else state.selectedPatentIds.delete(patent.document_id);
  renderPatentSelection();
}

function onSelectAllPatents(event) {
  if (state.patentDeleteInProgress) return;
  state.selectedPatentIds = new Set(event.target.checked ? state.patents.filter(canDeletePatent).map((patent) => patent.document_id) : []);
  renderPatentSelection();
}

function clearDeletedPatentState(documentIds) {
  const deleted = new Set(documentIds);
  state.patentMutationVersion += 1;
  state.patents = state.patents.filter((patent) => !deleted.has(patent.document_id));
  state.allRecords = state.allRecords.filter((record) => !deleted.has(record._patent_id || record.document_id));
  state.humanReviews = state.humanReviews.filter((review) => !deleted.has(review.document_id));
  for (const name of ["corrections", "reviews"]) {
    state[name] = Object.fromEntries(Object.entries(state[name]).filter(([, entry]) => !deleted.has(entry?.document_id)));
    saveText(`sda.studio.${name}`, JSON.stringify(state[name]));
  }
  documentIds.forEach((id) => state.selectedPatentIds.delete(id));
  if (deleted.has(state.pendingReviewPatentId)) {
    state.pendingReviewPatentId = null;
    state.pendingReviewAction = null;
  }
  const activeDeleted = deleted.has(state.activePatentId);
  if (activeDeleted) {
    stopSequenceSpeech({ updateUi: false });
    state.patentActivationVersion += 1;
    Object.assign(state, {activePatentId: null, activePatent: null, records: [], contexts: [], candidates: [],
      classifications: [], measurements: [], comparisons: [], validations: [], ocr: [], template: {}, runtime: {}, pages: {page_count: 0, pages: []},
      selectedRecordId: null, evidence: [], selectedEvidenceIndex: 0, currentPage: 1, continuousPagesReady: false});
    state.selectedRecordIds.clear();
    dom.continuousPages.innerHTML = "";
    dom.evidenceImage.removeAttribute("src");
    dom.evidenceImage.dataset.src = "";
    buildIndexes();
  }
  return activeDeleted;
}

async function deleteSelectedPatents() {
  if (state.patentDeleteInProgress) return;
  const selected = state.patents.filter((patent) => state.selectedPatentIds.has(patent.document_id) && canDeletePatent(patent));
  if (!selected.length) return;
  const names = selected.slice(0, 8).map((patent) => patent.document_id);
  if (selected.length > names.length) names.push(t("intake.moreSelected", {count: selected.length - names.length}));
  if (!window.confirm(t("intake.deleteConfirm", {count: selected.length, names: names.join("\n")}))) return;
  state.patentDeleteInProgress = true;
  renderPatentTable();
  try {
    const response = await fetch(PATENT_DELETE_API, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_ids: selected.map((patent) => patent.document_id)})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const activeDeleted = clearDeletedPatentState(payload.deleted_document_ids);
    // The delete already succeeded. Refresh failures must not present it as
    // a failed deletion or restore cached rows from the removed patents.
    if (activeDeleted) {
      const fallback = state.patents.find(hasReviewableResult);
      if (fallback) {
        try { await activatePatent(fallback.document_id); }
        catch (error) { console.warn("Unable to load replacement patent", error); }
      }
    }
    try { await refreshAllRecords(); }
    catch (error) { console.warn("Unable to refresh remaining dataset", error); }
    showToast(t("intake.deleteCompleted", {count: payload.deleted_count}), 4500);
  } catch (error) {
    showToast(t("intake.deleteFailed", {error: error.message}), 6500);
  } finally {
    state.patentDeleteInProgress = false;
    renderApp();
    schedulePatentPolling();
  }
}

function patentEtaMarkup(estimate) {
  if (!estimate) return "";
  const elapsed = t("intake.etaElapsed", { duration: formatEtaDuration(estimate.elapsedSeconds, state.locale) });
  const help = t("intake.etaHelp");
  if (estimate.recalculating) {
    return `<div class="job-eta" title="${escapeAttr(help)}"><span>${escapeHtml(elapsed)}</span><span class="job-eta-note">${escapeHtml(t("intake.etaRecalculating"))}</span></div>`;
  }
  const remaining = t("intake.etaRemaining", { duration: formatEtaRange(estimate.lowSeconds, estimate.highSeconds, state.locale) });
  const note = estimate.queued && estimate.queueAhead
    ? t("intake.etaQueue", { count: estimate.queueAhead })
    : estimate.preliminary ? "" : t("intake.etaLive");
  return `<div class="job-eta" title="${escapeAttr(help)}"><span>${escapeHtml(elapsed)} · ${escapeHtml(remaining)}</span>${note ? `<span class="job-eta-note">${escapeHtml(note)}</span>` : ""}</div>`;
}

function pdfFormKind(patent) {
  const kind = patent.pdf_form?.kind;
  return PDF_FORM_KINDS.includes(kind) ? kind : "unknown";
}

function pdfFormMarkup(patent) {
  const kind = pdfFormKind(patent);
  const counts = patent.pdf_form?.page_counts;
  const explanation = [t(`pdfForm.${kind}Hint`)];
  if (counts) explanation.push(t("pdfForm.counts", counts));
  return `<span class="pdf-form-badge pdf-form-${kind}" title="${escapeAttr(explanation.join("\n"))}" tabindex="0">${escapeHtml(t(`pdfForm.${kind}`))}<span class="pdf-form-tooltip" role="tooltip">${escapeHtml(explanation.join("\n"))}</span></span>`;
}

function patentTextStatus(patent) {
  const status = patent.patent_text?.status;
  return ["found", "not_found", "error", "unknown", "checking"].includes(status) ? status : "unchecked";
}

function patentTextUrl(patent) {
  const url = patent.patent_text?.url || "";
  return patentTextStatus(patent) === "found"
    && /^https:\/\/patents\.google\.com\/patent\/[A-Z0-9]+(?:\/[a-z-]+)?#description$/.test(url) ? url : "";
}

function patentTextMarkup(patent) {
  const status = patentTextStatus(patent);
  const result = patent.patent_text || {};
  const url = patentTextUrl(patent);
  const time = result.checked_at ? new Date(result.checked_at).toLocaleString() : "—";
  const hint = ["found", "not_found", "error", "unknown"].includes(status)
    ? t(`patentText.${status}Hint`, { time }) : t("patentText.help");
  const badge = url
    ? `<a class="patent-text-badge patent-text-found" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer" title="${escapeAttr(hint)}">${escapeHtml(t("patentText.found"))}</a>`
    : `<span class="patent-text-badge patent-text-${status}" title="${escapeAttr(hint)}">${escapeHtml(t(`patentText.${status}`))}</span>`;
  const number = url && result.publication_number ? `<small>${escapeHtml(result.publication_number)}</small>` : "";
  const retry = status === "checking" ? "" : `<button class="patent-text-check" type="button" data-patent-id="${escapeAttr(patent.document_id)}" data-action="check-text" title="${escapeAttr(hint)}">${escapeHtml(t(status === "unchecked" ? "patentText.check" : "patentText.retry"))}</button>`;
  return `${badge}${number}${retry}`;
}

function patentActionMarkup(patent) {
  const terminal = ["completed", "failed"].includes(patent.status);
  const controls = [];
  const button = (action, key, disabled = false) => `<button type="button" class="button patent-review-button" data-patent-id="${escapeAttr(patent.document_id)}" data-action="${action}" ${disabled || state.patentDeleteInProgress ? "disabled" : ""}>${escapeHtml(t(key))}</button>`;
  if (hasReviewableResult(patent)) controls.push(button("review", "intake.review"));
  const canRetry = patent.retry_available === true
    || (patent.retry_available == null && !patent.builtin);
  if (terminal && canRetry) controls.push(button("retry", "intake.retry"));
  if (terminal && patent.analyze_available === true) controls.push(button("analyze", "intake.analyze"));
  if (patent.artifacts?.structured_json) {
    controls.push(`<a class="button patent-review-button" href="${escapeAttr(new URL(patent.artifacts.structured_json, new URL("../", location.href)).href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t("intake.structuredDocument"))}</a>`);
  }
  if (patent.log_available === true || patent.log_url || (!patent.builtin && patent.log_available !== false)) {
    controls.push(button("log", "intake.log"));
  }
  if (!controls.length) controls.push(button("review", "intake.waiting", true));
  return `<div class="patent-action-buttons">${controls.join("")}</div>`;
}

function patentStrategyMarkup(patent) {
  const actual = patent.extraction_engine || "dataagent1";
  const selected = state.patentEngineChoices[patent.document_id] || actual;
  const available = patent.comparison_available ?? Boolean(patent.artifacts?.structured_json && patent.pipeline_available);
  const busy = state.pendingPatentComparisons.has(patent.document_id) || state.patentDeleteInProgress;
  return `<div class="patent-strategy-controls">
    <select data-patent-strategy="${escapeAttr(patent.document_id)}" aria-label="${escapeAttr(t("field.agentStrategy"))}" title="${escapeAttr(t("intake.strategyHelp"))}" ${busy ? "disabled" : ""}>
      <option value="dataagent1" ${selected === "dataagent1" ? "selected" : ""}>dataagent1</option>
      <option value="dataagent2" ${selected === "dataagent2" ? "selected" : ""}>dataagent2</option>
    </select>
    <small>${escapeHtml(t("intake.currentStrategy", {engine: actual}))}</small>
    <button type="button" class="button patent-review-button" data-patent-id="${escapeAttr(patent.document_id)}" data-action="compare" ${busy || !available ? "disabled" : ""}>${escapeHtml(t("intake.compare"))}</button>
    ${patent.comparison_source_id ? `<small>${escapeHtml(t("intake.comparisonRun"))}</small>` : ""}
  </div>`;
}

async function comparePatent(documentId) {
  const patent = state.patents.find(p => p.document_id === documentId);
  const engine = state.patentEngineChoices[documentId] || patent?.extraction_engine || "dataagent1";
  state.pendingPatentComparisons.add(documentId);
  renderPatentTable();
  try {
    const health = await fetchJson(serverAssetUrl("api/health"), 5000);
    if (!health.agent_comparison) throw new Error(t("intake.engineUnavailable"));
    const response = await fetch(PATENT_COMPARE_API, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_id: documentId, extraction_engine: engine})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.patents = [...state.patents.filter(p => p.document_id !== payload.patent.document_id), payload.patent];
    showToast(t("intake.compareQueued", {engine}), 5000);
    schedulePatentPolling();
  } catch (error) {
    showToast(t("intake.importFailed", {error: error.message}), 6500);
  } finally {
    state.pendingPatentComparisons.delete(documentId);
    renderPatentTable();
  }
}

async function checkPatentText(documentId) {
  const mutationVersion = state.patentMutationVersion;
  try {
    const response = await fetch(PATENT_TEXT_API, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (mutationVersion !== state.patentMutationVersion) return;
    state.patents = state.patents.map(patent => patent.document_id === documentId
      ? { ...patent, patent_text: payload.patent_text } : patent);
    schedulePatentPolling();
  } catch (error) {
    showToast(t("patentText.requestFailed", { error: error.message }), 6000);
  } finally {
    renderPatentTable();
  }
}

function renderPatentTable() {
  if (!dom.patentRows) return;
  const now = Date.now();
  const estimates = jobEtaEstimator.estimateAll(state.patents, now);
  dom.patentCount.textContent = t("intake.patentCount", { count: state.patents.length });
  dom.patentRows.innerHTML = state.patents.map((patent) => {
    const status = patent.status || "queued";
    const complete = status === "completed";
    const reviewable = hasReviewableResult(patent);
    const progress = Math.max(0, Math.min(100, Number(patent.progress || 0)));
    const stage = t(`intake.stage.${patent.stage || status}`);
    const current = Number(patent.stage_current || 0);
    const total = Number(patent.stage_total || 0);
    const stageProgress = total > 0
      ? t(patent.stage === "reader" ? "intake.chunkProgress" : patent.stage === "sequence_extract" ? "intake.batchProgress"
        : ["agent", "classify", "ocr", "extraction"].includes(patent.stage) ? "intake.tableProgress"
        : ["template", "tst", "construct"].includes(patent.stage) ? "intake.batchProgress" : "intake.stageProgress", { current, total })
      : "";
    const stageLabel = complete ? "" : stageProgress ? `${stage} · ${stageProgress}` : stage;
    const ocrBranches = patent.stage === "ocr" && patent.ocr_branches
      ? t("intake.ocrBranches", {
        text: t(`intake.status.${patent.ocr_branches.text || "queued"}`),
        table: t(`intake.status.${patent.ocr_branches.table || "queued"}`),
      }) : "";
    const errorTitle = status === "failed" ? patent.error || "" : "";
    const templateReview = complete && patent.review_required === true;
    const templateLabel = templateReview
      ? t("intake.templateReview")
      : patent.template_status === "validated" ? t("intake.templateValidated") : "";
    const statusTitle = templateReview
      ? `${templateLabel} · ${Number(patent.quality_warning_count || 0)} warning(s)`
      : errorTitle;
    const publishedCount = Number(patent.published_record_count || 0);
    const published = patent.dataset_published === true;
    const deletable = canDeletePatent(patent);
    const selectionTitle = patent.builtin ? t("intake.builtinProtected") : !deletable ? t("intake.processingProtected") : t("intake.selectPatent", {patent: patentDisplayName(patent)});
    return `<tr class="patent-row ${reviewable ? "is-clickable" : ""}" data-patent-id="${escapeAttr(patent.document_id)}">
      <td class="patent-select-column" title="${escapeAttr(selectionTitle)}"><input type="checkbox" data-patent-select="${escapeAttr(patent.document_id)}" aria-label="${escapeAttr(selectionTitle)}" ${deletable && !state.patentDeleteInProgress ? "" : "disabled"} /></td>
      <td><button type="button" class="patent-name-button" data-patent-id="${escapeAttr(patent.document_id)}" title="${escapeAttr(patentDisplayName(patent))}" ${reviewable && !state.patentDeleteInProgress ? "" : "disabled"}>${escapeHtml(patentDisplayName(patent))}</button><small title="${escapeAttr(patent.filename || "")}">${escapeHtml(patent.filename || "")}</small></td>
      <td class="patent-strategy-cell">${patentStrategyMarkup(patent)}</td>
      <td class="number-column">${Number(patent.record_count || 0)}</td>
      <td class="number-column">${Number(patent.page_count || 0) || "—"}</td>
      <td class="number-column processing-metric-cell">${timeMetricMarkup(patent.processing_metrics, patent.document_id, state.locale, now)}</td>
      <td class="number-column processing-metric-cell">${tokenMetricMarkup(patent.processing_metrics, patent.document_id, state.locale)}</td>
      <td class="pdf-form-cell">${pdfFormMarkup(patent)}</td>
      <td class="patent-text-cell">${patentTextMarkup(patent)}</td>
      <td title="${escapeAttr(statusTitle)}">
        <div class="job-status-row"><span class="job-status job-${escapeAttr(status)}">${escapeHtml(t(`intake.status.${complete && patent.semantic_status === "partial" ? "partial" : status}`))}</span><small>${escapeHtml(stageLabel)}</small>${templateLabel ? `<small>${escapeHtml(templateLabel)}</small>` : ""}${patent.llm_model ? `<small>${escapeHtml(`${patent.llm_model} · ${patent.llm_effort || ""}`)}</small>` : ""}</div>
        ${complete ? "" : `<div class="job-progress ${status === "processing" ? "is-active" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><span style="width:${progress}%"></span></div>`}
        ${ocrBranches ? `<small>${escapeHtml(ocrBranches)}</small>` : ""}
        ${patentEtaMarkup(estimates.get(patent.document_id))}
        ${errorTitle ? `<small class="patent-task-error">${escapeHtml(errorTitle)}</small>` : ""}
      </td>
      <td class="dataset-membership-cell">
        <span class="dataset-published-pill${published ? " is-published" : ""}">${escapeHtml(
          published
            ? t("intake.datasetPublished", { count: publishedCount })
            : t("intake.datasetNotPublished"),
        )}</span>
      </td>
      <td>${patentActionMarkup(patent)}</td>
    </tr>`;
  }).join("");
  renderPatentSelection();
  processingMetricTooltips?.refresh();
}

async function publishSelectedRecords() {
  const documentId = state.activePatentId;
  const datasetId = state.publishDatasetId;
  const recordIds = [...state.selectedRecordIds];
  if (!documentId || !recordIds.length || state.publishInProgress || !datasetId) return;
  state.publishInProgress = true;
  renderReviewControls();
  try {
    const response = await fetch(PATENT_PUBLISH_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId, record_ids: recordIds, dataset_id: datasetId }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.datasetMutationVersion += 1;
    state.patents = state.patents.map((patent) =>
      patent.document_id === documentId ? { ...patent, ...payload.patent } : patent,
    );
    if (state.activePatentId === documentId) {
      state.activePatent = state.patents.find((patent) => patent.document_id === documentId)
        || state.activePatent;
    }
    await refreshAllRecords();
    renderPatentTable();
    renderReviewControls();
    showToast(t("review.publishCompleted", {
      folder: state.datasetFolders.find(folder => folder.dataset_id === datasetId)?.name || datasetId,
      patent: documentId,
      previous: Number(payload.previous_record_count || 0),
      count: Number(payload.published_record_count || 0),
    }), 4800);
  } catch (error) {
    showToast(t("review.publishFailed", { error: error.message }), 6500);
  } finally {
    state.publishInProgress = false;
    renderReviewControls();
  }
}

async function onPatentTableClick(event) {
  const button = event.target.closest("button[data-patent-id]");
  if (!button || button.disabled) return;
  if (button.dataset.action === "compare") {
    await comparePatent(button.dataset.patentId);
    return;
  }
  if (button.dataset.action === "log") {
    await openPatentLog(button.dataset.patentId);
    return;
  }
  if (button.dataset.action === "check-text") {
    button.disabled = true;
    await checkPatentText(button.dataset.patentId);
    return;
  }
  if (button.dataset.action === "analyze") {
    button.disabled = true;
    await analyzePatent(button.dataset.patentId);
    return;
  }
  if (button.dataset.action === "retry") {
    button.disabled = true;
    await retryPatent(button.dataset.patentId);
    return;
  }
  if (state.activePatentId === button.dataset.patentId && state.records.length) {
    switchAppPage("review");
    return;
  }
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = t("intake.reviewLoading");
  button.setAttribute("aria-busy", "true");
  try {
    const loaded = await activatePatent(button.dataset.patentId);
    if (loaded) switchAppPage("review");
  } catch (error) {
    showToast(t("intake.reviewFailed", {error: error.message}), 7000);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
    button.removeAttribute("aria-busy");
  }
}

function patentLogUrl(patent) {
  const fallback = serverAssetUrl(`api/patents/${encodeURIComponent(patent.document_id)}/log`);
  if (!patent.log_url) return fallback;
  try {
    const value = String(patent.log_url);
    const url = new URL(value.startsWith(SERVER_ROOT.pathname) && SERVER_ROOT.pathname !== "/"
      ? value : value.replace(/^\/+/, ""), SERVER_ROOT);
    // A local API path must keep the IDE proxy prefix; never fetch external log URLs.
    return url.origin === SERVER_ROOT.origin && url.pathname.startsWith(SERVER_ROOT.pathname)
      ? url.href : fallback;
  } catch {
    return fallback;
  }
}

async function openPatentLog(documentId) {
  const patent = state.patents.find((item) => item.document_id === documentId);
  if (!patent) return;
  state.logPatentId = documentId;
  dom.patentLogDocument.textContent = patentDisplayName(patent);
  dom.patentLogContent.textContent = "";
  if (!dom.patentLogDialog.open) dom.patentLogDialog.showModal();
  await refreshPatentLog();
}

async function refreshPatentLog() {
  const patent = state.patents.find((item) => item.document_id === state.logPatentId);
  if (!patent || !dom.patentLogDialog.open) return;
  const requestVersion = ++state.logRequestVersion;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  dom.patentLogRefresh.disabled = true;
  dom.patentLogStatus.textContent = t("intake.logLoading");
  try {
    const response = await fetch(patentLogUrl(patent), {cache: "no-store", signal: controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const content = await response.text();
    if (requestVersion !== state.logRequestVersion) return;
    dom.patentLogContent.textContent = content;
    dom.patentLogStatus.textContent = t(content.trim() ? "intake.logReady" : "intake.logEmpty");
    dom.patentLogContent.scrollTop = dom.patentLogContent.scrollHeight;
  } catch (error) {
    if (requestVersion !== state.logRequestVersion) return;
    dom.patentLogStatus.textContent = t("intake.logFailed", {error: error.message});
  } finally {
    clearTimeout(timeout);
    if (requestVersion === state.logRequestVersion) dom.patentLogRefresh.disabled = false;
  }
}

async function retryPatent(documentId) {
  try {
    const patent = state.patents.find(p => p.document_id === documentId);
    const engine = state.patentEngineChoices[documentId] || patent?.extraction_engine || "dataagent1";
    if (engine !== (patent?.extraction_engine || "dataagent1")) return await comparePatent(documentId);
    if (engine === "dataagent2") {
      const health = await fetchJson(serverAssetUrl("api/health"), 5000);
      if (!health.extraction_engines?.includes(engine)) throw new Error(t("intake.engineUnavailable"));
    }
    const response = await fetch(PATENT_RETRY_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId, extraction_engine: engine }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const retried = payload.patent;
    state.patents = state.patents.map((item) =>
      item.document_id === retried.document_id ? retried : item,
    );
    state.pendingReviewPatentId = retried.document_id;
    state.pendingReviewAction = "pipeline";
    renderPatentTable();
    showToast(t("intake.retryQueued"), 4000);
    schedulePatentPolling();
  } catch (error) {
    showToast(t("intake.importFailed", { error: error.message }), 6500);
  } finally {
    renderPatentTable();
  }
}

async function analyzePatent(documentId) {
  try {
    const patent = state.patents.find(p => p.document_id === documentId);
    const engine = state.patentEngineChoices[documentId] || patent?.extraction_engine || "dataagent1";
    if (engine !== (patent?.extraction_engine || "dataagent1")) return await comparePatent(documentId);
    if (engine === "dataagent2") {
      const health = await fetchJson(serverAssetUrl("api/health"), 5000);
      if (!health.extraction_engines?.includes(engine)) throw new Error(t("intake.engineUnavailable"));
    }
    const response = await fetch(PATENT_ANALYZE_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId, extraction_engine: engine }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.patents = state.patents.map((item) => item.document_id === documentId ? payload.patent : item);
    state.pendingReviewPatentId = documentId;
    state.pendingReviewAction = "semantic_refresh";
    showToast(t("intake.analyzeQueued"), 4000);
    schedulePatentPolling();
  } catch (error) {
    showToast(t("intake.importFailed", { error: error.message }), 6500);
  } finally {
    renderPatentTable();
  }
}

function setImportFile(file) {
  if (!file) {
    state.importFile = null;
    dom.patentNameInput.value = "";
    dom.selectedPdfName.textContent = t("intake.choosePdf");
    updateImportButton();
    return;
  }
  if (!(file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"))) {
    state.importFile = null;
    dom.patentNameInput.value = "";
    dom.selectedPdfName.textContent = t("intake.choosePdf");
    updateImportButton();
    showToast(t("intake.invalidPdf"), 3500);
    return;
  }
  if (file.size > 300 * 1024 * 1024) {
    state.importFile = null;
    dom.patentNameInput.value = "";
    dom.selectedPdfName.textContent = t("intake.choosePdf");
    updateImportButton();
    showToast(t("intake.fileTooLarge"), 3500);
    return;
  }
  state.importFile = file;
  dom.patentNameInput.value = deriveDocumentId(file.name);
  dom.selectedPdfName.textContent = t("intake.fileSelected", { name: file.name, size: formatFileSize(file.size) });
  updateImportButton();
}

function updateImportButton() {
  const validName = /^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$/.test(dom.patentNameInput.value.trim());
  dom.startAgentButton.disabled = !state.importFile || !validName || dom.startAgentButton.classList.contains("loading");
}

async function startPatentImport(event) {
  event.preventDefault();
  updateImportButton();
  if (dom.startAgentButton.disabled) return;
  const documentId = dom.patentNameInput.value.trim();
  const number = patentNumber({filename: state.importFile.name, document_id: documentId});
  if (state.patents.some(patent => patent.document_id === documentId || (number && patentNumber(patent) === number))) {
    showToast(t("intake.duplicatePatent", {patent: number || documentId}), 5000);
    return;
  }
  const form = new FormData();
  form.append("document_id", documentId);
  form.append("extraction_engine", dom.patentEngineSelect.value);
  form.append("pdf", state.importFile, state.importFile.name);
  dom.startAgentButton.classList.add("loading");
  dom.startAgentButton.disabled = true;
  dom.startAgentButton.lastElementChild.textContent = t("intake.uploading");
  try {
    if (dom.patentEngineSelect.value === "dataagent2") {
      const health = await fetchJson(serverAssetUrl("api/health"), 5000);
      if (!health.extraction_engines?.includes("dataagent2")) throw new Error(t("intake.engineUnavailable"));
    }
    const response = await fetch(PATENT_IMPORT_API, { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const imported = payload.patent;
    state.patents = [...state.patents.filter((item) => item.document_id !== imported.document_id), imported];
    state.pendingReviewPatentId = imported.document_id;
    state.pendingReviewAction = "pipeline";
    state.importFile = null;
    dom.patentImportForm.reset();
    dom.selectedPdfName.textContent = t("intake.choosePdf");
    renderPatentTable();
    showToast(t("intake.queued"), 4000);
    schedulePatentPolling();
  } catch (error) {
    showToast(t("intake.importFailed", { error: error.message }), 6500);
  } finally {
    dom.startAgentButton.classList.remove("loading");
    dom.startAgentButton.lastElementChild.textContent = t("intake.startAgent");
    updateImportButton();
  }
}

function formatFileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderGrid() {
  state.experimentLabelFilter = renderExperimentLabelFilterMenu(
    dom.experimentLabelFilter, state.records, state.experimentLabelFilter,
  );
  state.keywordFilter = renderKeywordFilterMenu(dom.keywordFilter, state.records, state.keywordFilter);
  state.status = renderStatusFilterMenu(
    dom.statusFilter,
    state.records,
    state.status,
  );
  state.reviewerFilter = renderReviewerFilterMenu(
    dom.reviewerFilter,
    state.records,
    state.reviewerFilter,
  );
  updateUnitSortControl(dom.unitSort, state.unitSort);
  const records = filteredRecords();
  dom.recordCount.textContent = t("dataset.records", { count: records.length });
  if (!records.length) {
    dom.recordRows.innerHTML = `<tr class="empty-row"><td colspan="12">${escapeHtml(t("dataset.empty"))}</td></tr>`;
    renderReviewControls();
    return;
  }
  dom.recordRows.innerHTML = records
    .map((record, index) => {
      const activeRow = record.record_id === state.selectedRecordId ? " active-row" : "";
      const evidenceCount = buildEvidence(record, null).length;
      return `
        <tr class="${activeRow}" data-record-id="${escapeAttr(record.record_id)}">
          ${recordSelectionCell(record)}
          ${gridCell(record, index + 1, "_index", "index numeric")}
          ${gridCell(record, effectiveValue(record, "antibody_id"), "antibody_id")}
          ${gridCell(record, effectiveValue(record, "vh"), "vh", "sequence-cell")}
          ${gridCell(record, effectiveValue(record, "vl"), "vl", "sequence-cell")}
          ${gridCell(record, formatAssayValue(record), "ic50", "numeric")}
          ${gridCell(record, effectiveValue(record, "ic50_unit"), "ic50_unit")}
          ${gridCell(record, effectiveValue(record, "experiment_group"), "experiment_group")}
          ${gridKeywordCell(record)}
          ${gridStatusCell(record)}
          ${humanReviewerCell(record)}
          ${gridCell(record, evidenceCount, "_evidence", "numeric")}
        </tr>`;
    })
    .join("");
  renderReviewControls();
}

function gridKeywordCell(record) {
  const active = record.record_id === state.selectedRecordId && state.selectedField === "assay_keywords";
  return `<td><button type="button" class="data-cell experiment-keyword-cell ${active ? "active-cell" : ""}"
    data-record-id="${escapeAttr(record.record_id)}" data-field="assay_keywords"
    title="${escapeAttr(assayKeywordText(record))}">${assayKeywordBadges(record)}</button></td>`;
}

function recordSelectionCell(record) {
  const checked = state.selectedRecordIds.has(record.record_id);
  return `<td class="row-selection-cell"><input class="row-checkbox" type="checkbox"
    data-action="select-record" data-record-id="${escapeAttr(record.record_id)}"
    aria-label="${escapeAttr(t("review.selectedCount", { count: 1 }))}" ${checked ? "checked" : ""} /></td>`;
}

function selectedReparseFields() {
  return [...document.querySelectorAll('input[name="reparse-field"]:checked')]
    .map((input) => input.value);
}

function renderReviewControls() {
  if (!dom.reviewPublishButton) return;
  renderDatasetPublishTarget();
  const patent = state.patents.find((item) => item.document_id === state.activePatentId)
    || state.activePatent;
  const busy = ["queued", "processing"].includes(patent?.status);
  const complete = patent?.status === "completed";
  const selectedCount = state.selectedRecordIds.size;
  const fields = selectedReparseFields();
  const visibleIds = filteredRecords().map((record) => record.record_id);
  const selectedVisible = visibleIds.filter((recordId) => state.selectedRecordIds.has(recordId)).length;

  dom.reviewPublishButton.disabled = !complete || busy || selectedCount === 0
    || state.publishInProgress || !state.publishDatasetId;
  dom.reviewPublishButton.classList.toggle("loading", state.publishInProgress);
  dom.reviewPublishButton.lastElementChild.textContent = t(
    state.publishInProgress ? "review.publishing" : "review.publishSelected",
  );
  dom.reviewPublishButton.title = t("review.publishSelectedTitle");
  const reparsing = busy && patent?.operation === "reparse";
  const unavailableReason = patent?.reparse_available === false ? "review.reparseUnavailable"
    : state.reparseSubmitting ? "review.reparseSubmitting"
    : busy ? "review.reparseBusy"
    : !complete ? "review.reparseWaiting"
    : selectedCount === 0 ? "review.reparseChooseRecords"
    : fields.length === 0 ? "review.reparseChooseFields"
    : null;
  const hint = unavailableReason ? t(unavailableReason)
    : t("review.reparseReady", { count: selectedCount,
      fields: fields.map(fieldLabel).join(state.locale === "en" ? ", " : "、") });
  dom.reviewSelectionCount.textContent = unavailableReason
    ? t(unavailableReason) : t("review.selectedCount", { count: selectedCount });
  dom.reparseFieldSummary.textContent = t("review.reparseFieldSummary", { count: fields.length });
  dom.reparseFieldHint.textContent = hint;
  dom.reparseSelectedRecords.disabled = Boolean(unavailableReason);
  dom.reparseSelectedRecords.title = hint;
  dom.reparseSelectedRecords.classList.toggle("loading", state.reparseSubmitting || reparsing);
  dom.reparseSelectedRecords.lastElementChild.textContent = t(state.reparseSubmitting
    ? "review.reparseSubmitting" : reparsing ? "review.reparseRunning" : "review.reparseSelected");
  dom.selectAllRecords.disabled = !visibleIds.length;
  dom.selectAllRecords.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  dom.selectAllRecords.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
}

function onRecordSelectionChange(event) {
  const input = event.target.closest('input[data-action="select-record"]');
  if (!input) return;
  if (input.checked) state.selectedRecordIds.add(input.dataset.recordId);
  else state.selectedRecordIds.delete(input.dataset.recordId);
  renderGrid();
}

async function reparseSelectedRecords() {
  renderReviewControls();
  if (dom.reparseSelectedRecords.disabled) return;
  const recordIds = [...state.selectedRecordIds];
  const fields = selectedReparseFields();
  if (!recordIds.length || !fields.length || !state.activePatentId) return;
  state.reparseSubmitting = true;
  renderReviewControls();
  try {
    const response = await fetch(PATENT_REPARSE_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_id: state.activePatentId,
        record_ids: recordIds,
        fields,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const previousPatent = state.patents.find(
      (patent) => patent.document_id === payload.patent.document_id,
    );
    const reparsingPatent = {
      ...previousPatent,
      ...payload.patent,
    };
    state.patents = state.patents.map((patent) =>
      patent.document_id === reparsingPatent.document_id ? reparsingPatent : patent,
    );
    state.activePatent = reparsingPatent;
    state.pendingReviewPatentId = reparsingPatent.document_id;
    state.pendingReviewAction = "reparse";
    dom.reparseFieldMenu.open = false;
    showToast(t("review.reparseQueued", {
      count: recordIds.length,
      fields: fields.map((field) => fieldLabel(field)).join("、"),
    }), 4800);
    renderReviewControls();
    renderPatentTable();
    schedulePatentPolling();
  } catch (error) {
    showToast(t("review.reparseFailed", { error: error.message }), 6500);
    renderReviewControls();
  } finally {
    state.reparseSubmitting = false;
    renderReviewControls();
  }
}

function sequenceConflictClass(record, field, value) {
  if (!["vh", "vl"].includes(field)) return "";
  const conflict = [...(record.sequence_source_conflicts || []), ...(record.sequence_image_reviews || [])]
    .some((item) => item.chain?.toLowerCase() === field);
  return conflict ? (value ? "sequence-source-conflict" : "sequence-source-unresolved") : "";
}

function gridCell(record, value, field, className = "") {
  const active = record.record_id === state.selectedRecordId && field === state.selectedField;
  const display = value ?? "";
  const reviewState = cellReviewState(record, field);
  const stateClass = cellReviewStateClass(reviewState);
  const stateHint = cellReviewStateHint(reviewState);
  const conflictClass = sequenceConflictClass(record, field, display);
  const title = [typeof display === "string" && display.length > 24 ? display : "", stateHint,
    conflictClass ? t(display ? "reviewCheck.table_gold_standard" : "reviewCheck.table_unresolved") : ""]
    .filter(Boolean)
    .join(" · ");
  return `<td><button type="button" class="data-cell ${className} ${stateClass} ${conflictClass} ${active ? "active-cell" : ""}"
    data-record-id="${escapeAttr(record.record_id)}" data-field="${escapeAttr(field)}"
    title="${escapeAttr(title)}">${escapeHtml(display)}</button></td>`;
}

function reviewCheckBadges(record) {
  const checks = REVIEW_CHECK_SOURCES.map((source) => {
    const status = reviewCheckStatus(record, source);
    const sourceLabel = source === "esm2" && status === "manual_review" && ["VH", "VL"].every((chain) => record.esm2_review?.chains?.[chain]?.metric_status === "scored")
      ? t("reviewCheck.esm2Scored") : t(`reviewCheck.${source}`);
    const statusLabel = t(`reviewCheck.${source === "esm2" && status === "error" ? "risk" : status}`);
    const accessibleLabel = `${sourceLabel}：${statusLabel}`;
    return `<span class="review-check-badge review-check-${escapeAttr(status)}" title="${escapeAttr(accessibleLabel)}" aria-label="${escapeAttr(accessibleLabel)}">${escapeHtml(sourceLabel)}</span>`;
  }).join("");
  return checks + (record.sequence_image_reviews?.length
    ? `<span class="review-check-badge review-check-passed">${escapeHtml(t("reviewCheck.image_corrected"))}</span>` : "")
    + (record.sequence_source_conflicts?.length
    ? `<span class="review-check-badge review-check-table_text_mismatch" title="${escapeAttr(t("reviewCheck.table_gold_standard"))}">${escapeHtml(t("reviewCheck.table_text_mismatch"))}</span>`
    : record.sequence_table_text_status === "matched"
    ? `<span class="review-check-badge review-check-passed">${escapeHtml(t("reviewCheck.table_text_match"))}</span>` : "");
}

function reviewCheckTitle(record) {
  const checks = REVIEW_CHECK_SOURCES.map((source) => {
    const review = (source === "record" ? record?.record_verification : record?.[`${source}_review`]) || {};
    const messages = Array.isArray(review.messages)
      ? review.messages.join(" ")
      : review.message || (review.findings || []).map((finding) => finding.message).join(" ");
    const label = `${t(`reviewCheck.${source}`)} · ${t(`reviewCheck.${reviewCheckStatus(record, source)}`)}`;
    return messages ? `${label}：${messages}` : label;
  }).join(" · ");
  return [checks, ...(record.sequence_source_conflicts || []).map((conflict) =>
    `${t("reviewCheck.table_text_mismatch")} · ${conflict.chain || ""}: ${conflict.message || ""}`)].join(" · ");
}

function gridStatusCell(record) {
  const active = record.record_id === state.selectedRecordId && state.selectedField === "link_status";
  const reviewState = cellReviewState(record, "link_status");
  const stateClass = cellReviewStateClass(reviewState);
  const stateHint = cellReviewStateHint(reviewState);
  const title = [reviewCheckTitle(record), stateHint].filter(Boolean).join(" · ");
  return `<td><button type="button" class="data-cell review-check-cell ${stateClass} ${active ? "active-cell" : ""}"
    data-record-id="${escapeAttr(record.record_id)}" data-field="link_status" title="${escapeAttr(title)}">
    ${reviewCheckBadges(record)}
  </button></td>`;
}

function cellReviewState(record, field) {
  if (!FIELD_LABEL_KEYS[field]) return { edited: false, flagged: false };
  const key = correctionKey(record.record_id, field);
  return {
    edited: Object.prototype.hasOwnProperty.call(state.corrections, key),
    flagged: state.reviews[key]?.status === "flagged",
  };
}

function cellReviewStateClass(reviewState) {
  return [reviewState.edited ? "cell-edited" : "", reviewState.flagged ? "cell-flagged" : ""]
    .filter(Boolean)
    .join(" ");
}

function cellReviewStateHint(reviewState) {
  if (reviewState.edited && reviewState.flagged) return t("cell.editedFlagged");
  if (reviewState.flagged) return t("cell.flagged");
  if (reviewState.edited) return t("cell.edited");
  return "";
}

function onGridClick(event) {
  const cell = event.target.closest("button[data-record-id]");
  if (!cell) return;
  stopSequenceSpeech({ updateUi: false });
  const field = cell.dataset.field;
  state.selectedRecordId = cell.dataset.recordId;
  state.selectedField = field === "_index" || field === "_evidence" ? "antibody_id" : field;
  state.tab = DEFAULT_TAB_BY_FIELD[state.selectedField] || "ocr";
  state.viewMode = "page";
  state.selectedEvidenceIndex = 0;
  selectEvidenceForCurrentField();
  renderGrid();
  renderAllLinkedViews();
}

function renderAllLinkedViews() {
  renderHighlightTools();
  renderReviewMapLayout();
  renderEvidenceStrip();
  renderViewer();
  renderInspector();
}

function selectEvidenceForCurrentField() {
  const record = currentRecord();
  state.evidence = record ? buildEvidence(record, state.selectedField) : [];
  state.selectedEvidenceIndex = Math.min(state.selectedEvidenceIndex, Math.max(0, state.evidence.length - 1));
  syncPageToEvidence();
}

function buildEvidence(record, field) {
  const evidence = [];
  const assays = (record.evidence || []).filter(
    (item) => item && item.table_id && Number.isInteger(item.row_index),
  ).filter((item) => item.type === "assay_measurement" || !item.type);
  const sequenceIndex = (record.evidence || []).find((item) => item?.type === "sequence_index") || {};
  const sequenceSupport = (record.evidence || []).find((item) => item?.type === "sequence_support") || {};
  const validation = state.validationByConstruct.get(String(sequenceIndex.construct_key || ""));
  const mappingEvidence = validation?.mapping_check?.evidence || [];
  const sequenceRows = sequenceIndex.rows || [];
  const supportingRows = sequenceSupport.rows || [];
  const wantedChain = field === "vh" || field === "vl" ? field.toUpperCase() : null;
  const locations = sequenceIndex.locations || {};

  const add = (item) => {
    if (!item?.table_id) return;
    const locationKey = `${item.kind}:${item.table_id}:${item.row_index ?? ""}:${item.col_index ?? ""}:${item.chain ?? ""}:${item.seq_id ?? ""}`;
    // A shared cell can have conflicting interpretations; keep those reviewable.
    const key = item.kind === "assay" ? `${locationKey}:${JSON.stringify([
      item.value_ref, item.endpoint, item.value, item.unit, item.operator, item.measurement_id,
      item.experiment_id, item.assay_protocol_id, item.condition_label, item.measurement_label, item.donor_id,
    ])}` : locationKey;
    const existingIndex = evidence.findIndex((current) => current.key === key);
    if (existingIndex >= 0) {
      if (item.segments?.length && !evidence[existingIndex].segments?.length) evidence[existingIndex] = { ...item, key };
      return;
    }
    evidence.push({ ...item, key });
  };

  for (const source of record.sda_sources || []) {
    add({ ...source, detail: source.detail || t("sda.tableEvidence") });
  }

  mappingEvidence.forEach((item) => {
    if (!item.table_id || item.row_index == null) return;
    if (wantedChain && !Object.prototype.hasOwnProperty.call(item.assignments || {}, wantedChain)) return;
    add({
      kind: "mapping",
      label: t("viewer.mapping"),
      detail: formatAssignments(item.assignments),
      table_id: item.table_id,
      row_index: item.row_index,
      confidence: item.confidence,
      chain: wantedChain,
    });
  });
  for (const sequenceField of ["vh", "vl"]) {
    const location = locations[sequenceField];
    if (!location?.segments?.length) continue;
    const scope = sequenceField === "vh" ? "variable_heavy" : "variable_light";
    const byPage = new Map();
    for (const segment of location.segments) {
      if (!byPage.has(segment.page)) byPage.set(segment.page, []);
      byPage.get(segment.page).push(segment);
    }
    for (const [page, segments] of byPage) {
      const start = Math.min(...segments.map((s) => s.sequence_start));
      const end = Math.max(...segments.map((s) => s.sequence_end));
      add({ kind: scope, scope, chain: sequenceField.toUpperCase(), seq_id: location.seq_id,
        label: `${sequenceScopeLabel(scope)} · SEQ ${location.seq_id}`,
        detail: t("viewer.residueRange", { start: start + 1, end, length: location.length }),
        table_id: segments[0].table_id, row_index: segments[0].row_index, page,
        segments: location.segments, sequence_length: location.length,
        source_scope: location.source_scope, location_status: location.status,
      });
    }
  }
  [...sequenceRows, ...supportingRows].forEach((item) => {
    // One chip per page holds every residue segment, instead of a separate
    // chip for each wrapped OCR row or the rest of a full-length chain.
    if (locations[String(item.chain).toLowerCase()]?.segments?.length && item.evidence_role === "primary"
        && !item.sequence_scope?.startsWith("full_")) return;
    const fallbackScope = item.chain === "VH" ? "variable_heavy" : "variable_light";
    const scope = item.sequence_scope || fallbackScope;
    const kind = scope === "constant_region" ? `constant_region_${String(item.chain).toLowerCase()}` : scope;
    add({
      kind,
      scope,
      label: `${sequenceScopeLabel(scope, item.chain)}${item.cdr_name ? ` · ${item.cdr_name}` : ""} · SEQ ${item.seq_id}`,
      detail: t("viewer.row", { row: Number(item.row_index) + 1 }),
      table_id: item.table_id,
      row_index: item.row_index,
      col_index: item.col_index,
      chain: item.chain,
      seq_id: item.seq_id,
    });
  });
  assays.forEach((assay) => {
    const classification = state.classificationByTable.get(String(assay.table_id));
    for (const region of classification?.regions || []) {
      const group = (region.entity_groups || []).find((item) => item.raw_name === record.antibody_id);
      const location = group?.name_ref?.match(/:R(\d+)C(\d+)$/);
      if (location) {
        add({ kind: "entity_header", label: t("field.antibody_id"), detail: group.raw_name,
          table_id: assay.table_id, row_index: Number(location[1]), col_index: Number(location[2]) });
      }
    }
    const assayValue = assay.value ?? record.ic50;
    const assayUnit = assay.unit || record.ic50_unit || "";
    const operator = assay.operator || record.ic50_operator || record.operator || "";
    const displayValue = `${operator === "=" ? "" : operator}${assayValue ?? "—"}`;
    const experiment = assay.experiment_group || record.experiment_group || "";
    add({
      kind: "assay",
      label: `${assay.endpoint || record.assay_endpoint || t("sda.unknownMetric")} · ${displayValue} ${assayUnit}`.trim(),
      endpoint: assay.endpoint || record.assay_endpoint || t("sda.unknownMetric"),
      detail: [experiment, t("viewer.row", { row: Number(assay.row_index) + 1 })].filter(Boolean).join(" · "),
      table_id: assay.table_id,
      row_index: assay.row_index,
      col_index: assay.col_index,
      value_ref: assay.value_ref,
      image: assay.image,
      value: assayValue,
      unit: assayUnit,
      operator: operator || "=",
      experiment_group: experiment,
      record_id: record.record_id,
      measurement_id: assay.measurement_id ?? record.measurement_id ?? "",
      experiment_id: assay.experiment_id ?? record.experiment_id ?? "",
      assay_protocol_id: assay.assay_protocol_id ?? record.assay_protocol_id ?? "",
      condition_label: assay.condition_label ?? record.assay_condition ?? "",
      measurement_label: assay.measurement_label ?? record.assay_measurement_label ?? "",
      donor_id: assay.donor_id ?? record.donor_id ?? "",
      species: assay.species ?? record.species ?? [],
      assay_method: assay.assay_method ?? record.assay_method ?? "",
      assay_system: assay.assay_system ?? record.assay_system ?? "",
      antigen_target: assay.antigen_target ?? record.antigen_target ?? "",
      assay_keywords: assay.assay_keywords ?? record.assay_keywords ?? [],
      assay_keyword_status: assay.assay_keyword_status ?? record.assay_keyword_status ?? "",
    });
  });

  const order = {
    antibody_id: ["entity_header", "mapping", "assay", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl"],
    vh: ["variable_heavy", "mapping", "heavy_cdr", "full_heavy_chain", "constant_region_vh", "assay", "variable_light", "light_cdr", "full_light_chain", "constant_region_vl"],
    vl: ["variable_light", "mapping", "light_cdr", "full_light_chain", "constant_region_vl", "assay", "variable_heavy", "heavy_cdr", "full_heavy_chain", "constant_region_vh"],
    ic50: ["assay", "mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl"],
    ic50_unit: ["assay", "mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl"],
    experiment_group: ["assay", "mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl"],
    assay_keywords: ["assay", "mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl"],
    link_status: ["mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl", "assay"],
  }[field] || ["mapping", "variable_heavy", "variable_light", "heavy_cdr", "light_cdr", "full_heavy_chain", "full_light_chain", "constant_region_vh", "constant_region_vl", "assay"];
  evidence.sort((left, right) => {
    const leftIndex = order.indexOf(left.kind);
    const rightIndex = order.indexOf(right.kind);
    return (leftIndex < 0 ? order.length : leftIndex) - (rightIndex < 0 ? order.length : rightIndex)
      || Number(Boolean(right.segments?.length)) - Number(Boolean(left.segments?.length));
  });
  return wantedChain ? evidence.filter((item) => item.chain === wantedChain) : evidence;
}

function sequenceScopeLabel(scope, chain = "") {
  const label = t(`viewer.scope.${scope}`);
  return scope === "constant_region" && chain ? `${label} · ${chain}` : label;
}

function evidencePageNumber(item) {
  const identity = String(item?.table_id || "");
  const methodPage = identity.match(/^method:p(\d+)$/);
  return item?.page
    ?? state.candidateByTable.get(identity)?.page
    ?? state.contextByTable.get(identity)?.page
    ?? state.ocrByTable.get(identity)?.page
    ?? item?.page
    ?? (methodPage ? Number(methodPage[1]) : undefined);
}

function setHighlightVisibility(kind, visible) {
  if (!["cell", "table", "context", "manual"].includes(kind)) return;
  state.highlightVisibility[kind] = Boolean(visible);
  saveText("sda.studio.highlightVisibility", JSON.stringify(state.highlightVisibility));
  renderHighlightTools();
}

function renderHighlightTools() {
  dom.highlightControls.querySelectorAll("input[data-highlight]").forEach((input) => {
    const kind = input.dataset.highlight;
    input.checked = state.highlightVisibility[kind] !== false;
    dom.viewerPanel.classList.toggle(`hide-highlight-${kind}`, !input.checked);
  });
}

function reviewEntityKey(record) {
  return String(record.canonical_entity_key
    || (record.evidence || []).find((item) => item.type === "sequence_index")?.construct_key
    || effectiveValue(record, "antibody_id") || "");
}

function renderReviewMapLayout() {
  const collapsed = state.reviewMapCollapsed;
  dom.annotationWorkspace.classList.toggle("map-collapsed", collapsed);
  dom.reviewMapBody.hidden = collapsed;
  dom.toggleReviewMap.setAttribute("aria-expanded", String(!collapsed));
  const label = t(collapsed ? "map.expand" : "map.collapse");
  dom.toggleReviewMap.title = label;
  dom.toggleReviewMap.setAttribute("aria-label", label);
  dom.toggleReviewMap.querySelector(".review-map-toggle-icon").textContent = collapsed ? "‹" : "›";
}

function buildReviewParameters(record) {
  const entityKey = reviewEntityKey(record);
  const documentKey = (item) => item.document_id || item._patent_id || state.activePatentId || "";
  const related = entityKey ? state.records.filter((item) => reviewEntityKey(item) === entityKey
    && documentKey(item) === documentKey(record)) : [record];
  const groups = new Map();
  const measurements = new Map();
  const endpoints = new Set();
  const categories = ["species", "system", "method", "endpoint", "cell_model", "readout", "sample", "other"];
  const text = (value) => String(value ?? "").trim().replace(/\s+/g, " ");
  related.forEach((owner) => {
    buildEvidence(owner, "ic50").filter((item) => item.kind === "assay").forEach((item) => {
      const endpoint = String(item.endpoint || "IC50").toUpperCase().replace(/[\s_]/g, "");
      const keywords = [...new Map(assayKeywordValues(item).map((keyword) => [assayKeywordIdentity(keyword), keyword])).values()];
      keywords.sort((a, b) => categories.indexOf(a.category || "other") - categories.indexOf(b.category || "other")
        || assayKeywordIdentity(a).localeCompare(assayKeywordIdentity(b)));
      const keywordIds = keywords.map(assayKeywordIdentity).sort();
      const target = text(item.antigen_target);
      const groupKey = JSON.stringify([target, keywordIds]);
      if (!groups.has(groupKey)) {
        groups.set(groupKey, {label: [target, keywords.length
          ? keywords.map(localizedAssayKeyword).join(" · ") : t("map.assayUnclassified")].filter(Boolean).join(" · "), leaves: []});
      }
      // Keywords describe an assay type, not a unique observation. Only an
      // explicit measurement ID can join different source cells. Incomplete
      // classification keeps the cells separate until reviewed.
      const hasDonor = !keywords.some((keyword) => keyword.category === "sample" && keyword.key === "donor") || text(item.donor_id);
      const identity = item.measurement_id && keywords.length && item.assay_keyword_status === "matched"
        && item.value != null && text(item.unit) && hasDonor
        ? ["measurement", item.measurement_id]
        : ["source", item.table_id, item.value_ref || item.key];
      const key = JSON.stringify([groupKey, identity, endpoint, item.value, text(item.unit), item.operator,
        text(item.experiment_id), text(item.assay_protocol_id), text(item.condition_label),
        text(item.measurement_label), text(item.donor_id), (Array.isArray(item.species) ? [...item.species] : [item.species]).sort(),
        text(item.assay_method), text(item.assay_system)]);
      if (!measurements.has(key)) {
        const label = `${endpoint === "KD" ? "Kd" : endpoint} · ${item.operator === "=" ? "" : item.operator}${item.value ?? "—"} ${item.unit}`.trim();
        const detail = [...new Set([item.experiment_group, item.condition_label, item.donor_id].map(text).filter(Boolean))].join(" · ");
        const leaf = {label, detail, field: "ic50", sources: [], recordId: owner.record_id, measurement: true, endpoint};
        measurements.set(key, leaf);
        groups.get(groupKey).leaves.push(leaf);
      }
      const leaf = measurements.get(key);
      if (!leaf.sources.some((source) => source.key === item.key)) leaf.sources.push(item);
      endpoints.add(endpoint);
    });
  });
  const result = [...groups.values()];
  result.forEach((group) => group.leaves.sort((a, b) => a.endpoint.localeCompare(b.endpoint)));
  // Show an absent endpoint once; numbered empty slots look like measurements.
  const missing = ["IC50", "KD"].filter((endpoint) => !endpoints.has(endpoint))
    .map((endpoint) => ({label: endpoint === "KD" ? "Kd" : endpoint, field: "ic50", sources: [], recordId: record.record_id}));
  if (missing.length) result.push({label: t("map.missingParameters"), leaves: missing, missing: true});
  return {groups: result, leaves: result.flatMap((group) => group.leaves)};
}

function buildReviewMap(record) {
  const all = buildEvidence(record, null);
  const leaf = (label, field, sources, value = "", owner = record) => ({
    label, field, sources, value, recordId: owner.record_id,
  });
  const chainLeaves = (field, chain) => [
    leaf(t(`map.${field === "vl" ? "light" : "heavy"}`), field,
      all.filter((item) => item.scope === (field === "vl" ? "variable_light" : "variable_heavy")),
      effectiveValue(record, field) ? `${String(effectiveValue(record, field)).length} aa` : ""),
    leaf(t(`map.${field === "vl" ? "fullLight" : "fullHeavy"}`), field,
      all.filter((item) => item.scope === (field === "vl" ? "full_light_chain" : "full_heavy_chain"))),
    leaf(t(`map.${field === "vl" ? "constantLight" : "constantHeavy"}`), field,
      all.filter((item) => item.scope === "constant_region" && item.chain === chain)),
    ...["heavy_cdr", "light_cdr"].filter((scope) => all.some((item) => item.scope === scope && item.chain === chain))
      .map((scope) => leaf(sequenceScopeLabel(scope), field, all.filter((item) => item.scope === scope && item.chain === chain))),
  ];
  const parameters = buildReviewParameters(record);
  const nameSources = all.filter((item) => item.kind === "entity_header");
  const mappings = all.filter((item) => item.kind === "mapping");
  // Legacy tables often store the name in the same row as the measurement.
  const parentSources = nameSources.length ? nameSources : mappings.length ? mappings : all.filter((item) => item.kind === "assay");
  return [
    { id: "parent", label: t("map.parent"), leaves: [leaf(record.canonical_antibody_id || effectiveValue(record, "antibody_id") || "—", "antibody_id", parentSources)] },
    { id: "light", label: t("map.light"), leaves: chainLeaves("vl", "VL") },
    { id: "heavy", label: t("map.heavy"), leaves: chainLeaves("vh", "VH") },
    { id: "parameters", label: t("map.parameters"), ...parameters },
    { id: "source", label: t("map.source"), leaves: [leaf(t("viewer.mapping"), "antibody_id", mappings)] },
  ];
}

function renderEvidenceStrip() {
  const record = currentRecord();
  state.reviewMapTargets = [];
  const allCollapsed = state.mapCollapsedGroups.size === 5;
  dom.mapToggleBranches.textContent = t(allCollapsed ? "map.expandAll" : "map.collapseAll");
  if (!record) {
    dom.evidenceStrip.innerHTML = `<p class="review-map-empty">${escapeHtml(t("viewer.noRecords"))}</p>`;
    return;
  }
  const activeEvidence = currentEvidence();
  const renderLeaf = (leaf) => {
    const sources = leaf.sources.length ? leaf.sources : [null];
    const nodes = sources.map((item) => {
      const recordId = item?.record_id || leaf.recordId;
      const index = state.reviewMapTargets.push({ recordId, field: leaf.field, evidence: item }) - 1;
      const disabled = !item && !leaf.value;
      const active = !disabled && recordId === state.selectedRecordId && leaf.field === state.selectedField
        && (item ? item.key === activeEvidence?.key : !activeEvidence);
      const page = item ? evidencePageNumber(item) : null;
      const sourceLabel = item
        ? [page ? `P${page}` : t("map.unlocated"), item.seq_id != null ? `SEQ ${item.seq_id}` : "",
            leaf.measurement ? `${item.table_id} · R${item.row_index + 1}${Number.isInteger(item.col_index) ? ` C${item.col_index + 1}` : ""}` : item.detail].filter(Boolean).join(" · ")
        : leaf.value ? `${leaf.value} · ${t("map.unlocated")}` : t("map.missing");
      return `<button type="button" class="map-source-node ${active ? "active" : ""} ${item ? "" : "missing"}"
        data-map-target="${index}" aria-pressed="${active}" ${disabled ? "disabled" : ""}
        title="${escapeAttr([leaf.label, sourceLabel, item?.experiment_group].filter(Boolean).join(" · "))}">
        ${escapeHtml(sourceLabel)}</button>`;
    }).join("");
    return `<div class="map-leaf"><span class="map-leaf-label">${escapeHtml(leaf.label)}</span>
      ${leaf.detail ? `<span class="map-measurement-detail">${escapeHtml(leaf.detail)}</span>` : ""}
      ${leaf.measurement ? `<small class="map-source-count">${escapeHtml(t("map.sourceCount", {count: leaf.sources.length}))}</small>` : ""}
      <div class="map-source-list">${nodes}</div></div>`;
  };
  dom.evidenceStrip.innerHTML = `<div class="review-map-tree" style="zoom:${state.mapZoom}">
    <div class="map-root"><small>${escapeHtml(t("map.root"))}</small><strong>${escapeHtml(effectiveValue(record, "antibody_id") || "—")}</strong></div>
    <div class="map-branches">${buildReviewMap(record).map((group, index) => {
      const collapsed = state.mapCollapsedGroups.has(group.id);
      return `<section class="map-branch map-branch-${group.id}">
        <button type="button" class="map-group-node" data-map-group="${group.id}"
          aria-expanded="${!collapsed}" aria-controls="map-leaves-${group.id}">
          <span class="map-group-index">${index + 1}</span><span>${escapeHtml(group.label)}</span><b aria-hidden="true">${collapsed ? "+" : "−"}</b>
        </button><div id="map-leaves-${group.id}" class="map-leaves" ${collapsed ? "hidden" : ""}>${group.groups
          ? group.groups.map((assayGroup) => `<section class="map-assay-group">
              <h3 class="map-assay-heading">${escapeHtml(assayGroup.label)}</h3>
              ${assayGroup.missing ? "" : `<small class="map-measurement-count">${escapeHtml(t("map.measurementCount", {count: assayGroup.leaves.length}))}</small>`}
              <div class="map-assay-measurements">${assayGroup.leaves.map(renderLeaf).join("")}</div>
            </section>`).join("") : group.leaves.map(renderLeaf).join("")}</div>
      </section>`;
    }).join("")}</div></div>`;
}

function onReviewMapClick(event) {
  const group = event.target.closest("button[data-map-group]");
  if (group) {
    const key = group.dataset.mapGroup;
    if (state.mapCollapsedGroups.has(key)) state.mapCollapsedGroups.delete(key);
    else state.mapCollapsedGroups.add(key);
    renderEvidenceStrip();
    dom.evidenceStrip.querySelector(`[data-map-group="${key}"]`)?.focus({ preventScroll: true });
    return;
  }
  const button = event.target.closest("button[data-map-target]");
  const target = button && state.reviewMapTargets[Number(button.dataset.mapTarget)];
  if (!target || button.disabled) return;
  const record = state.records.find((item) => item.record_id === target.recordId);
  if (!record) return;
  stopSequenceSpeech({ updateUi: false });
  state.selectedRecordId = record.record_id;
  state.selectedField = target.field;
  state.tab = DEFAULT_TAB_BY_FIELD[target.field] || "ocr";
  state.viewMode = "page";
  state.evidence = target.evidence ? buildEvidence(record, target.field) : [];
  state.selectedEvidenceIndex = Math.max(0, state.evidence.findIndex((item) => item.key === target.evidence?.key));
  syncPageToEvidence();
  renderGrid();
  renderAllLinkedViews();
}

function setMapZoom(value) {
  state.mapZoom = Math.round(Math.max(0.6, Math.min(1.4, value)) * 10) / 10;
  dom.mapZoomReset.textContent = `${Math.round(state.mapZoom * 100)}%`;
  dom.mapZoomOut.disabled = state.mapZoom <= 0.6;
  dom.mapZoomIn.disabled = state.mapZoom >= 1.4;
  const tree = dom.evidenceStrip.querySelector(".review-map-tree");
  if (tree) tree.style.zoom = String(state.mapZoom);
}

function renderViewer() {
  const selectedEvidence = currentEvidence();
  if (!selectedEvidence || state.viewMode === "table" && !state.candidateByTable.has(String(selectedEvidence?.table_id))
      || state.viewMode === "context" && !state.contextByTable.get(String(selectedEvidence?.table_id))?.context_image) {
    state.viewMode = "page";
  }
  document.querySelectorAll("[data-view-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewMode === state.viewMode);
  });
  dom.canvasSelect.classList.toggle("active", state.canvasTool === "select");
  dom.canvasRectangle.classList.toggle("active", state.canvasTool === "rectangle");
  dom.canvasRectangle.disabled = state.viewMode !== "page";
  dom.clearManualBoxes.disabled = state.viewMode !== "page";
  const record = currentRecord();
  const evidence = currentEvidence();
  if ((!record || !evidence) && !state.pages?.pages?.length) {
    showViewerEmpty();
    return;
  }
  const candidate = state.candidateByTable.get(String(evidence?.table_id));
  const context = state.contextByTable.get(String(evidence?.table_id));
  const ocrTable = state.ocrByTable.get(String(evidence?.table_id));
  const evidencePage = Number(evidencePageNumber(evidence) ?? 1);
  if (state.viewMode !== "page") state.currentPage = evidencePage;
  state.currentPage = Math.max(1, Math.min(getPageCount(), Number(state.currentPage) || 1));
  updateViewerMetadata(evidence, context, evidencePage);
  dom.emptyViewer.hidden = true;
  dom.zoomLevel.textContent = `${Math.round(state.zoom * 100)}%`;

  if (state.viewMode === "page") {
    dom.imageFrame.hidden = true;
    dom.continuousPages.hidden = false;
    renderContinuousPdf(evidence);
    return;
  }

  dom.continuousPages.hidden = true;
  dom.imageFrame.hidden = false;
  dom.imageFrame.classList.remove("rect-tool");
  dom.imageFrame.style.width = `${Math.max(45, state.zoom * 100)}%`;
  const sourcePath = state.viewMode === "context" ? context?.context_image : candidate?.image_path;
  const url = workspacePathToUrl(sourcePath);
  if (url && dom.evidenceImage.dataset.src !== url) {
    dom.evidenceImage.src = url;
    dom.evidenceImage.dataset.src = url;
  }
  const tableNumber = context?.table_number
    ? t("viewer.tableNumber", { number: context.table_number })
    : evidence.table_id;
  dom.evidenceImage.alt = t("viewer.altCrop", { table: tableNumber, page: evidencePage });
  renderTableCropAnnotations(ocrTable, evidence);
}

function updateViewerMetadata(evidence, context, evidencePage) {
  dom.pageIndicator.textContent = `${state.currentPage} / ${getPageCount()}`;
  dom.pagePrevious.disabled = state.currentPage <= 1;
  dom.pageNext.disabled = state.currentPage >= getPageCount();
  if (!evidence) {
    const record = currentRecord();
    dom.viewerLocation.textContent = t("viewer.browseLocation", { page: state.currentPage });
    const sequenceWarning = record && isSequenceField(state.selectedField)
      ? t(record[state.selectedField] ? "viewer.sequenceUnlocated" : "viewer.sequenceMissing") : "";
    dom.viewerCaption.textContent = [sequenceWarning, t(record ? "viewer.pdfBrowse" : "viewer.noRecords")].filter(Boolean).join(" ");
    return;
  }
  const showingEvidencePage = state.currentPage === evidencePage;
  const target = evidence.segments?.length ? evidence.detail : evidence.row_index != null
    ? t("viewer.row", { row: Number(evidence.row_index) + 1 })
    : t("viewer.tableTarget");
  dom.viewerLocation.textContent = showingEvidencePage
    ? t("viewer.location", { page: evidencePage, table: evidence.table_id, target })
    : t("viewer.browseLocation", { page: state.currentPage });
  let caption = context?.caption?.text || context?.nearby_context?.[0]?.text || "";
  if (state.selectedField === "vh" || state.selectedField === "vl") {
    const record = currentRecord();
    const location = (record?.evidence || []).find((item) => item.type === "sequence_index")?.locations?.[state.selectedField];
    const warning = !record?.[state.selectedField] ? "viewer.sequenceMissing"
      : evidence.segments?.length ? (evidence.segments.some((s) => s.precision !== "pdf_characters")
        ? "viewer.sequenceApproximate" : "viewer.sequenceExact") : "viewer.sequenceUnlocated";
    caption = t(warning);
    if (location?.status === "partial") caption += ` ${t("viewer.sequenceUnlocated")}`;
    if (evidence.source_scope?.startsWith("full_")) {
      caption += ` ${t("viewer.derivedFrom", { scope: sequenceScopeLabel(evidence.source_scope) })}`;
    }
    if (effectiveValue(record || {}, state.selectedField) !== record?.[state.selectedField]) {
      caption += ` ${t("viewer.sequenceChanged")}`;
    }
  }
  dom.viewerCaption.textContent = showingEvidencePage ? caption : t("viewer.browseHint");
}

function renderContinuousPdf(evidence) {
  ensureContinuousPages();
  dom.continuousPages.classList.toggle("rect-tool", state.canvasTool === "rectangle");
  dom.continuousPages.style.width = `${Math.max(64, state.zoom * 100)}%`;
  dom.continuousPages.querySelectorAll(".pdf-page-shell").forEach((shell) => {
    const page = Number(shell.dataset.page);
    shell.classList.toggle("current-page", page === state.currentPage);
    const image = shell.querySelector("img");
    if (image) image.alt = t("viewer.altPage", { page });
    const layer = shell.querySelector(".pdf-page-annotation");
    renderPageAnnotations(page, evidence, layer);
  });
  if (state.pendingPageScroll) {
    state.pendingPageScroll = false;
    requestAnimationFrame(() => scrollToPdfTarget(state.currentPage));
  }
}

function ensureContinuousPages() {
  const pages = state.pages?.pages || [];
  if (dom.continuousPages.children.length === pages.length && pages.length) return;
  dom.continuousPages.innerHTML = "";
  pages.forEach((pageInfo) => {
    const shell = document.createElement("section");
    shell.className = "pdf-page-shell";
    shell.dataset.page = pageInfo.page;

    const pageMeta = document.createElement("div");
    pageMeta.className = "pdf-page-meta";
    pageMeta.textContent = `P ${pageInfo.page}`;

    const frame = document.createElement("div");
    frame.className = "pdf-page-frame";
    frame.dataset.page = pageInfo.page;

    const image = document.createElement("img");
    image.loading = Number(pageInfo.page) <= 2 ? "eager" : "lazy";
    image.decoding = "async";
    // Reserve each page's real aspect ratio before the image arrives. Without
    // dimensions, all lazy images briefly collapse to zero height and the
    // browser treats most of the document as visible at once.
    image.width = Number(pageInfo.pixel_width || 1280);
    image.height = Number(pageInfo.pixel_height || 1811);
    image.src = pageImageUrl(pageInfo.page);
    image.alt = t("viewer.altPage", { page: pageInfo.page });

    const layer = document.createElement("div");
    layer.className = "annotation-layer pdf-page-annotation";
    layer.dataset.page = pageInfo.page;

    frame.append(image, layer);
    shell.append(pageMeta, frame);
    dom.continuousPages.appendChild(shell);
  });
}

function scrollToPdfTarget(page) {
  const shell = dom.continuousPages.querySelector(`.pdf-page-shell[data-page="${page}"]`);
  if (!shell) return;
  const visibleBox = (selector) => [...shell.querySelectorAll(selector)].find((box) => box.getClientRects().length);
  const target = visibleBox(".current-cell")
    || visibleBox(".cell-region")
    || visibleBox(".current-table")
    || shell;
  const stageRect = dom.viewerStage.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const centerOffset = Math.max(24, (stageRect.height - targetRect.height) / 2);
  const top = dom.viewerStage.scrollTop + targetRect.top - stageRect.top - centerOffset;
  dom.viewerStage.scrollTo({ top: Math.max(0, top), behavior: "auto" });
  if (target.classList.contains("page-box")) {
    target.classList.remove("location-pulse");
    void target.offsetWidth;
    target.classList.add("location-pulse");
    target.addEventListener("animationend", () => target.classList.remove("location-pulse"), { once: true });
  }
}

function onViewerStageScroll() {
  if (state.viewMode !== "page" || state.scrollSyncFrame) return;
  state.scrollSyncFrame = requestAnimationFrame(() => {
    state.scrollSyncFrame = null;
    const stageRect = dom.viewerStage.getBoundingClientRect();
    const targetY = stageRect.top + stageRect.height / 2;
    let closestPage = state.currentPage;
    let closestDistance = Number.POSITIVE_INFINITY;
    dom.continuousPages.querySelectorAll(".pdf-page-shell").forEach((shell) => {
      const rect = shell.getBoundingClientRect();
      const distance = targetY >= rect.top && targetY <= rect.bottom
        ? 0
        : Math.min(Math.abs(rect.top - targetY), Math.abs(rect.bottom - targetY));
      if (distance < closestDistance) {
        closestDistance = distance;
        closestPage = Number(shell.dataset.page);
      }
    });
    if (closestPage === state.currentPage) return;
    state.currentPage = closestPage;
    dom.continuousPages.querySelectorAll(".pdf-page-shell").forEach((shell) => {
      shell.classList.toggle("current-page", Number(shell.dataset.page) === closestPage);
    });
    const evidence = currentEvidence();
    const context = state.contextByTable.get(String(evidence?.table_id));
    const evidencePage = Number(evidencePageNumber(evidence) ?? 1);
    updateViewerMetadata(evidence, context, evidencePage);
  });
}

function renderPageAnnotations(page, evidence, layer = dom.annotationLayer) {
  if (!layer) return;
  layer.innerHTML = "";
  const pageInfo = getPageInfo(page);
  if (!pageInfo) return;
  const activeTableId = String(evidence?.table_id || "");
  const pageCandidates = state.candidates.filter((item) => Number(item.page) === Number(page));

  pageCandidates.forEach((candidate) => {
    appendPageBox({
      bbox: candidate.bbox,
      pageInfo,
      label: candidate.table_number
        ? t("viewer.tableNumber", { number: candidate.table_number })
        : candidate.table_id,
      className: String(candidate.table_id) === activeTableId ? "current-table" : "table-region",
      action: "table",
      tableId: candidate.table_id,
      layer,
    });
  });

  const context = state.contextByTable.get(activeTableId);
  const contextRegions = [
    ...(context?.caption ? [{ ...context.caption, regionLabel: t("region.caption") }] : []),
    ...(context?.nearby_context || []).map((item) => ({ ...item, regionLabel: t("region.nearby") })),
    ...(context?.referring_sentences || []).map((item) => ({ ...item, regionLabel: t("region.reference") })),
    ...(context?.footnotes || []).map((item) => ({ ...item, regionLabel: t("region.footnote") })),
  ];
  contextRegions
    .filter((item) => Number(item.page) === Number(page) && Array.isArray(item.bbox))
    .forEach((item) => {
      appendPageBox({
        bbox: item.bbox,
        pageInfo,
        label: item.regionLabel,
        className: "context-region",
        action: "context",
        title: item.text,
        layer,
      });
    });

  if (Number(evidence?.page) === Number(page) && evidence?.source_bbox) {
    appendPageBox({ bbox: evidence.source_bbox, pageInfo, label: evidence.label,
      className: "context-region", action: "context", title: evidence.detail, layer });
  }

  const candidate = state.candidateByTable.get(activeTableId);
  const ocrTable = state.ocrByTable.get(activeTableId);
  if (evidence?.segments?.length) {
    evidence.segments.filter((segment) => Number(segment.page) === Number(page)).forEach((segment) => {
      const box = appendPageBox({ bbox: segment.bbox, pageInfo,
        label: currentFieldMarker(currentRecord(), String(evidence.chain).toLowerCase()),
        className: `current-cell sequence-target${segment.precision === "pdf_characters" ? "" : " approximate-sequence"}`,
        action: "cell", tableId: segment.table_id,
        title: `${segment.text} · ${t("viewer.residueRange", { start: segment.sequence_start + 1,
          end: segment.sequence_end, length: evidence.sequence_length })}`,
        padding: CELL_HIGHLIGHT_PADDING, layer });
      if (box) {
        box.dataset.sequenceStart = segment.sequence_start;
        box.dataset.sequenceEnd = segment.sequence_end;
        box.dataset.precision = segment.precision;
        box.dataset.chain = evidence.chain;
        box.dataset.seqId = evidence.seq_id;
      }
    });
  }
  if (
    !evidence?.segments?.length &&
    candidate &&
    Number(candidate.page) === Number(page) &&
    ocrTable &&
    evidence?.row_index != null
  ) {
    const row = ocrTable.rows?.[Number(evidence.row_index)] || [];
    const activeCell = chooseActiveCell(row, state.selectedField, currentRecord());
    row.forEach((cell) => {
      const mappedBbox = cellPageBbox(cell, candidate.bbox, ocrTable);
      if (!mappedBbox) return;
      const active = cell === activeCell;
      appendPageBox({
        bbox: mappedBbox,
        pageInfo,
        label: active
          ? currentFieldMarker(currentRecord(), state.selectedField)
          : String(cell.text || t("field.generic")),
        className: active ? "current-cell" : "cell-region",
        action: "cell",
        tableId: activeTableId,
        title: cell.text,
        padding: CELL_HIGHLIGHT_PADDING,
        layer,
      });
    });
  }

  state.manualBoxes
    .filter((item) => Number(item.page) === Number(page))
    .forEach((item, index) => {
      appendPageBox({
        bbox: item.bbox,
        pageInfo,
        label: t("region.manualNamed", { number: index + 1 }),
        className: "manual-region",
        action: "manual",
        manualId: item.id,
        layer,
      });
    });

  if (state.drawingBox && Number(state.drawingBox.page) === Number(page)) {
    appendPageBox({
      bbox: state.drawingBox.bbox,
      pageInfo,
      label: t("region.new"),
      className: "drawing-region",
      action: "drawing",
      layer,
    });
  }
}

function appendPageBox({
  bbox,
  pageInfo,
  label,
  className,
  action,
  tableId = "",
  manualId = "",
  title = "",
  padding = 0,
  layer = dom.annotationLayer,
}) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return;
  const [rawX1, rawY1, rawX2, rawY2] = bbox.map(Number);
  if (![rawX1, rawY1, rawX2, rawY2].every(Number.isFinite)) return;
  const x1 = Math.max(0, Math.min(pageInfo.width, Math.min(rawX1, rawX2)));
  const y1 = Math.max(0, Math.min(pageInfo.height, Math.min(rawY1, rawY2)));
  const x2 = Math.max(0, Math.min(pageInfo.width, Math.max(rawX1, rawX2)));
  const y2 = Math.max(0, Math.min(pageInfo.height, Math.max(rawY1, rawY2)));
  const box = document.createElement("button");
  box.type = "button";
  box.className = `page-box ${className}`;
  const left = (x1 / pageInfo.width) * 100;
  const top = (y1 / pageInfo.height) * 100;
  const width = ((x2 - x1) / pageInfo.width) * 100;
  const height = ((y2 - y1) / pageInfo.height) * 100;
  const inset = normalizeBoxPadding(padding);
  box.style.left = inset.left ? `calc(${left}% - ${inset.left}px)` : `${left}%`;
  box.style.top = inset.top ? `calc(${top}% - ${inset.top}px)` : `${top}%`;
  box.style.width = inset.left || inset.right
    ? `calc(${width}% + ${inset.left + inset.right}px)`
    : `${width}%`;
  box.style.height = inset.top || inset.bottom
    ? `calc(${height}% + ${inset.top + inset.bottom}px)`
    : `${height}%`;
  box.dataset.action = action;
  if (tableId) box.dataset.tableId = tableId;
  if (manualId) box.dataset.manualId = manualId;
  box.title = title || label;
  const badge = document.createElement("span");
  badge.className = "box-label";
  badge.textContent = label;
  box.appendChild(badge);
  layer.appendChild(box);
  return box;
}

function normalizeBoxPadding(padding) {
  if (typeof padding === "number") {
    const value = Math.max(0, padding);
    return { top: value, right: value, bottom: value, left: value };
  }
  return {
    top: Math.max(0, Number(padding?.top || 0)),
    right: Math.max(0, Number(padding?.right || 0)),
    bottom: Math.max(0, Number(padding?.bottom || 0)),
    left: Math.max(0, Number(padding?.left || 0)),
  };
}

function validCellBbox(box) {
  return Array.isArray(box) && box.length === 4 && box.every((value) => typeof value === "number" && Number.isFinite(value))
    && box[0] < box[2] && box[1] < box[3];
}

function cellCropBbox(cell) {
  if (validCellBbox(cell?.bbox_crop)) return cell.bbox_crop;
  return cell?.coordinate_space !== "pdf_points" && validCellBbox(cell?.bbox) ? cell.bbox : null;
}

function cellPageBbox(cell, tableBbox, ocrTable) {
  if (validCellBbox(cell?.bbox_pdf)) return cell.bbox_pdf;
  if (cell?.coordinate_space === "pdf_points") return validCellBbox(cell.bbox) ? cell.bbox : null;
  if (cell?.coordinate_space === "crop_pixels" && !ocrTable?.crop_to_pdf && !ocrTable?.bbox_padded) return null;
  return mapCropBboxToPage(cellCropBbox(cell), tableBbox, ocrTable);
}

function mapCropBboxToPage(cellBbox, tableBbox, ocrTable) {
  if (!validCellBbox(cellBbox)) return null;
  const transform = ocrTable?.crop_to_pdf;
  if (Array.isArray(transform) && transform.length === 6 && transform.every((value) => typeof value === "number" && Number.isFinite(value))) {
    const [a, b, c, d, e, f] = transform;
    const [x1, y1, x2, y2] = cellBbox;
    const points = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]].map(([x, y]) => [a * x + c * y + e, b * x + d * y + f]);
    const box = [Math.min(...points.map((p) => p[0])), Math.min(...points.map((p) => p[1])),
      Math.max(...points.map((p) => p[0])), Math.max(...points.map((p) => p[1]))];
    return validCellBbox(box) ? box : null;
  }
  if (transform != null) return null;
  tableBbox = ocrTable?.bbox_padded || tableBbox;
  if (
    !Array.isArray(cellBbox) ||
    cellBbox.length !== 4 ||
    !Array.isArray(tableBbox) ||
    tableBbox.length !== 4 ||
    !(Number(ocrTable?.width) > 0) ||
    !(Number(ocrTable?.height) > 0)
  ) {
    return null;
  }
  const [tx1, ty1, tx2, ty2] = tableBbox.map(Number);
  const [cx1, cy1, cx2, cy2] = cellBbox.map(Number);
  const tableWidth = tx2 - tx1;
  const tableHeight = ty2 - ty1;
  const mapped = [
    tx1 + (cx1 / ocrTable.width) * tableWidth,
    ty1 + (cy1 / ocrTable.height) * tableHeight,
    tx1 + (cx2 / ocrTable.width) * tableWidth,
    ty1 + (cy2 / ocrTable.height) * tableHeight,
  ];
  return validCellBbox(mapped) ? mapped : null;
}

function renderTableCropAnnotations(ocrTable, evidence) {
  dom.annotationLayer.innerHTML = "";
  if (state.viewMode !== "table" || !ocrTable || evidence.row_index == null) return;
  const row = ocrTable.rows?.[Number(evidence.row_index)] || [];
  const activeCell = chooseActiveCell(row, state.selectedField, currentRecord());
  row.forEach((cell) => {
    const cropBox = cellCropBbox(cell);
    if (!cropBox || !(ocrTable.width > 0) || !(ocrTable.height > 0)) return;
    const [x1, y1, x2, y2] = cropBox;
    const box = document.createElement("div");
    const active = cell === activeCell;
    const padding = CELL_HIGHLIGHT_PADDING;
    box.className = `cell-box${active ? " active" : ""}`;
    const left = (x1 / ocrTable.width) * 100;
    const top = (y1 / ocrTable.height) * 100;
    const width = ((x2 - x1) / ocrTable.width) * 100;
    const height = ((y2 - y1) / ocrTable.height) * 100;
    const inset = normalizeBoxPadding(padding);
    box.style.left = `calc(${left}% - ${inset.left}px)`;
    box.style.top = `calc(${top}% - ${inset.top}px)`;
    box.style.width = `calc(${width}% + ${inset.left + inset.right}px)`;
    box.style.height = `calc(${height}% + ${inset.top + inset.bottom}px)`;
    box.dataset.label = active
      ? currentFieldMarker(currentRecord(), state.selectedField)
      : String(cell.text || t("field.generic"));
    dom.annotationLayer.appendChild(box);
  });
}

function onCanvasAnnotationClick(event) {
  if (state.canvasTool !== "select") return;
  const box = event.target.closest(".page-box[data-action]");
  if (!box) return;
  const action = box.dataset.action;
  if (action === "table" && box.dataset.tableId) {
    navigateToTable(box.dataset.tableId);
    return;
  }
  if (action === "cell") {
    state.tab = "ocr";
    renderInspector();
    showToast(t("toast.cellSelected"), 2200);
    return;
  }
  if (action === "context") {
    state.tab = "experiment";
    renderInspector();
    showToast(t("toast.contextSelected"), 2200);
    return;
  }
  if (action === "manual") {
    showToast(t("toast.manualExists"), 2400);
  }
}

function navigateToTable(tableId) {
  const directIndex = state.evidence.findIndex((item) => String(item.table_id) === String(tableId));
  if (directIndex >= 0) {
    state.selectedEvidenceIndex = directIndex;
    syncPageToEvidence();
    renderAllLinkedViews();
    return;
  }

  for (const record of state.records) {
    const allEvidence = buildEvidence(record, null);
    const target = allEvidence.find((item) => String(item.table_id) === String(tableId));
    if (!target) continue;
    const field =
      target.kind === "assay"
        ? "ic50"
        : target.kind === "vh"
          ? "vh"
          : target.kind === "vl"
            ? "vl"
            : "antibody_id";
    state.selectedRecordId = record.record_id;
    state.selectedField = field;
    state.tab = DEFAULT_TAB_BY_FIELD[field] || "detection";
    state.evidence = buildEvidence(record, field);
    state.selectedEvidenceIndex = Math.max(
      0,
      state.evidence.findIndex((item) => String(item.table_id) === String(tableId)),
    );
    syncPageToEvidence();
    renderGrid();
    renderAllLinkedViews();
    return;
  }

  const candidate = state.candidateByTable.get(String(tableId));
  if (candidate?.page) setCurrentPage(candidate.page);
  showToast(t("toast.tableUnlinked"), 3000);
}

function setCurrentPage(page) {
  const nextPage = Math.max(1, Math.min(getPageCount(), Number(page) || 1));
  state.currentPage = nextPage;
  state.viewMode = "page";
  state.pendingPageScroll = true;
  state.drawingBox = null;
  renderViewer();
}

function syncPageToEvidence() {
  const evidence = currentEvidence();
  if (!evidence) return;
  const page = Number(evidencePageNumber(evidence));
  if (Number.isFinite(page)) {
    state.currentPage = page;
    state.pendingPageScroll = true;
  }
}

function setCanvasTool(tool) {
  state.canvasTool = tool === "rectangle" ? "rectangle" : "select";
  state.viewMode = "page";
  state.drawingBox = null;
  renderViewer();
}

function onCanvasPointerDown(event) {
  if (state.viewMode !== "page" || state.canvasTool !== "rectangle" || event.button !== 0) return;
  const point = pointerToPagePoint(event);
  if (!point) return;
  event.preventDefault();
  state.currentPage = point.page;
  state.drawingSurface = point.frame;
  state.drawPointerId = event.pointerId;
  dom.continuousPages.setPointerCapture(event.pointerId);
  state.drawingBox = {
    page: point.page,
    bbox: [point.x, point.y, point.x, point.y],
  };
  renderContinuousPageAnnotations(point.page);
}

function onCanvasPointerMove(event) {
  if (state.drawPointerId !== event.pointerId || !state.drawingBox) return;
  const point = pointerToPagePoint(event);
  if (!point) return;
  state.drawingBox.bbox[2] = point.x;
  state.drawingBox.bbox[3] = point.y;
  renderContinuousPageAnnotations(state.drawingBox.page);
}

function onCanvasPointerUp(event) {
  if (state.drawPointerId !== event.pointerId || !state.drawingBox) return;
  const point = pointerToPagePoint(event);
  if (point) {
    state.drawingBox.bbox[2] = point.x;
    state.drawingBox.bbox[3] = point.y;
  }
  const [x1, y1, x2, y2] = state.drawingBox.bbox;
  const width = Math.abs(x2 - x1);
  const height = Math.abs(y2 - y1);
  if (width >= 4 && height >= 4) {
    state.manualBoxes.push({
      id: globalThis.crypto?.randomUUID?.() || `manual-${Date.now()}`,
      page: state.currentPage,
      bbox: [Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)],
      label: t("region.manual"),
      created_at: new Date().toISOString(),
    });
    saveText("sda.studio.manualBoxes", JSON.stringify(state.manualBoxes));
    showToast(t("toast.manualSaved"), 2600);
  }
  cancelCanvasDrawing(event);
  renderContinuousPageAnnotations(state.currentPage);
}

function cancelCanvasDrawing(event) {
  if (state.drawPointerId != null && dom.continuousPages.hasPointerCapture?.(state.drawPointerId)) {
    dom.continuousPages.releasePointerCapture(state.drawPointerId);
  }
  if (event?.pointerId == null || event.pointerId === state.drawPointerId) {
    state.drawPointerId = null;
    state.drawingBox = null;
    state.drawingSurface = null;
  }
}

function pointerToPagePoint(event) {
  const frame = state.drawingSurface || event.target.closest?.(".pdf-page-frame");
  const page = Number(frame?.dataset.page);
  const pageInfo = getPageInfo(page);
  const image = frame?.querySelector("img");
  const rect = image?.getBoundingClientRect();
  if (!pageInfo || !rect || !rect.width || !rect.height) return null;
  const px = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  const py = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
  return { x: px * pageInfo.width, y: py * pageInfo.height, page, frame };
}

function renderContinuousPageAnnotations(page) {
  const layer = dom.continuousPages.querySelector(`.pdf-page-annotation[data-page="${page}"]`);
  renderPageAnnotations(page, currentEvidence(), layer);
}

function clearManualBoxesForCurrentPage() {
  const before = state.manualBoxes.length;
  state.manualBoxes = state.manualBoxes.filter((item) => Number(item.page) !== state.currentPage);
  if (state.manualBoxes.length === before) {
    showToast(t("toast.noManual"), 2200);
    return;
  }
  saveText("sda.studio.manualBoxes", JSON.stringify(state.manualBoxes));
  renderViewer();
  showToast(t("toast.manualCleared"), 2400);
}

function getPageInfo(page) {
  return state.pages?.pages?.find((item) => Number(item.page) === Number(page)) || null;
}

function pageImageUrl(page) {
  const pageInfo = getPageInfo(page);
  return pageInfo?.image ? new URL(pageInfo.image, state.pageAssetBase).href : "";
}

function showViewerEmpty() {
  dom.emptyViewer.hidden = false;
  dom.continuousPages.hidden = true;
  dom.imageFrame.hidden = true;
  dom.pageIndicator.textContent = "0 / 0";
  dom.pagePrevious.disabled = true;
  dom.pageNext.disabled = true;
  dom.viewerTitle.textContent = t("panel.annotation");
  dom.viewerLocation.textContent = t("viewer.noneEvidence");
  const record = currentRecord();
  dom.viewerCaption.textContent = isSequenceField(state.selectedField) && record
    ? t(record[state.selectedField] ? "viewer.sequenceUnlocated" : "viewer.sequenceMissing") : "";
}

function renderInspector() {
  document.querySelectorAll("#inspector-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.tab);
  });
  const record = currentRecord();
  const evidence = currentEvidence();
  if (!record) {
    stopSequenceSpeech({ updateUi: false });
    dom.fieldKind.textContent = "—";
    dom.acceptField.disabled = true;
    dom.editField.disabled = true;
    dom.flagField.disabled = true;
    dom.inspectorContent.innerHTML = `<div class="empty-inspector"><p>${escapeHtml(t("inspector.empty"))}</p></div>`;
    return;
  }
  dom.acceptField.disabled = false;
  dom.editField.disabled = false;
  dom.flagField.disabled = false;
  const speechTargetVisible =
    state.tab === "ocr" &&
    isSequenceField(state.selectedField) &&
    state.speech.recordId === record.record_id &&
    state.speech.field === state.selectedField;
  if (state.speech.status === "playing" && !speechTargetVisible) {
    stopSequenceSpeech({ updateUi: false });
  }
  dom.fieldKind.textContent = state.selectedField === "ic50" && evidence?.endpoint
    ? evidence.endpoint : fieldLabel(state.selectedField);
  const renderers = {
    detection: renderDetectionInspector,
    ocr: renderOcrInspector,
    experiment: renderExperimentInspector,
  };
  const renderer = renderers[state.tab] || renderOcrInspector;
  dom.inspectorContent.innerHTML = renderer(record, evidence) + renderSdaFields(record);
  updateSequenceSpeechUi();
}

function renderSdaFields(record) {
  const field = {ic50: "value", ic50_unit: "unit", experiment_group: "metric"}[state.selectedField] || state.selectedField;
  const slot = record?.sda_fields?.[field];
  if (!slot) return "";
  const citations = (slot.provenance || []).map((item) => `<p class="context-copy">${escapeHtml(item.quote || item.table_id || item.source_type || "")}</p>`).join("");
  const failures = (record.sequence_ocr_failures || []).filter((item) => item.chain?.toLowerCase() === field)
    .map((item) => `<p class="context-copy">${escapeHtml(item.message || "")}</p>`).join("");
  const constantChain = {vh: "CH", vl: "CL"}[field];
  const constants = (record.constant_regions || []).filter((item) => item.region === constantChain)
    .map((item) => `<section class="inspector-section"><h3>${escapeHtml(item.region)}${item.seq_id ? ` · SEQ ID NO: ${escapeHtml(item.seq_id)}` : ""}</h3>
      <p class="context-copy">${escapeHtml(item.sequence || "")}</p></section>`).join("");
  return `<section class="inspector-section"><h3>${escapeHtml(t("sda.fields"))}</h3>
    ${propertyList([[t("sda.status"), t(`sda.${slot.status || "missing"}`)], [t("sda.source"), slot.provenance?.length || 0]])}
    ${failures}${citations || `<p class="context-copy">${escapeHtml(t("sda.noEvidence"))}</p>`}</section>${constants}`;
}

function renderDetectionInspector(record, evidence) {
  if (state.selectedField === "link_status") return renderSequenceReviewInspector(record);
  const candidate = state.candidateByTable.get(String(evidence?.table_id));
  const context = state.contextByTable.get(String(evidence?.table_id));
  if (!candidate) return emptyInspector(t("detect.none"));
  const table = state.ocrByTable.get(String(evidence?.table_id));
  const classification = state.classificationByTable.get(String(evidence?.table_id));
  const rowIndex = Number.isInteger(evidence?.row_index) ? Number(evidence.row_index) : 0;
  return `
    <section class="inspector-section">
      <h3>${escapeHtml(t("detect.caption"))}</h3>
      <p class="context-copy">${escapeHtml(context?.caption?.text || t("detect.noCaption"))}</p>
    </section>
    <section class="inspector-section">
      <h3>${escapeHtml(t("detect.content"))}</h3>
      ${renderTableContentPreview(table, rowIndex)}
    </section>
    <section class="inspector-section">
      <h3>${escapeHtml(t("detect.location"))}</h3>
      ${propertyList([
        ["table_id", candidate.table_id],
        [t("detect.page"), candidate.page],
        [t("detect.logicalTable"), context?.logical_table_id || "—"],
        [t("detect.tableNumber"), context?.table_number || t("detect.unconfirmed")],
        [t("detect.confidence"), formatConfidence(candidate.crop_confidence)],
        [t("ocr.classification"), classificationLabel(classification?.primary_label)],
        [t("ocr.classificationConfidence"), formatConfidence(classification?.confidence)],
        [t("ocr.orientation"), classificationOrientation(classification)],
        [t("ocr.classifierModel"), classification ? semanticAgentLabel(classification.classifier) : t("ocr.proseEvidence")],
        [t("ocr.templateModel"), semanticAgentLabel(state.template?.agent)],
        [t("ocr.classificationReason"), (classification?.reasons || []).join(" · ") || "—"],
        [t("ocr.classificationIssues"), (classification?.issues || []).map((item) => item.message).join(" · ") || "—"],
        ["PDF bbox", formatBbox(candidate.bbox)],
        [t("detect.contextStatus"), localizedStatus(context?.context_status || "missing")],
      ])}
    </section>`;
}

function renderSequenceReviewInspector(record) {
  const agent = record.agent_review || {};
  const anarci = record.anarci_review || {};
  const esm2 = record.esm2_review || {};
  const messages = [
    ...(Array.isArray(agent.messages) ? agent.messages : []),
    ...(anarci.message ? [anarci.message] : []),
    ...(esm2.message ? [esm2.message] : []),
    ...(record.record_verification?.findings || []).map((finding) => finding.message),
  ].join(" ") || "—";
  return `
    <section class="inspector-section">
      <h3>${escapeHtml(t("reviewCheck.title"))}</h3>
      <div class="review-check-inspector">${reviewCheckBadges(record)}</div>
    </section>
    <section class="inspector-section">
      <h3>${escapeHtml(t("reviewCheck.details"))}</h3>
      ${propertyList([
        [t("reviewCheck.agent"), t(`reviewCheck.${reviewCheckStatus(record, "agent")}`)],
        [t("reviewCheck.anarci"), t(`reviewCheck.${reviewCheckStatus(record, "anarci")}`)],
        [t("reviewCheck.esm2"), t(`reviewCheck.${reviewCheckStatus(record, "esm2")}`)],
        [t("reviewCheck.record"), t(`reviewCheck.${reviewCheckStatus(record, "record")}`)],
        [t("reviewCheck.vhAnarci"), anarci.chain_statuses?.VH || "—"],
        [t("reviewCheck.vlAnarci"), anarci.chain_statuses?.VL || "—"],
        [t("reviewCheck.message"), messages],
      ])}
    </section>
    ${renderSequenceEvaluationDetails(record)}
    ${(record.sequence_image_reviews || []).map((review) => `
      <section class="inspector-section">
        <h3>${escapeHtml(t("reviewCheck.image_corrected"))} · ${escapeHtml(review.chain || "")}</h3>
        <p>${escapeHtml(review.sequence_rationale || review.message || "")}</p>
      </section>`).join("")}
    ${(record.question_answers || []).map((item) => `
      <section class="inspector-section">
        <h3>${escapeHtml(t("reviewCheck.question_answer"))} · ${escapeHtml((item.chains || []).join(" / "))}</h3>
        ${propertyList([
          [t("reviewCheck.question"), item.question],
          [t("reviewCheck.answer"), item.answer || "—"],
          [t("reviewCheck.message"), [t(`reviewCheck.${item.status}`), item.reason].filter(Boolean).join(" · ")],
        ])}
      </section>`).join("")}
    ${(record.sequence_source_conflicts || []).map((conflict) => `
      <section class="inspector-section">
        <h3>${escapeHtml(t("reviewCheck.table_text_mismatch"))} · ${escapeHtml(conflict.chain || "")}</h3>
        <p>${escapeHtml(t(record.sequence_image_reviews?.some((review) => review.chain === conflict.chain)
          ? "reviewCheck.image_corrected" : conflict.sequence_status && conflict.sequence_status !== "resolved"
          ? "reviewCheck.table_unresolved" : "reviewCheck.table_gold_standard"))}</p>
        ${propertyList([
          [t("reviewCheck.table_value"), `${conflict.table_ref}: ${conflict.table_quote}`],
          [t("reviewCheck.text_value"), `${conflict.text_ref}: ${conflict.text_quote}`],
          [t("reviewCheck.message"), [conflict.message, conflict.sequence_rationale].filter(Boolean).join(" ")],
        ])}
      </section>`).join("")}`;
}

function renderSequenceEvaluationDetails(record) {
  const number = (value) => Number.isFinite(value) ? value.toFixed(4) : "—";
  return ["VH", "VL"].map((chain) => {
    const anarci = record.anarci_review?.chains?.[chain];
    const esm2 = record.esm2_review?.chains?.[chain];
    const domains = (anarci?.domains || []).map((domain) => `
      <section class="inspector-section">
        <h3>${escapeHtml(t("reviewCheck.anarciDomains"))} · ${chain}</h3>
        ${propertyList([
          [t("reviewCheck.chainType"), domain.chain_type || "—"],
          [t("reviewCheck.domainCoverage"), Number.isFinite(domain.coverage) ? `${(domain.coverage * 100).toFixed(1)}%` : "—"],
          ["HMM bitscore", number(domain.bitscore)],
          ...Object.entries(domain.regions || {}),
        ])}
      </section>`).join("");
    const scores = esm2?.metric_status === "scored" ? `
      <section class="inspector-section">
        <h3>${escapeHtml(t("reviewCheck.esm2Metrics"))} · ${chain}</h3>
        ${propertyList([
          [t("reviewCheck.meanPll"), number(esm2.mean_pll)],
          [t("reviewCheck.pseudoPerplexity"), number(esm2.pseudo_perplexity)],
          [t("reviewCheck.minLogProbability"), number(esm2.min_log_probability)],
          [t("reviewCheck.bottomDecile"), number(esm2.bottom_decile_mean)],
          [t("reviewCheck.sitePerplexity"), number(esm2.max_site_pseudo_perplexity)],
          [t("reviewCheck.siteThreshold"), number(esm2.site_screening?.max_pseudo_perplexity)],
          [t("reviewCheck.message"), esm2.thresholds || esm2.site_screening?.enabled ? esm2.message : t("reviewCheck.noThresholds")],
        ])}
        ${esm2.flagged_positions?.length ? `<h4>${escapeHtml(t("reviewCheck.flaggedSites"))}</h4>
          <p>${escapeHtml(t("reviewCheck.siteRiskNote"))}</p>
          <ul class="sequence-risk-sites">${esm2.flagged_positions.map((position) => `<li>
            <span class="review-check-badge review-check-error">${chain} · ${escapeHtml(position.position)} ${escapeHtml(position.residue)}</span>
            <span>PPL ${escapeHtml(number(position.pseudo_perplexity))} · P ${escapeHtml(Number.isFinite(position.probability) ? position.probability.toExponential(3) : "—")}</span>
          </li>`).join("")}</ul>` : ""}
      </section>` : "";
    return domains + scores;
  }).join("");
}

function renderExperimentInspector(record, evidence) {
  const assayEvidence = state.evidence.find((item) => item.kind === "assay") || evidence;
  const context = state.contextByTable.get(String(assayEvidence?.table_id));
  const protocolEvidence = (record.evidence || [])
    .find((item) => item?.type === "assay_protocol")?.evidence || [];
  const passageCandidates = [
    ...contextPassages(context),
    ...protocolEvidence.map((item) => ({ ...item, evidenceKind: "reference" })),
  ];
  const passages = [...new Map(
    passageCandidates.map((item) => [`${item.page}:${item.text}`, item]),
  ).values()].sort(compareContextPassages);
  const hasSummary = Boolean(
    record.assay_protocol_summary_zh || record.assay_protocol_summary_en,
  );
  if (!passages.length && !hasSummary && !record.assay_keyword_status
      && !record.experiment_name && !assayKeywordValues(record).length) return emptyInspector(t("experiment.none"));
  const summaries = codexExperimentSummaries(context, record, passages);
  const reasons = assayKeywordReviewReasons(record);
  const facts = record.assay_experiment?.facts || [];
  return `
    <section class="inspector-section experiment-keywords-section">
      <h3>${escapeHtml(t("field.experiment_keywords"))}</h3>
      ${propertyList([
        [t("field.experiment_group"), record.assay_endpoint || record.experiment_group || "—"],
        [t("field.experiment_name"), record.experiment_name || "—"],
      ])}
      <div class="experiment-keyword-panel">${assayKeywordBadges(record)}</div>
      ${reasons.length ? `<div class="experiment-review-reasons"><strong>${escapeHtml(t("experiment.reviewReasons"))}</strong><ul>${reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></div>` : ""}
      ${facts.length ? `<details class="experiment-fact-evidence"><summary>${escapeHtml(t("experiment.factEvidence"))}</summary>${facts.map(fact => `<article><strong>${escapeHtml(t(`experiment.dimension.${fact.field}`))} · ${escapeHtml(experimentFactLabel(record, fact))}</strong>${fact.evidence.map(citation => `<blockquote>${escapeHtml(citation.quote)}<small>${escapeHtml(citation.ref)}</small></blockquote>`).join("")}</article>`).join("")}</details>` : ""}
    </section>
    <section class="inspector-section experiment-process-section">
      <h3>${escapeHtml(t("experiment.summaryTitle"))}</h3>
      ${renderExperimentSummaries(summaries)}
    </section>
    <section class="inspector-section experiment-evidence-section">
      <h3>${escapeHtml(t("experiment.evidenceTitle"))}</h3>
      ${renderContextEvidence(context, passages)}
    </section>`;
}

function codexExperimentSummaries(context, record, passages) {
  const recordSummary = state.locale === "en"
    ? record.assay_protocol_summary_en || record.assay_protocol_summary_zh
    : record.assay_protocol_summary_zh || record.assay_protocol_summary_en;
  if (String(recordSummary || "").trim()) {
    const methodKeyword = assayKeywordValues(record).find((item) => item.category === "method");
    return [{
      method: localizedAssayKeyword(methodKeyword) || record.assay_method || t("experiment.unknownMethod"),
      metric: record.assay_endpoint || "",
      summary: recordSummary,
    }];
  }
  const provided = Array.isArray(context?.experiment_summaries)
    ? context.experiment_summaries
        .map((item) => ({
          method: item.method || t("experiment.unknownMethod"),
          metric: item.metric || "",
          summary: state.locale === "en"
            ? item.summary_en || item.summary || item.summary_zh
            : item.summary_zh || item.summary || item.summary_en,
        }))
        .filter((item) => String(item.summary || "").trim())
    : [];
  if (provided.length) return provided;

  const builtin = BUILTIN_CODEX_EXPERIMENT_SUMMARIES[String(context?.table_id)]?.[state.locale];
  if (builtin?.length) return builtin;

  const sourceText = passages.map((item) => item.text || "").join(" ");
  const method = /\bELISA\b/i.test(sourceText)
    ? "ELISA"
    : /\bSPR\b/i.test(sourceText)
      ? "SPR"
      : /\bBLI\b/i.test(sourceText)
        ? "BLI"
        : /流式|FACS|flow cytometry/i.test(sourceText)
          ? state.locale === "en" ? "Flow cytometry" : "流式细胞术"
          : t("experiment.unknownMethod");
  return [{
    method,
    metric: record?.assay_endpoint || "",
    summary: t("experiment.fallbackSummary", { method }),
  }];
}

function renderExperimentSummaries(summaries) {
  return `<div class="experiment-summary-list">${summaries
    .map(
      (item) => `<article class="experiment-summary-card">
        <div class="experiment-summary-meta">
          <span class="experiment-method-tag">${escapeHtml(item.method)}</span>
          ${item.metric ? `<span class="experiment-metric-tag">${escapeHtml(item.metric)}</span>` : ""}
          <span class="codex-summary-tag">${escapeHtml(t("experiment.codex"))}</span>
        </div>
        <p>${escapeHtml(item.summary)}</p>
      </article>`,
    )
    .join("")}</div>`;
}

function contextPassages(context) {
  return [
    ...(context?.nearby_context || []).map((item) => ({ ...item, evidenceKind: "nearby" })),
    ...(context?.referring_sentences || []).map((item) => ({ ...item, evidenceKind: "reference" })),
    ...(context?.footnotes || []).map((item) => ({ ...item, evidenceKind: "footnote" })),
  ]
    .filter((item) => String(item?.text || "").trim())
    .sort(compareContextPassages);
}

function renderContextEvidence(context, preparedPassages = null) {
  const passages = preparedPassages || contextPassages(context);

  if (!passages.length) {
    return `<p class="context-copy">${escapeHtml(t("detect.noNearby"))}</p>`;
  }

  return `
    <div class="context-evidence-list">
      ${passages
        .map(
          (item, index) => `
            <article class="context-evidence-card">
              <div class="context-evidence-meta">
                <span class="context-evidence-index">${index + 1}</span>
                <span class="context-evidence-type context-evidence-${escapeAttr(item.evidenceKind)}">${escapeHtml(contextPassageLabel(item))}</span>
                <span>${escapeHtml(t("detect.pageValue", { page: item.page ?? "—" }))}</span>
                <span>${escapeHtml(formatConfidence(item.confidence))}</span>
              </div>
              <p>${escapeHtml(item.text)}</p>
            </article>`,
        )
        .join("")}
    </div>`;
}

function compareContextPassages(left, right) {
  const pageDelta = Number(left?.page || 0) - Number(right?.page || 0);
  if (pageDelta) return pageDelta;
  const leftTop = Array.isArray(left?.bbox) ? Number(left.bbox[1]) : Number.POSITIVE_INFINITY;
  const rightTop = Array.isArray(right?.bbox) ? Number(right.bbox[1]) : Number.POSITIVE_INFINITY;
  if (leftTop !== rightTop) return leftTop - rightTop;
  const leftX = Array.isArray(left?.bbox) ? Number(left.bbox[0]) : Number.POSITIVE_INFINITY;
  const rightX = Array.isArray(right?.bbox) ? Number(right.bbox[0]) : Number.POSITIVE_INFINITY;
  return leftX - rightX;
}

function contextPassageLabel(item) {
  if (item.evidenceKind === "footnote") return t("detect.footnote");
  if (item.relation === "explicit_reference") return t("detect.explicitReference");
  if (item.relation === "implicit_reference") return t("detect.implicitReference");
  if (item.evidenceKind === "reference") return t("detect.referenceSentence");
  if (item.relation === "preceding_paragraph") return t("detect.precedingParagraph");
  if (item.relation === "following_paragraph") return t("detect.followingParagraph");
  return t("detect.nearbyParagraph");
}

function renderTableContentPreview(table, activeRowIndex) {
  const rows = Array.isArray(table?.rows) ? table.rows : [];
  if (!rows.length) return `<p class="context-copy">${escapeHtml(t("detect.noContent"))}</p>`;
  const windowSize = 7;
  const safeIndex = Math.max(0, Math.min(rows.length - 1, activeRowIndex));
  const start = Math.max(0, Math.min(rows.length - windowSize, safeIndex - 3));
  const visibleRows = rows.slice(start, start + windowSize);
  return `<div class="table-content-preview"><table><tbody>${visibleRows
    .map((row, offset) => {
      const rowIndex = start + offset;
      const cells = (row || []).filter((cell) => String(cell?.text || "").trim());
      return `<tr class="${rowIndex === safeIndex ? "active" : ""}">
        <th>R${rowIndex + 1}</th>
        ${cells.map((cell) => `<td title="${escapeAttr(cell.text)}">${escapeHtml(cell.text)}</td>`).join("")}
      </tr>`;
    })
    .join("")}</tbody></table></div>`;
}

function renderOcrInspector(record, evidence) {
  const table = state.ocrByTable.get(String(evidence?.table_id));
  const classification = state.classificationByTable.get(String(evidence?.table_id));
  const row = table?.rows?.[Number(evidence?.row_index)] || [];
  const cell = chooseActiveCell(row, state.selectedField, record);
  const evidenceValue = evidence?.kind === "assay"
    ? {
        ic50: evidence.value,
        ic50_unit: evidence.unit,
        experiment_group: evidence.experiment_group,
      }[state.selectedField]
    : undefined;
  const value = String(evidenceValue ?? effectiveValue(record, state.selectedField) ?? "");
  const isSequence = state.selectedField === "vh" || state.selectedField === "vl";
  const compactSequence = normalizeSequence(value);
  const speechActive = isCurrentSequenceSpeech(record, state.selectedField);
  const confidence = cell?.confidence == null ? null : Number(cell.confidence);
  const sourceText = cell?.text || (evidence?.segments || []).map((segment) => segment.text).join("\n");
  const correction = state.corrections[correctionKey(record.record_id, state.selectedField)];
  return `
    <section class="inspector-section">
      <h3>${escapeHtml(t("ocr.original"))}</h3>
      <pre class="ocr-value">${escapeHtml(sourceText || t("ocr.none"))}</pre>
      ${confidence == null ? "" : `<div class="confidence-bar" title="${escapeAttr(t("ocr.confidence"))} ${formatConfidence(confidence)}"><span style="width:${Math.max(2, confidence * 100)}%"></span></div>`}
    </section>
    <section class="inspector-section">
      ${
        isSequence
          ? `<div class="sequence-result-heading">
              <h3>${escapeHtml(t("ocr.sequenceResult", { field: state.selectedField.toUpperCase() }))}</h3>
              <button type="button" class="speech-button${speechActive ? " playing" : ""}"
                data-action="toggle-sequence-speech" aria-pressed="${speechActive}"
                aria-label="${escapeAttr(t(speechActive ? "speech.stopLabel" : "speech.playLabel", { field: state.selectedField.toUpperCase() }))}"
                ${compactSequence ? "" : "disabled"}>
                <span class="speech-button-icon" aria-hidden="true">${speechActive ? "■" : "▶"}</span>
                <span data-speech-button-label>${escapeHtml(t(speechActive ? "speech.stop" : "speech.play"))}</span>
              </button>
            </div>`
          : `<h3>${escapeHtml(t("ocr.result"))}</h3>`
      }
      ${
        isSequence
          ? `<div class="speech-progress" data-speech-progress ${speechActive ? "" : "hidden"}>${escapeHtml(sequenceSpeechProgress())}</div>
             <div class="sequence-ruler"><span>1</span><span>${Math.max(1, Math.round(compactSequence.length / 2))}</span><span>${compactSequence.length}</span></div>
             <div class="sequence-view sequence-chunk-list">${renderSequenceSpeechChunks(compactSequence, speechActive ? state.speech.chunkIndex : -1)}</div>`
          : `<pre class="ocr-value">${escapeHtml(value || "—")}</pre>`
      }
    </section>
    <section class="inspector-section">
      <h3>${escapeHtml(t("ocr.processing"))}</h3>
      ${propertyList([
        [t("ocr.backend"), table?.backend || "—"],
        [t("ocr.classifierModel"), semanticAgentLabel(classification?.classifier)],
        [t("ocr.templateModel"), semanticAgentLabel(state.template?.agent)],
        [t("ocr.classification"), classificationLabel(classification?.primary_label)],
        [t("ocr.extractionStatus"), classification?.extraction_status ? t(`ocr.extraction.${classification.extraction_status}`) : "—"],
        [t("ocr.classificationConfidence"), formatConfidence(classification?.confidence)],
        [t("ocr.orientation"), classificationOrientation(classification)],
        [t("ocr.sequenceScope"), evidence?.scope ? sequenceScopeLabel(evidence.scope, evidence.chain) : "—"],
        [t("ocr.length"), value.length],
        [t("ocr.correction"), correction ? t("ocr.saved") : t("ocr.noCorrection")],
        [t("ocr.reason"), correctionReasonLabels(correction)],
        [t("ocr.note"), correction?.note || "—"],
      ])}
    </section>
    ${renderTableMeasurements(evidence?.table_id)}`;
}

function renderTableMeasurements(tableId) {
  const values = (state.measurements || []).filter((item) => item.source_table === tableId);
  const comparisons = (state.comparisons || []).filter((item) => item.source_table === tableId);
  if (!values.length && !comparisons.length) return "";
  const rows = values.map((item) => `<tr title="${escapeAttr(item.value_ref)}">
    <td>${escapeHtml(item.antibody_id)}</td><td>${escapeHtml(item.assay_condition)}</td>
    <td>${escapeHtml(item.metric)}</td><td>${escapeHtml(`${item.operator === "=" ? "" : item.operator}${item.value} ${item.unit || ""}`)}${item.status === "source_checked" ? "" : ` · ${escapeHtml(t("ocr.measurementReview"))}`}</td></tr>`).join("");
  return `<section class="inspector-section">
    <details open><summary>${escapeHtml(t("ocr.allMeasurements"))} (${values.length})</summary>
      <p class="context-copy">${escapeHtml(t("ocr.measurementNote"))}</p>
      <div style="overflow-x:auto"><table class="measurement-preview"><thead><tr><th>Antibody</th>
        <th>${escapeHtml(t("ocr.assayCondition"))}</th><th>${escapeHtml(t("ocr.metric"))}</th><th>${escapeHtml(t("ocr.value"))}</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
    </details>
    ${comparisons.length ? `<details><summary>${escapeHtml(t("ocr.comparisons"))} (${comparisons.length})</summary>
      ${comparisons.map((item) => `<p class="context-copy" title="${escapeAttr(item.value_ref)}">${escapeHtml(`${item.numerator} / ${item.denominator} · ${item.assay_condition} · ${item.metric}: ${item.value}`)}${item.check === "consistent_with_rounding" ? "" : ` · ${escapeHtml(t("ocr.measurementReview"))}`}</p>`).join("")}</details>` : ""}
  </section>`;
}

function openEditDialog() {
  const record = currentRecord();
  if (!record) return;
  stopSequenceSpeech({ updateUi: true });
  const field = state.selectedField;
  if (!FIELD_LABEL_KEYS[field] || field === "assay_keywords") {
    showToast(t("toast.notEditable"), 2600);
    return;
  }
  const original = record[field] ?? "";
  const corrected = effectiveValue(record, field) ?? "";
  const existing = state.corrections[correctionKey(record.record_id, field)];
  dom.editDialogTitle.textContent = t("dialog.editTitle", { field: fieldLabel(field) });
  dom.originalValue.textContent = String(original);
  dom.correctedValue.value = String(corrected);
  const reasons = Array.isArray(existing?.reasons)
    ? existing.reasons
    : existing?.reason
      ? [existing.reason]
      : existing?.note
        ? ["other"]
        : [];
  setSelectedCorrectionReasons(reasons);
  dom.correctionReasonError.hidden = true;
  dom.correctionNote.value = existing?.note || "";
  dom.editDialog.showModal();
}

function closeEditDialog() {
  if (dom.editDialog.open) dom.editDialog.close("cancel");
}

function saveCorrection(event) {
  event.preventDefault();
  const record = currentRecord();
  if (!record) return;
  const reasons = selectedCorrectionReasons();
  if (!reasons.length) {
    dom.correctionReasonError.hidden = false;
    dom.correctionReasons.querySelector("input").focus();
    return;
  }
  const key = correctionKey(record.record_id, state.selectedField);
  const previousCorrection = state.corrections[key];
  state.corrections[key] = {
    document_id: recordDocumentId(record),
    record_id: record.record_id,
    reviewer_name: state.reviewerName,
    reviewer_history: updatedReviewerHistory(previousCorrection),
    value: parseCorrectionValue(state.selectedField, dom.correctedValue.value),
    reasons,
    reason: reasons[0],
    note: dom.correctionNote.value.trim(),
    updated_at: new Date().toISOString(),
    original_value: record[state.selectedField],
  };
  saveText("sda.studio.corrections", JSON.stringify(state.corrections));
  registerHumanReview(record, "corrected");
  dom.editDialog.close();
  renderGrid();
  renderDatasetOverview();
  renderInspector();
  showToast(t("toast.correctionSaved"), 3800);
}

function selectedCorrectionReasons() {
  return Array.from(dom.correctionReasons.querySelectorAll("input:checked"), (input) => input.value);
}

function setSelectedCorrectionReasons(reasons) {
  const selected = new Set(reasons);
  dom.correctionReasons.querySelectorAll("input").forEach((input) => {
    input.checked = selected.has(input.value);
  });
}

function correctionReasonLabels(correction) {
  if (!correction) return "—";
  const reasons = Array.isArray(correction.reasons)
    ? correction.reasons
    : correction.reason
      ? [correction.reason]
      : correction.note
        ? ["other"]
        : [];
  const labels = reasons.map((reason) => t(CORRECTION_REASON_KEYS[reason] || "correctionReason.other"));
  return labels.length ? labels.join(state.locale === "zh" ? "、" : ", ") : "—";
}

function setReviewStatus(status) {
  const record = currentRecord();
  if (!record) return;
  const key = correctionKey(record.record_id, state.selectedField);
  const previousReview = state.reviews[key];
  state.reviews[key] = {
    document_id: recordDocumentId(record),
    record_id: record.record_id,
    reviewer_name: state.reviewerName,
    reviewer_history: updatedReviewerHistory(previousReview),
    status,
    updated_at: new Date().toISOString(),
  };
  saveText("sda.studio.reviews", JSON.stringify(state.reviews));
  registerHumanReview(record, status);
  renderGrid();
  renderDatasetOverview();
  renderInspector();
  showToast(status === "accepted" ? t("toast.accepted") : t("toast.flagged"), 2600);
}

function exportCsv(scope) {
  if (scope === "patents") {
    const headers = [
      t("field.patentName"),
      t("field.pdfFile"),
      t("field.agentStrategy"),
      t("field.collectedCount"),
      t("field.pdfPages"),
      t("field.pdfForm"),
      t("field.patentText"),
      t("field.patentTextUrl"),
      t("field.agentStatus"),
      t("field.datasetMembership"),
    ];
    const rows = state.patents.map((patent) => {
      const status = patent.status || "queued";
      const stage = patent.stage || status;
      return [
        patentDisplayName(patent),
        patent.filename || "",
        patent.extraction_engine || "dataagent1",
        Number(patent.record_count || 0),
        Number(patent.page_count || 0),
        t(`pdfForm.${pdfFormKind(patent)}`),
        t(`patentText.${patentTextStatus(patent)}`),
        patentTextUrl(patent),
        `${t(`intake.status.${status}`)} · ${t(`intake.stage.${stage}`)}`,
        patent.dataset_published === true
          ? t("intake.datasetPublished", { count: Number(patent.published_record_count || 0) })
          : t("intake.datasetNotPublished"),
      ];
    });
    downloadCsv("SeqDataAgent_patent_collection.csv", headers, rows);
    return;
  }

  const aggregate = scope === "dataset";
  const sourceRecords = aggregate ? filteredOverviewRecords() : filteredRecords();
  const headers = [
    ...(!aggregate ? ["#"] : []),
    ...(aggregate ? [t("field.patent")] : []),
    fieldLabel("antibody_id"),
    fieldLabel("vh"),
    fieldLabel("vl"),
    fieldLabel("ic50"),
    fieldLabel("ic50_unit"),
    fieldLabel("experiment_group"),
    fieldLabel("assay_keywords"),
    t("reviewCheck.agent"),
    t("reviewCheck.anarci"),
    t("reviewCheck.esm2"),
    t("reviewCheck.record"),
    t("reviewCheck.table_text_mismatch"),
    t("field.humanReviewer"),
    t("field.evidence"),
  ];
  const rows = sourceRecords.map((record, index) => {
    const activePatent = !aggregate || record._patent_id === state.activePatentId;
    const value = (field) => activePatent ? effectiveValue(record, field) : record[field];
    const operator = record.ic50_operator || record.operator || "";
    const assayValue = `${operator === "=" ? "" : operator}${value("ic50") ?? "—"}`;
    return [
      ...(!aggregate ? [index + 1] : []),
      ...(aggregate ? [patentLabel(record._patent_id || record.document_id)] : []),
      value("antibody_id"),
      value("vh"),
      value("vl"),
      assayValue,
      value("ic50_unit"),
      value("experiment_group"),
      assayKeywordText(record),
      t(`reviewCheck.${reviewCheckStatus(record, "agent")}`),
      t(`reviewCheck.${reviewCheckStatus(record, "anarci")}`),
      t(`reviewCheck.${reviewCheckStatus(record, "esm2")}`),
      t(`reviewCheck.${reviewCheckStatus(record, "record")}`),
      (record.sequence_source_conflicts || []).map((conflict) =>
        `${conflict.chain || ""}: ${conflict.table_ref}=${conflict.table_quote}; ${conflict.text_ref}=${conflict.text_quote}`).join(" | "),
      recordReviewerNames(record).join(state.locale === "zh" ? "、" : ", "),
      aggregate ? overviewEvidenceCount(record) : buildEvidence(record, null).length,
    ];
  });
  downloadCsv(
    aggregate ? `${state.datasetFolders.find(folder => folder.dataset_id === state.activeDatasetId)?.name || "SeqDataAgent"}_dataset.csv` : `${state.activePatentId}_dataset.csv`,
    headers,
    rows,
  );
}

function downloadCsv(filename, headers, rows) {
  const csv = `\uFEFF${[headers, ...rows].map((row) => row.map(csvValue).join(",")).join("\n")}`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
  showToast(t("toast.exported"), 3000);
}

function setupSplitters() {
  makeSplitter(dom.mapSplitter, "vertical", (event) => {
    const rect = dom.annotationWorkspace.getBoundingClientRect();
    const width = Math.max(240, Math.min(520, rect.width * 0.55, rect.right - event.clientX));
    dom.annotationWorkspace.style.setProperty("--review-map-width", `${Math.round(width)}px`);
  });
  makeSplitter(dom.horizontalSplitter, "horizontal", (event) => {
    const topbarHeight = 58;
    const min = 170;
    const max = window.innerHeight - topbarHeight - 300;
    document.documentElement.style.setProperty(
      "--data-height",
      `${Math.max(min, Math.min(max, event.clientY - topbarHeight))}px`,
    );
  });
  makeSplitter(dom.verticalSplitter, "vertical", (event) => {
    const workspace = document.getElementById("evidence-workspace");
    const rect = workspace.getBoundingClientRect();
    const inspector = Math.max(360, Math.min(rect.width - 420, rect.right - event.clientX));
    document.documentElement.style.setProperty("--inspector-width", `${inspector}px`);
  });
}

function makeSplitter(element, axis, onMove) {
  element.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    element.classList.add("dragging");
    element.setPointerCapture(event.pointerId);
    const move = (nextEvent) => onMove(nextEvent);
    const end = () => {
      element.classList.remove("dragging");
      element.removeEventListener("pointermove", move);
      element.removeEventListener("pointerup", end);
    };
    element.addEventListener("pointermove", move);
    element.addEventListener("pointerup", end);
  });
  element.dataset.axis = axis;
}

function setZoom(value) {
  state.zoom = Math.max(0.45, Math.min(2.5, Number(value.toFixed(2))));
  renderViewer();
}

function filteredRecords() {
  const records = state.records.filter((record) => {
    if (!recordMatchesReviewChecks(record, state.status)) return false;
    if (!recordMatchesAssayKeywords(record, state.keywordFilter)) return false;
    if (!recordMatchesExperimentLabels(record, state.experimentLabelFilter)) return false;
    if (!recordMatchesHumanReviewers(record, state.reviewerFilter)) return false;
    if (!state.search) return true;
    const haystack = [
      effectiveValue(record, "antibody_id"),
      effectiveValue(record, "vh"),
      effectiveValue(record, "vl"),
      effectiveValue(record, "ic50"),
      effectiveValue(record, "experiment_group"),
      assayKeywordText(record),
      recordReviewerNames(record).join(" "),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(state.search);
  });
  return sortRecordsByUnit(records, state.unitSort);
}

function currentRecord() {
  return state.records.find((record) => record.record_id === state.selectedRecordId) || null;
}

function currentEvidence() {
  return state.evidence[state.selectedEvidenceIndex] || null;
}

function chooseActiveCell(row, field, record, evidence = currentEvidence()) {
  if ((field === "vh" || field === "vl") && Number.isInteger(evidence?.col_index) && row[evidence.col_index]) {
    return row[evidence.col_index];
  }
  if (((field === "ic50" || field === "ic50_unit") && evidence?.kind === "assay") || evidence?.kind === "entity_header") {
    if (Number.isInteger(evidence.col_index) && row[evidence.col_index]) return row[evidence.col_index];
  }
  const cells = row.filter((cell) => String(cell?.text || "").trim());
  if (!cells.length) return null;
  if (field === "ic50" || field === "ic50_unit") {
    const value = String(record?.ic50 ?? "");
    return cells.find((cell) => String(cell.text).includes(value)) || cells[cells.length - 1];
  }
  if (field === "antibody_id" || field === "experiment_group" || field === "assay_keywords") {
    const name = String(record?.antibody_id || "").toLowerCase();
    return (
      cells.find((cell) => name.includes(String(cell.text || "").toLowerCase())) || cells[0]
    );
  }
  return cells.reduce((longest, cell) =>
    String(cell.text || "").length > String(longest.text || "").length ? cell : longest,
  );
}

function currentFieldMarker(record, field) {
  const name = String(effectiveValue(record || {}, "antibody_id") || t("viewer.unnamedRecord")).trim();
  const endpoint = record?.assay_endpoint || t("field.ic50");
  const suffix = {
    antibody_id: t("field.antibody_id"),
    vh: "VH",
    vl: "VL",
    ic50: endpoint,
    ic50_unit: `${endpoint}${t("field.ic50_unit")}`,
    experiment_group: t("field.experiment_group"),
    assay_keywords: t("field.experiment_keywords"),
    link_status: t("field.link_status"),
  }[field] || fieldLabel(field);
  return t("viewer.currentField", { name, field: suffix });
}

function effectiveValue(record, field) {
  const correction = state.corrections[correctionKey(record.record_id, field)];
  return correction ? correction.value : record[field];
}

function effectiveStatus(record) {
  const hasCorrection = Object.keys(state.corrections).some((key) => key.startsWith(`${record.record_id}::`));
  const hasFlag = Object.entries(state.reviews).some(
    ([key, value]) => key.startsWith(`${record.record_id}::`) && value.status === "flagged",
  );
  if (hasFlag) return "review";
  if (hasCorrection) return "draft";
  return record.link_status || "review";
}

function formatAssayValue(record) {
  const value = effectiveValue(record, "ic50");
  const operator = record.ic50_operator || record.operator || "";
  return `${operator === "=" ? "" : operator}${value ?? "—"}`;
}

function getPageCount() {
  if (Number(state.pages?.page_count)) return Number(state.pages.page_count);
  if (state.pages?.pages?.length) return state.pages.pages.length;
  const pages = state.candidates.map((item) => Number(item.page || 0));
  return Math.max(0, ...pages);
}

function workspacePathToUrl(path) {
  if (!path) return "";
  const normalized = String(path).replaceAll("\\", "/");
  const marker = "/SDA/";
  const relative = normalized.includes(marker) ? normalized.split(marker).pop() : normalized.replace(/^\/+/, "");
  return serverAssetUrl(relative.split("/").map(encodeURIComponent).join("/"));
}

function correctionKey(recordId, field) {
  return `${recordId}::${field}`;
}

function parseCorrectionValue(field, rawValue) {
  if (field === "ic50") {
    const numeric = Number(rawValue.trim());
    return Number.isFinite(numeric) ? numeric : rawValue.trim();
  }
  return rawValue.trim();
}

function formatAssignments(assignments = {}) {
  return Object.entries(assignments)
    .map(([chain, seqId]) => `${chain}: SEQ ${seqId}`)
    .join(" · ");
}

function formatConfidence(value) {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(1)}%` : "—";
}

function formatBbox(value) {
  return Array.isArray(value) && value.length === 4
    ? value.map((item) => Number(item).toFixed(1)).join(", ")
    : "—";
}

function isSequenceField(field) {
  return field === "vh" || field === "vl";
}

function normalizeSequence(value) {
  return String(value || "").toUpperCase().replace(/[^A-Z]/g, "");
}

function sequenceSpeechChunks(value) {
  return normalizeSequence(value).match(/.{1,10}/g) || [];
}

function renderSequenceSpeechChunks(value, activeChunkIndex = -1) {
  const chunks = sequenceSpeechChunks(value);
  if (!chunks.length) return "—";
  return chunks
    .map((chunk, index) => {
      const start = index * 10 + 1;
      const active = index === activeChunkIndex;
      return `<button type="button" class="sequence-chunk${active ? " speaking" : ""}"
        data-action="play-sequence-chunk" data-chunk-index="${index}"
        aria-label="${escapeAttr(t("speech.chunkLabel", { start, letters: [...chunk].join(" ") }))}"
        ${active ? 'aria-current="true"' : ""}>${escapeHtml(chunk)}</button>`;
    })
    .join("");
}

function speechPlaybackSupported() {
  return "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
}

function isCurrentSequenceSpeech(record = currentRecord(), field = state.selectedField) {
  return Boolean(
    record &&
    state.speech.status === "playing" &&
    state.speech.recordId === record.record_id &&
    state.speech.field === field,
  );
}

function onInspectorSpeechClick(event) {
  const toggle = event.target.closest('button[data-action="toggle-sequence-speech"]');
  if (toggle) {
    if (isCurrentSequenceSpeech()) {
      stopSequenceSpeech();
    } else {
      startSequenceSpeech(0);
    }
    return;
  }

  const chunk = event.target.closest('button[data-action="play-sequence-chunk"]');
  if (!chunk) return;
  startSequenceSpeech(Number(chunk.dataset.chunkIndex) || 0);
}

function startSequenceSpeech(startChunkIndex = 0) {
  if (!speechPlaybackSupported()) {
    showToast(t("speech.unsupported"), 3200);
    return;
  }
  const record = currentRecord();
  if (!record || !isSequenceField(state.selectedField)) return;
  const chunks = sequenceSpeechChunks(effectiveValue(record, state.selectedField));
  if (!chunks.length) {
    showToast(t("speech.empty"), 2600);
    return;
  }

  stopSequenceSpeech({ updateUi: false });
  const chunkIndex = Math.max(0, Math.min(chunks.length - 1, Number(startChunkIndex) || 0));
  Object.assign(state.speech, {
    status: "playing",
    recordId: record.record_id,
    field: state.selectedField,
    chunks,
    chunkIndex,
    utterance: null,
    timer: null,
  });
  speakSequenceChunk(state.speech.runId);
}

function speakSequenceChunk(runId) {
  if (
    state.speech.status !== "playing" ||
    state.speech.runId !== runId ||
    !speechPlaybackSupported()
  ) {
    return;
  }
  const chunk = state.speech.chunks[state.speech.chunkIndex];
  if (!chunk) {
    finishSequenceSpeech(runId);
    return;
  }

  const utterance = new SpeechSynthesisUtterance([...chunk].join(", "));
  utterance.lang = "en-US";
  utterance.rate = 0.72;
  utterance.pitch = 1;
  state.speech.utterance = utterance;
  utterance.onend = () => {
    if (state.speech.runId !== runId || state.speech.status !== "playing") return;
    const nextIndex = state.speech.chunkIndex + 1;
    if (nextIndex >= state.speech.chunks.length) {
      finishSequenceSpeech(runId);
      return;
    }
    state.speech.chunkIndex = nextIndex;
    updateSequenceSpeechUi();
    state.speech.timer = window.setTimeout(() => {
      state.speech.timer = null;
      speakSequenceChunk(runId);
    }, 120);
  };
  utterance.onerror = (speechEvent) => {
    if (state.speech.runId !== runId) return;
    const expectedCancellation = ["canceled", "interrupted"].includes(speechEvent.error);
    finishSequenceSpeech(runId);
    if (!expectedCancellation) showToast(t("speech.failed"), 3600);
  };

  updateSequenceSpeechUi();
  window.speechSynthesis.speak(utterance);
}

function stopSequenceSpeech({ updateUi = true } = {}) {
  const wasActive = state.speech.status === "playing" || state.speech.timer != null;
  state.speech.runId += 1;
  if (state.speech.timer != null) window.clearTimeout(state.speech.timer);
  if (wasActive && speechPlaybackSupported()) window.speechSynthesis.cancel();
  Object.assign(state.speech, {
    status: "idle",
    recordId: null,
    field: null,
    chunks: [],
    chunkIndex: -1,
    utterance: null,
    timer: null,
  });
  if (updateUi) updateSequenceSpeechUi();
}

function finishSequenceSpeech(runId) {
  if (state.speech.runId !== runId) return;
  Object.assign(state.speech, {
    status: "idle",
    recordId: null,
    field: null,
    chunks: [],
    chunkIndex: -1,
    utterance: null,
    timer: null,
  });
  updateSequenceSpeechUi();
}

function sequenceSpeechProgress() {
  if (state.speech.status !== "playing" || state.speech.chunkIndex < 0) return "";
  const total = state.speech.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const start = state.speech.chunkIndex * 10 + 1;
  const end = Math.min(total, start + (state.speech.chunks[state.speech.chunkIndex]?.length || 1) - 1);
  return t("speech.progress", { start, end, total });
}

function updateSequenceSpeechUi() {
  if (!dom.inspectorContent) return;
  const record = currentRecord();
  const active = isCurrentSequenceSpeech(record, state.selectedField);
  const field = String(state.selectedField || "").toUpperCase();
  const button = dom.inspectorContent.querySelector('button[data-action="toggle-sequence-speech"]');
  if (button) {
    button.classList.toggle("playing", active);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-label", t(active ? "speech.stopLabel" : "speech.playLabel", { field }));
    const icon = button.querySelector(".speech-button-icon");
    const label = button.querySelector("[data-speech-button-label]");
    if (icon) icon.textContent = active ? "■" : "▶";
    if (label) label.textContent = t(active ? "speech.stop" : "speech.play");
  }

  const progress = dom.inspectorContent.querySelector("[data-speech-progress]");
  if (progress) {
    progress.hidden = !active;
    progress.textContent = active ? sequenceSpeechProgress() : "";
  }

  let activeChunk = null;
  dom.inspectorContent.querySelectorAll("button[data-chunk-index]").forEach((chunkButton) => {
    const speaking = active && Number(chunkButton.dataset.chunkIndex) === state.speech.chunkIndex;
    chunkButton.classList.toggle("speaking", speaking);
    if (speaking) {
      chunkButton.setAttribute("aria-current", "true");
      activeChunk = chunkButton;
    } else {
      chunkButton.removeAttribute("aria-current");
    }
  });
  activeChunk?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function propertyList(entries) {
  return `<dl class="property-list">${entries
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${isTrustedHtml(value) ? value : escapeHtml(value ?? "—")}</dd>`)
    .join("")}</dl>`;
}

function statusBadge(status) {
  const normalized = ["linked", "review", "conflict", "missing", "draft"].includes(status)
    ? status
    : "review";
  return `<span class="status-badge status-${normalized}">${escapeHtml(localizedStatus(normalized))}</span>`;
}

function isTrustedHtml(value) {
  return typeof value === "string" && value.startsWith('<span class="status-badge');
}

function emptyInspector(message) {
  return `<div class="empty-inspector"><p>${escapeHtml(message)}</p></div>`;
}

function csvValue(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function loadJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) || fallback;
  } catch {
    return fallback;
  }
}

function loadText(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function saveText(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    console.warn(`Unable to persist ${key}`, error);
  }
}

function reportFatalError(error) {
  console.error(error);
  const message = error?.message || String(error || "Unknown error");
  const app = document.getElementById("app");
  app?.setAttribute("aria-busy", "false");
  const rows = document.getElementById("record-rows");
  if (rows) {
    const prefix = state.locale === "en" ? "Initialization failed" : "初始化失败";
    rows.innerHTML = `<tr class="empty-row"><td colspan="9">${escapeHtml(prefix)}: ${escapeHtml(message)}</td></tr>`;
  }
}

let toastTimer = null;
function showToast(message, duration = 3000) {
  dom.toast.textContent = message;
  dom.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => dom.toast.classList.remove("show"), duration);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function toCamelCase(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

// Preserve the selected patent when switching to the document run report.
document.getElementById("run-overview-link")?.addEventListener("click", (event) => {
  event.preventDefault();
  window.location.href = `./run.html?document_id=${encodeURIComponent(state.activePatentId || "")}`;
});
