"""Build a local Reader guide from saved OCR and template facts; no model calls."""

import argparse
import copy
import hashlib
from html import escape
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dataagent2.contracts import INSTRUCTIONS, SCHEMA, empty_response
from dataagent2.inputs import load_input, make_chunks
from dataagent2.ledger import Ledger
from dataagent2.pipeline import READER_INPUT_REVISION, initial_dataset, reader_payload
from dataagent2.provider import digest, write_json
from dataagent2.action_agent import ACTION_INSTRUCTIONS, SCHEMA_ACTION, build_action_payload, validate_action_result


def build(seed_dir, full_ocr_dir, snapshot):
    seed_dir, full_ocr_dir, snapshot = map(Path, (seed_dir, full_ocr_dir, snapshot))
    prepared = json.loads((seed_dir / "sda_input.json").read_text())
    full = json.loads((full_ocr_dir / "sda_input.json").read_text())
    if not prepared.get("pdf_sha256") or prepared["pdf_sha256"] != full.get("pdf_sha256"):
        raise ValueError("Table and full-page OCR must belong to the same PDF")
    if prepared["inputs"]["pdf_ocr_sha256"] != full["inputs"]["pdf_ocr_sha256"]:
        raise ValueError("Prepared inputs must reference the same page OCR")
    # The title ablation intentionally stripped body blocks. Restore only the
    # original page blocks, keeping its validated tables and Template output.
    prepared["document_blocks"] = full["document_blocks"]
    with tempfile.TemporaryDirectory() as temporary:
        native = Path(temporary)
        write_json(native / "sda_input.json", prepared)
        for name in ("document_template.json", "assay_measurements.json"):
            (native / name).write_bytes((seed_dir / name).read_bytes())
        data = load_input(native)

    folder = snapshot / "reader-guide-data"
    folder.mkdir(parents=True, exist_ok=True)
    dataset = initial_dataset(data)
    chunks = make_chunks(data)
    write_json(folder / "initial_dataset.json", dataset)
    write_json(folder / "output-schema.json", SCHEMA)
    (folder / "prompt.txt").write_text(INSTRUCTIONS)

    plan = []
    for chunk in chunks:
        pages = sorted({data["sources"][sid]["page"] for sid in chunk["core"]})
        plan.append({**chunk, "pages": pages, "core_count": len(chunk["core"]),
                     "context_count": len(chunk["context"])})
    write_json(folder / "chunk-plan.json", plan)
    table_id = "p0196-r0001"
    target = next(c for c in chunks if any(data["sources"][sid]["table_id"] == table_id
                  and data["sources"][sid]["row"] == 3 for sid in c["core"]))
    for chunk in [chunks[0], target]:
        write_json(folder / f'{chunk["id"]}-input.json', reader_payload(data, chunk))

    ids = target["core"] + target["context"]
    sequence = next(data["sources"][sid] for sid in ids if data["sources"][sid]["table_id"] == table_id
                    and data["sources"][sid]["row"] == 3 and data["sources"][sid]["column"] == 2)
    seq_id = next(data["sources"][sid] for sid in ids if data["sources"][sid]["table_id"] == table_id
                  and data["sources"][sid]["row"] == 3 and data["sources"][sid]["column"] == 1)
    caption = next(data["sources"][sid] for sid in ids if data["sources"][sid]["table_id"] == table_id
                   and data["sources"][sid]["row"] is None)
    example = empty_response()
    example["sequences"] = [{"seq": 4, "name": None, "region": "VH",
        "parts": [{"s": sequence["id"], "start": sequence["text"][:20], "end": sequence["text"][-20:]}],
        "refs": [caption["id"], seq_id["id"], sequence["id"]]}]
    ledger = Ledger(data)
    ledger.apply(example, ids, target["id"])
    if ledger.rejections:
        raise ValueError(f"Illustrative response is not grounded: {ledger.rejections}")
    write_json(folder / "illustrative-output.json", example)
    # One constructed Reader proposal demonstrates the integration interface.
    # It is not a substitute for running all Readers or the ActionAgent model.
    action_input = build_action_payload(data, [target], {target["id"]: {"response": example}})
    action_output = {"dataset": copy.deepcopy(action_input["initial_dataset"]), "action_decisions": [],
                     "seed_changes": [], "conflicts": []}
    value = next(f["value"] for f in ledger.facts["sequences"] if f["seq"] == 4 and f["region"] == "VH")
    action_output["dataset"]["sequences"].append({**copy.deepcopy(example["sequences"][0]), "value": value})
    action_output["action_decisions"].append({"action_ids": [action_input["reader_actions"][0]["id"]],
        "outcome": "accept", "targets": ["/dataset/sequences/0"], "refs": example["sequences"][0]["refs"],
        "reason": "Format example: the printed VH table and original SEQ ID 4 cell provide a literal definition."})
    validate_action_result(data, action_input, action_output)
    write_json(folder / "action-input.example.json", action_input)
    write_json(folder / "action-output.example.json", action_output)
    write_json(folder / "action-output-schema.json", SCHEMA_ACTION)
    (folder / "action-prompt.txt").write_text(ACTION_INSTRUCTIONS)

    provenance = {
        "status": "prepared_not_sent", "model_calls_made": 0,
        "notice": "New full-seed payloads and a host-checked illustrative response, not new model answers.",
        "pdf_sha256": prepared["pdf_sha256"], "reader_input_revision": READER_INPUT_REVISION,
        "initial_dataset_hash": digest(dataset), "seed_counts": {k: len(v) for k, v in dataset.items()},
        "body_block_count": len(full["document_blocks"]), "source_count": len(data["sources"]),
        "chunk_count": len(chunks), "chunk_char_budget": 24000, "overlap_chars": 700,
        "example_chunk": target["id"],
        "action_example": "One constructed proposal with the real complete business seed; host-validated, no model calls.",
        "source_files": {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            seed_dir / "sda_input.json", seed_dir / "document_template.json",
            seed_dir / "assay_measurements.json", full_ocr_dir / "sda_input.json")},
    }
    write_json(folder / "provenance.json", provenance)
    rows = "".join(f'<tr><td>{c["id"].upper()}</td><td>{c["pages"][0]}–{c["pages"][-1]}</td>'
                   f'<td>{c["core_count"]}</td><td>{c["context_count"]}</td>'
                   f'<td>{c["source_characters"]:,}</td></tr>' for c in plan)
    output = escape(json.dumps(example, ensure_ascii=False, indent=2))
    prompt = escape(INSTRUCTIONS)
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>49-Sructure · Chunk Reader 的输入与输出</title><link rel="icon" href="data:,"><style>
:root{{font:15px/1.8 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;color:#23332f;background:#f5f6f2;--line:#dce2dc;--muted:#6c7973;--green:#176b52}}*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1120px;margin:auto;padding:28px 24px 70px}}a{{color:var(--green)}}header{{margin:18px 0 30px}}h1{{font-size:30px;line-height:1.4}}h2{{font-size:22px;margin:0 0 10px}}h3{{font-size:16px;margin:0 0 8px}}p{{margin:8px 0}}section{{background:white;border:1px solid var(--line);border-radius:9px;padding:24px;margin:20px 0}}.muted{{color:var(--muted);font-size:13px}}.notice{{padding:14px 18px;border-left:3px solid var(--green);background:#edf6ef;font-size:13px}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}.stats div{{background:#edf5ef;padding:14px;border-radius:6px}}.stats strong{{display:block;font-size:26px;font-weight:600}}.stats span{{color:var(--muted);font-size:12px}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}article{{background:#fafbf8;border:1px solid var(--line);padding:18px;border-radius:6px}}ul,ol{{padding-left:23px;margin:12px 0}}li{{margin:6px 0}}.scroll{{max-height:350px;overflow:auto;border:1px solid var(--line);border-radius:6px}}table{{width:100%;border-collapse:collapse;font-size:13px;text-align:left}}td,th{{padding:9px 12px;border-bottom:1px solid var(--line)}}th{{position:sticky;top:0;background:#edf3eb}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:18px;background:#f1f3ed;border-radius:6px;font:12px/1.8 ui-monospace,monospace;max-height:500px;overflow:auto}}blockquote{{margin:15px 0;padding:14px 18px;background:#eff5ef;border-left:3px solid var(--green)}}.downloads{{display:flex;flex-wrap:wrap;gap:10px 24px;font-size:13px}}code{{font-size:.9em}}details{{margin-top:16px}}summary{{cursor:pointer;color:var(--green)}}@media(max-width:700px){{main{{padding:20px 16px}}section{{padding:18px}}h1{{font-size:25px}}.pair{{grid-template-columns:1fr}}.stats{{grid-template-columns:1fr 1fr}}th,td{{padding:8px;font-size:11px}}}}
</style></head><body><main><a href="index.html#reader-dataset-flow">← 返回整体流程</a>
<header><p class="muted">49-SRUCTURE · 全量业务 SEED · 输入修订 {READER_INPUT_REVISION}</p><h1>每块读什么、怎么读、最后交回什么？</h1><p>每个 Reader 都接收相同的完整初始 Dataset1，再结合当前 chunk 提出有证据的增量。</p></header>
<div class="notice">代码已切换为全量 seed。本页用同一 PDF 的已保存全文 OCR 与重新校验后的表格 seed 离线组包，未调用新模型。此前“5 块”属于仅表格对照；本页全文分块为 {len(chunks)} 块。</div>
<div class="stats"><div><strong>{len(full["document_blocks"]):,}</strong><span>已保存的正文 OCR 记录</span></div><div><strong>{len(data["sources"]):,}</strong><span>正文、表格和表题来源</span></div><div><strong>{len(chunks)}</strong><span>按当前规则生成的 chunks</span></div><div><strong>47 / 38</strong><span>每块都携带的实体 / 已有关联</span></div></div>
<section><h2>1. Chunk 怎么切？</h2><ol><li>按页码排列 OCR 正文与表格来源，逐块装入，默认预算为 <strong>24,000 个来源 JSON 字符</strong>。</li><li>小表尽量整张保留；大表优先按完整数据行切开。超长的单个来源保留并标记。</li><li>为上下文预留最多 2,700 个字符。优先补表格首两行，再补边界前后文；前后邻文通常各最多 350 字符。</li><li>每条来源只归属一个核心块；辅助上下文可以出现在相邻块。完整 seed 另加到每个请求中，不计入这 24,000 字符。</li></ol>
<p class="muted">按页面和 Example/实施例标题优先找边界，当前是 Python 规则切分。不是固定页数，也不是由 LLM 预先分段。上下文受预算限制，不保证把所有跨页语义带齐。</p>
<div class="scroll"><table><thead><tr><th>块</th><th>核心 PDF 页码</th><th>核心来源</th><th>上下文来源</th><th>来源 JSON 字符</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="muted">页码范围会重叠，因为同一页可能拆成多个块；上表显示核心块范围。来源字段包括编号、页码、类型、表格 ID、行、列和原文。</p>
<div class="downloads"><a href="reader-guide-data/chunk-plan.json">完整分块清单</a><a href="reader-guide-data/c1-input.json">C1 的完整新输入（未发送）</a><a href="reader-guide-data/{target["id"]}-input.json">{target["id"].upper()} 的完整新输入（含 Table 1，未发送）</a></div></section>
<section><h2>2. 每个 Reader 拿到什么？</h2><div class="pair"><article><h3>相同的完整 initial_dataset</h3><p>47 个实体、38 条关联、16 个实验、197 条测量、62 个标签；保留原有值、单位和引用。存在初始序列定义时也完整发送。</p><p class="muted">不再通过名称、编号或引用交集筛掉业务事实。模型运行日志留在主机。</p></article><article><h3>本块的 sources 与上下文</h3><p>只有当前块和边界上下文的实际 OCR 原文，以及相关 OCR 状态、可用的样式或域边界信息。</p><p class="muted">完整 seed 中的远处引用是背景；本块新增事实的 refs 必须指向本次 sources。</p></article></div><div class="downloads"><a href="reader-guide-data/initial_dataset.json">每块共用的完整业务 seed</a></div></section>
<section><h2>3. Prompt 是怎么要求它的？</h2><p>下面是实际英文指令的中文要点，英文全文可展开或下载。</p><blockquote>阅读当前 chunk，结合完整初始 dataset，提出新增事实或有证据的冲突。<br>复用已有实体 ID；新实体要有名称证据。<br>不要重复输出已有事实，不要重写整份 dataset。<br>每个新增事实必须引用本次 sources 中的编号。<br>区分 VH/VL、完整重轻链、CDR 和恒定区；保留不同实验条件。<br>序列用原文起止锚点定位，由主机取回；不改写残基。<br>拿不准就记录问题，当前块没找到不代表全文没有。<br>只返回给定 JSON 格式；没有新增内容时各数组为空。</blockquote><details><summary>展开当前实际英文 Prompt</summary><pre>{prompt}</pre></details><div class="downloads"><a href="reader-guide-data/prompt.txt">下载实际英文 Prompt</a><a href="reader-guide-data/output-schema.json">下载输出 JSON Schema</a></div></section>
<section><h2>4. Reader 返回什么？</h2><p>Reader 返回的是<strong>增量事实包</strong>，字段包括来源提及、新实体、编号关联、序列定义、实验、测量、标签、实体关系和问题。各块的事实包汇入 LLM ActionAgent，进行跨块语义整合。</p><div class="pair"><article><h3>例：给已有 SEQ ID 4 补上序列定义</h3><p>Table 1 的表题明确是 VH；AB-1 行列出 NO:4。{target["id"].upper()} 包含这条序列单元格。</p><p>模型可返回“SEQ ID 4 的 VH 序列位于来源 <strong>{sequence["id"]}</strong>，起止锚点如下”，主机再取回完整原文。</p><p class="muted">右侧是使用真实来源编号构造、已通过主机证据检查的格式示例，<strong>不是新模型回答</strong>。本例 seed 已有关联，因此只示范补充序列定义。</p></article><article><h3>示例返回 JSON</h3><pre>{output}</pre></article></div><div class="downloads"><a href="reader-guide-data/illustrative-output.json">下载 Reader 格式示例（非模型回答）</a><a href="reader-guide-data/provenance.json">查看组包来源与范围</a></div></section>
<section id="action-agent"><h2>5. ActionAgent 综合后，直接返回完整 Dataset2</h2><div class="pair"><article><h3>它收到的输入</h3><p>完整初始 Dataset1、所有 Reader actions，以及这些 actions 和底稿引用的原文。每条 action 带稳定编号，引用附近的段落、表格行和表头也一起提供。</p><p>ActionAgent 跨块核对名称、共享链、SEQ ID、序列定义和实验归属，决定哪些信息采纳、合并或保留冲突。</p></article><article><h3>它返回的完整结果</h3><p><code>dataset</code>：全部实体和各类事实；<code>action_decisions</code>：每条建议的处理；<code>seed_changes</code>：对初始事实的明确修正；<code>conflicts</code>：未解决的冲突及证据。</p><p>程序检查引用、原文字串、实体和事实是否无故消失、actions 是否都有去向。检查失败时反馈一次，仍失败则保留 partial。</p></article></div><p class="notice">下面的整合样例使用真实全量 seed 和 {target["id"].upper()} 的一条构造 action，通过了主机校验；未执行新的 Reader 或 ActionAgent 模型调用。它展示接口和输出格式，不是新抽取结果。</p><div class="downloads"><a href="reader-guide-data/action-input.example.json">ActionAgent 输入格式示例</a><a href="reader-guide-data/action-output.example.json">完整 Dataset2 输出格式示例</a><a href="reader-guide-data/action-prompt.txt">ActionAgent 实际 Prompt</a><a href="reader-guide-data/action-output-schema.json">ActionAgent 输出 Schema</a></div></section>
</main></body></html>'''
    (snapshot / "reader-guide.html").write_text(html)
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-dir", required=True, type=Path)
    parser.add_argument("--full-ocr-dir", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    result = build(args.seed_dir, args.full_ocr_dir, args.snapshot)
    print(json.dumps({k: v for k, v in result.items() if k != "source_files"}, ensure_ascii=False, indent=2))
