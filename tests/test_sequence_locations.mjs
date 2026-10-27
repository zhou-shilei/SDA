import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../studio/app.js", import.meta.url), "utf8");
const context = vm.createContext({
  state: { validationByConstruct: new Map(), classificationByTable: new Map() },
  t: (key) => key,
});
vm.runInContext(source.slice(source.indexOf("function buildEvidence("), source.indexOf("function evidencePageNumber(")), context);
context.record = {
  sda_sources: [{kind: "variable_heavy", table_id: "T", row_index: 1, chain: "VH", label: "mapping only"}],
  evidence: [{type: "sequence_index", construct_key: "ab", locations: {
    vh: {status: "located", length: 4, segments: [{table_id: "T", page: 2, row_index: 1,
      sequence_start: 0, sequence_end: 4, text: "ACDE", bbox: [20,30,80,40], precision: "ocr_row"}]},
    vl: {status: "located", length: 4, segments: [{table_id: "L", page: 3, row_index: 2,
      sequence_start: 0, sequence_end: 4, text: "FGHI", bbox: [20,50,80,60], precision: "ocr_line"}]},
  }}],
};
const heavy = vm.runInContext("buildEvidence(record,'vh')", context);
assert.ok(heavy[0].segments?.length, "the actual sequence location must precede generic provenance");
assert.ok(heavy.every((item) => item.chain === "VH"));
assert.equal(heavy[0].page, 2);
assert.equal(heavy[0].segments[0].text, "ACDE");
const light = vm.runInContext("buildEvidence(record,'vl')", context);
assert.equal(light[0].page, 3);
assert.ok(light.every((item) => item.chain === "VL"));
console.log("PASS: sequence locations reach evidence chips, take priority and stay chain-specific.");
