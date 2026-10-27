import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../studio/app.js", import.meta.url), "utf8");
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf("function validCellBbox("), source.indexOf("function renderTableCropAnnotations(")), context);
const run = (expression) => JSON.parse(JSON.stringify(vm.runInContext(expression, context)));

assert.deepEqual(run("cellPageBbox({bbox_pdf:[220,240,240,250],bbox:[260,100,300,120]},[100,200,300,400],{})"), [220,240,240,250]);
assert.deepEqual(run("mapCropBboxToPage([260,100,300,120],[100,200,300,400],{crop_to_pdf:[.5,0,0,.5,90,190]})"), [220,240,240,250]);
assert.deepEqual(run("mapCropBboxToPage([260,100,300,120],[100,200,300,400],{bbox_padded:[90,190,310,410],width:440,height:440})"), [220,240,240,250]);
assert.deepEqual(run("mapCropBboxToPage([10,20,30,40],null,{crop_to_pdf:[0,1,-1,0,500,0]})"), [460,10,480,30]);
assert.equal(run("cellPageBbox({bbox:[260,100,300,120],coordinate_space:'crop_pixels'},[100,200,300,400],{width:440,height:440})"), null);
assert.equal(run("cellCropBbox({bbox:[220,240,240,250],coordinate_space:'pdf_points'})"), null);
assert.deepEqual(run("cellCropBbox({bbox_crop:[260,100,300,120],bbox_pdf:[220,240,240,250]})"), [260,100,300,120]);
assert.equal(run("cellPageBbox({bbox_pdf:[1,2,Infinity,4]},null,{})"), null);
assert.equal(run("mapCropBboxToPage([30,40,10,20],[0,0,100,100],{width:100,height:100})"), null);
assert.equal(run("mapCropBboxToPage([10,20,30,40],[0,0,100,100],{crop_to_pdf:[1,2],width:100,height:100})"), null);
console.log("PASS: cell highlights respect crop/PDF frames, padding, rotation and missing geometry.");
