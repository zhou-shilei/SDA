// Read-only projections of existing facts. Missing metadata is never inferred
// from an endpoint, a molecule name, a table, or another experiment.
export const DATABASE_COLUMNS = [
  ["endpoint", "测量指标", "Endpoint", true],
  ["target", "实验靶点", "Assay target", true],
  ["antigen_species", "抗原物种", "Antigen species", true],
  ["purpose", "测量目的", "Measurement purpose", true],
  ["system", "实验材料类型", "Assay system", true],
  ["method", "检测方法", "Method", true],
  ["readout", "检测信号", "Readout", true],
  ["cell_model", "细胞模型", "Cell model", false],
  ["cell_host_species", "细胞宿主物种", "Cell host species", false],
  ["sample", "样本", "Sample", false],
  ["format", "抗体格式", "Antibody format", false],
  ["antigen_construct", "抗原构建体", "Antigen construct", false],
  ["conditions", "实验条件", "Assay conditions", false],
  ["donor", "供者", "Donor", false],
  ["replicate", "原文重复编号", "Reported replicate", false],
  ["sequence", "序列提供情况", "Sequence availability", true],
  ["value_kind", "数值形式", "Value form", true],
  ["entity_link", "分子关联", "Molecule link", false],
  ["experiment_link", "实验关联", "Experiment link", false],
  ["experiment_id", "实验 ID", "Experiment ID", false],
  ["batch_id", "原文实验批次", "Reported assay batch", false],
  ["comparison_group", "可比组", "Comparison group", true],
  ["comparison_status", "可比性审核", "Comparability review", true],
].map(([key, zh, en, visible]) => ({key, zh, en, visible}));

export const MISSING_VALUE = "__missing__";
export const columnLabel = (column, locale) => locale === "en" ? column.en : column.zh;
const text = value => typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
const list = value => Array.isArray(value) ? value : value == null ? [] : [value];
const entry = (key, zh = key, en = key) => ({key: String(key), zh, en});
const fieldEntries = value => list(value).map(text).filter(Boolean).map(v => entry(v));
const unique = values => [...new Map(values.map(v => [v.key, v])).values()];
const status = (key, zh, en) => [entry(key, zh, en)];

function keywords(record, category, entity = false) {
  return list(record[entity ? "entity_annotations" : "assay_keywords"])
    .filter(k => k && k.category === category)
    .map(k => {
      const key = text(k.key || k.value);
      return entry(key, text(k.label_zh) || key, text(k.label_en) || key);
    }).filter(k => k.key);
}

export function valueKind(record) {
  const raw = text(record.ic50);
  if (!raw) return [];
  const operator = text(record.ic50_operator || record.operator);
  const numeric = "[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][+-]?\\d+)?";
  if (/^[<≤]/.test(operator || raw)) return status("upper", "上界", "Upper bound");
  if (/^[>≥]/.test(operator || raw)) return status("lower", "下界", "Lower bound");
  if (new RegExp(`^${numeric}\\s*(?:[-–—~～]|to|至)\\s*${numeric}$`, "i").test(raw))
    return status("interval", "区间", "Interval");
  if (/^[~≈～]/.test(operator || raw)) return status("approximate", "近似值", "Approximate");
  if ((!operator || operator === "=") && new RegExp(`^=?\\s*${numeric}$`).test(raw))
    return status("exact", "精确数值", "Exact numeric value");
  if (/^(?:NA|N\/A|ND|N\.D\.|[-—–]|未测定|未检测)$/i.test(raw))
    return status("unreported", "未报告数值", "Not reported");
  if (/^(?:no binding|non[- ]?bind(?:ing|er)|not detected|不结合|未检出|阳性|阴性|positive|negative)$/i.test(raw))
    return status("qualitative", "定性结果", "Qualitative");
  return status("unparsed", "待解析", "Needs parsing");
}

export function databaseValues(record, key) {
  if (key === "endpoint") return fieldEntries(record.assay_endpoint);
  if (key === "sequence") {
    const values = [];
    const vhh = keywords(record, "format", true).some(k => /^(vhh|nanobody)$/i.test(k.key));
    if (vhh && (text(record.vhh) || text(record.vh))) values.push(entry("vhh", "VHH 已提供", "VHH provided"));
    else if (text(record.vh) && text(record.vl)) values.push(entry("paired", "VH/VL 已提供", "VH/VL provided"));
    else if (text(record.vh) || text(record.vl) || text(record.vhh)) values.push(entry("partial", "部分链已提供", "Some chains provided"));
    else values.push(entry("missing", "未提供可变区序列", "No variable sequence provided"));
    if (text(record.vh)) values.push(entry("vh", "VH 已提供", "VH provided"));
    if (text(record.vl)) values.push(entry("vl", "VL 已提供", "VL provided"));
    if (text(record.h_chain)) values.push(entry("full_heavy", "完整重链已提供", "Full heavy chain provided"));
    if (text(record.l_chain)) values.push(entry("full_light", "完整轻链已提供", "Full light chain provided"));
    if (list(record.sequence_source_conflicts).length) values.push(entry("conflict", "存在来源冲突", "Source conflict"));
    return values;
  }
  if (key === "value_kind") return valueKind(record);
  if (key === "comparison_status") {
    const labels = {candidate: ["候选待复核", "Candidate · needs review"],
      confirmed: ["已确认可比", "Confirmed comparable"], rejected: ["已标记不可比", "Marked incomparable"]};
    const value = text(record.comparison_status);
    return value ? [entry(value, ...(labels[value] || [value, value]))] : [];
  }
  if (key === "entity_link" || key === "experiment_link") {
    const id = key === "entity_link" ? record.entity_id || record.canonical_entity_id : record.experiment_id;
    return text(id) ? status("linked", "已记录关联", "Link recorded") : status("unknown", "未记录关联", "No link recorded");
  }
  const fields = {target: "antigen_target", method: "assay_method", system: "assay_system",
    antigen_species: "antigen_species", purpose: "assay_purpose", readout: "assay_readout",
    cell_model: "cell_model", cell_host_species: "cell_host_species", sample: "sample",
    format: "construct_type", conditions: "assay_condition", donor: "donor_id", replicate: "replicate_id",
    antigen_construct: "antigen_construct", experiment_id: "experiment_id", batch_id: "batch_id",
    comparison_group: "comparison_group_id", comparison_status: "comparison_status"};
  const tags = keywords(record, key, key === "format");
  // Prefer categorized annotations to avoid duplicate localized/raw versions.
  if (tags.length) return unique(tags);
  const values = fields[key] ? fieldEntries(record[fields[key]]) : [];
  // Short IDs are local to a document; G1 from two patents is not one group.
  const document = text(record.document_id || record._patent_id);
  if (document && ["experiment_id", "batch_id", "comparison_group"].includes(key)) {
    return values.map(v => entry(`${document}:${v.key}`, `${document} · ${v.zh}`, `${document} · ${v.en}`));
  }
  return values;
}

export function localizedValues(record, key, locale = "zh") {
  return databaseValues(record, key).map(v => locale === "en" ? v.en : v.zh);
}

export function databaseFilterOptions(records, key, locale = "zh") {
  const counts = new Map();
  for (const record of records) {
    const values = databaseValues(record, key);
    for (const value of values.length ? values : [entry(MISSING_VALUE, "未提供", "Not provided")]) {
      const current = counts.get(value.key);
      if (current) current.count++;
      else counts.set(value.key, {value: value.key, label: locale === "en" ? value.en : value.zh, count: 1});
    }
  }
  return [...counts.values()].sort((a, b) => a.label.localeCompare(b.label, locale === "en" ? "en" : "zh-CN"));
}

export function matchesDatabaseFilters(record, filters) {
  return DATABASE_COLUMNS.every(({key}) => {
    const selected = filters?.[key];
    if (!selected?.length) return true;
    const values = databaseValues(record, key).map(v => v.key);
    return selected.some(value => values.length ? values.includes(value) : value === MISSING_VALUE);
  });
}

const escape = value => String(value ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));

export function databaseValueMarkup(record, key, locale = "zh") {
  const en = locale === "en";
  const values = databaseValues(record, key);
  const label = value => en ? value.en : value.zh;
  if (key === "sequence") {
    const present = new Set(values.map(v => v.key));
    const choices = [["vh", "VH 已提供", "VH provided"], ["vl", "VL 已提供", "VL provided"],
      ["full_heavy", "完整重链已提供", "Full heavy chain provided"],
      ["full_light", "完整轻链已提供", "Full light chain provided"]];
    if (present.has("vhh")) choices.unshift(["vhh", "VHH 已提供", "VHH provided"]);
    return choices.filter(([id]) => present.has(id)).map(([id, zh, english]) =>
      `<span class="database-sequence-option" data-sequence-availability="${id}">${escape(en ? english : zh)}</span>`).join("")
      + (present.has("conflict") ? `<span class="experiment-keyword-badge keyword-conflict">${escape(label(values.find(v => v.key === "conflict")))}</span>` : "");
  }
  if (!values.length) return `<span class="experiment-keyword-empty">${en ? "Not provided" : "未提供"}</span>`;
  return values.map(value => {
    const category = key === "comparison_status" ? value.key : key;
    const displayed = key === "comparison_group" ? text(record.comparison_group_id) : label(value);
    return `<span class="experiment-keyword-badge keyword-${escape(category)}" title="${escape(label(value))}">${escape(displayed)}</span>`;
  }).join("");
}
