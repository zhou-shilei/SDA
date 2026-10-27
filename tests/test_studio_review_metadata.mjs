import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../studio/app.js", import.meta.url), "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const labels = {"reviewCheck.table_text_mismatch": "表格和文字不一致", "reviewCheck.table_text_match": "表文一致"};
const context = vm.createContext({
  t: (key) => labels[key] || key,
  escapeAttr: (value) => String(value).replaceAll("<", "&lt;"),
  escapeHtml: (value) => String(value).replaceAll("<", "&lt;"),
  renderColumnFilterMenu: (_menu, options) => options,
  propertyList: (rows) => JSON.stringify(rows).replaceAll("<", "&lt;"),
});
vm.runInContext(section("const REVIEW_CHECK_SOURCES", "const UNREVIEWED_FILTER_VALUE")
  + section("function reviewCheckBadges(", "function gridStatusCell(")
  + section("function renderSequenceReviewInspector(", "function renderExperimentInspector("), context);

const conflict = {chain: "VH", table_ref: "seq:R0C1", text_ref: "method:p19",
  table_quote: "ACDF", text_quote: "ACDY", message: "F/Y <difference>"};
context.conflicted = {sequence_source_conflicts: [conflict]};
context.clean = {};
assert.equal(vm.runInContext('recordMatchesReviewChecks(conflicted, ["source:table_text_mismatch"])', context), true);
assert.equal(vm.runInContext('recordMatchesReviewChecks(clean, ["source:table_text_mismatch"])', context), false);
assert.ok(vm.runInContext("reviewCheckBadges(conflicted)", context).includes("表格和文字不一致"));
assert.ok(!vm.runInContext("reviewCheckBadges(clean)", context).includes("表格和文字不一致"));
context.matched = {sequence_table_text_status: "matched"};
assert.ok(vm.runInContext("reviewCheckBadges(matched)", context).includes("表文一致"));
assert.ok(vm.runInContext("reviewCheckBadges(matched)", context).includes("review-check-passed"));
const options = vm.runInContext("renderStatusFilterMenu(null, [clean, conflicted], [])", context);
assert.equal(options.find((item) => item.value === "source:table_text_mismatch").count, 1);
const inspector = vm.runInContext("renderSequenceReviewInspector(conflicted)", context);
assert.ok(inspector.includes("seq:R0C1: ACDF"));
assert.ok(inspector.includes("method:p19: ACDY"));
assert.ok(!inspector.includes("<difference>"));
context.corrected = {sequence_image_reviews: [{chain: "VL", sequence_rationale: "Corrected <underline>"}],
  question_answers: [{question: "Which <sequence>?", answer: "From <image>", status: "applied", chains: ["VL"]}]};
assert.ok(vm.runInContext("reviewCheckBadges(corrected)", context).includes("reviewCheck.image_corrected"));
const correctedInspector = vm.runInContext("renderSequenceReviewInspector(corrected)", context);
assert.ok(correctedInspector.includes("Which &lt;sequence>"));
assert.ok(correctedInspector.includes("From &lt;image>"));
assert.ok(!correctedInspector.includes("<underline>"));
vm.runInContext(section("function renderSdaFields(", "function renderDetectionInspector("), context);
context.state = {selectedField: "vl"};
context.constantRecord = {sda_fields: {vl: {status: "present", provenance: []}},
  constant_regions: [{region: "CL", seq_id: 67, sequence: "ACD<residues>"},
    {region: "CH", seq_id: 68, sequence: "HEAVYCONSTANT"}]};
const constantInspector = vm.runInContext("renderSdaFields(constantRecord)", context);
assert.ok(constantInspector.includes("SEQ ID NO: 67"));
assert.ok(constantInspector.includes("ACD&lt;residues>"));
assert.ok(!constantInspector.includes("HEAVYCONSTANT"));
context.constantRecord.sequence_ocr_failures = [{chain: "VL", message: "Re-read <image>"}];
assert.ok(vm.runInContext("renderSdaFields(constantRecord)", context).includes("Re-read &lt;image>"));
context.evaluated = {anarci_review: {status: "passed", chains: {VH: {domains: [{chain_type: "H", coverage: 1, bitscore: 190,
  regions: {CDR3: "ACD<test>"}}]}}}, esm2_review: {status: "manual_review", chains: {
  VH: {metric_status: "scored", mean_pll: -0.5123, pseudo_perplexity: 1.6691},
  VL: {metric_status: "scored", mean_pll: -0.6123, pseudo_perplexity: 1.8447}}}};
assert.ok(vm.runInContext("reviewCheckBadges(evaluated)", context).includes("reviewCheck.esm2Scored"));
const metricsInspector = vm.runInContext("renderSequenceReviewInspector(evaluated)", context);
assert.ok(metricsInspector.includes("-0.5123"));
assert.ok(metricsInspector.includes("1.8447"));
assert.ok(metricsInspector.includes("ACD&lt;test>"));
assert.ok(!metricsInspector.includes("ACD<test>"));
context.risky = {esm2_review: {status: "error", chains: {VH: {status: "passed", metric_status: "scored"},
  VL: {status: "error", metric_status: "scored", max_site_pseudo_perplexity: 10352,
    site_screening: {enabled: true, max_pseudo_perplexity: 1000},
    flagged_positions: [{position: 56, residue: "W<test>", probability: 0.0000966, pseudo_perplexity: 10352}]}}}};
const riskBadge = vm.runInContext("reviewCheckBadges(risky)", context);
assert.ok(riskBadge.includes("review-check-error"));
assert.ok(!riskBadge.includes("reviewCheck.esm2Scored"));
const riskInspector = vm.runInContext("renderSequenceReviewInspector(risky)", context);
assert.ok(riskInspector.includes("VL · 56 W&lt;test>"));
assert.ok(riskInspector.includes("10352"));
assert.ok(!riskInspector.includes("W<test>"));
console.log("PASS: table/text conflict badges, filtering, evidence details and escaping.");
