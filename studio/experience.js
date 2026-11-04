const escape = value => String(value ?? "").replace(/[&<>"']/g,
  character => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[character]));
let cases = [];
let failed = false;

function render() {
  const english = document.documentElement.lang.startsWith("en");
  const text = (zh, en) => english ? en : zh;
  document.getElementById("experience-title").textContent = text("从一个真实案例开始", "Start with a real case");
  document.getElementById("experience-subtitle").textContent = text(
    "查看抗体序列与实验数据，沿证据回到专利原页，再尝试审阅和导出。",
    "Explore antibody sequences and measurements, follow evidence to the patent, then review and export.");
  document.getElementById("experience-steps").innerHTML = [
    text("01 · 选择案例", "01 · Choose a case"), text("02 · 核对原文证据", "02 · Inspect source evidence"),
    text("03 · 审阅与导出", "03 · Review and export")].map(step => `<span>${step}</span>`).join("");
  document.getElementById("experience-upload-title").textContent = text("也可以试试你自己的 PDF", "Bring your own PDF");
  document.getElementById("experience-upload-description").textContent = text(
    "上传专利，进入解析队列，再用同一套界面核对结果。", "Upload a patent, join the processing queue and review the results in the same workspace.");
  document.getElementById("experience-upload-link").textContent = text("上传专利 PDF →", "Upload patent PDF →");
  document.getElementById("experience-cards").innerHTML = cases.map((item, index) => `
    <article class="experience-card">
      <div class="experience-card-top"><span class="experience-case-number">CASE ${String(index + 1).padStart(2, "0")}</span><span class="experience-case-state">${text("真实专利 · 保留待复核项", "Real patent · Review status preserved")}</span></div>
      <h3>${escape(english ? item.title_en : item.title)}</h3>
      <p class="experience-case-description">${escape(english ? item.description_en : item.description)}</p>
      <p class="experience-patent-id">${escape(item.document_id)}</p>
      <div class="experience-case-stats"><div><strong>${item.record_count}</strong><span>${text("条实验记录", "measurements")}</span></div><div><strong>${item.page_count}</strong><span>${text("页专利原文", "patent pages")}</span></div><div><strong>${item.experiment_count}</strong><span>${text("组实验", "assays")}</span></div></div>
      <div class="experience-card-bottom"><span>${item.paired_count} ${text("条记录已提供 VH/VL", "records with VH/VL")}</span><a class="button button-primary" href="?view=review&document_id=${encodeURIComponent(item.document_id)}">${text("进入案例审阅 →", "Explore case →")}</a></div>
    </article>`).join("") || `<p>${failed ? text("案例读取失败，请刷新页面重试。", "Cases could not load. Refresh to retry.") : text("正在读取案例…", "Loading cases…")}</p>`;
}

new MutationObserver(render).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
render();
fetch(new URL("../api/experience", import.meta.url), {cache: "no-store"})
  .then(response => { if (!response.ok) throw Error(response.status); return response.json(); })
  .then(payload => { cases = payload.cases || []; render(); })
  .catch(() => { failed = true; render(); });
