import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../studio/intake_metrics.js", import.meta.url), "utf8");
const { formatTokenK, formatProcessingTime, timeMetricMarkup, tokenMetricMarkup, completedRunUsage } =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

assert.equal(formatTokenK(285964), "285.96K");
assert.equal(formatTokenK(1000000), "1,000.00K");
assert.equal(formatTokenK(1), "0.001K");
assert.equal(formatTokenK(0), "0.00K");
assert.equal(formatTokenK(null), "未记录");
assert.equal(formatProcessingTime(419.6), "6分59秒");
assert.equal(formatProcessingTime(0), "0秒");
assert.equal(formatProcessingTime(null), "未记录");
const partial = tokenMetricMarkup({ tokens: { available: true,
  totals: { total_tokens: 12000, input_tokens: 10000, output_tokens: 2000, cached_input_tokens: 7000, unknown_requests: 1 },
  stages: [{ stage: "document_extraction", total_tokens: 12000, input_tokens: 10000, output_tokens: 2000 }],
} }, 'DOC"<img>', "zh");
assert.ok(partial.includes("≥12.00K"));
assert.ok(partial.includes("其中缓存输入: 7.00K"));
assert.ok(partial.includes("全文信息抽取: 12.00K"));
assert.ok(partial.includes("总量为输入 + 输出"));
assert.ok(!partial.includes("<img>"));
assert.ok(partial.includes("&quot;&lt;img&gt;"));
assert.ok(tokenMetricMarkup({}, "DOC").includes("未记录"));
assert.ok(tokenMetricMarkup({ time: { running: true } }, "DOC").includes("记录中"));
const live = timeMetricMarkup({ time: { running: true, started_at: "2026-10-25T01:00:00Z", seconds: 2,
  stages: [{ stage: "text_ocr", seconds: 50, level: 2 }, { stage: "table_ocr", seconds: 59, level: 2 }],
} }, "DOC", "zh", Date.parse("2026-10-25T01:01:30Z"));
assert.ok(live.includes("1分30秒"));
assert.ok(live.includes("全文 OCR: 50秒"));
assert.ok(live.includes("表格 OCR: 59秒"));
assert.ok(live.includes("明细用时不能直接相加"));
assert.ok(timeMetricMarkup({ time: { seconds: 2.7, scope: "stage_only" } }, "DOC").includes("不代表整篇 PDF 总用时"));
assert.ok(tokenMetricMarkup({}, "DOC", "en").includes("Not recorded"));
const corrected = completedRunUsage({ time: { scope: "cumulative" }, tokens: { totals: { total_tokens: 0 } } },
  { resumed: true, token_usage: { totals: { total_tokens: 285964, requests: 5 },
    by_stage: [{ stage: "document_extraction", total_tokens: 119000 }] } });
assert.ok(tokenMetricMarkup(corrected, "DOC").includes("285.96K"));
assert.equal(corrected.tokens.stages[0].total_tokens, 119000);
console.log("PASS: intake time/token formatting, partial usage, unknown values, and escaping.");
