(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('audit-data').textContent);
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  };
  const steps = [
    ['提供证据', '28 张候选表全部送给了 Template。表格中包含表头和完整数据行，还附有正文证据。', '由于输入预算，63 个正文片段未提供；模型不能依据这些未看到的内容建立关系。'],
    ['模型负责理解与规划', 'v6 已把表格筛选、结构定位和文档关联合并在一次 Template 调用里，没有单独的上游 LabelAgent。', '它提出了 45 个实体、89 条序列编号关联、16 个实验配置。这些是待校验的回答，不是最终入库数量。'],
    ['程序负责核对证据', '主机程序检查引用是否存在、名称是否一致、链类型和编号是否有明确对应。最新重校验保留 45 个实体和 18 条序列关联。', '71 条序列关联未通过。被拒绝不等于一定错误；有些是引用不足，有些是 OCR 表达没有被规则识别。'],
    ['程序负责解析与组装', '按表格区域读取单元格，把数值接到实体和实验上，组成 Reader 之前的 Dataset1。当前有 197 条测量，全部有实验归属。', '序列编号关联还需要对应的氨基酸内容。当前序列定义为 0，完整 VH/VL 配对为 0；Reader 还没执行。']
  ];
  function showStep(index) {
    document.querySelectorAll('[data-step]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.step) === index)));
    const detail = node('div');
    steps[index].slice(1).forEach(text => detail.append(node('p', text)));
    $('step-detail').replaceChildren(node('strong', steps[index][0]), detail);
  }
  document.querySelectorAll('[data-step]').forEach(b => b.addEventListener('click', () => showStep(Number(b.dataset.step))));
  showStep(2);

  data.examples.forEach(e => {
    const option = node('option', e.name); option.value = e.name; $('entity-select').append(option);
  });
  function showEntity(name) {
    const example = data.examples.find(e => e.name === name);
    $('example-name').textContent = name;
    $('source-quote').textContent = example.quote;
    $('source-ref').textContent = example.source;
    $('example-relations').replaceChildren();
    example.mappings.forEach(m => {
      const tr = node('tr'); tr.append(node('th', m.chain), node('td', 'SEQ ID ' + m.seq_id));
      const status = node('td'); status.append(node('span', m.retained_matches ? '已保留' : '未保留', 'pill ' + (m.retained_matches ? 'ok' : 'warning')));
      const residues = node('td'); residues.append(node('span', '尚未恢复', 'pill quiet'));
      tr.append(status, residues); $('example-relations').append(tr);
    });
    $('entity-json').textContent = JSON.stringify(example.dataset_state, null, 2);
  }
  $('entity-select').addEventListener('change', event => showEntity(event.target.value));
  showEntity('AB-1');
  data.audit.inspected_ab1_to_ab14_mapping_subset.checks.forEach(c => {
    const tr = node('tr');
    [c.entity, c.chain, String(c.seq_id), c.raw_model_matches ? '一致' : '不一致', c.retained_matches ? '已保留' : '未保留'].forEach((v, i) => tr.append(node('td', v, i === 4 && !c.retained_matches ? 'amber-text' : '')));
    const source = node('td'); source.append(node('code', c.source)); tr.append(source); $('mapping-rows').append(tr);
  });
  const names = ['AB-2a', 'AB-2b', 'Reference Antibody'];
  function showMissing(filter) {
    const rows = data.audit.measurement_coverage.missing_cells.filter(c => filter === 'all' || c.ref.startsWith(filter + ':'));
    $('missing-rows').replaceChildren();
    rows.forEach(c => {
      const tr = node('tr'); const source = node('td'); source.append(node('code', c.ref)); tr.append(source);
      const col = Number(c.ref.match(/C(\d+)$/)[1]);
      [names[col - 1], c.endpoint_header, c.text, '未入库'].forEach((v, i) => tr.append(node('td', v, i === 3 ? 'amber-text' : '')));
      $('missing-rows').append(tr);
    });
    $('missing-count').textContent = '显示 ' + rows.length + ' 个结果单元格';
  }
  $('missing-filter').addEventListener('change', event => showMissing(event.target.value));
  showMissing('all');
  data.donor_example.forEach(d => $('donor-values').append(node('td', d.value)));
  const formatTime = t => new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t));
  $('run-times').textContent = '真实 Template 试跑：' + formatTime(data.audit.run.started_at_utc) + ' 至 ' + formatTime(data.audit.run.finished_at_utc) + '（北京时间）。随后复用同一份回答，按更新后的程序规则重新校验。';
  $('provenance-json').textContent = JSON.stringify({scope: data.audit.scope, run: data.audit.run, input_bounds: data.audit.input_bounds, provenance: data.audit.provenance}, null, 2);
  $('download-audit').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json;charset=utf-8'}));
    const a = node('a'); a.href = url; a.download = '49-Sructure-template-v6-audit.json'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
