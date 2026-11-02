"""Freeze the sources-only trial without changing older experiment pages."""

import argparse
import html
import json
from pathlib import Path
import shutil


def build(run, output):
    read = lambda name: json.loads((run / name).read_text())
    audit, report = read("transport_audit.json"), read("report.json")
    review = read("review.audit.json") if (run / "review.audit.json").exists() else {}
    output.mkdir(parents=True, exist_ok=True)
    def copy(name):
        source, target = run / name, output / "files" / name
        if not source.exists():
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return "files/" + name
    def link(name, label):
        path = copy(name)
        return f'<a href="{html.escape(path)}" target="_blank" rel="noopener">{html.escape(label)} ↗</a>' if path else ""
    escape = lambda v: html.escape(str(v))
    local = audit["local_totals"]
    results = report["results"]
    table = "".join(f'<tr><td>{label}</td><td>{results[mode]["usage"]["input_tokens"]:,}</td>'
        f'<td>{results[mode]["usage"]["output_tokens"]:,}</td><td>{results[mode]["usage"]["total_tokens"]:,}</td>'
        f'<td>{results[mode]["seconds"]:.1f}秒</td><td>{results[mode]["grounding_rejections"]}</td></tr>'
        for mode, label in [("rows", "原七列格式"), ("grouped", "仅来源分组格式")])
    labels = {"entities": "实体", "links": "序列映射", "sequences": "字面序列定义", "observations": "测量值与来源",
              "experiments": "实验定义", "annotations": "属性标签", "relations": "关系"}
    facts = "".join(f'<tr><td>{labels[k]}</td><td>{v["rows"]}</td><td>{v["grouped"]}</td>'
        f'<td>{v["shared"]}</td><td>{v["rows_only"]}</td><td>{v["grouped_only"]}</td></tr>'
        for k, v in report["comparison"].items())
    per_chunk = read("by_chunk_comparison.json")
    sample = ""
    for cid in report["sample"]:
        changes = {labels[k]: {"原格式独有": v["rows_only_examples"], "分组格式独有": v["grouped_only_examples"]}
                   for k, v in per_chunk[cid].items() if v["rows_only"] or v["grouped_only"]}
        sample += f'<details><summary>{escape(cid)} · 查看逐项差异与真实输入输出</summary><p>'
        sample += " · ".join(link(f'{m}/inputs/{cid}.json', label) for m, label in [("rows", "原格式输入"), ("grouped", "分组输入")])
        sample += '</p><pre>' + escape(json.dumps(changes, ensure_ascii=False, indent=2)) + '</pre></details>'
    downloads = " · ".join(link(n, label) for n, label in [
        ("transport_audit.json", "全部62块可逆核对"), ("report.json", "模型对照报告"),
        ("review.audit.json", "逐项复核与结论"),
        ("rows/completed.json", "原格式全部输出"), ("grouped/completed.json", "分组格式全部输出"),
        ("rows/grounding_rejections.json", "原格式字面检查拒绝"), ("grouped/grounding_rejections.json", "分组格式字面检查拒绝")])
    output.joinpath("index.html").write_text(f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>49-Sructure · 来源包装压缩对照</title><style>
*{{box-sizing:border-box}}body{{margin:0;color:#20364e;background:#f3f6fa;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}}header{{background:#142e4a;color:#fff;padding:32px max(24px,calc((100vw - 1160px)/2))}}header p{{color:#c7d9e9}}h1{{font-size:28px;margin:8px 0}}main{{max-width:1208px;padding:24px;margin:auto}}section{{padding:24px;border:1px solid #d9e4ee;background:white;border-radius:13px;margin-bottom:20px}}h2{{font-size:19px;margin:0 0 10px}}.note{{padding:14px 18px;border:1px solid #ccdcf4;background:#edf4ff;border-radius:10px;margin-bottom:20px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e3eaf2;vertical-align:top}}th{{background:#f6f9fc;color:#637a91}}.scroll{{overflow:auto}}a{{color:#315ee7}}details{{border:1px solid #dce5ef;border-radius:8px;padding:12px;margin-top:12px}}summary{{cursor:pointer}}pre{{background:#f5f8fc;padding:14px;border-radius:8px;max-height:500px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}.muted{{font-size:13px;color:#687d92}}header a{{color:#cfe3ff}}@media(max-width:600px){{main{{padding:14px}}section{{padding:15px}}h1{{font-size:23px}}}}</style></head><body><header><small>SOURCE FORMAT / SAME DATASET / SAME CHUNKS</small><h1>只压缩来源包装，模型还能读出相同信息吗？</h1><p>完整 Dataset1、原始62个分块、OCR文字和来源ID都保持原样。</p><a href="../49-Sructure-followup-20261102/index.html">查看问答优化与完整流程 ↗</a></header><main>
<div class="note">这次只测试来源格式，没有做目录筛选、dataset压缩或调整分块。默认仍使用旧格式。结构还原通过，不等于模型语义能力已经完全等价。</div>
<section><h2>全部输入能还原吗？</h2><p>62 / 62 块通过逐项还原：{audit['unique_sources']:,} 个独立来源、{audit['source_transmissions_checked']:,} 次来源传输，{audit['source_image_style_entries_checked']}条样式复核信息及{audit['failed_or_stale_ocr_entries_checked']}条失败或过期OCR状态原样保留。原文、整数ID、页码、表号、行列、来源顺序、完整Dataset1和上下文范围一致。</p>
<div class="scroll"><table><tr><th>本地 o200k 分词估算</th><th>原格式</th><th>分组格式</th><th>减少</th></tr><tr><td>来源部分</td><td>{local['source_tokens_rows']:,}</td><td>{local['source_tokens_grouped']:,}</td><td>{local['source_tokens_rows']-local['source_tokens_grouped']:,}</td></tr><tr><td>完整显式输入（计入格式说明）</td><td>{local['prompt_tokens_rows']:,}</td><td>{local['prompt_tokens_grouped']:,}</td><td>{local['prompt_tokens_rows']-local['prompt_tokens_grouped']:,}</td></tr></table></div><p class="muted">以上是本地材料分词，不是模型服务端的精确计费。模型实测用量见下一表。</p></section>
<section><h2>同一批6块的真实模型调用</h2><p class="muted">两组均为 gpt-6.1-sol / xhigh，6路并发，先运行原格式再运行分组格式。服务负载和缓存会影响耗时；没有重做OCR、Template、Action或问答。</p><div class="scroll"><table><tr><th>格式</th><th>输入token</th><th>输出token</th><th>合计token</th><th>实际用时</th><th>字面检查拒绝</th></tr>{table}</table></div></section>
<section><h2>具体事实有没有差异？</h2><p class="muted">包含相同初始seed事实。实验ID和措辞变化也可能形成差异，这些数量不直接代表错误率。对照是模型输出，不是人工标准；本轮没有验证完整Dataset2。</p>{''.join('<p>'+escape(n)+'</p>' for n in review.get('human_review_notes',[]))}<div class="scroll"><table><tr><th>类别</th><th>原格式</th><th>分组格式</th><th>共同</th><th>原格式独有</th><th>分组独有</th></tr>{facts}</table></div>{sample}</section>
<section><h2>原始记录</h2><p>{downloads}</p><p class="muted">每种格式的完整输入输出、实际用量、失败和字面检查拒绝分别保存。这个页面不会调用模型或覆盖原专利结果。</p></section></main></body></html>''')
    print(output / "index.html")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    build(args.run.resolve(), args.output.resolve())
