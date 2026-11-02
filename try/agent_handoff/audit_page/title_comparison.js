(() => {
  'use strict';
  const data=JSON.parse(document.getElementById('comparison-data').textContent),packages=JSON.parse(document.getElementById('dataset-downloads').textContent);
  const $=id=>document.getElementById(id),el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const groups=[['tables_only','仅表格'],['with_titles','表格＋标题'],['with_text','表格＋正文']].map(([key,label])=>({key,label,arm:data.arms[key]}));
  const pct=x=>(x*100).toFixed(1)+'%',num=x=>typeof x==='number'?x.toLocaleString('zh-CN'):'未记录';
  const headers=document.querySelector('.comparison-table thead tr');headers.replaceChildren(el('th','比较内容'));
  groups.forEach((g,i)=>{const th=el('th');th.append(el('span',String.fromCharCode(65+i)+' / '+(g.key==='with_text'?'已有真实回答':'真实试跑'),'group-tag'),document.createTextNode(g.label),el('small',g.key==='with_titles'?(data.title_validation_replay?'14 个标题；同回答按修正后的标题校验入库':'只增加 14 个原图核对的表标题'):g.key==='tables_only'?'表头和数据行，无标题和正文':'相同表格，另有 513 个正文片段'));headers.append(th);});
  const rh=el('th');rh.append(el('span','D / 用户选定参考','group-tag'),document.createTextNode('完整 PDF 提取'),el('small','模型参考结果，非人工标准'));headers.append(rh);headers.querySelectorAll('th').forEach(th=>th.style.width='20%');
  const row=(label,fn,last,main=false)=>{const tr=el('tr',undefined,main?'main-row':'');tr.append(el('th',label));groups.forEach(g=>tr.append(el('td',fn(g.arm))));tr.append(el('td',last));$('summary-rows').append(tr);};
  row('基础数值结果：与共同参考一致',a=>a.measurement_base_agreement.matched+' / 221','221 个参考结果',true);
  row('再要求物种一致',a=>a.with_species.matched+' / 221','221 个参考结果');
  row('再要求实验方法类别一致',a=>a.with_method_family.matched+' / 221','221 个参考结果');
  row('核心 VH/VL 编号：模型原始回答',a=>a.core_variable_links.raw_model_agreement+' / 30','30 条编号关系');
  row('核心 VH/VL 编号：当前校验后入库',a=>a.core_variable_links.retained_agreement+' / 30','30 条编号关系',true);
  row('全部序列关联（含完整链）',a=>String(a.total_links),'范围更广，不直接计召回');
  row('实体 / 实验配置 / 测量',a=>a.total_entities+' / '+a.total_experiments+' / '+a.total_observations,'70 / 20 / 291（范围更广）');
  row('模型实际输入 token',a=>num(a.llm_usage.input_tokens),'完整 PDF 流程不直接比较');
  row('正文片段',a=>String(a.passages_supplied),'263 页原始 PDF 自主提取');
  const title=data.arms.with_titles,body=data.arms.with_text;
  $('token-reduction').textContent='减少 '+pct(1-title.llm_usage.input_tokens/body.llm_usage.input_tokens);
  $('token-detail').textContent='表格＋标题 '+num(title.llm_usage.input_tokens)+'，表格＋正文 '+num(body.llm_usage.input_tokens)+' 输入 token。标题只增加 554 个字符，未额外提供实验方法或身份说明。';
  $('page-reduction').textContent=title.input_pages_represented+' 页 / '+body.input_pages_represented+' 页';
  const costhead=document.querySelector('#cost-rows').closest('table').querySelector('thead tr');costhead.replaceChildren(el('th','成本 / 缺口'));groups.forEach(g=>costhead.append(el('th',g.label)));
  const cost=[['输入字符',a=>num(a.input_chars)],['模型输出 token',a=>num(a.llm_usage.output_tokens)],['模型调用耗时 / 秒',a=>num(a.original_llm_elapsed_seconds)],['核心 30 条编号：仍未入库',a=>String(a.core_variable_links.missing.length)],['核心 15 个抗体：完整序列配对',a=>a.core_variable_pairs.candidate+' / 15'],['主机生成的待补问题数',a=>String(a.host_followup_questions)]];
  cost.forEach(([label,fn])=>{const tr=el('tr');tr.append(el('th',label));groups.forEach(g=>tr.append(el('td',fn(g.arm))));$('cost-rows').append(tr);});
  const bars=document.querySelector('.bars');bars.replaceChildren();
  [...groups,{key:'reference',label:'完整 PDF 参考'}].forEach((g,i)=>{const tr=el('div',undefined,'bar-row'),track=el('div',undefined,'bar-track'),fill=el('div',undefined,'bar-fill'),score=el('span',undefined,'score');fill.id='bar-'+g.key;score.id='score-'+g.key;fill.style.background=['#b89c71','#236b53','#79916e','#b3bca6'][i];track.append(fill);tr.append(el('span',g.label),track,score);bars.append(tr);});
  const fields={measurement_base_agreement:[],with_species:['species'],with_method_family:['method'],with_species_method_and_donor:['species','method','donor']};
  const base=r=>JSON.stringify([r.table,r.entity,r.metric,r.value,r.unit]),full=(r,mode)=>JSON.stringify([base(r),...fields[mode].map(f=>r[f]||'')]);
  const describe=r=>'物种：'+(r.species||'未填写')+' · 方法：'+(r.method||'未填写')+' · 供者：'+(r.donor||'未填写');
  let matches=[],visible=[],page=0;const pageSize=8;
  const recordhead=document.querySelector('#record-rows').closest('table').querySelector('thead tr');recordhead.replaceChildren(el('th','参考中的一条记录'));groups.forEach(g=>recordhead.append(el('th',g.label)));recordhead.append(el('th','完整 PDF 模型参考'));
  const filter=$('record-filter');filter.options[0].textContent='任一组未完整匹配的记录';
  [...new Set(data.reference_rows.map(r=>r.table))].sort((a,b)=>Number(a.match(/\d+/)[0])-Number(b.match(/\d+/)[0])).forEach(t=>{const o=el('option',t);o.value=t;$('table-filter').append(o);});
  function assign(arm,mode){const pool=arm.all_normalized_rows.map(r=>({row:r,used:false}));return data.reference_rows.map(r=>{const hit=pool.find(x=>!x.used&&full(x.row,mode)===full(r,mode));if(hit){hit.used=true;return{pass:true,row:hit.row};}return{pass:false,row:pool.find(x=>base(x.row)===base(r))?.row};});}
  function render(){
    $('record-rows').replaceChildren();visible.slice(page*pageSize,(page+1)*pageSize).forEach(x=>{const tr=el('tr'),first=el('td',undefined,'row-detail');first.append(el('strong',x.reference.table+' · '+x.reference.entity),el('span',x.reference.metric+' = '+x.reference.raw_value+' '+x.reference.unit));tr.append(first);groups.forEach(g=>{const c=x[g.key],td=el('td',undefined,'row-detail');td.append(el('strong',c.pass?'一致':c.row?'数值一致，语义字段有缺口':'未找到一致数值结果',c.pass?'cell-pass':'cell-fail'));if(c.row)td.append(el('span',describe(c.row)),el('code',c.row.source));tr.append(td);});const last=el('td',undefined,'row-detail');last.append(el('strong',describe(x.reference)),el('span',JSON.stringify(x.reference.source)));tr.append(last);$('record-rows').append(tr);});
    const pages=Math.max(1,Math.ceil(visible.length/pageSize));$('record-count').textContent='当前显示范围 '+visible.length+' / 221';$('page-label').textContent='第 '+(page+1)+' / '+pages+' 页 · '+visible.length+' 条记录';$('previous').disabled=page===0;$('next').disabled=page+1>=pages;
  }
  function applyFilter(){const table=$('table-filter').value,differences=$('record-filter').value==='differences';visible=matches.filter(r=>(table==='all'||r.reference.table===table)&&(!differences||groups.some(g=>!r[g.key].pass)));page=0;render();}
  function changeMode(){const mode=$('match-mode').value;groups.forEach(g=>{const s=g.arm[mode];$('bar-'+g.key).style.width=pct(s.reference_coverage);$('score-'+g.key).replaceChildren(document.createTextNode(s.matched+' / 221'),el('small',pct(s.reference_coverage)));});$('bar-reference').style.width='100%';$('score-reference').replaceChildren(document.createTextNode('221 / 221'),el('small','参考自身基数'));$('interpretation').replaceChildren(el('strong',groups.map(g=>g.label+' '+g.arm[mode].matched+' 条一致').join('；')+'。'),el('p','只比较所选字段。留空信息不算一致，亦不算错误数值；原 PDF 参考本身仍需要人工复核。'));const assignments=Object.fromEntries(groups.map(g=>[g.key,assign(g.arm,mode)]));matches=data.reference_rows.map((r,i)=>Object.assign({reference:r},...groups.map(g=>({[g.key]:assignments[g.key][i]}))));applyFilter();}
  $('match-mode').addEventListener('change',changeMode);$('table-filter').addEventListener('change',applyFilter);$('record-filter').addEventListener('change',applyFilter);$('previous').addEventListener('click',()=>{page--;render();});$('next').addEventListener('click',()=>{page++;render();});
  const replay=data.title_validation_replay;
  $('decision-text').replaceChildren(el('strong','以初级 Dataset 为目标：先保住能确定的索引。'),el('p','标题组模型原始回答与核心 30 条编号关系一致 '+title.core_variable_links.raw_model_agreement+' 条，当前入库 '+title.core_variable_links.retained_agreement+' 条；可比数值结果 '+title.measurement_base_agreement.matched+' / 221。'),el('p','初级 Dataset 不要求此时就具备完整的氨基酸序列。后续 Reader 可以结合 chunk 补充定义、实验解释和身份关系。'));
  if(replay){const box=el('div',undefined,'interpretation');box.id='caption-validation-replay';box.append(el('strong','同一份标题组回答：修正标题证据校验后'),el('p','原校验保留 '+replay.original_core_links+' / 30；标题与同表同一行联合校验后，保留 '+replay.corrected_core_links+' / 30。没有新增模型调用，也没有补入正文。'));$('decision-text').after(box);}
  const downloads=document.querySelector('.downloads');downloads.replaceChildren();Object.keys(packages).forEach(filename=>{const labels={'tables-only-dataset1.json':'A · 仅表格','with-titles-dataset1.json':'B · 表格＋标题'+(replay?'（标题校验修正）':'（原校验）'),'with-text-dataset1.json':'C · 表格＋正文','full-pdf-model-reference.json':'D · 完整 PDF 模型参考','with-titles-original-validation-dataset1.json':'标题组原校验结果（保留作对照）','comparison.json':'完整对照与实验记录'};const link=el('a','下载 '+(labels[filename]||filename));link.href='title-comparison-data/'+filename;link.download=filename;link.addEventListener('click',event=>{event.preventDefault();const url=URL.createObjectURL(new Blob([JSON.stringify(packages[filename],null,2)],{type:'application/json;charset=utf-8'}));const a=el('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);});downloads.append(link);});
  $('experiment-json').textContent=JSON.stringify({reference_label:data.reference_label,title_experiment:data.title_experiment,title_validation_replay:data.title_validation_replay,source_pdf_provenance:data.source_pdf_provenance,methodology:data.methodology},null,2);
  changeMode();
})();
