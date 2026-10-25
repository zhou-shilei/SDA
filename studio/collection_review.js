// Collection decisions are persisted by the server; notes stay local until saved.
export const escapeReviewHtml = (value) => String(value ?? "").replace(/[&<>"']/g,
  (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]);

export class CollectionReviewPanel {
  constructor(element, options) {
    this.element = element;
    this.options = options;
    this.drafts = new Map();
    this.generation = 0;
    element.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-cr]");
      if (button && !button.disabled) this.act(button.dataset.cr, button.dataset.id);
    });
    element.addEventListener("input", (event) => {
      if (event.target.matches("textarea")) {
        this.drafts.set(`${this.documentId}:${this.taskId}`, event.target.value);
        const prepare = this.element.querySelector('[data-cr="prepare"]');
        if (prepare) prepare.disabled = true;
      }
    });
    element.addEventListener("change", (event) => {
      if (event.target.matches("select[data-cr-tasks]")) {
        this.taskId = event.target.value;
        this.error = "";
        this.render();
        this.locate();
      }
    });
    element.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.close();
    });
  }

  l(zh, en) { return this.options.locale() === "en" ? en : zh; }
  async responseData(response) {
    if (!response.headers.get("content-type")?.includes("application/json")) {
      throw Error(this.l("采集确认接口请求失败，请重试。", "The collection review request failed. Please retry.") + ` (HTTP ${response.status})`);
    }
    return response.json();
  }
  task() { return this.data?.tasks.find((task) => task.id === this.taskId); }
  taskTitle(task) {
    if (task.question_number) return `Q${task.question_number}. ${this.options.locale() === "en" ? task.question_en : task.question}`;
    return task.title;
  }
  close() { this.element.hidden = true; clearTimeout(this.timer); this.generation++; this.options.onClose?.(); }

  async open(documentId) {
    this.generation++;
    this.documentId = documentId;
    this.taskId = null;
    this.data = null;
    this.error = "";
    this.notice = "";
    this.saving = false;
    this.questionAttempt = null;
    this.element.hidden = false;
    this.render();
    await this.load();
    if (!this.element.hidden) this.element.querySelector("button")?.focus();
  }

  async load() {
    clearTimeout(this.timer);
    const generation = this.generation;
    try {
      const response = await fetch(`${this.options.url}?document_id=${encodeURIComponent(this.documentId)}`, {cache: "no-store"});
      const data = await this.responseData(response);
      if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
      if (generation !== this.generation) return;
      this.error = "";
      this.data = data;
      this.options.onData?.(data);
      if (!this.task()) this.taskId = data.tasks.find((t) => !t.decision_current)?.id || data.tasks[0]?.id;
      this.render();
      if (data.busy || ["queued", "running"].includes(data.question_agent?.status)) this.timer = setTimeout(() => this.load(), 3500);
      if (!data.busy && data.tasks.length && ["missing", "stale"].includes(data.question_agent?.status)
          && this.questionAttempt !== data.source_revision) {
        this.questionAttempt = data.source_revision;
        await this.act("questions");
      }
    } catch (error) {
      if (generation !== this.generation) return;
      this.error = error.message;
      this.render();
    }
  }

  locate(ref) {
    const task = this.task();
    if (task) this.options.navigate(task, ref);
  }

  async act(action, id) {
    if (action === "close") return this.close();
    if (action === "refresh") { this.error = ""; this.render(); return this.load(); }
    if (action === "source") return this.locate(id);
    if (action === "names") {
      this.taskId = this.data.tasks.find(t => t.name_issue && !t.decision_current)?.id
        || this.data.tasks.find(t => t.name_issue)?.id;
      this.render(); return;
    }
    if (action === "next") {
      const tasks = this.data.tasks;
      this.taskId = tasks[(tasks.findIndex(t => t.id === this.taskId) + 1) % tasks.length]?.id;
      this.render(); this.locate(); return;
    }
    const reviewer = this.options.reviewer();
    if (!reviewer && action !== "questions") { this.options.requestIdentity(); return; }
    if (!this.data || this.saving) return;
    const task = this.task();
    const answering = action.startsWith("answer_");
    const operation = ["prepare", "apply", "questions"].includes(action) ? action : "decide";
    const note = this.drafts.get(`${this.documentId}:${this.taskId}`) ?? task?.decision?.note ?? "";
    const payload = {document_id: this.documentId, operation, action: answering ? "answer" : action, note,
      task_id: task?.id, reviewer_name: reviewer, version: this.data.version,
      source_revision: this.data.source_revision, job_id: this.data.job?.id,
      ...(answering ? {answer: action.slice(7), question_fingerprint: task.fingerprint} : {})};
    this.saving = true;
    this.error = "";
    this.notice = "";
    this.render();
    const generation = this.generation;
    const documentId = this.documentId;
    try {
      const response = await fetch(this.options.url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const data = await this.responseData(response);
      if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
      if (generation !== this.generation) return;
      this.data = data;
      this.options.onData?.(data);
      if (!this.task()) this.taskId = data.tasks.find(t => !t.decision_current)?.id || data.tasks[0]?.id;
      this.notice = operation === "decide"
        ? this.l("回答已保存，将用于后续数据采集。", "Your answer is saved for subsequent data collection.")
        : operation === "questions" ? ""
        : operation === "apply" ? this.l("已应用到工作区。请审阅记录，再选择需要入库的数据。", "Applied to the workspace. Review and select records before publishing.")
        : this.l("Agent 正在依据已保存的确认意见生成候选。可以关闭面板，稍后回来查看。", "The Agent is preparing a candidate from saved decisions. You can close this panel and return later.");
      if (operation === "decide") this.drafts.delete(`${this.documentId}:${task.id}`);
      if (operation === "apply") await this.options.onApplied(documentId);
      if (operation === "prepare") { this.data.busy = true; this.timer = setTimeout(() => this.load(), 1500); }
      if (operation === "questions" && ["queued", "running"].includes(data.question_agent?.status)) this.timer = setTimeout(() => this.load(), 3500);
    } catch (error) {
      if (generation === this.generation) this.error = error.message;
    } finally {
      if (generation === this.generation) { this.saving = false; this.render(); }
    }
  }

  render() {
    if (this.element.hidden) return;
    const e = escapeReviewHtml;
    const l = (zh, en) => this.l(zh, en);
    const data = this.data;
    const task = this.task();
    const sameTask = this.renderedTaskId === task?.id;
    const scroll = sameTask ? this.element.querySelector?.(".collection-content")?.scrollTop : 0;
    const opened = sameTask ? [...(this.element.querySelectorAll?.("details") || [])].map(detail => detail.open) : [];
    const note = typeof document !== "undefined" && document.activeElement?.id === "collection-note" && sameTask
      ? {start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd} : null;
    const busy = this.saving || data?.busy;
    const disabled = busy ? "disabled" : "";
    const decision = task?.decision;
    const actions = {confirm: l("确认纳入", "Include"), adjust: l("提交修改", "Correct"),
      exclude: l("本表不采集", "Exclude table"), recheck: l("请 Agent 再查", "Ask Agent to recheck"), unsure: l("暂时无法判断", "Unsure")};
    const answered = data?.tasks.filter(t => t.decision_current && t.decision.action !== "unsure" && t.decision.answer !== "unsure").length || 0;
    const dirty = [...this.drafts.entries()].some(([key, note]) => key.startsWith(`${this.documentId}:`)
      && note !== (data?.tasks.find(t => key === `${this.documentId}:${t.id}`)?.decision?.note || ""));
    const job = data?.job;
    const questionsReady = data?.questions_available || data?.question_agent?.status === "ready";
    const questionsRunning = ["queued", "running"].includes(data?.question_agent?.status);
    const answerLabel = decision => ({yes:l("是", "Yes"),no:l("否", "No"),unsure:l("暂不确定", "Unsure"),text:decision.note})[decision.answer] || actions[decision.action];
    this.element.innerHTML = `
      <header class="collection-header"><div><small>${e(this.options.patentLabel?.(this.documentId) || this.documentId)}</small><h2>${l("采集提问", "Collection questions")}</h2></div>
        <button class="icon-button" data-cr="close" aria-label="${l("关闭采集确认", "Close collection review")}">×</button></header>
      <div class="collection-content">
        ${this.error ? `<div class="collection-error" role="alert">${e(this.error)} <button class="button" data-cr="refresh">${l("刷新", "Refresh")}</button></div>` : ""}
        ${this.notice ? `<p class="collection-notice" role="status">${e(this.notice)}</p>` : ""}
        ${!data ? (this.error ? "" : `<p>${l("正在读取采集证据…", "Loading collection evidence…")}</p>`) : `
          <div class="collection-stats">${[[data.summary.records,l("候选记录", "Candidates")], [data.summary.complete_pairs,l("VH/VL 已填", "Both chains present")],
            [data.summary.verified,l("证据校验通过", "Evidence passed")], [questionsReady ? data.tasks.length : "—",l("待回答问题", "Questions")]]
            .map(([n,label]) => `<div><strong>${n}</strong><span>${label}</span></div>`).join("")}</div>
          <p class="collection-intro">${l("请回答采集过程中需要确认的问题；需要时可展开原文依据。", "Answer the questions raised during collection; expand the source evidence when needed.")}</p>
          ${questionsReady ? `<p class="collection-meta">${l(`已明确回答 ${answered} / ${data.tasks.length} 个问题。`, `${answered} / ${data.tasks.length} questions have definite answers.`)}</p>` : ""}
          ${data.busy ? `<p class="collection-notice" role="status">${l("此专利正在处理，完成后可继续确认。页面会自动更新。", "This patent is processing. Review will resume automatically when it finishes.")}</p>` : ""}
          ${questionsReady && questionsRunning ? `<p class="collection-meta" role="status">${l("可先回答已生成的问题，其余问题仍在整理。", "You can answer the available questions while the rest are being prepared.")}</p>` : ""}
          ${questionsReady && data.question_agent?.status === "failed" ? `<p class="collection-error">${l("部分问题尚未完成整理，已有问题可以继续回答。", "Some questions could not be prepared; available questions can still be answered.")} <button class="button" data-cr="questions" ${disabled}>${l("继续整理", "Retry remaining questions")}</button></p>` : ""}
          ${!questionsReady && data.tasks.length ? `<p class="collection-notice" role="status">${questionsRunning ? l("正在整理待确认的问题…", "Preparing questions…") : l("需要先整理本次采集的问题。", "Questions need to be prepared for this collection.")}</p>
            ${data.question_agent?.error ? `<p class="collection-error">${e(data.question_agent.error)}</p>` : ""}
            ${!questionsRunning ? `<button class="button button-primary" data-cr="questions" ${disabled}>${l("生成待确认问题", "Prepare questions")}</button>` : ""}` : ""}
          ${questionsReady && task ? `
            <label class="collection-task-label" for="collection-task-select">${l("选择待确认问题", "Choose a review task")}</label>
            <select id="collection-task-select" data-cr-tasks>${data.tasks.map(t => `<option value="${e(t.id)}" ${t.id === task.id ? "selected" : ""}>${t.decision_current ? "✓ " : "○ "}${e(this.taskTitle(t))}</option>`).join("")}</select>
            <article class="collection-task"><div class="collection-task-heading"><h3>${e(this.taskTitle(task))}</h3><button class="button" data-cr="next">${l("下一项", "Next")}</button></div>
              ${decision ? `<p class="collection-notice">${l("已回答", "Answered")}：${e(answerLabel(decision))} · ${e(decision.reviewer_name)}${!task.decision_current ? ` · ${l("问题已更新，请重新回答", "Question changed; answer again")}` : ""}</p>` : ""}
              <label class="collection-task-label" for="collection-note">${task.answer_type === "text" ? l("你的回答", "Your answer") : l("补充说明（可选）", "Additional context (optional)")}</label>
              <textarea id="collection-note" rows="2" maxlength="4000" ${disabled} placeholder="${task.answer_type === "text" ? l("请填写回答，或选择暂不确定。", "Enter your answer, or choose Unsure.") : l("可补充判断依据或需要注意的区别。", "Add supporting context or distinctions if needed.")}">${e(this.drafts.get(`${this.documentId}:${task.id}`) ?? decision?.note ?? "")}</textarea>
              <div class="collection-actions">${(task.answer_type === "yes_no" ? [["yes", l("是", "Yes")], ["no", l("否", "No")], ["unsure", l("暂不确定", "Unsure")]] : [["text", l("提交回答", "Submit answer")], ["unsure", l("暂不确定", "Unsure")]])
                .map(([key,label]) => `<button class="button button-secondary" data-cr="answer_${key}" ${disabled}>${label}</button>`).join("")}</div>
              <details class="collection-question-evidence"><summary>${l("查看原文依据与受影响数据", "View source evidence and affected data")}</summary>
              ${task.name_issue ? this.nameIssueMarkup(task.name_issue) : ""}
              <div class="collection-source-links"><button class="button button-secondary" data-cr="source">${l("在原文中查看", "View in source")}</button>
                ${task.evidence_refs.slice(0, 10).map(ref => `<button class="collection-ref" data-cr="source" data-id="${e(ref)}">${e(ref)}</button>`).join("")}</div>
              ${task.excerpt.length ? `<details><summary>${l(`OCR 表格预览（展示 ${task.excerpt.length} / ${task.source_row_count} 行，含问题行）`, `OCR preview (${task.excerpt.length} / ${task.source_row_count} rows)`)}</summary><div class="collection-table-wrap"><table>${task.excerpt.map((row, index) => `<tr><th>${l("行", "Row")} ${(task.excerpt_indices?.[index] ?? index) + 1}</th>${row.map(cell => `<td>${e(cell)}</td>`).join("")}</tr>`).join("")}</table></div></details>` : ""}
              <details><summary>${l("查看 Agent 发现的问题", "Agent findings")}</summary><ul>${task.messages.map(m => `<li>${e(m)}</li>`).join("")}</ul></details>
              </details>
            </article>` : !data.tasks.length ? `<p>${l("当前没有待确认问题。", "There are no current questions.")}</p>` : ""}
          ${dirty ? `<p class="collection-meta">${l("有尚未提交的说明，请先点击上方操作保存。", "Some notes are unsaved. Submit a decision before preparing a candidate.")}</p>` : ""}
          <div class="collection-prepare"><button class="button button-primary" data-cr="prepare" ${busy || !questionsReady || !answered || dirty ? "disabled" : ""}>${l("根据回答生成候选结果", "Prepare candidate from answers")}</button>
            <small>${l("依据已保存意见重新分析已有证据，可从零条记录开始。", "Reanalyzes available evidence using saved decisions, even with zero records.")}</small></div>
          ${job ? `<section class="collection-preview"><h3>${l("Agent 候选结果", "Agent candidate")}</h3>
            ${job.status === "failed" ? `<p class="collection-error">${e(job.error)}</p>` : job.status === "ready" ? this.previewMarkup(job, busy) : `<p role="status">${l("正在生成，请稍候…", "Preparing candidate…")}</p>`}
          </section>` : ""}
          <details class="collection-history"><summary>${l("回答记录", "Answer history")}</summary>${data.history.slice().reverse().map(h => `<p><strong>${e(h.reviewer_name)}</strong> · ${e(answerLabel(h) || l("应用候选", "Applied candidate"))}<br>${h.action === "answer" ? e(h.question) + "<br>" : ""}<small>${e(new Date(h.at).toLocaleString())}</small>${h.note ? `<br>${e(h.note)}` : ""}</p>`).join("") || l("暂无记录", "No answers yet")}</details>
        `}
      </div>`;
    this.renderedTaskId = task?.id;
    [...(this.element.querySelectorAll?.("details") || [])].forEach((detail, i) => { detail.open = opened[i] || false; });
    const content = this.element.querySelector?.(".collection-content");
    if (content && scroll) content.scrollTop = scroll;
    if (note) {
      const field = this.element.querySelector("#collection-note");
      field?.focus({preventScroll: true});
      field?.setSelectionRange(note.start, note.end);
    }
  }

  nameIssueMarkup(issue) {
    const e = escapeReviewHtml;
    const l = (zh, en) => this.l(zh, en);
    return `<section class="collection-name-issue">
      <p><strong>${l("实验表中的名称", "Name in assay table")}</strong>：${e(issue.raw_name)}</p>
      ${issue.candidates.length ? issue.candidates.map(candidate => `<p>
        <strong>${l("待核对的模板实体", "Template entity to check")}</strong>：${e(candidate.canonical_name)}<br>
        ${l("已登记的原文名称", "Registered source names")}：${candidate.raw_names.map(e).join("、") || "—"}<br>
        ${e(candidate.has_sequence_pair ? l(`该模板实体已有 VH/VL：SEQ ${candidate.vh_seq_id ?? "—"} / ${candidate.vl_seq_id ?? "—"}。`, `This template entity has VH/VL: SEQ ${candidate.vh_seq_id ?? "—"} / ${candidate.vl_seq_id ?? "—"}.`) : l("该模板实体的完整 VH/VL 尚未就绪。", "Complete VH/VL are not yet available for this template entity."))}
        ${candidate.evidence_refs.filter(ref => /:R\d+C\d+$/.test(ref)).map(ref => `<button class="collection-ref" data-cr="source" data-id="${e(ref)}">${l("查看名称定义", "View name definition")} · ${e(ref)}</button>`).join(" ")}
      </p>`).join("") : `<p>${l("模板中没有同名实体，需要结合原文确认归属。", "No entity with the same canonical name is in the template. Check the source to establish ownership.")}</p>`}
      <p class="collection-meta">${l("名称对应关系仍待确认；以下测量尚未关联到最终记录。", "Name correspondence remains unresolved; these measurements are not linked to final records.")}</p>
      <div class="collection-table-wrap"><table><thead><tr><th>${l("测量", "Metric")}</th><th>${l("数值", "Value")}</th><th>${l("原文", "Source")}</th></tr></thead>
        <tbody>${issue.measurements.map(m => `<tr><td>${e(m.metric)}</td><td>${e(m.operator === "=" ? "" : m.operator)}${e(m.value)} ${e(m.unit)}</td>
          <td><button class="collection-ref" data-cr="source" data-id="${e(m.value_ref || m.entity_ref)}">${e(m.page ? l(`第 ${m.page} 页`, `Page ${m.page}`) : m.source_table)}</button></td></tr>`).join("")}</tbody></table></div>
    </section>`;
  }

  previewMarkup(job, busy) {
    const e = escapeReviewHtml;
    const l = (zh, en) => this.l(zh, en);
    const diff = job.diff;
    const value = (item) => item == null ? "—" : typeof item === "object" ? JSON.stringify(item, null, 2) : String(item);
    return `<p>${l(`新增 ${diff.added} · 修改 ${diff.changed} · 移除 ${diff.removed} · 未变 ${diff.unchanged}`, `Added ${diff.added} · Changed ${diff.changed} · Removed ${diff.removed} · Unchanged ${diff.unchanged}`)}</p>
      <p>${l(`新版有 ${job.summary.records} 条记录，仍有 ${job.remaining_tasks} 项问题。`, `The candidate has ${job.summary.records} records and ${job.remaining_tasks} remaining issues.`)}</p>
      ${diff.removed ? `<p class="collection-error">${l(`应用后将从工作区移除 ${diff.removed} 条旧记录，请核对下方差异。`, `Applying will remove ${diff.removed} existing workspace records. Review the differences below.`)}</p>` : ""}
      ${job.stale ? `<p class="collection-error">${l("来源或确认意见已变化，请重新生成候选。", "Sources or decisions changed. Prepare a new candidate.")}</p>` : ""}
      <div class="collection-diff">${diff.rows.map(row => `<details><summary>${e(({added:l("新增", "Added"),removed:l("移除", "Removed"),changed:l("修改", "Changed")})[row.kind])} · ${e(row.name)}</summary>
        <table><thead><tr><th>${l("字段", "Field")}</th><th>${l("原值", "Before")}</th><th>${l("候选值", "After")}</th></tr></thead><tbody>${(row.fields.length ? row.fields : ["record_verification_status"]).map(field => `<tr><td>${e(field)}</td><td>${e(value(row.before?.[field]))}</td><td>${e(value(row.after?.[field]))}</td></tr>`).join("")}</tbody></table>
        ${!row.fields.length ? `<p>${l("证据或其他元数据发生变化。", "Evidence or other metadata changed.")}</p>` : ""}</details>`).join("")}</div>
      <button class="button button-primary" data-cr="apply" ${busy || job.stale ? "disabled" : ""}>${l("确认差异，应用到工作区", "Confirm changes and apply to workspace")}</button>
      <p class="collection-meta">${l("应用后保留旧版本备份；正式入库仍需在数据表中选择记录并推送。", "The previous version is backed up. Publishing still requires selecting records in the data table.")}</p>`;
  }
}
