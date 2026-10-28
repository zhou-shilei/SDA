import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../studio/app.js", import.meta.url), "utf8");
const context = vm.createContext({
  state: {records: []}, t: (key) => key,
  reviewEntityKey: (r) => r.entity_id,
  effectiveValue: (r, key) => r[key],
  buildEvidence: (r) => r.sources,
  assayKeywordValues: (r) => r.assay_keywords || [],
  assayKeywordIdentity: (k) => `${k.category}:${k.key}`,
  localizedAssayKeyword: (k) => k.key,
});
vm.runInContext(source.slice(source.indexOf("function buildReviewParameters("), source.indexOf("function renderEvidenceStrip(")), context);
const measurement = (experiment, donor) => ({kind: "assay", table_id: "table1", key: `${experiment}-${donor}`,
  experiment_id: experiment, measurement_id: `${experiment}-${donor}`, donor_id: donor,
  experiment_group: experiment, endpoint: "IC50", value: "1.2", unit: "nM", operator: "=",
  assay_keyword_status: "matched", assay_keywords: [{category: "method", key: "ELISA"}]});
context.record = {entity_id: "a", document_id: "d", record_id: "a1", sources: [
  measurement("exp1", "donor1"), measurement("exp1", "donor2"), measurement("exp2", "donor1"),
  {scope: "heavy_cdr", region: "CDRH1", chain: "VH", seq_id: 11},
  {scope: "heavy_framework", region: "FRH1", chain: "VH", seq_id: 12},
]};
context.state.records = [context.record];
const groups = vm.runInContext("buildReviewParameters(record).groups.filter(g => !g.missing)", context);
assert.equal(groups.length, 2, "identical assay tags cannot merge distinct experiment IDs");
assert.equal(groups[0].leaves.length, 2, "different donors remain separate measurements");
const chain = vm.runInContext('buildReviewMap(record).find(b => b.id === "heavy")', context);
assert.ok(chain.leaves.some(l => l.label === "CDRH1" && l.sources[0].seq_id === 11));
assert.ok(chain.leaves.some(l => l.label === "FRH1" && l.sources[0].seq_id === 12));
console.log("PASS: atomic CDR/FR graph nodes and experiment identity independent of labels.");
