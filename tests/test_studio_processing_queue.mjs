import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

const source = readFileSync(new URL("../studio/processing_queue.js", import.meta.url), "utf8");
const {processingQueueMarkup, processingQueueScroll, restoreProcessingQueueScroll} =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const queue = {running: 1, waiting: 1, concurrency: 1, worker_online: true, tasks: [
  {document_id: "NEW", filename: "new.pdf", submitted_by: "张三", status: "processing", progress: 42,
    position: 1, queued_at: "2026-11-02T01:00:00Z"},
  {document_id: "OLD-RETRY", filename: '<img onerror="bad">.pdf', submitted_by: "李四", status: "queued",
    position: 2, queue_position: 1, queue_ahead: 1, queued_at: "2026-11-02T01:01:00Z"},
]};
const markup = processingQueueMarkup(queue);
assert.ok(markup.indexOf('data-processing-document="NEW"') < markup.indexOf('data-processing-document="OLD-RETRY"'));
assert.ok(markup.includes("共享解析队列"));
assert.ok(markup.includes("张三"));
assert.ok(markup.includes("李四"));
assert.ok(markup.includes("42%"));
assert.ok(markup.includes("等待第 1 位 · 前方 1 个任务"));
assert.ok(markup.includes("09:00:00"));
assert.ok(!markup.includes("<img"));
assert.ok(markup.includes("&lt;img"));
assert.ok(!markup.includes("执行器暂未连接"));
assert.ok(processingQueueMarkup({...queue, worker_online: false}).includes("任务和顺序已保存"));
assert.ok(processingQueueMarkup({...queue, tasks: []}).includes("当前没有正在解析或等待的任务"));
assert.ok(processingQueueMarkup(null).includes("正在加载共享解析队列"));
assert.ok(processingQueueMarkup(queue, "en").includes("Shared processing queue"));
assert.ok(processingQueueMarkup(queue, "en").includes("Waiting #1 · 1 ahead"));
const viewport = {scrollTop: 120, scrollLeft: 80};
const element = {querySelector: () => viewport};
const position = processingQueueScroll(element);
viewport.scrollTop = 0;
viewport.scrollLeft = 0;
restoreProcessingQueueScroll(element, position);
assert.deepEqual(viewport, {scrollTop: 120, scrollLeft: 80});
assert.deepEqual(processingQueueScroll({querySelector: () => null}), {top: 0, left: 0});
console.log("Shared processing queue rendering checks passed.");
