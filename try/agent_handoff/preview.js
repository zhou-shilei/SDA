/* Render stored cells and classifications as tables and reading cards, using text nodes only. */
(() => {
  'use strict';
  const P = JSON.parse(document.getElementById('preview-data').textContent);
  const prepared = JSON.parse(document.getElementById('current-contract-data').textContent).label_inputs;
  const hasActual = Object.values(P.requests||{}).some(r=>r.value.task==='table-classification-v5');
  const requestedView = new URLSearchParams(location.search).get('label_view');
  let dataView = ['current','history'].includes(requestedView)?requestedView:hasActual?'actual':'current';
  const {el,button} = window.HandoffInspector;
  let selected = Math.max(0,(P.cases||[]).findIndex(c=>c.table_id===new URLSearchParams(location.search).get('table')));
  const labels = {sequence_definition:'序列定义表',assay_ec50:'EC50 实验表',assay_ic50:'IC50 实验表',binding_kd:'KD 结合实验表',other_biological_table:'其他生物学表格',non_target_table:'非目标表格',context_only:'作为上下文保留',extract:'进入提取',extract_measurements:'提取测量',needs_review:'需要复核',skip:'跳过',exclude:'不纳入目标提取',rows:'按行排列',columns:'按列排列',unknown:'未确定',identified:'已识别',partial:'部分完成',ready:'可处理',assay:'实验测量区'};
  const label = value => value === null || value === undefined ? '未记录' : labels[value] || String(value);
  function scopeNote(table, request) {
    if(!table)return '未记录匹配输入。';
    const task=request?.task||'版本未记录';
    if(!Array.isArray(table.rows))return `历史请求 · ${task}：保存的是旧版 cells 单元格输入；当前 v5 的“表头＋一行”规则不代表该次历史调用的实际输入。`;
    const omitted=table.omitted_data_row_count??'未记录';
    return `v5 行式输入 · ${task}：${table.input_scope==='headers_and_one_data_row'?'候选表头＋至多一个非空数据行':'完整入选表数据行'}；省略 ${omitted} 行。单元格使用原始字符串引用，坐标与置信度留在本地证据库。`;
  }
  function current() {
    const entry=P.cases?.[selected];if(!entry)return null;
    if(dataView==='current'){
      const envelope=prepared?.tables?.[entry.table_id];
      const request=envelope?{value:envelope,original_path:prepared.source.path,sha256:prepared.source.sha256}:null;
      return {entry,request,result:null,table:envelope?.payload.tables.find(t=>t.table_id===entry.table_id),prepared:true};
    }
    const request=P.requests[entry.request_key], result=P.classification?.value[entry.classification_index];
    const table=request?.value.payload.tables.find(t=>t.table_id===entry.table_id);
    return {entry,request,result,table,actual:dataView==='actual'};
  }
  function choices(side) {
    const c=current();if(!c)return [{label:'未保存配对表格',missing:true,scope:'没有可用的表格分类记录'}];
    if(c.prepared){
      if(side==='input')return [{label:`${c.entry.table_id} · 当前 v5 待发送输入`,value:c.request?.value.payload,missing:!c.request,path:c.request?.original_path,scope:prepared?.notice||'尚未准备当前输入，请运行 prepare_label_inputs.py；这里不使用旧请求替代。'}];
      return [{label:`${c.entry.table_id} · 尚未生成 v5 分类结果`,missing:true,scope:'仅生成输入预览，未调用模型。历史结果请切换“历史真实请求与结果”。'}];
    }
    if(side==='input')return [{label:`${c.entry.table_id} · ${c.actual?'本次 v5 实际请求':'历史实际请求'} payload（完整）`,value:c.request?.value.payload,missing:!c.request,path:c.request?.original_path,scope:scopeNote(c.table,c.request?.value)+' 与右侧按 table_id 和请求 input_hash 配对。'}];
    const rows=[{label:`${c.entry.table_id} · 程序整理后的分类结果（完整）`,value:c.result,missing:!c.result,path:P.classification.original_path,scope:'从 classification_progress.json 按 table_id 选择，不是模型原始回复。'}];
    if(c.entry.raw_response)rows.push({label:'该次模型原始返回（完整）',value:c.entry.raw_response.value,path:c.entry.raw_response.original_path,scope:'该次请求的原始 answer.json；与程序整理后的结果分开保留。'});
    return rows;
  }
  function metadata(target, fields) { const dl=el('dl',undefined,'render-meta');for(const[name,value]of fields){const d=el('div');d.append(el('dt',name),el('dd',value));dl.append(d);}target.append(dl); }
  function message(text,cls='render-notice') { return el('p',text,cls); }
  function chip(text,cls='render-chip') { return el('span',text,cls); }
  function renderGrid(table,result,onCell){
    const map=new Map(), refs=new Map(), occupied=new Set();
    const view=HandoffCore.tableView(table),cells=view.cells;
    const rows=Math.max(1,...view.rowIndices.map(r=>r+1)),cols=view.columnCount;
    const evidence=new Set(result?.evidence_refs||[]), issues=new Set((result?.issues||[]).flatMap(i=>i.evidence_refs||[]));
    const headers=new Set(view.isV5?view.headerRows:(result?.regions||[]).flatMap(r=>r.header_rows||[]));
    const container=el('div',undefined,'render-grid-scroll'),grid=el('table',undefined,'render-table');
    cells.forEach(c=>{if(!map.has(`${c.row}:${c.column}`))map.set(`${c.row}:${c.column}`,c);});
    let previous=-1;
    function omittedRow(start,end){if(start>end)return;const tr=el('tr',undefined,'render-omitted-row'),td=el('td',`原行 ${start}–${end} 未包含在此输入中`);td.colSpan=cols+1;tr.append(td);grid.append(tr);}
    for(const r of view.rowIndices){
      if(view.isV5)omittedRow(previous+1,r-1);
      previous=r;
      const tr=el('tr');tr.dataset.row=r;const number=el('th',String(r),'render-row-number');number.scope='row';tr.append(number);
      for(let col=0;col<cols;col++){
        if(occupied.has(`${r}:${col}`))continue;
        const cell=map.get(`${r}:${col}`), td=el(headers.has(r)?'th':'td');
        if(!cell){td.append(el('span','未提供','missing-cell'));tr.append(td);continue;}
        const rs=Math.max(1,Math.min(Number(cell.rowspan)||1,rows-r)),cs=Math.max(1,Math.min(Number(cell.colspan)||1,cols-col));
        td.rowSpan=rs;td.colSpan=cs;
        for(let ri=r;ri<r+rs;ri++)for(let ci=col;ci<col+cs;ci++)if(ri!==r||ci!==col)occupied.add(`${ri}:${ci}`);
        td.dataset.ref=cell.ref||'';td.classList.toggle('cell-evidence',evidence.has(cell.ref));td.classList.toggle('cell-issue',issues.has(cell.ref));
        const b=button(cell.text===''?'（空）':String(cell.text??''),()=>select(cell),'render-cell');b.title=`${cell.ref||''} · 点击查看来源和坐标`;td.append(b);refs.set(cell.ref,td);tr.append(td);
      }
      grid.append(tr);
    }
    if(view.isV5)omittedRow(previous+1,(Number(table.row_count)||0)-1);
    container.append(grid);
    function select(cell){refs.forEach(td=>td.classList.remove('cell-selected'));const td=refs.get(cell.ref);td?.classList.add('cell-selected');onCell(cell);return td;}
    return {container,focus(ref){const cell=cells.find(c=>c.ref===ref);if(cell){const td=select(cell);td?.scrollIntoView({block:'nearest',inline:'nearest',behavior:'smooth'});return true;}return false;}};
  }
  function showCell(target,cell){
    target.replaceChildren(el('h4','选中单元格'));
    metadata(target,[['来源地址',cell.ref||'未记录'],['行 / 列（从 0 开始）',`${cell.row} / ${cell.column}`],['OCR 置信度',cell.confidence===null||cell.confidence===undefined?'未记录':String(cell.confidence)],['bbox（完整坐标）',Array.isArray(cell.bbox)?JSON.stringify(cell.bbox):'未记录']]);
    if(cell.view)metadata(target,[['已有单元格视图',JSON.stringify(cell.view)]]);
    target.append(el('pre',String(cell.text??''),'render-cell-text'));
  }
  function renderOutput(target,result,focus){
    target.append(el('p','OUTPUT · 分类结果','render-kicker'),el('h3',label(result.primary_label)));
    const badges=el('div',undefined,'render-badges');(result.labels||[]).forEach(v=>badges.append(chip(label(v))));if(result.review_required)badges.append(chip('需要人工复核','render-chip warning'));target.append(badges);
    target.append(message(`本表被标记为“${label(result.primary_label)}”，后续${label(result.decision)}。${result.review_required?'记录要求人工复核。':''}`));
    metadata(target,[['表格 ID',result.table_id],['PDF 页码',String(result.page??'未记录')],['实体方向',label(result.entity_axis)],['测量方向',label(result.measurement_axis)],['后续处理',label(result.decision)],['保留到后续流程',result.retain_for_pipeline===true?'是':result.retain_for_pipeline===false?'否':'未记录'],['模型自报置信度',result.confidence===null||result.confidence===undefined?'未记录':String(result.confidence)]]);
    const why=el('section',undefined,'render-explanation');why.append(el('h4','分类理由 · 已保存原文'),el('p',result.explanation||'未记录'));target.append(why);
    const regions=el('section',undefined,'render-regions');regions.append(el('h4','识别到的表格分区'));
    for(const r of result.regions||[]){const card=el('div',undefined,'render-region');card.append(el('strong',`${label(r.kind)} · 行 ${r.row_start}–${r.row_end}`));if(r.kind==='assay'){metadata(card,[['实体列',r.entity_column??'未确定']]);const metrics=[...(r.metrics||[]),...(r.entity_groups||[]).flatMap(g=>g.metrics||[])];for(const m of metrics)card.append(message(`${m.endpoint??'指标未定'} · 数值列 ${m.value_column??'未定'} · 单位 ${m.unit??'未定'}`));}else metadata(card,[['名称列',r.name_column??'未确定'],['SEQ ID 列',r.sequence_id_column??'未确定'],['序列列',r.sequence_column??'未确定'],['区域 / 链范围',label(r.sequence_scope)]]);regions.append(card);}if(!result.regions?.length)regions.append(message('没有已记录的分区。'));target.append(regions);
    const issueBox=el('section',undefined,'render-issues');issueBox.append(el('h4',`待复核项 · ${(result.issues||[]).length}`));
    for(const issue of result.issues||[]){const card=el('article',undefined,'render-issue');card.append(el('strong',issue.code||'待复核'),el('p',issue.message||''));const refs=el('div',undefined,'render-refs');(issue.evidence_refs||[]).forEach(ref=>refs.append(button(ref,()=>focus(ref),'render-ref')));card.append(refs);issueBox.append(card);}if(!result.issues?.length)issueBox.append(message('此分类结果没有记录额外问题。'));target.append(issueBox);
    const evidence=el('details',undefined,'render-evidence');evidence.append(el('summary',`分类证据 · ${(result.evidence_refs||[]).length} 个单元格（点击可定位）`));const refs=el('div',undefined,'render-refs');(result.evidence_refs||[]).forEach(ref=>refs.append(button(ref,()=>focus(ref),'render-ref')));evidence.append(refs);target.append(evidence);
  }
  function render(container,onChange){
    container.replaceChildren();
    if(!P.cases?.length){container.append(message('尚未保存可配对的完整表格记录。'));return;}
    const c=current(),bar=el('div',undefined,'render-toolbar'),labelEl=el('label','选择表格');
    const select=el('select');select.id='render-table-select';select.setAttribute('aria-label','选择配对的表格');
    P.cases.forEach((entry,i)=>{const r=P.classification.value[entry.classification_index],o=el('option',`第 ${r.page??'?'} 页 · ${entry.table_id} · ${c.prepared?'待分类':label(r.primary_label)}${c.prepared?'':entry.request_key?'':'（缺少匹配输入）'}`);o.value=i;select.append(o);});select.value=selected;
    select.addEventListener('change',()=>{selected=Number(select.value);const url=new URL(location.href);url.searchParams.set('table',P.cases[selected].table_id);history.replaceState(null,'',url.href);render(container,onChange);onChange?.();});labelEl.append(select);bar.append(labelEl,chip(`${P.cases.length} 张表 · ${c.prepared?'当前输入预览':c.actual?'本次真实输入输出按 ID 配对':'历史输入输出按 ID 配对'}`));container.append(bar);
    if(!c.table){container.append(message(c.prepared?'尚未准备当前输入，请运行 prepare_label_inputs.py；不会用历史请求替代当前输入。':c.entry.missing_reason||'未记录匹配输入。'));return;}
    const view=HandoffCore.tableView(c.table);
    container.append(message(c.prepared?`当前 v5 待发送输入 · ${prepared.notice}`:scopeNote(c.table,c.request.value),'render-version-note render-notice'));
    container.append(message(`当前展示 ${c.table.table_id}，第 ${c.table.page??c.result?.page} 页。${view.isV5?'原表':'输入'} ${c.table.row_count} 行 × ${c.table.column_count} 列；输入中包含 ${view.cells.length} 个单元格${view.isV5?`，覆盖原行 ${view.rowIndices.join('、')||'无'}`:''}。${c.prepared?'尚无新版分类输出。':'输出来自同一 table_id 和请求 input_hash。'}`));
    const layout=el('div',undefined,'render-layout'),left=el('section',undefined,'render-input-card'),right=el('section',undefined,'render-output-card');left.append(el('p',c.prepared?'INPUT · 当前 v5 打包预览（未发送）':c.actual?'INPUT · 本次 v5 实际输入':'INPUT · 历史 Agent 实际输入','render-kicker'),el('h3',c.table.caption||`表格 ${c.table.table_id}`));
    const legend=el('p',undefined,'render-cell-legend');legend.append(chip('绿色：分类引用'),chip('黄色：需复核','render-chip warning'),el('span','点击单元格查看原文、地址和坐标'));left.append(legend);
    const detail=el('section',undefined,'render-cell-detail');detail.append(message(view.isV5?'点击单元格查看原始引用和原文。v5 请求未携带 bbox 和置信度，页面不补造坐标。':'点击上方单元格，查看它的来源地址、完整 bbox 和识别置信度。'));
    const grid=renderGrid(c.table,c.result,cell=>showCell(detail,cell));left.append(grid.container,detail);
    const image=P.images?.[c.table.table_id];
    if(image){const d=el('details',undefined,'render-original');d.append(el('summary','展开原始表格裁图 · 与文字表格对照'));const img=el('img');img.src=image.data_url;img.alt=`${c.table.table_id} 原始裁图`;d.append(img,message(`原图来自本地已保存裁图，并核对了图像 SHA-256。${view.isV5?'仅供人工对照，未发送给本次分类 Agent。':''}`));left.append(d);}else left.append(message('此表的原始图片未内嵌；上方仅显示实际请求提供的文字单元格。'));
    if(c.prepared){right.append(el('p','OUTPUT · 尚未运行新版分类','render-kicker'),el('h3',c.table.table_id),message('本次只准备新版输入，未调用模型，没有新版分类结果。历史结果请切换上方“历史真实请求与结果”。'));}
    else renderOutput(right,c.result,ref=>{if(grid.focus(ref))return;const context=(c.table.context||[]).find(s=>s.ref===ref);detail.replaceChildren(el('h4','引用的辅助来源'));metadata(detail,[['来源地址',ref],['来源类型',ref.endsWith(':image')?'原始裁图':context?'上下文文字':'本表网格之外的来源']]);detail.append(message(context?.text||'这条引用不是本表的文字单元格；请在原始 JSON 或裁图中核对。'));detail.scrollIntoView({block:'nearest',behavior:'smooth'});});layout.append(left,right);container.append(layout);
    const provenance=el('details',undefined,'render-provenance');provenance.append(el('summary','查看本例的数据来源与版本'));metadata(provenance,c.prepared?[['OCR 输入原文件',c.request.original_path],['OCR 输入 SHA-256',c.request.sha256],['预览生成时间',prepared.captured_at],['执行状态','prepared_not_sent · 未调用模型']]:[['输入原文件',c.request.original_path],['输入 SHA-256',c.request.sha256],['分类结果原文件',P.classification.original_path],['结果数组下标',String(c.entry.classification_index)],['配对依据',`table_id=${c.entry.table_id}；input_hash=${c.result.classifier?.input_hash||'未记录'}`],['采集时间',P.captured_at]]);container.append(provenance);
  }
  function setView(value){dataView=value==='actual'&&hasActual?'actual':value==='history'?'history':'current';const url=new URL(location.href);url.searchParams.set('label_view',dataView);history.replaceState(null,'',url.href);}
  window.HandoffPreview={render,current,choices,scopeNote,setView,hasActual,hasPrepared:Boolean(prepared),view:()=>dataView,data:P};
})();
