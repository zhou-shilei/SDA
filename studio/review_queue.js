import { processingQueueMarkup, processingQueueScroll, restoreProcessingQueueScroll } from "./processing_queue.js?v=20261102-shared-order";

const escape = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

export class ReviewQueue {
  constructor(element, options) {
    this.element = element;
    this.options = options;
    this.tasks = [];
    this.active = null;
    this.tail = Promise.resolve();
    this.filter = "all";
    this.search = "";
    this.error = "";
    this.busy = false;
    this.dirty = false;
    this.restoring = false;
    element.addEventListener("click", event => {
      const button = event.target.closest("button[data-queue-action]");
      if (button) this.run(button.dataset.queueAction, button.dataset.id);
    });
    element.addEventListener("change", event => {
      if (event.target.id === "queue-filter") { this.filter = event.target.value; this.render(); }
    });
    element.addEventListener("input", event => {
      if (event.target.id === "queue-search") { this.search = event.target.value; this.renderRows(); }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.scheduleSave(true);
      else this.refresh();
    });
    window.addEventListener("beforeunload", event => {
      if (this.dirty) { event.preventDefault(); event.returnValue = ""; }
    });
    this.render();
    this.refresh();
  }

  l(zh, en) { return this.options.locale() === "en" ? en : zh; }

  async request(payload) {
    const response = await fetch(this.options.url, payload ? {
      method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload), keepalive: false,
    } : {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) {
      const error = Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  credentials() {
    return this.active && this.active.owner === this.options.reviewer()
      ? {reviewer_name: this.active.owner, token: this.active.token} : {};
  }

  async loadDraft(identity) {
    const response = await fetch(`${this.options.url}?document_id=${encodeURIComponent(identity)}`, {cache: "no-store"});
    if (!response.ok) throw Error(this.l("团队草稿读取失败，请重试。", "Could not load the shared draft. Retry."));
    const {tasks} = await response.json();
    if (this.options.documentId() === identity && tasks[0]?.draft) this.options.restore(tasks[0].draft);
  }

  async refresh() {
    clearTimeout(this.pollTimer);
    try {
      const response = await this.request();
      this.tasks = response.tasks;
      this.processingQueue = response.processing_queue;
      this.connectionError = "";
    } catch (error) { this.connectionError = error.message; }
    this.render();
    this.pollTimer = setTimeout(() => this.refresh(), 5000);
  }

  async ensureOwned(identity) {
    if (!this.options.reviewer()) { this.options.requestIdentity(); return false; }
    if (this.active?.document_id === identity && this.active.owner === this.options.reviewer() && this.active.token) return true;
    await this.refresh();
    const task = this.tasks.find(t => t.document_id === identity);
    this.options.notice(task?.owner
      ? this.l(`此任务由 ${task.owner} 领取，请到任务队列继续自己的任务。`, `Assigned to ${task.owner}. Continue your task in the queue.`)
      : this.l("请先到任务队列领取此文件，再进行标注。", "Claim this document in the task queue before annotating."));
    this.options.showQueue();
    return false;
  }

  async run(operation, identity) {
    if (this.busy) return;
    if (!this.options.reviewer()) { this.options.requestIdentity(); return; }
    this.busy = true;
    this.error = "";
    this.render();
    try {
      if (this.conflict && ["resume", "open"].includes(operation)) {
        this.tail = Promise.resolve();
        this.dirty = false;
        this.conflict = false;
        operation = "resume";
      } else await this.flush();
      if (operation === "open") {
        if (this.active?.document_id !== identity || this.active.owner !== this.options.reviewer()) throw Error(this.l("请点击继续任务恢复草稿。", "Use Continue to restore this task."));
        await this.open(this.active);
      } else if (operation === "retry-save") {
        await this.save();
      } else {
        const payload = {operation, document_id: identity, reviewer_name: this.options.reviewer()};
        if (!["claim", "next", "resume"].includes(operation)) {
          if (this.active?.document_id !== identity) throw Error(this.l("请先继续此任务。", "Continue this task first."));
          Object.assign(payload, this.credentials(), {version: this.active.version});
        }
        const {task} = await this.request(payload);
        if (["claim", "next", "resume"].includes(operation)) {
          this.active = task;
          await this.open(task);
        } else {
          this.active = null;
          this.lastSaved = "";
          this.options.notice(this.l("任务状态已更新，草稿已保留。", "Task updated. Draft retained."));
        }
      }
    } catch (error) {
      this.error = error.message;
      this.options.notice(error.message);
    } finally {
      this.busy = false;
      await this.refresh();
    }
  }

  async open(task) {
    if (task.pipeline_status !== "completed") {
      this.options.showQueue();
      this.options.notice(this.l("文件尚未处理完成。可以退回队列，或等待处理后继续标注。", "Processing has not finished. Release the task or wait before annotating."));
      return;
    }
    this.restoring = true;
    try {
      await this.options.openPatent(task.document_id);
      this.options.restore(task.draft || {});
      this.lastSaved = JSON.stringify(this.options.draft());
      // Persist browser drafts on first claim, including legacy local annotations.
      if (!Object.keys(task.draft || {}).length) this.lastSaved = "";
    } finally { this.restoring = false; }
    await this.save();
  }

  scheduleSave(immediate = false) {
    if (this.restoring || !this.active || this.active.owner !== this.options.reviewer() || this.options.documentId() !== this.active.document_id) return;
    const snapshot = JSON.stringify(this.options.draft());
    if (snapshot === this.lastSaved) return;
    this.dirty = true;
    this.renderStatus();
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.save().catch(() => {}), immediate ? 0 : 450);
  }

  save() {
    if (this.restoring || !this.active || this.active.owner !== this.options.reviewer() || this.options.documentId() !== this.active.document_id) return this.tail;
    const identity = this.active.document_id;
    const snapshot = JSON.stringify(this.options.draft());
    if (snapshot === this.lastSaved) return this.tail;
    this.dirty = true;
    this.renderStatus();
    const taskToken = this.active.token;
    const result = this.tail.catch(() => {}).then(async () => {
      if (this.active?.document_id !== identity || this.active.token !== taskToken) throw Error(this.l("任务已切换，请重新继续任务。", "Task changed. Continue the task again."));
      const {task} = await this.request({operation: "save", document_id: identity, ...this.credentials(), version: this.active.version, draft: JSON.parse(snapshot)});
      this.active = task;
      this.lastSaved = snapshot;
      this.dirty = JSON.stringify(this.options.draft()) !== snapshot;
      this.error = "";
      this.render();
    }).catch(error => {
      this.dirty = true;
      this.conflict = error.status === 409;
      if (this.conflict) {
        // Keep a recovery copy before an explicit Continue reloads server state.
        try { sessionStorage.setItem(`sda.studio.unsavedDraft.${identity}`, snapshot); } catch { /* Existing browser field drafts are retained as well. */ }
      }
      this.error = `${this.l("草稿尚未保存：", "Draft not saved: ")}${error.message}`;
      this.render();
      this.options.notice(this.error);
      throw error;
    });
    this.tail = result;
    return result;
  }

  async flush() {
    clearTimeout(this.saveTimer);
    await this.save();
    await this.tail;
    if (this.dirty) throw Error(this.l("草稿未保存，请先重试保存。", "Save the draft before continuing."));
  }

  render() {
    this.renderStatus();
    const processingElement = this.options.processingElement || this.element;
    const scroll = processingQueueScroll(processingElement);
    const l = (zh, en) => this.l(zh, en);
    const counts = {ready:0, in_progress:0, pending_review:0, completed:0};
    this.tasks.forEach(t => { if (t.pipeline_status === "completed") counts[t.status === "reviewing" ? "pending_review" : t.status]++; });
    const processing = processingQueueMarkup(this.processingQueue, this.options.locale());
    if (this.options.processingElement) processingElement.innerHTML = processing;
    this.element.innerHTML = `${this.options.processingElement ? "" : processing}<article class="page-card queue-card">
      <div class="page-card-header"><div><h2>${l("人工标注任务", "Annotation tasks")}</h2><p class="page-card-description">${l("文件处理完成后领取标注。每人领取一篇，草稿自动保存；退出后点击继续任务恢复。", "Claim documents for annotation after processing. Claim one at a time; drafts are saved automatically. Use Continue after returning.")}</p></div>
      <button class="button button-primary" data-queue-action="next" ${this.busy ? "disabled" : ""}>${l("领取下一个", "Claim next")}</button></div>
      <div class="queue-stats">${[["ready",l("待领取","Ready")],["in_progress",l("标注中","Annotating")],["pending_review",l("待复核 / 复核中","Review")],["completed",l("已完成","Completed")]].map(([key,label])=>`<div><strong>${counts[key]}</strong><span>${label}</span></div>`).join("")}</div>
      <div class="queue-tools"><label>${l("查看", "Show")} <select id="queue-filter">${[["all",l("全部任务","All tasks")],["mine",l("我的任务","My tasks")],["ready",l("待领取","Ready")],["pending_review",l("待复核","Awaiting review")],["failed",l("处理失败","Processing failed")],["completed",l("已完成","Completed")]].map(([key,label])=>`<option value="${key}" ${this.filter===key?"selected":""}>${label}</option>`).join("")}</select></label>
      <input id="queue-search" type="search" aria-label="${l("搜索文件", "Search documents")}" placeholder="${l("搜索专利号、文件名或标注人", "Search ID, filename or reviewer")}" value="${escape(this.search)}">
      <span role="status">${this.dirty ? l("正在保存草稿…", "Saving draft…") : l("队列每 5 秒更新", "Updates every 5 seconds")}</span></div>
      <p class="queue-error" role="alert" ${this.error || this.connectionError ? "" : "hidden"}>${escape(this.error || this.connectionError)} ${this.dirty && this.active ? `<button class="button" data-queue-action="retry-save">${l("重试保存", "Retry save")}</button>` : ""}</p>
      <div class="queue-table-wrap"><table class="queue-table"><thead><tr>${[l("顺序","Order"),l("文件","Document"),l("提交人","Submitted by"),l("状态","Status"),l("当前标注人","Assigned to"),l("已审阅记录","Records reviewed"),l("提交时间","Submitted"),l("操作","Actions")].map(t=>`<th>${t}</th>`).join("")}</tr></thead><tbody id="queue-rows"></tbody></table></div>
    </article>`;
    this.renderRows();
    restoreProcessingQueueScroll(processingElement, scroll);
  }

  renderRows() {
    const l = (zh, en) => this.l(zh, en);
    const labels = {ready:l("待领取","Ready"), in_progress:l("标注中","Annotating"), pending_review:l("待复核","Awaiting review"), reviewing:l("复核中","Reviewing"), completed:l("已完成","Completed"), queued:l("等待处理","Queued for processing"), processing:l("正在处理","Processing"), failed:l("处理失败","Processing failed")};
    const rows = this.tasks.map((task,index)=>({task,index})).filter(({task:t}) => {
      if (this.search && !`${t.document_id} ${t.filename} ${t.owner} ${t.submitted_by}`.toLowerCase().includes(this.search.toLowerCase())) return false;
      if (this.filter === "mine") return t.owner === this.options.reviewer() && Boolean(t.owner);
      if (this.filter === "failed") return t.pipeline_status === "failed";
      return this.filter === "all" || (t.pipeline_status === "completed" && t.status === this.filter);
    });
    this.element.querySelector("#queue-rows").innerHTML = rows.map(({task:t,index})=>{
      const status = t.pipeline_status === "completed" ? t.status : t.pipeline_status;
      const mine = t.owner && t.owner === this.options.reviewer();
      const active = this.active?.document_id === t.document_id && this.active.owner === this.options.reviewer();
      const button = (op,label)=>`<button class="button button-secondary" data-queue-action="${op}" data-id="${escape(t.document_id)}" ${this.busy?"disabled":""}>${label}</button>`;
      let actions = "";
      if (t.pipeline_status === "completed") {
        if (mine) {
          actions = button(active ? "open" : "resume", l("继续任务","Continue"));
          if (active) actions += button("release",l("退回队列","Release")) + (t.status === "reviewing" ? button("approve",l("复核通过","Approve"))+button("return",l("退回修改","Request changes")) : button("submit",l("提交复核","Submit for review")));
        } else if (!t.owner && ["ready","pending_review"].includes(t.status) && !(t.status === "pending_review" && t.last_reviewer === this.options.reviewer())) actions = button("claim",t.status === "ready" ? l("领取标注","Claim") : l("领取复核","Review"));
      } else if (mine) {
        actions = button(active ? "release" : "resume", active ? l("退回队列", "Release") : l("继续任务", "Continue"));
      }
      return `<tr><td>${index+1}</td><td><strong>${escape(t.document_id)}</strong><small>${escape(t.filename)}</small>${t.error?`<small class="queue-error">${escape(t.error)}</small>`:""}</td><td>${escape(t.submitted_by || "—")}</td><td><span class="queue-status status-${escape(status)}">${escape(labels[status] || status)}${status === "processing" ? ` · ${Number(t.pipeline_progress)||0}%` : ""}</span></td><td>${escape(t.owner || "—")}</td><td>${t.reviewed_count} / ${t.record_count}</td><td>${escape(t.created_at.startsWith("1970") ? "—" : new Date(t.created_at).toLocaleString(this.options.locale() === "en" ? "en-US" : "zh-CN", {timeZone: "Asia/Shanghai", hour12: false}))}</td><td><div class="queue-actions">${actions || "—"}</div></td></tr>`;
    }).join("") || `<tr><td colspan="8" class="queue-empty">${l("暂无匹配任务。上传的文件处理完成后即可领取。", "No matching tasks. Uploaded documents can be claimed once processing completes.")}</td></tr>`;
  }

  renderStatus() {
    const element = this.options.statusElement;
    if (!element) return;
    const identity = this.options.documentId();
    const task = this.active?.document_id === identity ? this.active : this.tasks.find(t => t.document_id === identity);
    const active = task?.token && task.owner === this.options.reviewer();
    element.textContent = active
      ? `${task.owner} · ${this.error ? this.l("保存失败", "Save failed") : this.dirty ? this.l("保存中…", "Saving…") : this.l("草稿已保存", "Draft saved")}`
      : task?.owner ? `${task.owner} · ${this.l("任务队列", "Task queue")}` : this.l("任务队列", "Task queue");
    element.title = this.error || this.l("打开团队任务队列，领取、继续或提交任务", "Open the team queue to claim, resume or submit tasks");
  }
}
