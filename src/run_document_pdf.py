"""Run a PDF through fresh layout, text/table OCR and document extraction.

Every invocation uses a new run directory. End-to-end time includes child process
startup, OCR, extraction, repairs and export. Developer/test time is excluded.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import perf_counter

from document_extraction import save


def write_html_report(run_dir, report):
    """Embed data so the report also works in a local VS Code HTML preview."""
    document = Path(report["result_dir"])
    def read(name):
        path = document / name
        return json.loads(path.read_text()) if path.is_file() else {}
    bundle = {"run": report, "template": read("template.completed.json"), "coverage": read("coverage.json"),
              "runtime": read("_timings.json"), "missing": read("missing_fields.json"),
              "document_report": read("document_report.json"), "dataset": read("dataset.json"),
              "sequence_report": read("template_agent_report.json"),
              "page_images": {int(p.stem.split('-')[-1]): os.path.relpath(p, document)
                              for p in (Path(run_dir) / "text_ocr/images").glob("page-*.png")}}
    html = (Path(__file__).resolve().parents[1] / "studio/run.html").read_text()
    value = json.dumps(bundle, ensure_ascii=False).replace("<", "\\u003c")
    html = html.replace('<script type="application/json" id="report-data"></script>',
                        '<script type="application/json" id="report-data">' + value + '</script>')
    (document / "report.html").write_text(html, encoding="utf-8")
    return document / "report.html"


def run_pdf(pdf, run_dir, *, document_id=None, device="gpu:0", text_workers=6,
            repair_rounds=2, catalog=None, on_stage=None, template_agent=True,
            template_agent_max_calls=12, template_agent_context_chars=24000,
            table_ocr_workers=6, table_ocr_batch_size=1):
    started = perf_counter()
    if template_agent_max_calls < 0 or template_agent_context_chars < 4000:
        raise ValueError("TemplateAgent 调用预算须非负，上下文至少 4000 字符")
    if min(table_ocr_workers, table_ocr_batch_size) < 1:
        raise ValueError("表格 OCR 进程数和提交批大小必须为正整数")
    pdf, run_dir = Path(pdf).resolve(), Path(run_dir).resolve()
    identity = document_id or pdf.stem
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
        raise ValueError("无效 document_id")
    if not pdf.is_file() or run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("需要存在的 PDF 和全新输出目录")
    run_dir.mkdir(parents=True, exist_ok=True)
    inputs = run_dir / "data" / identity
    outputs = run_dir / "results"
    document = outputs / identity
    inputs.mkdir(parents=True)
    source = run_dir / "source.pdf"
    shutil.copyfile(pdf, source)
    report = {"document_id": identity, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_pdf": str(pdf), "source_pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
              "run_dir": str(run_dir), "input_dir": str(inputs), "result_dir": str(document),
              "status": "running", "phases": [], "cache_policy": "Fresh run directory; no previous OCR/extractions reused.",
              "catalog": None, "python": sys.executable,
              "token_scope": "Actual Codex CLI usage for extraction/repair/mapping/template-sequence planning and verification, including reported cached-input tokens. Local OCR decoder tokens and development chat excluded."}
    report["template_agent"] = {"enabled": template_agent, "max_calls": template_agent_max_calls,
                                "context_chars": template_agent_context_chars}
    report["table_ocr"] = {"workers": table_ocr_workers, "submission_batch_size": table_ocr_batch_size}
    if catalog:
        catalog = Path(catalog).resolve()
        listing = json.loads(catalog.read_text())
        if listing.get("document_id") != identity:
            raise ValueError("序列表与 PDF document_id 不一致")
        report["catalog"] = {"path": str(catalog), "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                             "scope": "Existing same-document local sequence listing; no network fetch."}
    root = Path(__file__).resolve().parents[1]
    scripts = root / "src"

    def phase(name, arguments):
        if on_stage:
            on_stage(name)
        print(f"PDF stage={name}", flush=True)
        tick = perf_counter()
        command = [sys.executable, *map(str, arguments)]
        with (run_dir / f"{name}.log").open("w") as log:
            process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONUNBUFFERED": "1", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "true"})
            try:
                code = process.wait()
            except BaseException:
                process.terminate()
                process.wait()
                raise
        report["phases"].append({"name": name, "seconds": perf_counter() - tick, "returncode": code, "command": command})
        save(run_dir / "run_summary.json", report)
        if code:
            raise RuntimeError(f"{name} 失败，退出码 {code}；日志：{run_dir / (name + '.log')}")

    try:
        layout_model = Path(os.environ.get("SDA_PP_LAYOUT_MODEL_DIR", Path.home() / ".paddlex/official_models/PP-DocLayoutV3"))
        phase("layout", [scripts / "pdf2fig.py", "--input", source, "--document-id", identity,
                         "--output-dir", inputs, "--device", device, "--orientation", "off",
                         "--pp-model-dir", layout_model])
        manifest = json.loads((inputs / f"{identity}_manifest.json").read_text())
        shutil.copyfile(source, inputs / "source.pdf")
        if catalog:
            (inputs / "wipo").mkdir()
            shutil.copyfile(catalog, inputs / "wipo/sequences.json")
        prepared_pdf = Path(manifest.get("processing_pdf", source))
        text_output = run_dir / "text_ocr"
        phase("text_ocr", [scripts / "paten_ocr.py", prepared_pdf, "--document-id", identity,
                           "--output-dir", text_output, "--backend", "paddle", "--device", device,
                           "--batch-size", "6", "--workers", text_workers, "--render-prefetch", "4"])
        graph_args = [scripts / "graph.py", "--image-dir", inputs, "--output-dir", outputs,
              "--text-context-dir", inputs, "--text-pages-jsonl", text_output / "pages.jsonl",
              "--extraction-mode", "document", "--document-repair-rounds", repair_rounds,
              "--ocr-device", device, "--ocr-backend", "vl", "--ocr-workers", table_ocr_workers,
              "--ocr-batch-size", table_ocr_batch_size, "--page-ocr-max-pages", "0",
              "--template-agent-max-calls", template_agent_max_calls,
              "--template-agent-context-chars", template_agent_context_chars]
        if not template_agent:
            graph_args.append("--no-document-template-agent")
        phase("table_ocr_and_extraction", graph_args)
        batch = json.loads((outputs / "_batch_summary.json").read_text())
        dataset = json.loads((document / "dataset.json").read_text())
        report.update(status=batch["status"], wall_seconds=perf_counter() - started,
                      token_usage=batch["token_usage"], dataset_status=dataset["status"],
                      records=len(dataset["records"]), entities=len(dataset["entities"]),
                      experiments=len(dataset.get("experiments", [])), relations=len(dataset.get("relations", [])),
                      total_pages=manifest["total_pages"], table_count=manifest["image_count"],
                      layout_status=manifest["status"], text_ocr_summary=json.loads((text_output / "summary.json").read_text()),
                      layout_scope=manifest.get("scope"),
                      orientation="off: preserve source page geometry; orientation was not automatically assessed")
        save(document / "run_summary.json", report)
        return report
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}", wall_seconds=perf_counter() - started)
        batch = outputs / "_token_usage.json"
        if batch.exists():
            report["token_usage"] = json.loads(batch.read_text())
        raise
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(run_dir / "run_summary.json", report)
        if document.exists():
            save(document / "run_summary.json", report)
            if report["status"] != "failed":
                write_html_report(run_dir, report)


def resume_pdf(run_dir, *, repair_rounds=2, template_agent=None,
               template_agent_max_calls=None, template_agent_context_chars=None,
               table_ocr_workers=None, table_ocr_batch_size=None):
    """Continue validated model checkpoints without paying for another full read."""
    run_dir = Path(run_dir).resolve()
    report = json.loads((run_dir / "run_summary.json").read_text())
    settings = report.get("template_agent", {})
    settings = {"enabled": settings.get("enabled", True) if template_agent is None else template_agent,
                "max_calls": settings.get("max_calls", 12) if template_agent_max_calls is None else template_agent_max_calls,
                "context_chars": settings.get("context_chars", 24000) if template_agent_context_chars is None else template_agent_context_chars}
    if settings["max_calls"] < 0 or settings["context_chars"] < 4000:
        raise ValueError("TemplateAgent 调用预算须非负，上下文至少 4000 字符")
    previous_ocr = report.get("table_ocr", {})
    ocr_settings = {"workers": previous_ocr.get("workers", 1) if table_ocr_workers is None else table_ocr_workers,
        "submission_batch_size": previous_ocr.get("submission_batch_size", 1) if table_ocr_batch_size is None else table_ocr_batch_size}
    if min(ocr_settings.values()) < 1:
        raise ValueError("表格 OCR 进程数和提交批大小必须为正整数")
    identity, result = report["document_id"], Path(report["result_dir"])
    payload = result / "document_extraction.json"
    if not payload.exists():
        payload = result / "document_extraction.raw.json"
    if not payload.exists():
        raise ValueError("没有可续跑的文档级模型检查点")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    save(run_dir / f"run_summary.before-resume-{stamp}.json", report)
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "src/graph.py"), "--image-dir", report["input_dir"],
               "--output-dir", str(result.parent), "--text-context-dir", report["input_dir"],
               "--text-pages-jsonl", str(run_dir / "text_ocr/pages.jsonl"), "--extraction-mode", "document",
               "--document-resume", str(payload), "--reuse-table-ocr-from", str(result),
               "--document-repair-rounds", str(repair_rounds),
               "--template-agent-max-calls", str(settings["max_calls"]),
               "--template-agent-context-chars", str(settings["context_chars"]),
               "--ocr-workers", str(ocr_settings["workers"]),
               "--ocr-batch-size", str(ocr_settings["submission_batch_size"])]
    if not settings["enabled"]:
        command.append("--no-document-template-agent")
    report["template_agent"] = settings
    report["table_ocr"] = ocr_settings
    tick = perf_counter()
    with (run_dir / f"resume-{stamp}.log").open("w") as log:
        code = subprocess.call(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    duration = perf_counter() - tick
    report["phases"].append({"name": "document_resume", "seconds": duration, "returncode": code, "command": command})
    report["wall_seconds"] += duration
    report["wall_time_scope"] = "Sum of measured automated processing intervals across original and resumed attempts; developer pause excluded."
    report["elapsed_since_first_start_seconds"] = (datetime.now(timezone.utc) - datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    report["resumed"] = True
    manifest = json.loads((Path(report["input_dir"]) / f"{identity}_manifest.json").read_text())
    report.update(total_pages=manifest["total_pages"], table_count=manifest["image_count"], layout_status=manifest["status"])
    report["cache_policy"] = "首次执行重新 OCR；失败后复用同一批 OCR 和原始全文模型输出续跑，原请求用量保留并累计。总用时为自动处理区间累计，不包含开发暂停。"
    batch = json.loads((result.parent / "_batch_summary.json").read_text())
    report["token_usage"] = batch["token_usage"]
    report["status"] = "failed" if code else batch["status"]
    if not code:
        dataset = json.loads((result / "dataset.json").read_text())
        report.update(records=len(dataset["records"]), entities=len(dataset["entities"]),
                      experiments=len(dataset.get("experiments", [])), relations=len(dataset.get("relations", [])),
                      dataset_status=dataset["status"])
        report.pop("error", None)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    save(run_dir / "run_summary.json", report)
    save(result / "run_summary.json", report)
    if not code:
        write_html_report(run_dir, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, nargs="?")
    parser.add_argument("--resume", action="store_true", help="复用同一输入下的模型检查点，补未覆盖项；累计原有实际用量")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--document-id")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--text-workers", type=int, default=6)
    parser.add_argument("--table-ocr-workers", type=int, default=None)
    parser.add_argument("--table-ocr-batch-size", type=int, default=None,
                        help="表格图片提交批大小；native VL 实际模型 batch 可能仍为 1")
    parser.add_argument("--document-template-agent", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--template-agent-max-calls", type=int, default=None)
    parser.add_argument("--template-agent-context-chars", type=int, default=None)
    parser.add_argument("--repair-rounds", type=int, default=2)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args()
    if args.resume:
        result = resume_pdf(args.run_dir, repair_rounds=args.repair_rounds,
            template_agent=args.document_template_agent, template_agent_max_calls=args.template_agent_max_calls,
            template_agent_context_chars=args.template_agent_context_chars,
            table_ocr_workers=args.table_ocr_workers, table_ocr_batch_size=args.table_ocr_batch_size)
        print(json.dumps({k: result.get(k) for k in ("status", "wall_seconds", "records", "experiments")}, ensure_ascii=False))
        return int(result["status"] == "failed")
    if args.pdf is None:
        parser.error("首次运行需要 PDF")
    result = run_pdf(args.pdf, args.run_dir, document_id=args.document_id, device=args.device,
                     text_workers=args.text_workers, repair_rounds=args.repair_rounds, catalog=args.catalog,
                     template_agent=True if args.document_template_agent is None else args.document_template_agent,
                     template_agent_max_calls=12 if args.template_agent_max_calls is None else args.template_agent_max_calls,
                     template_agent_context_chars=24000 if args.template_agent_context_chars is None else args.template_agent_context_chars,
                     table_ocr_workers=6 if args.table_ocr_workers is None else args.table_ocr_workers,
                     table_ocr_batch_size=1 if args.table_ocr_batch_size is None else args.table_ocr_batch_size)
    print(json.dumps({k: result[k] for k in ("status", "wall_seconds", "records", "experiments")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
