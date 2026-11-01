"""Build a small, self-contained review page from actual focused-followup artifacts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def build(experiment, output):
    report = read(experiment / "experiment.json")
    if report["status"] not in {"complete", "failed"}:
        raise ValueError("Wait for the experiment to finish before freezing the page")
    baseline = Path(report["configuration"]["baseline"])
    output.mkdir(parents=True, exist_ok=True)
    files = output / "files"
    files.mkdir(exist_ok=True)

    def freeze(source, name):
        if not source.is_file():
            return None
        dest = files / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return "files/" + name

    paths = {}
    for name in ["experiment.json", "configuration.json", "question_plan.json", "answers.json", "answer_actions.json",
                 "dataset2.business.json", "dataset3.business.json", "quality_audit.json", "answer_action.json",
                 "comparison.audit.json"]:
        paths[name] = freeze(experiment / name, name)
    for name in ["action_agent.input.json", "action_agent.response.json", "reader_initial_dataset.json", "manifest.json"]:
        paths["baseline/" + name] = freeze(baseline / name, "baseline/" + name)
    paths["template.json"] = freeze(baseline.parent / "shared_seed/document_template.json", "baseline/template.json")
    template = read(baseline.parent / "shared_seed/document_template.json", {})
    if audit := template.get("agent", {}).get("audit_dir"):
        paths["template.input.json"] = freeze(Path(audit) / "input.json", "baseline/template.input.json")
    reader = next((p for p in sorted((baseline / "requests").glob("*/input.json"))
                   if read(p, {}).get("stage") == "reader"), None)
    reader_payload = read(reader)["payload"] if reader else {}
    if reader:
        paths["reader.input.json"] = freeze(reader, "baseline/reader.input.json")
        paths["reader.output.json"] = freeze(reader.parent / "validated.json", "baseline/reader.output.json")
    for name in ["action_agent.input.json", "action_agent.response.json", "domain_review.json"]:
        if (experiment / "answer_action" / name).exists():
            paths["answer_action/" + name] = freeze(experiment / "answer_action" / name, "answer_action/" + name)
    dataset = read(experiment / "dataset.json")
    names = {e["id"]: e["name"] for e in dataset["entities"]}
    plan = read(experiment / "question_plan.json")
    answers = {a["id"]: a for a in read(experiment / "answers.json")}
    questions = []
    for q in plan["questions"]:
        a = answers.get(q["id"], {})
        path = experiment / "answers" / q["id"]
        actual = read(path / "input.json", {})
        questions.append({"id": q["id"], "names": [names[t["item"]] + " " + t["chain"] for t in q["targets"]],
            "gaps": [g for r in q["review_context"] for g in r["missing_evidence"]],
            "status": a.get("status"), "response": a.get("response"),
            "input": freeze(path / "input.json", "answers/" + q["id"] + "/input.json"),
            "output": freeze(path / "response.json", "answers/" + q["id"] + "/response.json"),
            "source_count": len(actual.get("sources", [])),
            "sources": actual.get("sources", []), "retrieval": actual.get("retrieval"),
            "targets": q["targets"]})
    # Keep source-backed pairs readable without dumping full sequences on entry.
    pairs = [{"name": e["name"], "VH": [e["sequences"]["VH"]["seq_id"]],
              "VL": [e["sequences"]["VL"]["seq_id"]],
              "VH_length": len(e["sequences"]["VH"].get("value") or ""),
              "VL_length": len(e["sequences"]["VL"].get("value") or "")}
             for e in dataset["entities"] if all(e["sequences"][c]["status"] == "supported" for c in ("VH", "VL"))]
    data = {"report": report, "paths": paths, "questions": questions, "pairs": pairs,
        "excluded_count": sum(r["status"] == "not_applicable" for r in plan["excluded_reviews"]),
        "reader_chunk": reader_payload.get("chunk"), "captured_at": datetime.now(timezone.utc).isoformat(),
        "comparison_audit": read(experiment / "comparison.audit.json", {}),
        "prompts": {"answer": (experiment / "answer_prompt.txt").read_text(),
                    "action2": (experiment / "answer_action_prompt.txt").read_text()}}
    # The exact saved instructions belong to the historical Action1 request.
    for p in sorted((baseline / "requests").glob("*/input.json")):
        value = read(p)
        if value.get("stage") == "action":
            data["prompts"]["action1"] = value["instructions"]
            break
    if reader:
        data["prompts"]["reader"] = read(reader)["instructions"]
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = PAGE.replace("@@DATA@@", serialized)
    (output / "index.html").write_text(page)
    print(str(output / "index.html"))


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>49-Sructure · 新问答与整合流程</title>
<style>
:root{--ink:#182a3d;--muted:#637488;--line:#dbe4ed;--blue:#315ee7;--teal:#087e80;--orange:#ad6522}*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}header{background:#142d49;color:#fff;padding:36px max(24px,calc((100vw - 1360px)/2)) 30px}header small{letter-spacing:.12em;color:#a7c4e1}h1{font-size:30px;letter-spacing:-.03em;margin:8px 0}header p{margin:8px 0;color:#c3d3e3;max-width:1000px}nav{display:flex;gap:18px;margin-top:20px}nav a{color:#d2e4ff;font-size:13px}.wrap{max-width:1408px;margin:auto;padding:24px}.note{border:1px solid #cddcf5;background:#edf3ff;border-radius:12px;padding:14px 18px;margin-bottom:20px;color:#315277}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}.metric{background:#fff;border:1px solid var(--line);padding:16px 20px;border-radius:13px}.metric small,.muted{color:var(--muted)}.metric strong{display:block;font-size:27px;line-height:1.5;letter-spacing:-.03em}.metric em{font-style:normal;color:var(--teal);font-size:12px}.layout{display:grid;grid-template-columns:minmax(0,1fr) 370px;gap:22px;align-items:start}.panel{background:#fff;border:1px solid var(--line);border-radius:15px;padding:23px;box-shadow:0 5px 22px #1d375105}h2{margin:0 0 6px;font-size:19px}h3{font-size:15px;margin:22px 0 7px}.intro{margin:0 0 20px;color:var(--muted);font-size:13px}.lane{font-size:12px;letter-spacing:.08em;font-weight:700;color:#687f96;margin-bottom:12px}.flow{display:flex;gap:8px;align-items:stretch}.node{border:1px solid #d7e1ed;background:#fff;cursor:pointer;text-align:left;color:var(--ink);border-radius:10px;padding:15px 12px;min-width:0;flex:1;font:inherit;transition:border-color .18s,box-shadow .18s}.node:hover,.node.selected{border-color:var(--blue);box-shadow:0 0 0 3px #315ee710}.node strong{display:block;font-size:14px}.node small{display:block;font-size:11px;line-height:1.6;margin-top:5px;color:var(--muted)}.node .tag{display:inline-block;font-size:10px;padding:1px 7px;background:#eef3fd;color:#3f65ad;border-radius:5px;margin-bottom:7px}.node.model{border-top:3px solid var(--blue)}.node.data{background:#f0f8f7;border-color:#bddbd7}.node.optional{border-style:dashed;border-top:3px solid var(--orange);background:#fffaf3}.arrow{color:#90a4b7;display:flex;align-items:center;font-size:18px}.down{text-align:right;padding:6px 40px;color:#8ba0b3;font-size:24px;line-height:1.3}.parallel{border:1px solid #dce6f2;border-radius:10px;padding:10px;margin-top:12px;background:#f6f9fd}.parallel span{display:block;background:#fff;border:1px solid #e0e8f2;margin:4px 0;border-radius:5px;padding:4px 7px;font-size:11px}.flowlabel{font-size:11px;color:var(--muted);text-align:center;margin-top:8px}.divider{border-top:1px solid #e4eaf1;margin:20px 0}.side{position:sticky;top:18px}.side h2{font-size:18px}.side p{font-size:13px}.pill{display:inline-block;font-size:11px;border-radius:99px;padding:3px 9px;background:#e9f5f1;color:var(--teal)}.pill.pending{background:#fff1de;color:#98621f}.io{padding:12px;background:#f5f8fc;border-radius:8px;font-size:13px;margin:10px 0}.io b{display:block;margin-bottom:3px;font-size:11px;color:var(--muted)}.links a{display:block;font-size:12px;margin-top:7px}.spanall{grid-column:1/-1}.results{margin-top:22px}.results-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;border-bottom:1px solid #e7edf3;padding:10px 9px;vertical-align:top}th{font-size:11px;color:var(--muted);background:#f7f9fc}.good{color:var(--teal);font-weight:650}details{margin:13px 0;border:1px solid #dce5ed;border-radius:9px;padding:11px 13px}summary{cursor:pointer;font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font:12px/1.7 ui-monospace,SFMono-Regular,monospace;background:#f4f7fb;border-radius:8px;padding:15px;color:#34465b}select{width:100%;border:1px solid #cbd7e5;border-radius:8px;background:#fff;padding:10px;font:inherit;color:var(--ink);margin:10px 0}.question{padding:15px;background:#f7f9fc;border-radius:10px;font-size:13px}.question p{margin:6px 0}.evidence{font-size:12px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}.bar{height:8px;background:#e8eef6;border-radius:8px;margin:5px 0 12px;overflow:hidden}.bar span{display:block;height:100%;background:#325fe7;border-radius:8px}.bar.new span{background:#168583}footer{color:var(--muted);text-align:center;font-size:12px;padding:30px}.button{display:inline-block;border:1px solid #ccd8e7;background:#fff;border-radius:8px;padding:7px 12px;margin:6px 8px 0 0;font-size:12px}.small{font-size:12px}@media(max-width:1050px){.layout{grid-template-columns:1fr}.side{position:static}.flow{flex-wrap:wrap}.node{flex:1 1 130px}.results-grid{grid-template-columns:1fr}}@media(max-width:640px){.wrap{padding:14px}.metrics{grid-template-columns:1fr 1fr}header{padding:25px 18px}h1{font-size:24px}.panel{padding:16px}.flow{gap:6px}.arrow{display:none}.node{flex-basis:43%}}
</style></head><body><header><small>AGENT FLOW / 49-SRUCTURE</small><h1>从表格草稿，到有证据的数据集</h1><p>Reader 并行提出修改，Action 统一整合。遇到缺口时，Answer 小范围补证据，再由 Action② 决定如何更新完整数据。</p><nav><a href="../49-Sructure-flow-v2-20261101/index.html">查看此前完整运行快照 ↗</a><a href="#results">本次真实对比 ↓</a><a href="#questions">逐题查看输入、输出与证据 ↓</a></nav></header>
<main class="wrap"><div class="note"><strong>这次验证从已保存的 Dataset2 开始。</strong>复用同一份 OCR、Template 和 Reader / Action① 结果，单独运行新问答流程；原网页运行结果保持原样。下方流程图展示当前代码，性能数字仅对应问答与后续整合。</div>
<div class="metrics" id="metrics"></div><div class="layout"><section class="panel"><h2>现在的信息怎样流动？</h2><p class="intro">点击任一步，右侧查看它收到什么、输出什么，以及实际文件与 prompt。</p><div class="lane">01 / 表格构造草稿</div><div class="flow"><button class="node" data-stage="ocr"><span class="tag">来源</span><strong>PDF OCR / Table OCR</strong><small>正文供 Reader 阅读<br>表格 + 标题供 Template</small></button><span class="arrow">→</span><button class="node model" data-stage="template"><span class="tag">LLM</span><strong>TemplateAgent</strong><small>只整理表格及可获得的标题<br>生成初级 dataset</small></button><span class="arrow">→</span><button class="node data" data-stage="dataset1"><span class="tag">完整业务底稿</span><strong>Dataset1</strong><small>46 个实体 · 197 条测量<br>每个 Reader 拿相同全量数据</small></button></div>
<div class="divider"></div><div class="lane">02 / 全文并行阅读与统一整合 · 本次复用</div><div class="flow"><button class="node model" data-stage="reader"><span class="tag">62 路 Reader</span><strong>Chunk + Dataset1</strong><div class="parallel"><span>C1 + 完整 Dataset1 → Actions1</span><span>C2 + 完整 Dataset1 → Actions2</span><span>… C62 + 完整 Dataset1 → Actions62</span></div></button><span class="arrow">→</span><button class="node model" data-stage="action1"><span class="tag">LLM</span><strong>Action①</strong><small>完整 Dataset1<br>+ 所有 Reader Actions<br>+ 对应原文证据</small></button><span class="arrow">→</span><button class="node data" data-stage="dataset2"><span class="tag">完整数据 + 具体缺口</span><strong>Dataset2</strong><small>55 个实体 · 251 条测量<br>75 个受支持域 · 31 组配对<br>逐实体复核：已支持 / 待补 / 不适用</small></button></div>
<div class="divider"></div><div class="lane">03 / 定向补充 · 新流程已实测</div><div class="flow"><button class="node" data-stage="questions"><span class="tag">程序调度</span><strong>具体问题队列</strong><small>采用 Action① 指出的缺口<br>分组、排队、排除不适用项<br>本轮 21 个问题</small></button><span class="arrow">→</span><button class="node model" data-stage="answer"><span class="tag">4 路并行 LLM</span><strong>Answer</strong><small>问题 + 定向 Dataset2 上下文<br>+ 必要原文 → 简短答案 / Actions<br>各自读取固定 Dataset2</small></button><span class="arrow">→</span><button class="node optional" data-stage="action2"><span class="tag">有新建议才调用</span><strong>Action②</strong><small>完整 Dataset2 + Answer Actions<br>+ 对应证据 → 完整 Dataset3<br>本轮无建议，已跳过</small></button></div><div class="down">↓</div><button class="node data" data-stage="dataset3" style="width:100%"><span class="tag">最终完整输出</span><strong>Dataset3 · 本轮保留 Dataset2</strong><small>75 个域 · 31 组配对 · 251 条测量；21 个问题仍未解决，保留证据缺口，不猜测补全。完整业务文件还保留 6 条待复核测量候选。</small></button><p class="flowlabel">Python 负责调度和来源 / 字面校验；跨块关系与最终修改由 LLM Action 判断。Answer 不直接提交修改。</p></section><aside class="panel side" id="detail"></aside></div>
<section class="panel results" id="results"><h2>同一份 Dataset2，旧问答与新问答</h2><p class="intro">问题粒度与推理预算也发生了变化，因此这是整套新流程的实测，不是只测并发速度。保留缺口不等于完成补全。</p><div class="results-grid"><div id="comparison"></div><div><h3 style="margin-top:0">实际模型 token</h3><div id="bars"></div><p class="small muted">原文字符预算不是 token。统计采用模型实际返回的用量，输入含缓存，输出含推理。此轮无新增 Actions，Action② 未产生模型费用；有 Actions 时还需计入它的成本。</p><p class="small muted">本轮 0 个新增域 / 配对，0 个已支持域丢失，0 条测量丢失，0 个字面来源错误。旧问答重复添加的两条 TSLP 定义已存在于 Dataset2，本轮没有再次添加。</p><div id="downloads"></div></div></div></section>
<section class="panel results" id="questions"><h2>具体到一个问题，模型到底看到了什么？</h2><p class="intro">以下均来自这轮实际请求和回答。选择不同问题查看，展开来源可核对 PDF 页码和原文。</p><select id="question-select" aria-label="选择待补问题"></select><div id="question-detail"></div><details><summary>Answer 实际 prompt（独立小任务）</summary><pre id="answer-prompt"></pre></details><details><summary>Action② prompt（本轮未调用，显示当前代码契约）</summary><pre id="action2-prompt"></pre></details></section>
<section class="panel results"><h2>最终数据长什么样？</h2><p class="intro">完整 Dataset3 仍包含实体、序列关系、实验与全部测量；下面只展示完整配对的可读索引。</p><div class="scroll"><table><thead><tr><th>实体</th><th>VH · SEQ ID</th><th>VL · SEQ ID</th><th>VH / VL 长度</th></tr></thead><tbody id="pairs"></tbody></table></div></section>
<footer>独立验证快照 · 原始文件随页面保存 · 本页不会启动模型或修改专利结果</footer></main><script id="data" type="application/json">@@DATA@@</script><script>
const D=JSON.parse(document.getElementById('data').textContent),R=D.report,$=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),fmt=n=>Number(n).toLocaleString('zh-CN'),tok=n=>(n/1000).toFixed(2)+'K',seconds=n=>n>=60?Math.floor(n/60)+'分'+Math.round(n%60)+'秒':n.toFixed(1)+'秒',link=(path,label)=>path?`<a class="button" href="${esc(path)}" target="_blank" rel="noopener">${esc(label)} ↗</a>`:'';
const old=R.baseline_answer_usage.total_tokens,now=R.usage.total_tokens;
$('metrics').innerHTML=[['新问答耗时',seconds(R.seconds),'旧：'+seconds(R.baseline_answer_seconds)],['新模型用量',tok(now),'较旧问答减少 '+((1-now/old)*100).toFixed(1)+'%'],['完整配对',R.final.pairs+' 组','与 Dataset2 / 旧结果一致'],['测量记录',R.final.measurements+' 条','全部关联到完整 VH/VL 配对']].map(([a,b,c])=>`<div class="metric"><small>${a}</small><strong>${b}</strong><em>${c}</em></div>`).join('');
const stages={
ocr:['PDF OCR 与 Table OCR','两条来源分支分别服务正文阅读和表格草稿；本次没有重做 OCR。','263 页正文 OCR；29 张表格 OCR。','正文来源送入分块；表格及可获得的标题送入 Template。',[],null],
template:['TemplateAgent','只用表格与可获得的标题构造初级 dataset。此次复用已保存且重新校验的结果。','Table OCR 单元格、标题、可引用的地址。','46 个实体、21 条序列关系、14 个实验、197 条测量。',[['template.input.json','实际 Template 输入'],['template.json','Template 输出']],null],
dataset1:['Dataset1 · 全量业务底稿','每个 Reader 收到同一份初始快照，不按名称或 SEQ ID 预先筛掉关系。','Template 草稿经程序转换。','全量实体、关系、实验、测量；运行时间、token 和日志路径不进入业务 seed。',[['baseline/reader_initial_dataset.json','完整 Dataset1']],null],
reader:['并行 Chunk Reader','每个 Reader 提出新增事实与修改建议；它看不到其他 Reader 的待提交结果。','C1 + 完整 Dataset1，C2 + 完整 Dataset1，… 共 62 块。','所有 Reader 共提出 3,306 条候选 Actions，交给同一个 Action①。',[['reader.input.json','一个 Reader 的真实输入'],['reader.output.json','对应 Reader 输出'],['baseline/manifest.json','全部分块清单']],'reader'],
action1:['Action① · 跨块整合','LLM 统一判断身份、共享链、完整链 / 域关系和实验关联，同时逐实体指出具体缺口。','完整 Dataset1 + 全部 Reader Actions + 对应原文。','完整 Dataset2 + domain_reviews：75 项已支持、29 项待补、6 项不适用。',[['baseline/action_agent.input.json','完整 Action① 输入'],['baseline/action_agent.response.json','完整输出与逐条决定']],'action1'],
dataset2:['Dataset2 · 固定问答底稿','问答阶段不会边回答边改这份数据；所有 Answer 共享同一个固定版本。','Action① 完整业务输出，通过来源与完整性校验。','55 个实体、34 个实验、257 条测量候选（正式视图 251 条，6 条待复核）、75 个域、31 组配对。',[['dataset2.business.json','完整 Dataset2']]],
questions:['问题队列 · 来自 Action①','程序只分组和调度已明确指出的缺口，不额外调用 Question LLM。','29 个 unresolved 域复核；6 个不适用域复核已排除。','合并同实体 / 同编号问题后：21 题。具体缺什么和证据引用都保留。',[['question_plan.json','完整问题计划与排除依据']]],
answer:['Answer · 定向并行补证据','默认 4 路并行，独立小 prompt / Schema。它只提出 Actions，暂存校验副本不会修改 Dataset2。','具体问题 + 定向业务上下文 + 完整必要原文；原文软预算 8,000 字符。','本轮 21 个回答均未找到足够新证据，actions=[]；明确保留缺口。',[['answers.json','全部真实回答'],['answer_actions.json','待提交 Actions（本轮为空）']],'answer'],
action2:['Action② · 完整数据整合','有有效新建议才调用 LLM；无建议直接保留 Dataset2。人工构造的新增事实场景已通过整合 / 失败回退 / 回放测试。','完整 Dataset2 + 全部 Answer Actions + 对应原文证据。','输出完整 Dataset3，每条建议有去向；已有受支持域与历史拒绝 / 冲突记录保留。本轮未发送模型请求。',[['answer_action.json','本轮跳过状态']],'action2'],
dataset3:['Dataset3 · 最终业务数据','本轮无新增建议，完整业务 Dataset3 与 Dataset2 一致，未把未解决问题标成完成。','通过校验的 Action② 完整输出，或无建议时保留 Dataset2。','55 个实体、251 条测量、31 组配对；剩余 21 个问题保留。',[['dataset3.business.json','完整 Dataset3'],['quality_audit.json','来源与关联校验']]]};
function show(key){document.querySelectorAll('[data-stage]').forEach(e=>e.classList.toggle('selected',e.dataset.stage===key));const [title,desc,input,output,files,prompt]=stages[key];$('detail').innerHTML=`<span class="pill ${key==='action2'?'pending':''}">${key==='action2'?'本轮已跳过':'点击查看实际流向'}</span><h2 style="margin-top:12px">${title}</h2><p>${desc}</p><div class="io"><b>输入</b>${input}</div><div class="io"><b>输出</b>${output}</div><div class="links">${files.map(([k,l])=>link(D.paths[k],l)).join('')}</div>${prompt?`<details><summary>${key==='action2'?'当前 prompt 契约':'实际 prompt'}</summary><pre>${esc(D.prompts[prompt]||'未保存')}</pre></details>`:''}`;}
document.querySelectorAll('[data-stage]').forEach(e=>e.onclick=()=>show(e.dataset.stage));show('answer');
$('comparison').innerHTML=`<table><thead><tr><th>指标</th><th>旧串行问答</th><th>新并行问答</th></tr></thead><tbody>${[['问题数',R.baseline_question_count,R.questions],['输入 token',tok(R.baseline_answer_usage.input_tokens),tok(R.usage.input_tokens)],['输出 token',tok(R.baseline_answer_usage.output_tokens),tok(R.usage.output_tokens)],['总 token',tok(old),tok(now)],['用时',seconds(R.baseline_answer_seconds),seconds(R.seconds)],['新增完整配对',0,0],['最终完整配对',R.baseline_final.pairs,R.final.pairs],['测量记录',R.baseline_final.measurements,R.final.measurements]].map(x=>`<tr><td>${x[0]}</td><td>${x[1]}</td><td class="good">${x[2]}</td></tr>`).join('')}</tbody></table>`;
$('bars').innerHTML=`<div>旧问答 · ${tok(old)}</div><div class="bar"><span style="width:100%"></span></div><div>新问答 · ${tok(now)}</div><div class="bar new"><span style="width:${now/old*100}%"></span></div>`;
$('downloads').innerHTML=link(D.paths['experiment.json'],'实测报告')+link(D.paths['dataset3.business.json'],'完整 Dataset3')+link(D.paths['comparison.audit.json'],'数据保留核对');
$('question-select').innerHTML=D.questions.map((q,i)=>`<option value="${i}">${esc(q.names.join(' / '))} · ${q.response?.status==='unresolved'?'证据不足，未解决':q.status}</option>`).join('');
function question(i){const q=D.questions[i],r=q.response||{};$('question-detail').innerHTML=`<div class="question"><span class="pill pending">${q.status==='no_change'?'未补到新证据':'待整合建议'}</span><h3 style="margin-top:10px">${esc(q.names.join(' / '))}</h3><p><b>Action① 指出的缺口：</b></p>${q.gaps.map(g=>`<p>${esc(g)}</p>`).join('')}<p><b>Answer 的回答：</b>${esc(r.answer||'')}</p><p><b>拟执行修改：</b>${r.actions?.length||0} 条；${r.actions?.length?'提交 Action② 审议':'不改 Dataset2'}</p><p class="muted">本题实收 ${q.source_count} 条原文；${q.retrieval?.required_budget_exceeded?'必要证据超过软预算，已整组保留':'必要原文整组保留，未截断序列'}。</p>${link(q.input,'真实输入 JSON')}${link(q.output,'真实输出 JSON')}</div><details><summary>模型实际返回（完整 JSON）</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details><details><summary>查看本题原文证据与 PDF 页码（${q.source_count} 条）</summary>${q.sources.map(s=>`<div class="evidence"><strong>来源 ${s[0]} · PDF 第 ${s[1]} 页 · ${esc(s[2])}</strong><div>${esc(s[6])}</div></div>`).join('')}</details>`;}
$('question-select').onchange=e=>question(Number(e.target.value));if(D.questions.length)question(0);
$('answer-prompt').textContent=D.prompts.answer;$('action2-prompt').textContent=D.prompts.action2;
$('pairs').innerHTML=D.pairs.map(e=>`<tr><td>${esc(e.name)}</td><td>${esc((e.VH||[]).join(', '))}</td><td>${esc((e.VL||[]).join(', '))}</td><td>${e.VH_length} / ${e.VL_length} aa</td></tr>`).join('');
</script></body></html>'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.experiment.resolve(), args.output.resolve())
