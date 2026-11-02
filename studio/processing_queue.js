const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

export function processingQueueMarkup(queue, locale = "zh") {
  const l = (zh, en) => locale === "en" ? en : zh;
  const time = value => {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime()) ? date.toLocaleString(locale === "en" ? "en-US" : "zh-CN",
      {timeZone: "Asia/Shanghai", hour12: false}) : "—";
  };
  const tasks = queue?.tasks || [];
  const rows = tasks.map(task => {
    const running = task.status === "processing";
    const progress = Math.min(100, Math.max(0, Number(task.progress) || 0));
    const status = running ? l("正在解析", "Processing") : l("等待解析", "Waiting");
    const position = Number(task.queue_position) || 0;
    const ahead = Number(task.queue_ahead) || 0;
    const note = running ? `${progress}%` : l(`等待第 ${position} 位 · 前方 ${ahead} 个任务`, `Waiting #${position} · ${ahead} ahead`);
    return `<tr data-processing-document="${escape(task.document_id)}">
      <td><span class="processing-order ${running ? "is-running" : ""}">${escape(task.position)}</span></td>
      <td><strong>${escape(task.document_id)}</strong><small>${escape(task.filename)}</small></td>
      <td>${escape(task.submitted_by || "—")}</td>
      <td><span class="queue-status status-${running ? "processing" : "queued"}"><span>${status}</span>${running ? `<span class="processing-progress-value">${note}</span>` : ""}</span>${running ? "" : `<small>${note}</small>`}</td>
      <td>${escape(time(task.queued_at))}</td>
    </tr>`;
  }).join("");
  const summary = queue
    ? l(`运行中 ${queue.running} 个 · 等待中 ${queue.waiting} 个 · 同时执行 ${queue.concurrency} 个`,
        `${queue.running} running · ${queue.waiting} waiting · ${queue.concurrency} at a time`)
    : l("正在加载队列…", "Loading queue…");
  return `<section class="page-card processing-queue-card" data-processing-queue aria-label="${l("共享解析队列", "Shared processing queue")}">
    <div class="page-card-header"><div><h2>${l("共享解析队列", "Shared processing queue")}</h2>
      <p class="page-card-description">${l("所有成员共享执行顺序。正在解析的任务在前，其余按入队顺序排列。", "Everyone sees the same execution order. Running tasks come first, followed by tasks in submission order.")}</p></div>
      <span class="processing-queue-sync">${l("每 5 秒同步", "Syncs every 5 seconds")}</span></div>
    <p class="processing-queue-summary" role="status">${escape(summary)}</p>
    ${queue && !queue.worker_online && tasks.length ? `<p class="processing-queue-offline" role="status">${l("执行器暂未连接，任务和顺序已保存。", "Worker is offline. Tasks and their order are saved.")}</p>` : ""}
    <div class="queue-table-wrap" data-processing-scroll><table class="queue-table processing-queue-table"><thead><tr>
      ${[l("执行顺序", "Execution order"), l("文件", "Document"), l("提交人", "Submitted by"), l("状态 / 进度", "Status / progress"), l("入队时间（北京时间）", "Queued (Shanghai time)")].map(label => `<th>${label}</th>`).join("")}
    </tr></thead><tbody data-processing-rows>${rows || `<tr><td colspan="5" class="queue-empty">${queue ? l("当前没有正在解析或等待的任务。", "No running or waiting tasks.") : l("正在加载共享解析队列…", "Loading the shared processing queue…")}</td></tr>`}</tbody></table></div>
  </section>`;
}

export function processingQueueScroll(element) {
  const viewport = element.querySelector("[data-processing-scroll]");
  return {top: viewport?.scrollTop || 0, left: viewport?.scrollLeft || 0};
}

export function restoreProcessingQueueScroll(element, position) {
  const viewport = element.querySelector("[data-processing-scroll]");
  if (viewport) { viewport.scrollTop = position.top; viewport.scrollLeft = position.left; }
}
