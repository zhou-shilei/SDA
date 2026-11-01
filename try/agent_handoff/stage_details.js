/* Read-only views of the frozen run. No model or pipeline calls. */
(() => {
  'use strict';
  const {data,el,button,tree,copy}=HandoffInspector;
  const F=JSON.parse(document.getElementById('flow-data').textContent);
  const link=(text,path)=>{const a=el('a',text);a.href=path;a.target='_blank';a.rel='noopener';return a;};
  const arrays=value=>Object.entries(value||{}).filter(([,v])=>Array.isArray(v)).map(([k,v])=>`${k}: ${v.length}`).join(' · ');
  let generation=0;
  function prompt(target,stage,call){
    target.replaceChildren();
    if(stage.kind!=='llm'){
      target.append(el('h3','这一步由程序 / OCR 执行'),el('p','本节点没有文本 LLM Prompt。输入、输出文件和实际处理逻辑分别在“内容与样例”“JSON”“源码”中查看。','note'));return;
    }
    if(!call){target.append(el('p','没有找到本轮实际请求，不能用当前代码中的模板冒充实际 Prompt。','note'));return;}
    const request=call.request.value;
    const info=el('div',undefined,'request-summary');
    for(const [k,v] of [['模型',request.model],['推理预算',request.effort],['请求哈希',call.key],['接纳状态',call.current?'本轮使用的请求':'历史尝试 / 未作为最终接纳请求']]){
      const item=el('div');item.append(el('span',k),el('strong',v??'未记录'));info.append(item);
    }
    target.append(info);
    const tools=el('div',undefined,'prompt-tools');
    tools.append(button('复制 Instructions',()=>copy(request.instructions||'')));
    if(call.prompt){
      tools.append(link('打开 / 下载完整基础 Prompt',call.prompt.path));
      tools.append(button('复制完整基础 Prompt',async()=>{try{const r=await fetch(call.prompt.path);if(!r.ok)throw Error('无法读取');copy(await r.text());}catch{copy((request.instructions||'')+'\nINPUT_JSON:\n'+JSON.stringify(request.payload));}}));
    }
    tools.append(link('原始请求 JSON',call.request.path));target.append(tools);
    target.append(el('h3','Instructions · 冻结请求中的完整指令'),el('pre',request.instructions||'请求未记录 instructions','prompt-full'));
    target.append(el('p',call.prompt?.scope||'按冻结请求查看；没有重新调用模型。','note'));
    target.append(el('h3','INPUT_JSON · 本次实际业务输入'));
    const payload=el('div',undefined,'json-tree');target.append(payload);tree(payload,request.payload);
    const schema=el('details',undefined,'schema-box');schema.append(el('summary','本次实际输出 Schema'));
    const schemaTree=el('div',undefined,'json-tree');schema.append(schemaTree);tree(schemaTree,request.schema);target.append(schema);
    if(request.images?.length)target.append(el('h3',`本次还附带 ${request.images.length} 张图片`),el('pre',JSON.stringify(request.images,null,2),'prompt-full'));
  }
  function cellText(cell){return Array.isArray(cell)?String(cell[1]??''):typeof cell==='object'?JSON.stringify(cell):String(cell??'');}
  function table(target,t,raw){
    target.replaceChildren();target.append(el('h3',t.caption||`${t.table_id} · 未识别到表题`),el('p',`${t.table_id} · PDF ${t.page} 页 · 表头状态 ${t.header_status||'未记录'} · 数据行 ${t.rows?.length||0} · 遗漏数据行 ${t.omitted_data_row_count??'未记录'}`,'note'));
    const layout=el('div',undefined,'table-preview-layout'),left=el('div'),right=el('div');layout.append(left,right);target.append(layout);
    const grid=el('table',undefined,'stage-literal-table'),scroll=el('div',undefined,'stage-table-scroll');scroll.append(grid);left.append(scroll);
    const heads=t.headers||[];
    const headerRows=heads.length&&Array.isArray(heads[0])&&typeof heads[0][0]==='string'?[heads]:heads;
    const rows=[...headerRows,...(t.rows||[])];
    for(const [i,row] of rows.entries()){
      const tr=el('tr');tr.append(el('th',i<headerRows.length?'表头':`R${t.row_indices?.[i-headerRows.length]??i}`));
      for(const c of row){const td=el(i<headerRows.length?'th':'td');if(Array.isArray(c))td.append(el('span',String(c[0]),'source-address'));td.append(el('div',cellText(c),'literal-cell'));tr.append(td);}grid.append(tr);
    }
    const plan=raw?.tables?.find(x=>x.table_id===t.table_id);
    right.append(el('h4','该表的模型判断与定位'));
    if(plan){const box=el('div',undefined,'json-tree');right.append(box);tree(box,plan).expand();}else right.append(el('p','此处没有单独分类返回；可切换 JSON 查看本轮原始结果。','note'));
    const image=F.images?.[t.table_id];
    if(image){const d=el('details',undefined,'source-image');d.append(el('summary','查看本轮原始裁图（包含表题区域）'));const img=el('img');img.src=image.path;img.loading='lazy';img.alt=t.caption||t.table_id;d.append(img,el('p',`SHA-256 ${image.sha256}`,'note'));left.append(d);}
  }
  async function catalog(target,kind){
    const current=++generation,entries=F[kind]||[];target.replaceChildren();
    if(!entries.length){target.append(el('p','本快照未保存可切换的完整记录。','note'));return;}
    const label=el('label',kind==='pages'?'选择正文 OCR 页':'选择表格 OCR'),select=el('select');
    for(const x of entries){const o=el('option',`${x.id} · PDF ${x.page} 页 · ${x.count} 条记录`);o.value=x.path;select.append(o);}label.append(select);target.append(label);
    const detail=el('div');target.append(detail);
    const draw=async()=>{const path=select.value;detail.replaceChildren(el('p','读取已冻结的本地记录……','note'));try{
      const response=await fetch(path);if(!response.ok)throw Error('记录读取失败');const row=await response.json();if(current!==generation||path!==select.value)return;
      detail.replaceChildren(link('打开该条完整 JSON',path));
      if(kind==='pages'){
        detail.append(el('h3',`PDF 第 ${row.page_number} 页`));const lines=el('div',undefined,'ocr-lines');for(const line of row.lines||[])lines.append(el('p',line.text||''));detail.append(lines);
      }else{
        detail.append(el('p',`${row.table_id} · PDF ${row.page} 页 · ${row.status}`,'note'));
        const image=F.images?.[row.table_id];if(image){const d=el('details',undefined,'source-image');d.append(el('summary','查看本轮裁图'));const img=el('img');img.src=image.path;img.loading='lazy';img.alt=row.table_id;d.append(img);detail.append(d);}
        const box=el('div',undefined,'json-tree');detail.append(box);tree(box,row);
      }
    }catch(error){if(current===generation)detail.replaceChildren(el('p',error.message+'；请通过本地 HTTP 地址查看。','note'));}};
    select.addEventListener('change',draw);await draw();
  }
  let selectedSource=null;
  function sourcePreview(target){
    const sources=data.artifacts.source_index?.value;
    if(!sources)return false;
    target.replaceChildren(el('h3','原文地址与实际位置'),el('p','Agent 引用来源编号；主机保存原文与坐标。框是文字行或单元格范围，空坐标保持未定位。下方坐标使用工作 PDF 页，原始 PDF 变换见上游文件。','note'));
    const search=el('input');search.type='search';search.className='source-search';search.placeholder='搜索来源 ID、原始引用或文字，例如 0.6233 / p0207-r0002';search.setAttribute('aria-label','搜索原位来源');target.append(search);
    const layout=el('div',undefined,'source-inspector'),list=el('div',undefined,'source-result-list'),detail=el('div');layout.append(list,detail);target.append(layout);
    const entries=Object.entries(sources);selectedSource=selectedSource&&sources[selectedSource]?selectedSource:String(data.artifacts.final_dataset?.value.observations?.[0]?.s||entries[0][0]);
    function show(id){
      selectedSource=id;const source=sources[id],box=source.bbox_pdf||source.bbox||[];detail.replaceChildren(el('h4',`来源 ${id} · ${source.source_ref}`));
      detail.append(el('div',`PDF ${source.page} 页 · ${source.kind} · ${source.table_id||'正文'}\n工作页 bbox：${box.length===4?JSON.stringify(box):'未定位'}`,'source-position'),el('pre',source.text,'prompt-full'));
      if(F.pdf)detail.append(link('打开原始 PDF 对应页',F.pdf.path+'#page='+source.page));
      const image=F.images?.[source.table_id],meta=F.samples.layout?.value.images?.find(im=>im.id===source.table_id);
      if(image&&meta){
        const canvas=el('div',undefined,'source-overlay'),img=el('img');img.src=image.path;img.alt='原图位置：'+source.source_ref;canvas.append(img);detail.append(canvas);
        let crop=source.bbox_crop;
        if((!crop||crop.length!==4)&&box.length===4&&meta.crop_to_pdf?.length===6){
          const [a,b,c,d,e,f]=meta.crop_to_pdf,det=a*d-b*c;
          if(det){const ps=[[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]].map(([x,y])=>[(d*(x-e)-c*(y-f))/det,(-b*(x-e)+a*(y-f))/det]);crop=[Math.min(...ps.map(p=>p[0])),Math.min(...ps.map(p=>p[1])),Math.max(...ps.map(p=>p[0])),Math.max(...ps.map(p=>p[1]))];}
        }
        if(crop?.length===4){const ns='http://www.w3.org/2000/svg',overlay=document.createElementNS(ns,'svg'),rect=document.createElementNS(ns,'rect');overlay.setAttribute('viewBox',`0 0 ${meta.width_px} ${meta.height_px}`);for(const [k,v] of Object.entries({x:crop[0],y:crop[1],width:crop[2]-crop[0],height:crop[3]-crop[1],fill:'#ffdc6a77',stroke:'#246da1','stroke-width':5}))rect.setAttribute(k,v);overlay.append(rect);canvas.append(overlay);}
        else detail.append(el('p','本来源没有已保存的可靠框，展示原始裁图供复核。','note'));
      }
      const raw=el('details');raw.append(el('summary','完整来源记录'));const t=el('div',undefined,'json-tree');raw.append(t);tree(t,source);detail.append(raw);
    }
    function draw(){const q=search.value.trim().toLowerCase();list.replaceChildren();const matched=q?entries.filter(([id,v])=>(id+' '+v.source_ref+' '+v.text).toLowerCase().includes(q)):entries;list.append(el('p',`${matched.length} 条匹配，显示前 50 条；点击查看原位。`,'note'));for(const [id,v] of matched.slice(0,50))list.append(button(`${id} · p${v.page} · ${v.source_ref}\n${v.text.slice(0,65)}`,()=>show(id)));}
    search.addEventListener('input',draw);draw();show(selectedSource);return true;
  }
  function reviewPreview(target){
    const final=data.artifacts.final_dataset?.value;if(!final)return false;
    const names=new Map(final.entities.map(e=>[e.id,e.name]));target.replaceChildren(el('h3',`本轮已结束，业务状态 ${final.status}`),el('p','问题、冲突与未采纳建议保持原样留存；下面的清单可能重叠。标记数量不是错误率。','note'));
    const categories=[['未解决问题',data.artifacts.remaining_questions?.value||[]],['冲突标记',final.conflicts],['问题标记',final.issues],['未采纳建议',final.rejected_facts]],buttons=el('div',undefined,'review-category-buttons'),list=el('div',undefined,'review-list'),detail=el('div');target.append(buttons,list,detail);
    function select(title,rows){list.replaceChildren();detail.replaceChildren();for(const [i,row] of rows.entries()){
      const label=row.targets?row.targets.map(t=>(names.get(t.item)||t.item)+' '+t.chain).join(' / '):[names.get(row.item)||row.item,row.chain,row.kind,row.error].filter(Boolean).join(' · ');
      list.append(button(`${i+1}. ${label||row.id||title}`,()=>{detail.replaceChildren(el('h4',title+' · 原始保存记录'));const box=el('div',undefined,'json-tree');detail.append(box);tree(box,row).expand();}));
    }if(rows.length)list.firstElementChild.click();else detail.append(el('p','本清单为空。','note'));}
    for(const [title,rows] of categories)buttons.append(button(`${title} · ${rows.length}`,()=>select(title,rows)));select(...categories[0]);return true;
  }
  function preview(target,stage,call,navigate){
    generation++;
    if(['source_index','provenance'].includes(stage.id))return sourcePreview(target);
    if(stage.id==='review')return reviewPreview(target);
    if(stage.id==='retrieval'&&call){const p=call.request.value.payload;target.replaceChildren(el('h3','本题程序实际取证结果'),el('p','这是已保存的 Answer 输入；程序已经完成检索，模型随后按这份证据回答。','note'));const stats=el('div',undefined,'retrieval-summary');for(const [k,v] of [['原文来源',p.sources?.length||0],['已知可变区定义',p.known_variable_definitions?.length||0],['已知恒定区定义',p.known_constant_definitions?.length||0],['预算未容纳来源',p.omitted_matching_sources??'未记录']])stats.append(el('span',k+'：'+v));const box=el('div',undefined,'json-tree');target.append(stats,box);tree(box,p);return true;}
    const datasetKeys={seed:data.artifacts.reader_initial?'reader_initial':'seed',ledger:'first_pass',final:'final_dataset'};
    if(datasetKeys[stage.id]&&data.artifacts[datasetKeys[stage.id]]){HandoffDataset.render(target,datasetKeys[stage.id],{seed:'初始 Dataset1',ledger:'ActionAgent 后 Dataset2',final:'最终 Dataset3'}[stage.id]);return true;}
    if(stage.id==='template'||stage.id==='table_input'){
      const tcall=call?.stage==='template'?call:data.calls.find(c=>c.stage==='template'&&c.current);const tables=tcall?.request.value.payload.tables;
      if(!tables)return false;
      target.replaceChildren();const label=el('label','选择本轮实际输入的表格'),select=el('select');select.id='stage-table-select';tables.forEach((t,i)=>{const o=el('option',`${t.table_id} · ${t.caption||'无标题'}`);o.value=i;select.append(o);});label.append(select);target.append(label);
      const content=el('div');target.append(content);const draw=()=>table(content,tables[Number(select.value)],tcall.response?.value);select.addEventListener('change',draw);draw();return true;
    }
    if(stage.id==='page_ocr'||stage.id==='table_ocr'){catalog(target,stage.id==='page_ocr'?'pages':'tables');return true;}
    if(stage.id==='sequence_review'&&call){
      target.replaceChildren(el('h3',`${call.id} · 原图序列复核`));const image=F.images?.[call.id];if(image){const img=el('img');img.src=image.path;img.alt=call.id;img.className='review-image';target.append(img);}const box=el('div',undefined,'json-tree');target.append(el('h4','该图的实际模型返回'),box);tree(box,call.response?.value,!call.response);return true;
    }
    if(stage.id==='chunking'){
      target.replaceChildren(el('h3','62 个 chunk 的实际边界'));const list=el('div',undefined,'chunk-directory');target.append(list);
      for(const c of data.artifacts.manifest.value.chunks){const call=data.calls.find(x=>x.stage==='reader'&&x.id===c.id);list.append(button(`${c.id.toUpperCase()} · core ${c.core.length} · context ${c.context.length}`,()=>navigate(call?'call/'+call.key:'stage/reader')));}return true;
    }
    if(stage.id==='pdf'&&F.pdf){target.replaceChildren(el('h3','本轮输入的原始专利 PDF'),link('打开原始 PDF',F.pdf.path),el('p',`SHA-256 ${F.pdf.sha256}`,'note'));return true;}
    return false;
  }
  function directory(target,stages,navigate){
    target.replaceChildren();
    for(const [i,stage] of stages.entries()){
      const row=el('article',undefined,'stage-directory-row');const title=button(`${String(i+1).padStart(2,'0')} · ${stage.title}`,()=>navigate('stage/'+stage.id));
      const flow=el('p',`${stage.upstream} → ${stage.downstream}`);const actions=el('div');
      for(const [label,tab] of [['内容','preview'],['输入 / 输出','data'],['Prompt','prompt']])actions.append(button(label,()=>{navigate('stage/'+stage.id);setTimeout(()=>{let b=document.querySelector(`[data-stage-tab="${tab}"]`);if(b?.hidden)b=document.querySelector('[data-stage-tab="data"]');b?.click();},0);}));
      row.append(title,flow,actions);target.append(row);
    }
  }
  window.HandoffDetails={prompt,preview,directory};
})();
