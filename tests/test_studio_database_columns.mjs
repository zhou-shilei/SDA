import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

const source = readFileSync(new URL("../studio/database_columns.js", import.meta.url), "utf8");
const {DATABASE_COLUMNS, databaseValues, valueKind, matchesDatabaseFilters, databaseFilterOptions,
  localizedValues, databaseValueMarkup, MISSING_VALUE} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const tag = (category, key, zh = key) => ({category, key, label_zh: zh, label_en: key});
const kd = {record_id: "r1", entity_id: "a", experiment_id: "e1", assay_endpoint: "KD", ic50: "1e-9",
  vh: "SEQUENCE", vl: "OTHER", assay_keywords: [tag("method", "SPR"), tag("antigen_species", "human", "人"),
    tag("cell_host_species", "mouse", "鼠"), tag("purpose", "binding", "结合")]};
const ic50 = {record_id: "r2", assay_endpoint: "IC50", ic50: "<0.5", vh: "SEQUENCE"};
const before = JSON.stringify(kd);
assert.equal(valueKind(kd)[0].key, "exact");
assert.equal(valueKind(ic50)[0].key, "upper");
assert.equal(valueKind({ic50: "1", operator: ">="})[0].key, "lower");
assert.equal(valueKind({ic50: "1e-9–2e-9"})[0].key, "interval");
assert.equal(valueKind({ic50: "1e-9 ± 2e-10"})[0].key, "unparsed");
assert.equal(valueKind({ic50: "NA"})[0].key, "unreported");
assert.equal(valueKind({ic50: "no binding"})[0].key, "qualitative");
assert.equal(valueKind({ic50: 0})[0].key, "exact");
assert.deepEqual(valueKind({ic50: null}), []);
assert.deepEqual(localizedValues(kd, "antigen_species"), ["人"]);
assert.deepEqual(localizedValues(kd, "cell_host_species"), ["鼠"]);
assert.deepEqual(localizedValues(kd, "antigen_species", "en"), ["human"]);
assert.equal(databaseValues(ic50, "sequence")[0].key, "partial");
const fullChains = {h_chain: "ACDEFG", l_chain: "GHIKLM"};
assert.deepEqual(databaseValues(fullChains, "sequence").map(v => v.key), ["missing", "full_heavy", "full_light"]);
assert.equal(matchesDatabaseFilters(fullChains, {sequence: ["full_heavy"]}), true);
assert.equal(matchesDatabaseFilters(fullChains, {sequence: ["paired"]}), false);
assert.equal(databaseValues({...ic50, entity_annotations: [tag("format", "VHH")]}, "sequence")[0].key, "vhh");
assert.deepEqual(databaseValues({assay_endpoint: "KD"}, "method"), []);
assert.deepEqual(databaseValues({experiment_id: "e1", table_id: "t1"}, "batch_id"), []);
assert.deepEqual(databaseValues({experiment_id: "e1", table_id: "t1"}, "comparison_group"), []);
assert.notEqual(databaseValues({document_id: "P1", comparison_group_id: "G1"}, "comparison_group")[0].key,
  databaseValues({document_id: "P2", comparison_group_id: "G1"}, "comparison_group")[0].key);
assert.equal(databaseValues(kd, "entity_link")[0].zh, "已记录关联");
assert.equal(matchesDatabaseFilters(kd, {endpoint: ["KD", "IC50"], method: ["SPR"]}), true);
assert.equal(matchesDatabaseFilters(ic50, {endpoint: ["KD", "IC50"], method: ["SPR"]}), false);
assert.equal(matchesDatabaseFilters(ic50, {method: [MISSING_VALUE]}), true);
assert.equal(matchesDatabaseFilters(kd, {method: [MISSING_VALUE]}), false);
assert.deepEqual(databaseFilterOptions([kd, ic50], "method").map(o => [o.value, o.count]).sort(),
  [["SPR", 1], [MISSING_VALUE, 1]].sort());
const conflict = {...kd, sequence_source_conflicts: [{chain: "VH"}]};
assert.equal(matchesDatabaseFilters(conflict, {sequence: ["conflict"]}), true);
assert.equal(matchesDatabaseFilters(conflict, {sequence: ["paired"]}), true);
assert.equal(JSON.stringify(kd), before);
assert.equal(new Set(DATABASE_COLUMNS.map(c => c.key)).size, DATABASE_COLUMNS.length);
assert.ok(databaseValueMarkup(kd, "method").includes("keyword-method"));
assert.ok(databaseValueMarkup({antigen_target: '<img src=x onerror="evil()">'}, "target").includes("&lt;img"));
assert.ok(!databaseValueMarkup({antigen_target: '<img src=x onerror="evil()">'}, "target").includes("<img"));
const availability = databaseValueMarkup({...kd, ...fullChains}, "sequence");
assert.equal((availability.match(/data-sequence-availability=/g) || []).length, 4);
assert.equal(databaseValueMarkup({}, "sequence"), "");
assert.equal(matchesDatabaseFilters(kd, {sequence: ["vh", "vl"]}), true);
assert.ok(databaseValueMarkup({comparison_group_id: "G1", document_id: "P1"}, "comparison_group").includes('>G1</span>'));
assert.ok(databaseValueMarkup({comparison_status: "candidate"}, "comparison_status").includes("候选待复核"));
console.log("PASS: independent facets, bound semantics, VHH evidence, missing data, OR/AND filtering and non-mutation.");
