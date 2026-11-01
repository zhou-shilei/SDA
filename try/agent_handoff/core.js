/* Pure JSON utilities shared by the browser and offline checks. */
const HandoffCore = (() => {
  const missing = Symbol('missing');
  const type = v => v === missing ? 'missing' : v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v;
  const escape = s => String(s).replace(/~/g, '~0').replace(/\//g, '~1');
  const parts = pointer => {
    if (pointer === '') return [];
    if (!pointer.startsWith('/') || /~(?![01])/u.test(pointer)) throw Error('路径须为 JSON Pointer，例如 /relation_index/links；根节点留空');
    return pointer.slice(1).split('/').map(x => x.replace(/~1/g, '/').replace(/~0/g, '~'));
  };
  function at(value, pointer) {
    for (const key of parts(pointer)) {
      if (value === null || typeof value !== 'object' || !Object.hasOwn(value, key)) throw Error(`路径不存在：${pointer}`);
      value = value[key];
    }
    return value;
  }
  function diff(a, b, limit = 300) {
    const rows = []; let truncated = false;
    function walk(left, right, path) {
      const lt = type(left), rt = type(right);
      if (left === right) return;
      if (lt === rt && (lt === 'object' || lt === 'array')) {
        const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
        for (const key of keys) {
          walk(Object.hasOwn(left, key) ? left[key] : missing,
            Object.hasOwn(right, key) ? right[key] : missing, `${path}/${escape(key)}`);
          if (truncated) break;
        }
      } else {
        if (rows.length >= limit) { truncated = true; return; }
        rows.push({path, kind: left === missing ? 'added' : right === missing ? 'removed' : 'changed',
          before: left === missing ? '（不存在）' : JSON.stringify(left),
          after: right === missing ? '（不存在）' : JSON.stringify(right)});
      }
    }
    walk(a, b, '');
    return {rows, truncated};
  }
  function search(value, query, limit = 80) {
    const result = [], q = query.toLocaleLowerCase();
    function walk(v, path, key) {
      if (result.length >= limit) return;
      const scalar = v === null || typeof v !== 'object';
      if (String(key).toLocaleLowerCase().includes(q) || (scalar && String(v).toLocaleLowerCase().includes(q))) result.push({path, value: v});
      if (!scalar) for (const [k, child] of Object.entries(v)) {
        walk(child, `${path}/${escape(k)}`, k);
        if (result.length >= limit) break;
      }
    }
    if (q) walk(value, '', '');
    return result;
  }
  function tableView(table) {
    if (!Array.isArray(table.rows)) {
      const cells = table.cells || [];
      return {cells, rowIndices: Array.from({length: Math.max(Number(table.row_count)||0, ...cells.map(c=>Number(c.row)+1), 1)}, (_,i)=>i),
        columnCount: Math.max(Number(table.column_count)||0, ...cells.map(c=>Number(c.column)+1), 1),
        headerRows: [], isV5: false};
    }
    const cells = [];
    const add = ([ref,text], fallbackRow, fallbackCol) => {
      const match = String(ref).match(/:R(\d+)C(\d+)$/u);
      cells.push({ref, text, row: match ? Number(match[1]) : fallbackRow,
        column: match ? Number(match[2]) : fallbackCol, view: table.cell_views?.[ref]});
    };
    const cols = Math.max(1, Number(table.column_count)||0);
    (table.headers||[]).forEach((cell,i)=>add(cell, table.header_row_indices?.[Math.floor(i/cols)]??0, i%cols));
    table.rows.forEach((row,i)=>row.forEach((cell,col)=>add(cell, table.row_indices?.[i]??i, col)));
    return {cells, rowIndices: [...new Set([...(table.header_row_indices||[]), ...(table.row_indices||[]), ...cells.map(c=>c.row)])].sort((a,b)=>a-b),
      columnCount: Math.max(cols,...cells.map(c=>c.column+1)), headerRows: table.header_row_indices||[], isV5:true};
  }
  return {type, escape, parts, at, diff, search, tableView};
})();
if (typeof module !== 'undefined') module.exports = HandoffCore;
