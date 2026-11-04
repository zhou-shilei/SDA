const escape = value => String(value ?? "").replace(/[&<>"']/g,
  character => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[character]));
const arrow = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 12h15m-6-6 6 6-6 6"/></svg>';
const documentIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M7 3h7l4 4v14H6V3h1Zm7 0v5h4M9 12h6m-6 4h6"/></svg>';
const details = {
  US20150183856A1: {page: 17, title: ["抗体序列与功能实验", "Antibody sequences & functional assays"],
    description: ["从 38 条记录开始，查看抗体序列、结合数据与功能实验，沿证据定位到专利原页。", "Start with 38 records. Explore antibody sequences, binding data and functional assays, with evidence linked to the original patent."],
    category: ["推荐入门", "START HERE"], tags: ["DLL4", "IC50", "KD"]},
  WO2026086808A1: {page: 27, title: ["抗 TSLP 抗体", "Anti-TSLP antibodies"],
    description: ["联查 VH/VL 序列、实验标签与结合数据。", "Connect VH/VL sequences, assay labels and binding data."],
    category: ["序列与实验", "SEQUENCES & ASSAYS"], tags: ["TSLP", "EC50", "KD"]},
  WO2025108394A1: {page: 10, title: ["跨实验数据联查", "Evidence across assays"],
    description: ["在 15 组实验间筛选记录，对照原文核对数值与序列来源。", "Explore 15 assays and verify measurement and sequence sources."],
    category: ["多实验", "MULTIPLE ASSAYS"], tags: ["IC50", "EC50", "KD"]},
  WO2024020591A9: {page: 205, title: ["长篇专利证据审阅", "Review a long patent"],
    description: ["从 263 页原文中定位多表证据，查看关联记录和待复核字段。", "Navigate 263 pages of source evidence, linked records and fields awaiting review."],
    category: ["长篇专利", "LONG DOCUMENT"], tags: ["TSLP", "EC50", "KD"]},
};
let cases = [];
let failed = false;

function render() {
  const english = document.documentElement.lang.startsWith("en");
  const text = (zh, en) => english ? en : zh;
  const localized = value => value[english ? 1 : 0];
  const number = value => Number(value || 0).toLocaleString(english ? "en" : "zh-CN");
  document.getElementById("experience-eyebrow").textContent = text("案例库", "EXAMPLES");
  document.getElementById("experience-title").textContent = text("探索真实专利数据", "Real patents. Traceable data.");
  document.getElementById("experience-subtitle").textContent = text(
    "从抗体序列到实验数值，每一条记录都能回到原文。",
    "Follow antibody sequences and measurements back to their original evidence.");
  document.querySelector("#experience-upload-link span").textContent = text("上传你的 PDF", "Upload your PDF");
  document.getElementById("experience-footer-note").textContent = text(
    "案例保留原始缺失和待复核状态。进入审阅后，可对照原文逐项核对。",
    "Original missing and pending states are preserved. Review each field against its source.");
  document.getElementById("experience-workspace-link").innerHTML = `${text("查看数据集", "View datasets")} <span aria-hidden="true">↗</span>`;
  document.getElementById("experience-library-meta").innerHTML = cases.length ? `
    <span>${documentIcon}<strong>${number(cases.length)}</strong> ${text("份真实专利", "real patents")}</span>
    <span><strong>${number(cases.reduce((sum, item) => sum + item.record_count, 0))}</strong> ${text("条实验记录", "measurements")}</span>
    <span class="case-meta-evidence"><span class="case-evidence-dot"></span>${text("支持原文证据定位", "Linked to source evidence")}</span>` : "";

  const info = item => details[item.document_id] || {page: 1,
    title: [item.title, item.title_en || item.title], description: [item.description, item.description_en || item.description],
    category: ["专利案例", "PATENT CASE"], tags: []};
  const reviewUrl = item => `?view=review&document_id=${encodeURIComponent(item.document_id)}`;
  const imageUrl = item => new URL(`../api/patents/${encodeURIComponent(item.document_id)}/pages/${String(info(item).page).padStart(4, "0")}.png`, import.meta.url).href;
  const tags = item => `<div class="case-tags">${info(item).tags.map(tag => `<span>${escape(tag)}</span>`).join("")}</div>`;
  const stats = (item, featured = false) => `<div class="case-stats${featured ? " case-stats-featured" : ""}">
    <span><strong>${number(item.record_count)}</strong>${text("条记录", "records")}</span>
    <span><strong>${number(item.page_count)}</strong>${text("页原文", "pages")}</span>
    <span><strong>${number(item.experiment_count)}</strong>${text("组实验", "assays")}</span></div>`;
  const first = cases[0];
  if (!first) {
    document.getElementById("experience-cards").innerHTML = `<div class="case-loading" role="status">${failed
      ? text("案例暂时无法读取，请刷新重试。", "Cases could not load. Please refresh to retry.")
      : text("正在准备案例…", "Preparing cases…")}</div>`;
    return;
  }
  document.getElementById("experience-cards").innerHTML = `
    <article class="experience-card case-feature">
      <div class="case-document-stage">
        <div class="case-stage-label">${documentIcon}<span>${text("原文预览", "SOURCE PREVIEW")}</span></div>
        <div class="case-paper-back" aria-hidden="true"></div>
        <div class="case-paper"><img src="${escape(imageUrl(first))}" alt="${escape(first.document_id)} · ${text("原文第", "Page ")}${info(first).page}${text("页", "")}" decoding="async" /></div>
        <span class="case-stage-page">p. ${info(first).page}<span>/ ${first.page_count}</span></span>
        <span class="case-stage-caption">${escape(first.document_id)}</span>
      </div>
      <div class="case-feature-content">
        <div class="case-feature-kicker"><span class="case-recommended">${text("推荐入门", "START HERE")}</span><span class="case-patent-id">CASE 01</span></div>
        <h3>${escape(localized(info(first).title))}</h3>
        <p class="case-description">${escape(localized(info(first).description))}</p>
        ${tags(first)}${stats(first, true)}
        <div class="case-feature-bottom"><a class="case-open-primary" href="${reviewUrl(first)}">${text("开始探索", "Explore this case")}${arrow}</a><span class="case-review-hint">${text("查看序列、实验与原文", "Sequences, assays & source evidence")}</span></div>
      </div>
    </article>
    <div class="case-section-heading"><h3>${text("更多案例", "More to explore")}<span>0${cases.length - 1}</span></h3><span>${text("选择不同规模与实验类型的专利", "Explore different document sizes and assays")}</span></div>
    <div class="case-list">${cases.slice(1).map(item => `
      <article class="experience-card case-row">
        <div class="case-thumbnail"><img src="${escape(imageUrl(item))}" alt="${escape(item.document_id)}" loading="lazy" decoding="async" /></div>
        <div class="case-row-content"><div class="case-row-kicker"><span>${escape(localized(info(item).category))}</span><span class="case-patent-id">${escape(item.document_id)}</span></div><h3>${escape(localized(info(item).title))}</h3><p>${escape(localized(info(item).description))}</p></div>
        ${stats(item)}
        <a class="case-open-secondary" href="${reviewUrl(item)}" aria-label="${escape(text("打开案例：", "Open case: ") + localized(info(item).title))}"><span>${text("打开案例", "Open case")}</span>${arrow}</a>
      </article>`).join("")}</div>`;
}

new MutationObserver(render).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
render();
fetch(new URL("../api/experience", import.meta.url), {cache: "no-store"})
  .then(response => { if (!response.ok) throw Error(response.status); return response.json(); })
  .then(payload => { cases = payload.cases || []; render(); })
  .catch(() => { failed = true; render(); });
