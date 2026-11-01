const assert = require('node:assert/strict');
const C = require('./core.js');
assert.equal(C.at({'a/b': {'~key': 5}}, '/a~1b/~0key'), 5);
assert.equal(C.at({a: null}, '/a'), null);
assert.throws(() => C.at({}, '/a'), /路径不存在/);
assert.throws(() => C.at({}, '/__proto__'), /路径不存在/);
assert.throws(() => C.at({}, 'a'), /JSON Pointer/);
assert.throws(() => C.at({}, '/a~2'), /JSON Pointer/);
assert.equal(C.diff({a: [1, null]}, {a: [1, null]}).rows.length, 0);
assert.deepEqual(C.diff({}, {a: null}).rows.map(r => [r.path, r.kind, r.after]), [['/a', 'added', 'null']]);
assert.equal(C.diff({a: null}, {}).rows[0].kind, 'removed');
assert.equal(C.diff({a: 1}, {a: '1'}).rows[0].kind, 'changed');
assert.equal(C.diff({'a/b': [2]}, {'a/b': [3]}).rows[0].path, '/a~1b/0');
assert.equal(C.diff([1, 2], [1]).rows[0].kind, 'removed');
assert.deepEqual(C.diff({x: 1, y: 2}, {x: 0, y: 0}, 1).truncated, true);
assert.equal(C.diff({x: 1, y: 2}, {x: 0, y: 2}, 1).truncated, false);
assert.equal(C.search({entities: [{name: 'AB-2a'}]}, 'ab-2A')[0].path, '/entities/0/name');
assert.equal(C.search({seq_ids: [119]}, 'seq_ids')[0].path, '/seq_ids');
assert.equal(C.search(['a', 'a', 'a'], 'a', 2).length, 2);
const rowInput = {row_count: 10, column_count: 3, header_row_indices: [1],
  headers: [['T:R1C0', '名称'], ['T:R1C1', ''], ['T:R1C2', 'KD']],
  row_indices: [4], rows: [[['T:R4C0', 'AB-1'], ['T:R4C1', ''], ['T:R4C2', '0']]],
  cell_views: {'T:R1C0': {colspan: 2}}};
const rowView = C.tableView(rowInput);
assert.deepEqual(rowView.rowIndices, [1, 4]); // Unseen physical rows are never reconstructed as supplied cells.
assert.equal(rowView.cells.length, 6);
assert.deepEqual(rowView.cells.slice(3).map(c=>[c.row,c.column,c.text]), [[4,0,'AB-1'],[4,1,''],[4,2,'0']]);
assert.deepEqual(rowView.cells[0].view, {colspan:2});
assert.equal(rowView.cells[0].bbox, undefined); // Host geometry is not invented from compact payloads.
const legacyCell = {ref:'T:R0C0',row:0,column:0,text:'old',bbox:[1,2,3,4]};
assert.strictEqual(C.tableView({row_count:1,column_count:1,cells:[legacyCell]}).cells[0], legacyCell);
console.log('JSON Pointer、缺失/null、类型差异、数组、结果截断与搜索校验通过');
