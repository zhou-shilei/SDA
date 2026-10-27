const LABELS = {
  zh: {
    time: "处理用时", tokens: "Token 用量", missing: "未记录", pending: "记录中", running: "已用", total: "总用时",
    details: "悬停、聚焦或点击查看明细", operations: "最近记录的操作", counts: "次",
    overlap: "并行阶段会重叠，明细用时不能直接相加。",
    cumulative: "累计自动处理用时，不包含暂停。", reanalysis: "本次复用 OCR 的重新提取用时。",
    stageOnly: "只记录了处理阶段用时，不代表整篇 PDF 总用时。", noStages: "阶段明细未记录。",
    input: "输入", output: "输出", cached: "其中缓存输入", unknown: "次请求用量未知；这里只显示已知部分。",
    tokenNote: "1K = 1,000 tokens，展示值四舍五入。总量为输入 + 输出，缓存输入已包含在输入中。本地 OCR 解码 Token 未计入。",
    structured_document: "六步文档处理", load_pdf: "加载 PDF", orientation_and_layout: "方向校正、定位与裁图",
    recognition: "正文 / 表格 OCR", assembly: "文档组装", export: "文档导出", layout: "版面检测与裁图",
    text_ocr: "全文 OCR", table_ocr: "表格 OCR", table_ocr_and_extraction: "表格 OCR 与信息抽取",
    document_extraction: "全文信息抽取", document_repair: "缺口补提", document_resume: "补全与续跑",
    sequence_mapping: "序列映射", template_sequence_plan: "序列规划", template_sequence_verify: "序列核验",
    template_agent: "模板整理", template_completion: "模板补全", text_sequence_extraction: "正文序列提取",
    data_construction: "数据构建", schema_completion: "字段补全", result_io: "结果保存", ocr_cache: "OCR 缓存读取",
    unknownStage: "未标记阶段",
  },
  en: {
    time: "Processing time", tokens: "Token usage", missing: "Not recorded", pending: "Recording", running: "Elapsed", total: "Total time",
    details: "Hover, focus or click for details", operations: "Latest recorded operations", counts: "runs",
    overlap: "Concurrent stages overlap; their durations cannot be added to obtain total time.",
    cumulative: "Cumulative automated processing time, excluding pauses.", reanalysis: "This extraction run reuses OCR.",
    stageOnly: "Only processing-stage time was recorded, not full PDF processing time.", noStages: "Stage details were not recorded.",
    input: "Input", output: "Output", cached: "Cached input (included)", unknown: "requests have unknown usage; only known usage is shown.",
    tokenNote: "1K = 1,000 tokens; displayed values are rounded. Total = input + output; cached input is included in input. Local OCR decoder tokens are excluded.",
    structured_document: "Document preprocessing", load_pdf: "Load PDF", orientation_and_layout: "Orientation, layout and crops",
    recognition: "Text / table OCR", assembly: "Document assembly", export: "Document export", layout: "Layout and cropping",
    text_ocr: "Full-text OCR", table_ocr: "Table OCR", table_ocr_and_extraction: "Table OCR and extraction",
    document_extraction: "Document extraction", document_repair: "Coverage repair", document_resume: "Completion / resume",
    sequence_mapping: "Sequence mapping", template_sequence_plan: "Sequence planning", template_sequence_verify: "Sequence verification",
    template_agent: "Template processing", template_completion: "Template completion", text_sequence_extraction: "Text sequence extraction",
    data_construction: "Dataset construction", schema_completion: "Field completion", result_io: "Save results", ocr_cache: "Read OCR cache",
    unknownStage: "Unlabelled stage",
  },
};

const valid = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const labels = (locale) => LABELS[locale === "en" ? "en" : "zh"];

export function completedRunUsage(metrics, report) {
  // An older running server can still expose its latest cache-only snapshot.
  // Its completed run report retains the original cumulative request usage.
  if (!report?.resumed || !report.token_usage?.totals) return metrics;
  const row = (value) => ({ ...value, unknown_requests: Math.max(value.requests_without_usage || 0,
    value.field_coverage?.total_tokens?.unknown || 0) });
  return { ...metrics, tokens: { available: true, totals: row(report.token_usage.totals),
    stages: (report.token_usage.by_stage || []).map(row) } };
}

export function formatTokenK(value, locale = "zh") {
  if (!valid(value)) return labels(locale).missing;
  return new Intl.NumberFormat(locale === "en" ? "en-US" : "zh-CN", {
    minimumFractionDigits: 2, maximumFractionDigits: value > 0 && value < 1000 ? 3 : 2,
  }).format(value / 1000) + "K";
}

export function formatProcessingTime(value, locale = "zh") {
  if (!valid(value)) return labels(locale).missing;
  const zh = locale !== "en";
  if (value < 60) return `${Number(value.toFixed(value < 1 ? 2 : 1))}${zh ? "秒" : "s"}`;
  const seconds = Math.floor(value), hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
  return zh ? `${hours ? `${hours}小时` : ""}${minutes}分${seconds % 60}秒`
    : `${hours ? `${hours}h ` : ""}${minutes}m ${seconds % 60}s`;
}

function metricButton(value, description, key, label, locale) {
  return `<button type="button" class="processing-metric" data-metric-key="${escape(key)}" data-processing-tooltip="${escape(description)}" aria-label="${escape(`${label}: ${value}. ${labels(locale).details}`)}">${escape(value)}</button>`;
}

export function timeMetricMarkup(metrics, documentId, locale = "zh", now = Date.now()) {
  const data = metrics?.time || {}, l = labels(locale);
  const started = Date.parse(data.started_at || "");
  const seconds = data.running && Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : data.seconds;
  const value = valid(seconds) ? formatProcessingTime(seconds, locale) : data.running ? l.pending : l.missing;
  const lines = [`${data.running ? l.running : l.total}: ${value}`];
  if (data.scope === "cumulative") lines.push(l.cumulative);
  if (data.scope === "reanalysis") lines.push(l.reanalysis);
  if (data.scope === "stage_only") lines.push(l.stageOnly);
  if (data.stages?.length) {
    lines.push("");
    for (const row of data.stages) lines.push(`${"  ".repeat(Math.min(3, row.level || 0))}${l[row.stage] || row.stage || l.unknownStage}${row.count > 1 ? ` (${row.count} ${l.counts})` : ""}: ${formatProcessingTime(row.seconds, locale)}`);
  } else lines.push(l.noStages);
  if (data.operations?.length) {
    lines.push("", `${l.operations}:`);
    for (const row of data.operations) lines.push(`  ${l[row.stage] || row.stage || l.unknownStage}: ${formatProcessingTime(row.seconds, locale)}`);
  }
  lines.push("", l.overlap);
  return metricButton(value, lines.join("\n"), `${documentId}:time`, l.time, locale);
}

export function tokenMetricMarkup(metrics, documentId, locale = "zh") {
  const data = metrics?.tokens || {}, totals = data.totals || {}, l = labels(locale);
  const amount = (row) => `${row.unknown_requests && valid(row.total_tokens) ? "≥" : ""}${formatTokenK(row.total_tokens, locale)}`;
  const value = data.available ? amount(totals) : metrics?.time?.running ? l.pending : l.missing;
  const lines = [`${l.tokens}: ${value}`];
  if (data.available) {
    lines.push(`${l.input}: ${formatTokenK(totals.input_tokens, locale)}`, `${l.output}: ${formatTokenK(totals.output_tokens, locale)}`,
      `${l.cached}: ${formatTokenK(totals.cached_input_tokens, locale)}`);
    if (data.stages?.length) {
      lines.push("");
      for (const row of data.stages) lines.push(`${l[row.stage] || row.stage || l.unknownStage}: ${amount(row)}`,
        `  ${l.input} ${formatTokenK(row.input_tokens, locale)} · ${l.output} ${formatTokenK(row.output_tokens, locale)}`);
    } else lines.push(l.noStages);
    if (totals.unknown_requests) lines.push("", `${totals.unknown_requests} ${l.unknown}`);
  }
  lines.push("", l.tokenNote);
  return metricButton(value, lines.join("\n"), `${documentId}:tokens`, l.tokens, locale);
}

export function setupMetricTooltips(container) {
  if (!container) return { refresh() {}, hide() {} };
  const tooltip = document.createElement("div");
  tooltip.className = "processing-metric-tooltip";
  tooltip.id = "processing-metric-tooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.append(tooltip);
  let active = null, pinned = false;
  const buttonFor = (target) => target instanceof Element ? target.closest("[data-processing-tooltip]") : null;
  function position() {
    if (!active?.isConnected) return hide();
    const rect = active.getBoundingClientRect();
    tooltip.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - tooltip.offsetWidth - 12))}px`;
    const below = rect.bottom + 8;
    tooltip.style.top = `${Math.max(12, below + tooltip.offsetHeight > innerHeight - 12 ? rect.top - tooltip.offsetHeight - 8 : below)}px`;
  }
  function show(button) {
    if (active && active !== button) active.removeAttribute("aria-describedby");
    active = button;
    tooltip.textContent = button.dataset.processingTooltip;
    tooltip.hidden = false;
    button.setAttribute("aria-describedby", tooltip.id);
    position();
  }
  function hide() {
    active?.removeAttribute("aria-describedby");
    active = null;
    pinned = false;
    tooltip.hidden = true;
  }
  container.addEventListener("pointerover", (event) => {
    const button = buttonFor(event.target);
    if (button && button !== active) { pinned = false; show(button); }
  });
  container.addEventListener("pointerout", (event) => {
    if (active && !pinned && !active.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)
        && !active.matches(":focus")) hide();
  });
  container.addEventListener("focusin", (event) => { const button = buttonFor(event.target); if (button) show(button); });
  container.addEventListener("focusout", () => { if (!pinned) hide(); });
  container.addEventListener("click", (event) => {
    const button = buttonFor(event.target);
    if (!button) return;
    if (pinned && active === button) hide();
    else { pinned = true; show(button); }
  });
  tooltip.addEventListener("pointerleave", (event) => { if (!pinned && !active?.contains(event.relatedTarget)) hide(); });
  document.addEventListener("pointerdown", (event) => { if (!buttonFor(event.target) && !tooltip.contains(event.target)) hide(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
  document.addEventListener("scroll", () => { if (active) position(); }, true);
  window.addEventListener("resize", () => { if (active) position(); });
  return { hide, refresh() {
    if (!active) return;
    const key = active.dataset.metricKey;
    const replacement = [...container.querySelectorAll("[data-metric-key]")].find((node) => node.dataset.metricKey === key);
    if (replacement) show(replacement); else hide();
  } };
}
