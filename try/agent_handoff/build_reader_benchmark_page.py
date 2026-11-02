"""Freeze actual Reader input ablations and their validation, without calling a model."""

import argparse
import json
from pathlib import Path
import shutil


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def build(root, output):
    summary = read(root / "summary.json")
    if not summary:
        raise ValueError("Missing reviewed summary.json")
    output.mkdir(parents=True, exist_ok=True)
    def copy(source, relative):
        if not source.is_file():
            return None
        dest = output / "files" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return "files/" + relative
    data = {"summary": summary, "screen": {}, "trials": [], "paths": {}}
    for filename in ["summary.json", "local_input_sizes.json", "screen/protocol.json"]:
        data["paths"][filename] = copy(root / filename, filename)
    for mode, label in [("full", "原格式 · 全量 Dataset1"), ("compact_full", "紧凑格式 · 全量 Dataset1"), ("catalogue", "全局目录 + 按名称召回"), ("catalogue_typed", "全局目录 + 按名称 / 指标召回"), ("catalogue_native", "轻目录 + 原生 JSON（无压缩解码）")]:
        phase = {"catalogue_typed": "screen_typed", "catalogue_native": "screen_native"}.get(mode, "screen")
        folder = root / phase / mode
        report = read(folder / "reader_report.json")
        if not report:
            continue
        report = {**report, "label": label, "files": {}}
        for filename in ["reader_report.json", "configuration.json", "grounding_rejections.json", "completed.json", "inputs/c46.json"]:
            report["files"][filename] = copy(folder / filename, f"{phase}/{mode}/{filename}")
        completed = read(folder / "completed.json", {})
        if key := completed.get("c46", {}).get("meta", {}).get("key"):
            report["files"]["prompt"] = copy(folder / "requests" / key / "input.json", f"{phase}/{mode}/c46-request.json")
        data["screen"][mode] = report
    for trial in summary.get("full_trials", []):
        folder = root / trial["relative_dir"]
        report = read(folder / "reader_report.json", {})
        action = read(folder / "action/experiment.json", {})
        row = {**trial, "reader": report, "action": action,
               "audit": read(folder / "comparison.audit.json", {}), "files": {}}
        for filename in ["reader_report.json", "configuration.json", "grounding_rejections.json", "completed.json",
                         "action/experiment.json", "action/dataset.json", "action/facts.json", "action/quality_audit.json",
                         "action/action_agent.input.json", "action/action_agent.response.json", "comparison.audit.json", "reader_definition_audit.json"]:
            row["files"][filename] = copy(folder / filename, trial["relative_dir"] + "/" + filename)
        for attempt in (1, 2):
            filename = f"action/action_agent.validation.{attempt}.json"
            row["files"][filename] = copy(folder / filename, trial["relative_dir"] + "/" + filename)
        data["trials"].append(row)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (output / "index.html").write_text(PAGE.replace("@@DATA@@", encoded))
    print(output / "index.html")


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>49-Sructure · Reader 输入优化对照</title><style>
*{box-sizing:border-box}body{margin:0;color:#193047;background:#f3f6fa;font:15px/1.7 system-ui,-apple-system,"Microsoft YaHei",sans-serif}header{background:#142e4a;color:white;padding:36px max(22px,calc((100vw - 1260px)/2))}header small{color:#afc9e6;letter-spacing:.12em}h1{margin:8px 0;font-size:30px;letter-spacing:-.03em}header p{color:#c6d7e8;margin:8px 0}a{color:#315ee7;text-decoration:none}header a{color:#d2e6ff;margin-right:20px;font-size:13px}.wrap{max-width:1304px;margin:auto;padding:24px}.panel{background:white;border:1px solid #dce5ef;border-radius:14px;padding:22px;margin-bottom:20px;box-shadow:0 5px 25px #18324905}h2{font-size:20px;margin:0 0 8px}h3{font-size:15px}.note{background:#edf3ff;color:#35557b;padding:14px 18px;border:1px solid #d4e0f7;border-radius:10px;margin-bottom:20px}.muted{color:#697d91;font-size:13px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}.card{background:white;border:1px solid #dce5ef;border-radius:12px;padding:16px 20px}.card strong{display:block;font-size:27px;color:#1c3f6a;letter-spacing:-.03em}.card small{color:#687e95}.card em{font-size:12px;font-style:normal;color:#0c7c78}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e6edf5;padding:11px 10px;text-align:left;vertical-align:top}th{background:#f6f9fc;color:#657b91;font-size:12px}tr.best{background:#eff9f6}.tag{display:inline-block;background:#e8f4ef;color:#0c7c78;border-radius:99px;padding:2px 9px;font-size:11px}.tag.pending{background:#fff2df;color:#996122}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.recipe{border:1px solid #dce5ef;border-radius:10px;padding:16px;background:#f8fafd}.recipe b{display:block;margin-bottom:8px}.recipe p{font-size:13px;margin:7px 0}.bar{background:#eaf0f7;border-radius:8px;height:9px;margin:5px 0 15px;overflow:hidden}.bar i{display:block;background:#315ee7;height:100%;border-radius:8px}.bar.green i{background:#17867e}.button{display:inline-block;font-size:12px;border:1px solid #cfdaea;border-radius:7px;padding:6px 10px;margin:6px 7px 0 0}details{border:1px solid #dce5ef;border-radius:9px;padding:12px 14px;margin:12px 0}summary{cursor:pointer;font-size:13px}pre{background:#f4f7fb;border-radius:7px;padding:14px;font:12px/1.7 ui-monospace,monospace;max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}select{padding:9px;border:1px solid #cedaea;border-radius:7px;font:inherit;color:inherit;width:100%;margin:9px 0}.small{font-size:12px}.positive{color:#0c7c78;font-weight:650}.warn{color:#a35d24}footer{text-align:center;color:#778da2;font-size:12px;margin:25px}@media(max-width:850px){.cards{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}header{padding:25px 18px}h1{font-size:25px}.wrap{padding:14px}.panel{padding:17px}}
</style></head><body><header><small>READER INPUT EXPERIMENT / 49-SRUCTURE</small><h1>少传重复信息，是否还能读出同样的关系？</h1><p>固定原始 OCR 和 Dataset1，对照实际模型请求、阶段耗时和保留的信息。</p><nav><a href="../49-Sructure-followup-20261102/index.html">此前问答优化 ↗</a><a href="#screen">输入方案对照 ↓</a><a href="#quality">完整结果核对 ↓</a></nav></header><main class="wrap"><div class="note" id="scope"></div><div class="cards" id="cards"></div><section class="panel"><span class="tag">实测后的建议</span><h2 id="recommendation" style="margin-top:10px"></h2><div id="reasons"></div><p class="muted" id="status"></p></section>
<section class="panel"><h2>本次究竟跑了什么？</h2><div class="recipe"><b>已保存的 PDF OCR / Table OCR ＋ 原始 Template Dataset1</b><p>↓ 只改 Reader 输入表示或分块，不重新 OCR、不引入后来得到的答案。</p><b>并行 Reader：每一块独立提出 actions</b><p>↓ 自适应全量方案中，40 个 Reader 都收到同一份完整业务 Dataset1；普通正文两块合一，序列表和密集序列块保留。</p><b>Action①：完整原始 Dataset1 ＋ 本组全部 Reader actions ＋ 引用证据</b><p>↓ 验证完整性、实体身份、序列证据、测量与实验引用，再导出 Dataset2。本轮两个 40 块方案在这里被拒绝，未进入问答与 Action②。</p></div></section><section class="panel" id="screen"><h2>第一步：同一批 11 个 chunk，比较不同输入方案</h2><p class="muted">同一模型、xhigh 推理和 11 路并发；前三组依次运行，补充组与其他实验存在重叠。服务负载和缓存会影响耗时。样本刻意覆盖新实体、无命中正文、序列表续表、测量和跨块关系，不代表整篇准确率。</p><div class="scroll"><table><thead><tr><th>方案</th><th>输入 token</th><th>输出 token</th><th>总 token</th><th>相对全量节省</th><th>实际用时</th><th>请求状态</th></tr></thead><tbody id="screen-rows"></tbody></table></div><div class="grid" style="margin-top:18px"><div class="recipe"><b>紧凑格式：不筛掉任何业务记录</b><p>反复出现的字段名改成一次声明的列名，长的重复标识符使用可逆字符串字典。</p><p>来源按页面 / 表格分组，保留原始整数 ID、行列和全部文字；页码等元数据只声明一次。</p><p>程序解码后逐项核对，完整 seed 与原文必须相同。</p></div><div class="recipe"><b>目录召回：另行评估遗漏风险</b><p>保留全部实体名称、全局关系和实验目录；按名称、SEQ ID、来源地址召回相关完整记录；进一步测试按本段明确指标选择测量明细。</p><p>程序只找候选，不判断归属。所有正文仍然阅读，无命中段落也保留。</p><p>这组减少了详细背景，不能把更低的 token 当作同等质量。</p></div></div></section>
<section class="panel"><h2>第二步：完整覆盖全部原始来源</h2><p class="muted">Reader 用量和 Action 整合用量分别记录。历史完整运行与本次运行的服务负载不同，用时比较是观测结果，不是固定速度承诺。</p><div class="scroll"><table><thead><tr><th>方案</th><th>Chunk 数</th><th>Reader token</th><th>Reader 用时</th><th>Action token</th><th>Action 用时</th><th>状态</th></tr></thead><tbody id="full-rows"></tbody></table></div><div id="full-notes"></div></section>
<section class="panel" id="quality"><h2>结果有没有丢？</h2><p class="muted">比较具体域、序列字符、测量值 / 单位 / 来源和实体，不能只比较总条数。旧模型结果是对照，不是人工标准答案；字面核对也不是 PDF 像素的独立验收。</p><div class="scroll"><table><thead><tr><th>方案</th><th>受支持域</th><th>完整配对</th><th>正式测量</th><th>旧域丢失 / 残基变化</th><th>旧测量丢失</th><th>旧实体 / 序列定义丢失</th><th>字面来源错误</th></tr></thead><tbody id="quality-rows"></tbody></table></div><div id="quality-notes"></div></section>
<section class="panel"><h2>样本中的事实差异</h2><p class="muted">下表是与本轮原格式全量输入的字面事实差异，不是错误数量。不同实验命名、指标措辞和证据组织也会形成差异，需要结合完整整合结果判断。</p><select id="mode-select" aria-label="选择输入方案"></select><div id="diff"></div><div id="sample-files"></div></section>
<section class="panel"><h2>为得到结论，实验本身花了多少？</h2><p id="cost"></p><p class="muted">所有实验方案、控制组、失败和重试均单独计费统计；这些费用不是优化后处理一篇专利的费用。没有新做 PDF OCR / Table OCR。</p><div id="downloads"></div></section><footer>本地冻结快照 · 只展示已保存的实际请求与结果，不会触发模型调用</footer></main><script id="data" type="application/json">@@DATA@@</script><script>
const D=JSON.parse(document.getElementById('data').textContent),S=D.summary,$=x=>document.getElementById(x),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),tokens=n=>n==null?'未记录':(n/1000).toLocaleString('zh-CN',{maximumFractionDigits:2,minimumFractionDigits:2})+'K',duration=n=>n==null?'未记录':Math.floor(Math.round(n)/60)+'分'+(Math.round(n)%60)+'秒',link=(p,l)=>p?`<a class="button" href="${esc(p)}" target="_blank" rel="noopener">${esc(l)} ↗</a>`:'';
$('scope').textContent=S.scope;$('recommendation').textContent=S.recommendation.title;$('reasons').innerHTML=S.recommendation.reasons.map(x=>`<p>${esc(x)}</p>`).join('');$('status').textContent=S.recommendation.deployment_status;
$('cards').innerHTML=(S.headline||[]).map(x=>`<div class="card"><small>${esc(x.label)}</small><strong>${esc(x.value)}</strong><em>${esc(x.note)}</em></div>`).join('');
const full=D.screen.full?.usage?.total_tokens;
$('screen-rows').innerHTML=Object.entries(D.screen).map(([k,r])=>`<tr><td><strong>${esc(r.label)}</strong></td><td>${tokens(r.usage?.input_tokens)}</td><td>${tokens(r.usage?.output_tokens)}</td><td>${tokens(r.usage?.total_tokens)}</td><td>${full&&r.usage?((1-r.usage.total_tokens/full)*100).toFixed(1)+'%':'—'}</td><td>${duration(r.seconds)}</td><td>${esc(r.status)} · ${r.usage?.failed_requests??'—'} 失败 / ${r.usage?.unknown_requests??'—'} 用量未知</td></tr>`).join('');
const H=S.historical;
$('full-rows').innerHTML=`<tr><td>历史全量对照</td><td>${H.chunks}</td><td>${tokens(H.reader_tokens)}</td><td>${duration(H.reader_seconds)}</td><td>${tokens(H.action_tokens)}</td><td>${duration(H.action_seconds)}</td><td>已完成 · 含 Action 重试</td></tr>`+D.trials.map(t=>`<tr class="${t.best?'best':''}"><td>${esc(t.label)}</td><td>${t.reader.configuration?.chunks.length??'—'}</td><td>${tokens(t.reader.usage?.total_tokens)}</td><td>${duration(t.reader.seconds)}</td><td>${tokens(t.action.usage?.total_tokens)}</td><td>${duration(t.action.seconds)}</td><td>${esc(t.action.status==='complete'?'整合通过 · 尚有实体差异':t.action.status==='failed'?'整合未通过':t.action.status||t.reader.status||'未完成')}</td></tr>`).join('');
$('full-notes').innerHTML=(S.full_notes||[]).map(x=>`<p class="small muted">${esc(x)}</p>`).join('');
$('quality-rows').innerHTML=`<tr><td>历史 Action①</td><td>${H.domains}</td><td>${H.pairs}</td><td>${H.measurements}</td><td>—</td><td>—</td><td>—</td><td>0</td></tr>`+D.trials.map(t=>{const a=t.action,m=a.metrics||{},c=t.audit||{},q=a.quality||{};return `<tr><td>${esc(t.label)}</td><td>${m.domains??'未完成'}</td><td>${m.pairs??'—'}</td><td>${m.measurements??'—'}</td><td>${c.lost?.length??'—'} / ${c.changed_residues?.length??'—'}</td><td>${c.measurements_lost??'—'}</td><td>${c.unmatched_historical_entities?.length??'—'} / ${c.definitions?.old_only_count??'—'}</td><td>${q.literal_errors?.length??'—'}</td></tr>`}).join('');
$('quality-notes').innerHTML=(S.quality_notes||[]).map(x=>`<p>${esc(x)}</p>`).join('');
$('mode-select').innerHTML=Object.entries(D.screen).map(([k,r])=>`<option value="${k}">${esc(r.label)}</option>`).join('');
function show(mode){const r=D.screen[mode],v=r.versus_fresh_full_facts||{};$('diff').innerHTML=`<div class="scroll"><table><thead><tr><th>事实类型</th><th>全量对照</th><th>本方案</th><th>共同</th><th>仅全量有</th><th>仅本方案有</th></tr></thead><tbody>${Object.entries(v).map(([k,x])=>`<tr><td>${esc({entities:'实体',links:'序列映射',sequences:'字面序列定义',observations:'测量（不含实验ID差异）',annotations:'属性',relations:'实体 / 实验关系',experiments:'实验定义'}[k]||k)}</td><td>${x.reference}</td><td>${x.candidate}</td><td>${x.shared}</td><td>${x.reference_only}</td><td>${x.candidate_only}</td></tr>`).join('')}</tbody></table></div><details><summary>完整差异样例和对照范围</summary><pre>${esc(JSON.stringify(v,null,2))}</pre></details>`;$('sample-files').innerHTML=link(r.files['inputs/c46.json'],'同一序列表 chunk 的真实输入')+link(r.files['completed.json'],'全部样本实际输出')+link(r.files['reader_report.json'],'详细样本报告')+link(r.files.prompt,'真实 Prompt、输入和输出 schema');}
$('mode-select').onchange=e=>show(e.target.value);if(D.screen.compact_full){$('mode-select').value='compact_full';show('compact_full');}else if(D.screen.full)show('full');
$('cost').textContent=`本次全部实验已知模型用量（未知请求不按零计）：${tokens(S.experiment_cost?.total_tokens)}；实际请求 ${S.experiment_cost?.requests??'—'} 次，失败 ${S.experiment_cost?.failed_requests??'—'} 次，用量未知 ${S.experiment_cost?.unknown_requests??'—'} 次。`;
$('downloads').innerHTML=link(D.paths['summary.json'],'结论与全部实验用量')+D.trials.map(t=>link(t.files['action/dataset.json'],t.label+'完整 dataset')+link(t.files['action/quality_audit.json'],'来源核对')+link(t.files['comparison.audit.json'],'与历史逐项对照')+link(t.files['reader_definition_audit.json'],'Reader 序列字面核对')+link(t.files['action/action_agent.validation.2.json'],'第二次整合校验')).join('');
</script></body></html>'''


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    build(args.root.resolve(), args.output.resolve())
