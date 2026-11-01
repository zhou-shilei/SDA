(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('snapshot-data').textContent);
  const C = HandoffCore, $ = id => document.getElementById(id);
  const state = {call: null, panes: [], view: 'json'};
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const clip = (s, n = 160) => s.length > n ? `${s.slice(0, n)}…` : s;
  const preview = v => { const t = C.type(v); return t === 'array' ? `[${v.length} 项]` : t === 'object' ? `{${Object.keys(v).length} 字段}` : clip(JSON.stringify(v) ?? '未记录', 240); };
  function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 2500); }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast('已复制'); }
    catch { const input = el('textarea'); input.value = text; document.body.append(input); input.select(); const ok = document.execCommand('copy'); input.remove(); toast(ok ? '已复制' : '浏览器限制复制，请手动选中文本'); }
  }
  function button(label, action, cls) { const b = el('button', label, cls); b.type = 'button'; b.addEventListener('click', action); return b; }
  function download(value, name) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], {type: 'application/json'})); const a = el('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  function artifactChoice(key, pointer = '', label = null) {
    const a = data.artifacts[key];
    if (!a) return null;
    let value;
    try { value = C.at(a.value, pointer); } catch { return null; }
    return {label: label || a.label, value, path: `${a.original_path}${pointer ? ` #${pointer}` : ''}`, hash: a.sha256, file: a.path};
  }
  function upstreamChoices(call) {
    if(call.stage==='template'||call.stage==='vision')return [{label:call.stage==='template'?'实际表格与表题输入':'图片定位与单元格输入',value:call.request.value.payload,path:call.request.original_path+' #/payload'}];
    if (call.stage === 'reader') return [artifactChoice('seed'), artifactChoice('template_raw'), artifactChoice('template', '/document_plan', 'Template 返回（程序恢复后）'), artifactChoice('template')].filter(Boolean);
    if (call.stage === 'action') return [artifactChoice('action_input'), artifactChoice('seed'), artifactChoice('action_status')].filter(Boolean);
    const choices = [call.upstream_pointer !== null ? artifactChoice('questions', call.upstream_pointer, '本题问题（首轮队列）') : null,
      artifactChoice('questions'), artifactChoice('first_pass')].filter(Boolean);
    return choices.length ? choices : [{label: '上游问题队列未保存', missing: true, path: '未记录；实收问题仍可在下游 payload.question 中查看'}];
  }
  function tree(container, value, missing = false) {
    container.replaceChildren();
    const nodes = new Map();
    if (missing) { container.append(el('p', '本次快照没有保存这份 JSON。', 'empty')); return {reveal() {}, expand() {}, collapse() {}}; }
    function node(v, key, path, depth) {
      const complex = v !== null && typeof v === 'object';
      const wrapper = el(complex ? 'details' : 'div', undefined, complex ? 'json-node' : 'json-leaf');
      nodes.set(path, wrapper);
      const row = complex ? el('summary') : wrapper;
      row.append(el('span', key === '' ? 'root' : key, 'json-key'), el('span', ' : ', 'punctuation'), el('span', preview(v), `json-value value-${C.type(v)}`));
      row.title = path || '/（根节点）';
      if (complex) {
        wrapper.append(row);
        let populated = false;
        wrapper.populate = () => {
          if (populated) return;
          populated = true;
          const children = el('div', undefined, 'json-children');
          const entries = Object.entries(v);
          let shown = 0;
          function addBatch() {
            const stop = Math.min(shown + 120, entries.length);
            for (; shown < stop; shown++) { const [k, child] = entries[shown]; children.append(node(child, k, `${path}/${C.escape(k)}`, depth + 1)); }
            if (shown < entries.length) children.append(button(`继续展开 ${entries.length - shown} 项`, e => { e.currentTarget.remove(); addBatch(); }, 'load-more'));
          }
          wrapper.showThrough = key => { while (!nodes.has(`${path}/${C.escape(key)}`) && shown < entries.length) { children.querySelector('.load-more')?.remove(); addBatch(); } };
          addBatch(); wrapper.append(children);
        };
        wrapper.addEventListener('toggle', () => { if (wrapper.open) wrapper.populate(); });
        if (depth === 0) { wrapper.open = true; wrapper.populate(); }
      } else if (typeof v === 'string' && v.length > 240) {
        row.append(button('全文', () => { const old = wrapper.querySelector('pre'); if (old) old.remove(); else wrapper.append(el('pre', v, 'full-string')); }, 'inline-button'));
      }
      return wrapper;
    }
    container.append(node(value, '', '', 0));
    return {
      reveal(path) {
        let current = '';
        for (const part of C.parts(path)) {
          const parent = nodes.get(current); if (!parent) return;
          parent.open = true; parent.populate?.(); parent.showThrough?.(part);
          current += '/' + C.escape(part);
        }
        const target = nodes.get(current); if (!target) return;
        target.scrollIntoView({block: 'center', behavior: 'smooth'}); target.classList.add('highlight'); setTimeout(() => target.classList.remove('highlight'), 2200);
      },
      expand() { const root = nodes.get(''); root.populate?.(); root.open = true; for (const child of root.querySelectorAll(':scope > .json-children > details')) { child.populate?.(); child.open = true; } },
      collapse() { for (const n of nodes.values()) if (n.tagName === 'DETAILS') n.open = false; }
    };
  }
  function panel(index, title, caption, choices) {
    const card = el('article', undefined, 'panel');
    const head = el('div', undefined, 'panel-head'); head.append(el('span', String(index + 1).padStart(2, '0'), 'panel-number'), el('h3', title)); card.append(head, el('p', caption, 'panel-caption'));
    const select = el('select'); select.setAttribute('aria-label', `${title}的数据视图`);
    choices.forEach((choice, i) => { const option = el('option', choice.label); option.value = i; select.append(option); });
    const tools = el('div', undefined, 'panel-tools'), path = el('p', undefined, 'file-path'), content = el('div', undefined, 'json-tree');
    const pane = {card, choices, choice: choices[0], viewer: null};
    tools.append(button('展开两层', () => pane.viewer.expand()), button('折叠', () => pane.viewer.collapse()), button('复制', () => { if (!pane.choice.missing) copy(JSON.stringify(pane.choice.value, null, 2)); }), button('下载', () => { if (!pane.choice.missing) download(pane.choice.value, `${state.call.stage}-${state.call.id}-${index + 1}.json`); }));
    function render() {
      pane.choice = choices[Number(select.value)]; path.textContent = pane.choice.path || '从已保存请求中提取';
      path.title = `${path.textContent}${pane.choice.hash ? `\nSHA-256: ${pane.choice.hash}` : ''}`;
      pane.viewer = tree(content, pane.choice.value, pane.choice.missing);
      $('search-results').replaceChildren(); $('search-count').textContent = '';
    }
    select.addEventListener('change', () => { render(); resetDiffPaths(); });
    card.append(select, tools, path, content); render();
    return pane;
  }
  function namesFor(call) {
    const payload = call.request.value.payload;
    const ids = new Set((payload.question?.targets || []).map(t => t.item));
    return (payload.items || []).filter(row => ids.has(row[0])).flatMap(row => row[1]).join(' / ');
  }
  function listCalls() {
    const q = $('call-filter').value.toLocaleLowerCase(), stage = $('stage-filter').value;
    const calls = data.calls.filter(c => (stage === 'all' || c.stage === stage) && `${c.id} ${c.key} ${namesFor(c)}`.toLocaleLowerCase().includes(q));
    $('calls').replaceChildren(); $('call-count').textContent = `${calls.length} / ${data.calls.length}`;
    calls.forEach(call => {
      const b = button('', () => selectCall(call), 'call-item');
      b.dataset.key = call.key; b.classList.toggle('active', call.key === state.call?.key); b.setAttribute('aria-current', call.key === state.call?.key ? 'true' : 'false');
      const top = el('span', undefined, 'call-top'); top.append(el('strong', call.id), el('span', call.response ? '已返回' : '未记录', call.response ? 'status-ok' : 'status-pending'));
      const actions=call.request.value.payload.reader_actions;
      const actionCount=Array.isArray(actions)?actions.length:Object.values(actions||{}).reduce((n,t)=>n+(t.rows?.length||0),0);
      b.append(top, el('span', call.stage === 'template'?`Template · ${call.request.value.payload.tables?.length||0} 张表`:call.stage==='vision'?'图片序列复核':call.stage === 'reader' ? `Reader · ${call.request.value.payload.sources?.length || 0} 条来源` : call.stage === 'action' ? `ActionAgent · ${actionCount} 条建议` : clip(namesFor(call) || 'Answer · 定向补全', 55), 'call-subtitle'));
      $('calls').append(b);
    });
    if (!calls.length) $('calls').append(el('p', '没有匹配的调用', 'empty'));
  }
  function resetDiffPaths() {
    $('diff-left').value = '0'; $('diff-right').value = '1';
    const seedSelected = state.panes[0]?.choice.label === data.artifacts.seed?.label;
    $('left-path').value = state.call.stage === 'reader' && seedSelected ? '/links' : '';
    $('right-path').value = state.call.stage === 'reader' && seedSelected ? (state.call.request.value.payload.initial_dataset ? '/initial_dataset/links' : '/relation_index/links') : state.call.stage === 'answer' && state.panes[0]?.choice.label === '本题问题（首轮队列）' ? '/question' : '';
    $('diff-results').replaceChildren(); $('diff-summary').textContent = '选择两侧 JSON 和字段路径后点击“比较”。';
  }
  function renderSources(call) {
    $('source-list').replaceChildren();
    for (const key of call.source_keys) {
      const s = data.sources[key], card = el('details', undefined, 'source-card');
      card.append(el('summary', `${s.name}  ·  ${s.path}${s.available ? `:${s.line}` : '（源码未找到）'}`));
      if (s.available) {
        const info = el('div', undefined, 'source-info'); info.append(button('复制工作区路径', () => copy(`${s.path}:${s.line}`)), el('span', `SHA-256 ${s.sha256.slice(0, 16)}…`)); card.append(info);
        const code = el('pre', undefined, 'source-code');
        s.code.split('\n').forEach((line, i) => { const row = el('span', undefined, 'source-line'); row.append(el('span', String(s.line + i), 'line-number'), document.createTextNode(line + '\n')); code.append(row); });
        card.append(code);
      }
      $('source-list').append(card);
    }
  }
  function selectCall(call, updateHash = true) {
    state.call = call;
    $('call-stage').textContent = call.stage === 'reader' ? 'TEMPLATE → SEED → READER' : call.stage === 'action' ? 'SEED + READER ACTIONS → ACTIONAGENT → DATASET' : 'QUESTION → RETRIEVAL → ANSWER';
    $('call-title').textContent = call.stage === 'reader' ? `${call.id} · Reader 分块读取` : call.stage === 'action' ? 'ActionAgent · 全文语义整合' : `${call.id} · 定向补全`;
    if(call.stage==='template'||call.stage==='vision'){$('call-stage').textContent=call.stage==='template'?'表格 + 表题 → TEMPLATE → DATASET1':'裁图 + OCR → 原图序列复核';$('call-title').textContent=call.stage==='template'?'TemplateAgent · 本轮真实调用':`${call.id} · 原图序列复核`;}
    $('call-status').textContent = call.response ? '已有结构校验返回' : '尚无已保存返回';
    if(call.current===false)$('call-status').textContent=call.response?'历史返回 · 非本轮接纳':'历史尝试 · 无已保存返回';
    const report = call.report;
    $('call-note').textContent = call.stage === 'reader' ? '上游可切换原始模型返回、恢复后的计划、模板产物和初始种子。各 Reader 并行使用同一快照，互不传递增量结果。' :
      `上游显示首轮问题；本题实收证据在中栏。逐题完整 Ledger 未记录。${report ? `接纳状态：${report.status}；是否补全：${report.resolved === undefined ? '未记录' : report.resolved ? '是' : '否'}。` : '回答接纳状态未记录。'} ${namesFor(call)}`;
    const request = call.request, payload = request.value.payload;
    const inputs = [
      {label: '实收业务数据 · payload', value: payload, path: request.original_path + ' #/payload', hash: request.sha256},
      {label: '完整请求封装 · request.json', value: request.value, path: request.original_path, hash: request.sha256},
      {label: '提示词 · instructions', value: request.value.instructions, path: request.original_path + ' #/instructions'},
      {label: '约束输出 · schema', value: request.value.schema, path: request.original_path + ' #/schema'}
    ];
    const outputs = [{label: '通过结构校验的返回', value: call.response?.value, missing: !call.response, path: call.response?.original_path || '未记录；不使用其他调用的结果替代'}];
    if(call.stage==='action'&&call.key===data.artifacts.action_status?.value?.provider?.key){
      const canonical=data.artifacts.action_dataset;
      if(canonical)outputs.unshift({label:'通过完整校验的完整业务数据集',value:canonical.value,path:canonical.original_path,hash:canonical.sha256});
    }
    call.attempts.forEach((a, i) => outputs.push({label: `原始尝试 ${i + 1}（不等同于接纳）`, value: a.value, path: a.original_path, hash: a.sha256}));
    if(call.stage === 'action') $('call-note').textContent='完整初始 dataset、所有 Reader actions 和实际引用的原文共同进入 ActionAgent；输出完整业务数据集，并保存逐条 action 的处理和校验状态。';
    if(call.stage==='template'||call.stage==='vision')$('call-note').textContent='输入、指令、Schema、图片身份与原始返回均来自本轮保存的请求。Template 程序恢复后的计划单独保留，没有替代原始模型返回。';
    const titles = [call.stage === 'action' ? '完整底稿 / 综合上下文' : call.stage === 'reader' ? '上游输出 / 中间产物' : '上游问题 / 参考产物', '下游实际输入', '下游返回'];
    if(call.stage==='template'||call.stage==='vision')titles[0]='上游表格 / 图片定位';
    const captions = ['保留阶段区别，按需切换', '以已保存请求为准', '结构校验与语义正确性分开'];
    state.panes = [upstreamChoices(call), inputs, outputs].map((choices, i) => panel(i, titles[i], captions[i], choices));
    $('panels').replaceChildren(...state.panes.map(p => p.card)); renderSources(call); resetDiffPaths(); listCalls();
    if(state.view==='callprompt'&&window.HandoffDetails)HandoffDetails.prompt($('callprompt-view'),{kind:'llm'},call);
    if (updateHash) history.replaceState(null, '', '#call/' + encodeURIComponent(call.key));
  }
  function changeView(name) {
    state.view = name;
    if(name==='callprompt')HandoffDetails.prompt($('callprompt-view'),{kind:'llm'},state.call);
    for (const tab of document.querySelectorAll('[data-view]')) { const active = tab.dataset.view === name; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; $(`${tab.dataset.view}-view`).hidden = !active; }
  }
  document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => changeView(b.dataset.view)));
  document.querySelector('#calls-view [role=tablist]').addEventListener('keydown', e => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return; const tabs = [...document.querySelectorAll('[data-view]')]; let n = tabs.findIndex(t => t.dataset.view === state.view); n = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (n + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; changeView(tabs[n].dataset.view); tabs[n].focus(); e.preventDefault(); });
  $('stage-filter').addEventListener('change', listCalls); $('call-filter').addEventListener('input', listCalls);
  $('search-form').addEventListener('submit', e => {
    e.preventDefault(); const index = Number($('search-pane').value), pane = state.panes[index], query = $('json-search').value.trim();
    const results = pane.choice.missing ? [] : C.search(pane.choice.value, query);
    $('search-count').textContent = results.length === 80 ? '至少 80 项（仅显示前 80）' : `${results.length} 项`;
    $('search-results').replaceChildren();
    results.forEach(r => { const b = button('', () => pane.viewer.reveal(r.path), 'search-hit'); b.append(el('code', r.path || '/（根节点）'), el('span', preview(r.value))); $('search-results').append(b); });
  });
  $('diff-form').addEventListener('submit', e => {
    e.preventDefault(); $('diff-results').replaceChildren();
    try {
      const a = state.panes[Number($('diff-left').value)].choice, b = state.panes[Number($('diff-right').value)].choice;
      if (a.missing || b.missing) throw Error('选中的 JSON 未记录，无法比较');
      const result = C.diff(C.at(a.value, $('left-path').value.trim()), C.at(b.value, $('right-path').value.trim()));
      const labels = {added: '新增', removed: '仅左侧', changed: '变化'}, counts = {added: 0, removed: 0, changed: 0};
      result.rows.forEach(r => counts[r.kind]++);
      $('diff-summary').textContent = result.rows.length ? `${result.truncated ? '仅展示前 ' : '找到 '}${result.rows.length} 项差异：新增 ${counts.added} · 仅左侧 ${counts.removed} · 变化 ${counts.changed}${result.truncated ? '。结果已截断，可缩小字段范围继续比较。' : ''}` : '所选路径的 JSON 完全一致。';
      if (!result.rows.length) return;
      const table = el('table', undefined, 'diff-table'), head = el('tr'); ['类型', '相对字段路径', '左侧', '右侧'].forEach(t => head.append(el('th', t))); const thead = el('thead'); thead.append(head); table.append(thead);
      const body = el('tbody'); result.rows.forEach(r => { const row = el('tr', undefined, r.kind); row.append(el('td', labels[r.kind]), el('td', r.path || '/（根节点）')); [r.before, r.after].forEach(value => { const td = el('td'); if (value.length > 450) { const d = el('details'); d.append(el('summary', clip(value, 180)), el('pre', value)); td.append(d); } else td.textContent = value; row.append(td); }); body.append(row); }); table.append(body); $('diff-results').append(table);
    } catch (error) { $('diff-summary').textContent = error.message; }
  });
  const time = new Date(data.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
  $('subtitle').textContent = `${data.document_id} · 保存于 ${time}（北京时间）`;
  const metrics=data.actual_full_run?[[data.calls.filter(c=>c.stage==='reader').length,'并行 Reader'],[data.calls.filter(c=>c.stage==='answer'&&c.current).length,'本轮补全请求'],[data.calls.filter(c=>c.stage==='action'&&c.current).length,'最终 Action 整合']]:[[data.calls.filter(c => c.stage === 'reader').length, 'Reader 调用'], [data.calls.filter(c => c.stage === 'answer').length, '补全调用'], [data.calls.filter(c => !c.response).length, '返回未记录']];
  for (const [n, label] of metrics) { const item = el('div'); item.append(el('strong', n), el('span', label)); $('metrics').append(item); }
  data.notes.forEach(note => $('notes').append(el('li', note))); $('origin').textContent = `源运行：${data.reader_dir}`;
  window.HandoffInspector = {data, tree, el, button, copy, selectCall, currentCall: () => state.call};
  selectCall(data.calls[0], false);
})();
