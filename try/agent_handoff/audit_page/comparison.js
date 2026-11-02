(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('comparison-data').textContent);
  const offlineDatasets = JSON.parse(document.getElementById('dataset-downloads').textContent);
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const a=data.arms.tables_only,b=data.arms.with_text,ref=data.reference;
  const numeric = n => typeof n==='number'?n.toLocaleString('zh-CN'):'未记录';
  const percent = n => (n*100).toFixed(1)+'%';
  const row = (label,values,main=false) => {const tr=el('tr',undefined,main?'main-row':'');tr.append(el('th',label));values.forEach(v=>tr.append(el('td',v)));$('summary-rows').append(tr);};
  row('共同范围：已入库且基础字段一致',[a.measurement_base_agreement.matched+' / 221',b.measurement_base_agreement.matched+' / 221','221 个参考结果'],true);
  row('再要求物种＋方法类别＋供者一致',[a.with_species_method_and_donor.matched+' / 221',b.with_species_method_and_donor.matched+' / 221','221 个参考结果']);
  row('已入库的 197 条测量：方法字段留空',[String(a.explicit_missing_metadata_on_common_rows.method),String(b.explicit_missing_metadata_on_common_rows.method),'参考结果含方法说明']);
  row('已入库的 197 条测量：物种字段留空',[String(a.explicit_missing_metadata_on_common_rows.species),String(b.explicit_missing_metadata_on_common_rows.species),'参考结果含物种说明']);
  row('15 个核心抗体：VH/VL 编号关联',[a.core_variable_links.retained_agreement+' / 30',b.core_variable_links.retained_agreement+' / 30','30 条编号关系']);
  row('15 个核心抗体：完整序列配对',[a.core_variable_pairs.candidate+' / 15',b.core_variable_pairs.candidate+' / 15','15 对参考序列']);
  row('全部输出实体（范围不完全相同）',[String(a.total_entities),String(b.total_entities),String(ref.entities_all_scopes)+'（含其他实体）']);
  row('全部测量记录（范围不完全相同）',[String(a.total_observations),String(b.total_observations),String(ref.measurements_all_scopes)+'（含 N/A、评分等）']);
  row('进入 Template 的正文片段',[String(a.passages_supplied),String(b.passages_supplied),'263 页原始 PDF 自主提取']);
  const inputA=a.llm_usage.input_tokens,inputB=b.llm_usage.input_tokens;
  $('token-reduction').textContent=inputA&&inputB?'减少 '+percent(1-inputA/inputB):'用量待核对';
  $('token-detail').textContent='仅表格 '+numeric(inputA)+' → 表格＋正文 '+numeric(inputB)+' 输入 token。统计模型实际返回的输入用量，不包含 OCR 计算量。';
  $('page-reduction').textContent=a.input_pages_represented+' 页 / '+b.input_pages_represented+' 页';
  const costRows=[['输入字符',numeric(a.input_chars),numeric(b.input_chars)],['模型输出 token',numeric(a.llm_usage.output_tokens),numeric(b.llm_usage.output_tokens)],['模型调用耗时 / 秒',numeric(a.original_llm_elapsed_seconds),numeric(b.original_llm_elapsed_seconds)],['核心 30 条编号关系：模型原始回答一致数',String(a.core_variable_links.raw_model_agreement),String(b.core_variable_links.raw_model_agreement)],['核心 30 条编号关系：校验后缺失数',String(a.core_variable_links.missing.length),String(b.core_variable_links.missing.length)],['主机生成的待补问题数',String(a.host_followup_questions),String(b.host_followup_questions)],['本次新增 Reader / Answer 调用','0','0（复用已有 Template 回答）']];
  costRows.forEach(r=>{const tr=el('tr');r.forEach((v,i)=>tr.append(el(i===0?'th':'td',v)));$('cost-rows').append(tr);});
  const extras={measurement_base_agreement:[],with_species:['species'],with_method_family:['method'],with_species_method_and_donor:['species','method','donor']};
  const base=r=>JSON.stringify([r.table,r.entity,r.metric,r.value,r.unit]);
  const full=(r,mode)=>JSON.stringify([base(r),...extras[mode].map(k=>r[k]||'')]);
  const info=r=>'物种：'+(r.species||'未填写')+' · 方法：'+(r.method||'未填写')+' · 供者：'+(r.donor||'未填写');
  let matchedRows=[],visible=[],page=0;
  const size=8;
  [...new Set(data.reference_rows.map(r=>r.table))].sort((x,y)=>Number(x.match(/\d+/)[0])-Number(y.match(/\d+/)[0])).forEach(t=>{const o=el('option',t);o.value=t;$('table-filter').append(o);});
  function assign(arm,mode){
    const pool=arm.all_normalized_rows.map(r=>({row:r,used:false}));
    return data.reference_rows.map(r=>{
      let found=pool.find(c=>!c.used&&full(c.row,mode)===full(r,mode));
      if(found){found.used=true;return{pass:true,row:found.row,reason:'一致'};}
      // Keep a base-equal candidate as an explanation, never as a successful match.
      const similar=pool.find(c=>base(c.row)===base(r));
      return similar?{pass:false,row:similar.row,reason:'数值一致，语义字段缺失或不同'}:{pass:false,row:null,reason:'未找到一致数值结果'};
    });
  }
  function renderPage(){
    $('record-rows').replaceChildren();
    visible.slice(page*size,(page+1)*size).forEach(x=>{
      const tr=el('tr');const first=el('td',undefined,'row-detail');first.append(el('strong',x.reference.table+' · '+x.reference.entity),el('span',x.reference.metric+' = '+x.reference.raw_value+' '+x.reference.unit),el('code',x.reference.id));tr.append(first);
      [x.tables_only,x.with_text].forEach(c=>{const td=el('td',undefined,'row-detail');td.append(el('strong',c.reason,c.pass?'cell-pass':'cell-fail'));if(c.row){td.append(el('span',info(c.row)),el('code',c.row.source));}tr.append(td);});
      const last=el('td',undefined,'row-detail');last.append(el('strong',info(x.reference)),el('span',JSON.stringify(x.reference.source)));tr.append(last);$('record-rows').append(tr);
    });
    const pages=Math.max(1,Math.ceil(visible.length/size));$('page-label').textContent='第 '+(page+1)+' / '+pages+' 页 · '+visible.length+' 条记录';$('record-count').textContent='当前显示范围 '+visible.length+' / 221';$('previous').disabled=page===0;$('next').disabled=page+1>=pages;
  }
  function filter(){const table=$('table-filter').value,only=$('record-filter').value==='differences';visible=matchedRows.filter(r=>(table==='all'||r.reference.table===table)&&(!only||!r.tables_only.pass||!r.with_text.pass));page=0;renderPage();}
  function modeChange(){
    const mode=$('match-mode').value;
    for(const [name,arm]of Object.entries(data.arms)){const score=arm[mode];$('bar-'+name).style.width=percent(score.reference_coverage);$('score-'+name).replaceChildren(document.createTextNode(score.matched+' / 221'),el('small',percent(score.reference_coverage)));}
    const aa=a[mode].matched,bb=b[mode].matched;
    $('interpretation').replaceChildren(el('strong','此口径下：仅表格 '+aa+' 条一致，加入正文 '+bb+' 条一致。'),el('p',mode==='measurement_base_agreement'?'这里只检查实体、指标、数值、单位。它能说明数值是否接进数据集，不能证明实验解释完整。':'这里会把缺失或不同的语义字段算作未一致。方法类别、物种或供者有缺口时，同一个数值也不能算完整匹配。'));
    const assignedA=assign(a,mode),assignedB=assign(b,mode);matchedRows=data.reference_rows.map((r,i)=>({reference:r,tables_only:assignedA[i],with_text:assignedB[i]}));filter();
  }
  $('match-mode').addEventListener('change',modeChange);$('table-filter').addEventListener('change',filter);$('record-filter').addEventListener('change',filter);$('previous').addEventListener('click',()=>{page--;renderPage();});$('next').addEventListener('click',()=>{page++;renderPage();});
  $('decision-text').replaceChildren(el('strong','正文带来的收益，要看补齐了哪些关系。'),el('p','本次加入正文后，基础字段一致结果从 '+a.measurement_base_agreement.matched+' 条变成 '+b.measurement_base_agreement.matched+' 条；加上物种、方法类别和供者后，一致结果从 '+a.with_species_method_and_donor.matched+' 条变成 '+b.with_species_method_and_donor.matched+' 条。'),el('p','与此同时，两组核心抗体的完整序列配对仍都是 0。只增加正文，尚未解决序列定义和导出器的共同缺口。'));
  $('experiment-json').textContent=JSON.stringify({reference_label:data.reference_label,methodology:data.methodology,source_pdf_provenance:data.source_pdf_provenance,reference:data.reference,experiment:data.experiment},null,2);
  document.querySelectorAll('.downloads a').forEach(link=>link.addEventListener('click',event=>{
    event.preventDefault();
    const filename=link.getAttribute('href').split('/').pop();
    const payload=filename==='comparison.json'?data:offlineDatasets[filename];
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json;charset=utf-8'}));
    const anchor=el('a');anchor.href=url;anchor.download=filename;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }));
  modeChange();
})();
