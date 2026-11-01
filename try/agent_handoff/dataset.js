/* Read-only tables for the complete frozen initial dataset, not a structure sample. */
(() => {
  'use strict';
  const {data, el, button, tree} = window.HandoffInspector;
  const sections = [
    ['entities','实体',['id','name','names','role','refs']],
    ['links','编号映射',['item','region','seq','name','refs']],
    ['experiments','实验',['id','label','method','table','species','refs']],
    ['observations','测量',['item','experiment','metric','value','unit','table','refs']],
    ['annotations','标签',['subject_type','subject','category','value','refs']]
  ];
  const display = value => value === null || value === undefined ? '未记录' :
    Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
  function render(target, key='seed', titleText='初始 Dataset1') {
    const artifact = data.artifacts[key], seed = artifact?.value;
    target.replaceChildren();
    if (!seed) { target.append(el('p','本次快照没有保存初始 Dataset1。','note')); return; }
    const heading = el('div',undefined,'dataset-heading');
    const title = el('div');
    title.append(el('p',titleText+' · 完整冻结数据','render-kicker'),el('h3',`${data.document_id} / ${artifact.path.split('/').pop()}`));
    heading.append(title,button('下载完整 '+titleText+' JSON',()=>{
      const url = URL.createObjectURL(new Blob([JSON.stringify(seed,null,2)+'\n'],{type:'application/json'}));
      const a=el('a'); a.href=url; a.download=`${data.document_id}.dataset1.seed.json`; a.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    }));
    target.append(heading,el('p',key==='seed'||key==='reader_initial'?'每个 Reader 拿到相同的完整业务底稿，加上本块 OCR；全部建议由 ActionAgent 综合。下方是本轮实际保存的数据。':'下方展示本阶段实际保存的完整数据；序列是否支持、缺口和原文引用均保留。','note'));
    const provenance=el('details',undefined,'render-provenance');
    provenance.append(el('summary','数据来源与字段说明'),el('p',`原文件：${artifact.original_path}`),el('p',`冻结文件 SHA-256：${artifact.sha256}`),el('p',`采集时间：${artifact.captured_at}`),el('p','下方展示全部五个数组；null 显示为“未记录”，空数组保留为空。sources.json 是独立的来源字典，不属于 seed.json 的字段。点击记录可查看完整 JSON；来源文本摘自本快照实际保存的 Reader 请求，不重建缺失坐标。'));
    target.append(provenance);
    const tabs=el('div',undefined,'dataset-sections'); tabs.setAttribute('aria-label','Dataset1 数据类别');
    const search=el('input'); search.type='search'; search.id='dataset-search'; search.placeholder='搜索当前类别的字段、实体名称或值'; search.setAttribute('aria-label','搜索 Dataset1');
    const toolbar=el('div',undefined,'dataset-toolbar'), count=el('p',undefined,'render-notice');
    toolbar.append(search,count);
    const layout=el('div',undefined,'dataset-layout'), list=el('div'), detail=el('section',undefined,'dataset-detail');
    const scroll=el('div',undefined,'dataset-scroll'), table=el('table',undefined,'dataset-table'), pager=el('div',undefined,'dataset-pager');
    scroll.append(table);list.append(scroll,pager);layout.append(list,detail);target.append(tabs,toolbar,layout);
    const names=new Map((seed.entities||[]).map(e=>[e.id,e.name]));
    const sourceRows=new Map();
    for(const call of data.calls.filter(c=>c.stage==='reader')) for(const row of call.request?.value.payload?.sources||[]) sourceRows.set(row[0],row);
    let section=sections[0], page=0;
    function inspect(row) {
      detail.replaceChildren(el('h4','所选记录 · 完整 JSON'));
      const raw=el('div',undefined,'json-tree');detail.append(raw);tree(raw,row).expand();
      const refs=[...new Set([...(row.refs||[]),...(row.s===undefined?[]:[row.s])])];
      if(refs.length)detail.append(el('h4','refs / s · 请求中保存的原文'));
      for(const sid of refs){
        const source=sourceRows.get(sid), box=el('details',undefined,'dataset-source');
        box.append(el('summary',source?`来源 ${sid} · 第 ${source[1]} 页 · ${source[3]||source[2]}`:`来源 ${sid} · 本快照请求中未记录`));
        if(source)box.append(el('p',`类型 ${source[2]} / 表 ${source[3]??'—'} / 行 ${source[4]??'—'} / 列 ${source[5]??'—'}`),el('pre',source[6]));
        detail.append(box);
      }
    }
    function draw() {
      const [key,,columns]=section, all=seed[key]||[], query=search.value.trim().toLowerCase();
      const filtered=all.filter(row=>(JSON.stringify(row)+' '+(names.get(row.item)||'')).toLowerCase().includes(query));
      page=Math.min(page,Math.max(0,Math.ceil(filtered.length/25)-1));
      count.textContent=`${filtered.length} / ${all.length} 条 · 第 ${page+1} / ${Math.max(1,Math.ceil(filtered.length/25))} 页`;
      table.replaceChildren();const head=el('thead'), hr=el('tr');
      hr.append(el('th','#'));columns.forEach(k=>hr.append(el('th',k)));head.append(hr);table.append(head);
      const body=el('tbody');table.append(body);
      const visible=filtered.slice(page*25,(page+1)*25);
      for(const [i,row] of visible.entries()){
        const tr=el('tr'), cell=el('td');cell.append(button(String(page*25+i+1),()=>inspect(row)));tr.append(cell);
        for(const key of columns){const value=display(row[key]);const td=el('td',key==='item'&&names.has(row.item)?`${value} · ${names.get(row.item)}`:value);tr.append(td);}
        tr.tabIndex=0;tr.setAttribute('aria-label',`查看第 ${page*25+i+1} 条记录`);
        tr.addEventListener('click',()=>inspect(row));tr.addEventListener('keydown',e=>{if(e.target===tr&&(e.key==='Enter'||e.key===' ')){e.preventDefault();inspect(row);}});body.append(tr);
      }
      if(!visible.length){const tr=el('tr'),td=el('td',all.length?'没有匹配记录。':`${key} = []；初始种子尚未记录这类事实。`);td.colSpan=columns.length+1;tr.append(td);body.append(tr);}
      pager.replaceChildren();const prev=button('上一页',()=>{page--;draw();}),next=button('下一页',()=>{page++;draw();});prev.disabled=page===0;next.disabled=(page+1)*25>=filtered.length;pager.append(prev,next);
      tabs.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.key===key)));
      if(visible.length)inspect(visible[0]);else detail.replaceChildren(el('p','选择有数据的类别或调整搜索条件。','note'));
    }
    const available=[...sections,['sequences','序列定义',['seq','region','name','value','refs']],['relations','实体关系',['subject','relation','object','refs']],['issues','未解决项',['kind','items','seq','refs']]].filter(item=>Array.isArray(seed[item[0]]));
    for(const item of available){const b=button(`${item[1]} · ${item[0]} (${(seed[item[0]]||[]).length})`,()=>{section=item;page=0;search.value='';draw();});b.dataset.key=item[0];tabs.append(b);}
    search.addEventListener('input',()=>{page=0;draw();});draw();
  }
  window.HandoffDataset={render};
})();
