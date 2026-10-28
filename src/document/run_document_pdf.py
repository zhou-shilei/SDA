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

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document.document_extraction import save


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
    structured = Path(report.get("structured_dir") or Path(run_dir) / "structured")
    bundle["structured_artifacts"] = {key: os.path.relpath(structured / name, document)
                                      for key, name in (("json", "document.json"), ("markdown", "document.md"))
                                      if (structured / name).is_file()}
    html = (Path(__file__).resolve().parents[2] / "studio/run.html").read_text()
    value = json.dumps(bundle, ensure_ascii=False).replace("<", "\\u003c")
    html = html.replace('<script type="application/json" id="report-data"></script>',
                        '<script type="application/json" id="report-data">' + value + '</script>')
    (document / "report.html").write_text(html, encoding="utf-8")
    return document / "report.html"


def prepare_semantic_ocr_cache(structured_dir, input_dir, cache_dir):
    """将本轮原始表格证据适配为 graph.py 的严格复用格式，不再识别图片。"""
    structured_dir, input_dir, cache_dir = map(lambda p: Path(p).resolve(), (structured_dir, input_dir, cache_dir))
    document = json.loads((structured_dir / "document.json").read_text())
    identity = document["document_id"]
    manifest = json.loads((input_dir / f"{identity}_manifest.json").read_text())
    if document["processing_pdf_sha256"] != manifest["processing_pdf_sha256"]:
        raise ValueError("结构化文档与裁图的工作 PDF 不一致")
    rows = [json.loads(line) for line in (structured_dir / "table_ocr/tables.jsonl").read_text().splitlines() if line.strip()]
    evidence = {row["table_id"]: row for row in rows}
    expected = {crop["id"] for crop in manifest["images"] if crop["label"] == "table"}
    if len(evidence) != len(rows) or set(evidence) != expected:
        raise ValueError("原始表格 OCR 与裁图清单不完整或存在重复")
    prepared = []
    for crop in manifest["images"]:
        if crop["label"] != "table":
            continue
        row = evidence[crop["id"]]
        image = (input_dir / crop["image"]).resolve()
        text = row.get("text")
        if (row.get("status") != "success" or row.get("document_id") != identity
                or row.get("page") != crop["page"] or not isinstance(text, str) or not text.strip()
                or Path(row.get("image_path", "")).resolve() != image
                or row.get("image_sha256") != hashlib.sha256(image.read_bytes()).hexdigest()
                or row.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()):
            raise ValueError(f"原始表格 OCR 不完整或身份/哈希不匹配：{crop['id']}")
        prepared.append((image.stem + ".json", {
            "document_id": identity, "table_id": image.relative_to(input_dir).as_posix(),
            "image_path": str(image), "table_text": text, "table_evidence": row,
            "source_ocr_file": str(structured_dir / "table_ocr" / f"{crop['id']}.json"),
        }))
    cache_dir.mkdir(parents=True, exist_ok=False)
    for name, row in prepared:
        save(cache_dir / name, row)
    return cache_dir


def run_pdf(pdf, run_dir, *, document_id=None, device="gpu:0", text_workers=1,
            repair_rounds=2, catalog=None, on_stage=None, template_agent=True,
            template_agent_max_calls=12, template_agent_context_chars=24000,
            table_ocr_workers=1, table_ocr_batch_size=1, text_backend="paddle",
            table_backend="vl", ocr_parallel=True, extraction_mode="dataagent", semantic_provider=None,
            llm_workers=None, sequence_workers=None, reader_chunk_chars=24000):
    """网页和 CLI 共用：统一六步预处理，再复用证据进行业务抽取。"""
    started = perf_counter()
    if template_agent_max_calls < 0 or template_agent_context_chars < 4000:
        raise ValueError("TemplateAgent 调用预算须非负，上下文至少 4000 字符")
    if min(text_workers, table_ocr_workers, table_ocr_batch_size) < 1:
        raise ValueError("OCR 进程数和提交批大小必须为正整数")
    if text_backend not in {"paddle", "vl"} or table_backend not in {"paddle", "vl"}:
        raise ValueError("OCR backend 必须为 paddle 或 vl")
    if extraction_mode not in {"dataagent", "dataagent2", "document"}:
        raise ValueError("extraction_mode 必须为 dataagent、dataagent2 或 document")
    if llm_workers is not None and not 0 <= llm_workers <= 8:
        raise ValueError("表格种子模型并发数必须介于 0 和 8；0 表示逐表全部并发")
    if sequence_workers is not None:
        if extraction_mode == "dataagent2" and sequence_workers < 0:
            raise ValueError("Reader 并发数必须非负，0 表示所有 chunk 同时调用")
        if extraction_mode != "dataagent2" and not 1 <= sequence_workers <= 8:
            raise ValueError("序列模型并发数必须介于 1 和 8")
    if reader_chunk_chars < 3000:
        raise ValueError("Reader chunk 字符预算至少为 3000")
    pdf, run_dir = Path(pdf).resolve(), Path(run_dir).resolve()
    identity = document_id or pdf.stem
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", identity):
        raise ValueError("无效 document_id")
    if not pdf.is_file() or run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("需要存在的 PDF 和全新输出目录")
    run_dir.mkdir(parents=True, exist_ok=True)
    structured = run_dir / "structured"
    inputs, text_output = run_dir / "data" / identity, run_dir / "text_ocr"
    outputs, document = run_dir / "results", run_dir / "results" / identity
    inputs.parent.mkdir(parents=True)
    # 保留既有网页、报告和续跑路径；证据只存一份，图片绝对路径保持一致。
    inputs.symlink_to(structured / "layout", target_is_directory=True)
    text_output.symlink_to(structured / "page_ocr", target_is_directory=True)
    source = run_dir / "source.pdf"
    shutil.copyfile(pdf, source)
    report = {"document_id": identity, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_pdf": str(pdf), "source_pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
              "run_dir": str(run_dir), "input_dir": str(inputs), "result_dir": str(document),
              "structured_dir": str(structured), "structured_document": str(structured / "document.json"),
              "status": "running", "phases": [], "extraction_mode": extraction_mode,
              "extraction_engine": "dataagent1" if extraction_mode == "dataagent" else extraction_mode,
              "semantic_provider": semantic_provider,
              "cache_policy": f"Fresh six-step preprocessing; page/table OCR execution={'parallel' if ocr_parallel else 'serial'}; semantic extraction reuses this run's exact OCR evidence.",
              "catalog": None, "python": sys.executable,
              "token_scope": "Actual Codex CLI usage for semantic extraction/repair/mapping/sequence verification. Local OCR decoder tokens and development chat excluded."}
    report["template_agent"] = ({"enabled": template_agent, "max_calls": template_agent_max_calls,
                                  "context_chars": template_agent_context_chars} if extraction_mode == "document" else
                                 {"enabled": template_agent and template_agent_max_calls != 0,
                                  "implementation": "dataagent.SequenceExtractionAgent",
                                  "max_turns_per_batch": 6, "source_budget_chars": 180000})
    report["table_ocr"] = {"workers": table_ocr_workers, "submission_batch_size": table_ocr_batch_size, "backend": table_backend}
    if extraction_mode == "dataagent2":
        report["template_agent"] = {"enabled": True, "implementation": "dataagent2.Reader + bounded questions",
                                    "shared_stages": ["table_classification", "document_template"],
                                    "seed_before_reader": True, "reader_chunk_chars": reader_chunk_chars}
    report["text_ocr"] = {"workers": text_workers, "backend": text_backend}
    report["ocr_execution"] = "parallel" if ocr_parallel else "serial"
    if catalog:
        catalog = Path(catalog).resolve()
        listing = json.loads(catalog.read_text())
        if listing.get("document_id") != identity:
            raise ValueError("序列表与 PDF document_id 不一致")
        report["catalog"] = {"path": str(catalog), "sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                             "scope": "Existing same-document local sequence listing; no network fetch."}
    root = Path(__file__).resolve().parents[2]
    scripts = root / "src"

    def phase(name, arguments, *, allow_partial=False):
        if on_stage:
            on_stage(name)
        print(f"PDF stage={name}", flush=True)
        tick = perf_counter()
        command = [sys.executable, *map(str, arguments)]
        with (run_dir / f"{name}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1,
                                       env={**os.environ, "PYTHONUNBUFFERED": "1", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "true"})
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                code = process.wait()
            except BaseException:
                process.terminate()
                process.wait()
                raise
            finally:
                process.stdout.close()
        entry = {"name": name, "seconds": perf_counter() - tick, "returncode": code, "command": command}
        partial = (allow_partial and code == 1 and (structured / "summary.json").is_file()
                   and json.loads((structured / "summary.json").read_text()).get("status") == "partial")
        if partial:
            entry["accepted_partial"] = True
        report["phases"].append(entry)
        save(run_dir / "run_summary.json", report)
        if code and not partial:
            raise RuntimeError(f"{name} 失败，退出码 {code}；日志：{run_dir / (name + '.log')}")

    try:
        layout_model = Path(os.environ.get("SDA_PP_LAYOUT_MODEL_DIR", Path.home() / ".paddlex/official_models/PP-DocLayoutV3"))
        phase("structured_document", [scripts / "document/pdf2doc.py", "--input", source,
              "--document-id", identity, "--output-dir", structured, "--device", device,
              "--pp-model-dir", layout_model, "--orientation", "auto", "--backend", text_backend,
              "--text-workers", text_workers, "--render-prefetch", "4",
              "--table-backend", table_backend, "--table-workers", table_ocr_workers,
              "--table-batch-size", table_ocr_batch_size,
              "--ocr-execution", "parallel" if ocr_parallel else "serial"], allow_partial=True)
        manifest = json.loads((inputs / f"{identity}_manifest.json").read_text())
        structured_summary = json.loads((structured / "summary.json").read_text())
        report["preprocessing_status"] = structured_summary["status"]
        report["structured_summary"] = structured_summary
        text_summary = json.loads((text_output / "summary.json").read_text())
        if text_summary["status"] != "complete" or not text_summary["full_document_ocr_complete"]:
            raise RuntimeError("全文 OCR 不完整；已保留结构化结果，不启动业务模型抽取")
        shutil.copyfile(source, inputs / "source.pdf")
        if catalog:
            (inputs / "wipo").mkdir()
            shutil.copyfile(catalog, inputs / "wipo/sequences.json")
        cache = prepare_semantic_ocr_cache(structured, inputs, run_dir / "semantic_ocr_cache")
        graph_args = [scripts / "graph.py", "--image-dir", inputs, "--output-dir", outputs,
              "--text-context-dir", inputs, "--text-pages-jsonl", text_output / "pages.jsonl",
              "--reuse-table-ocr-from", cache,
              "--extraction-mode", extraction_mode, "--document-repair-rounds", repair_rounds,
              "--ocr-device", device, "--ocr-backend", table_backend, "--ocr-workers", table_ocr_workers,
              "--ocr-batch-size", table_ocr_batch_size,
              "--template-agent-max-calls", template_agent_max_calls,
              "--template-agent-context-chars", template_agent_context_chars]
        if extraction_mode in {"dataagent", "dataagent2"}:
            graph_args.extend(["--structured-dir", structured, "--document-id", identity])
            for flag, value in (("--llm-workers", llm_workers), ("--sequence-workers", sequence_workers)):
                if value is not None:
                    graph_args.extend([flag, value])
            if semantic_provider:
                graph_args.extend(["--semantic-provider", semantic_provider])
        if extraction_mode == "dataagent2":
            graph_args.extend(["--reader-chunk-chars", reader_chunk_chars])
        if not template_agent:
            graph_args.append("--no-document-template-agent")
        phase("document_extraction", graph_args)
        batch = json.loads((outputs / "_batch_summary.json").read_text())
        dataset = json.loads((document / "dataset.json").read_text())
        if structured_summary["status"] != "complete" and dataset["status"] == "complete":
            dataset["status"] = "partial"
        dataset["preprocessing"] = {"status": structured_summary["status"],
                                    "document_file": str(structured / "document.json")}
        save(document / "dataset.json", dataset)
        overall = "failed" if batch["status"] == "failed" else "partial" if "partial" in (
            batch["status"], structured_summary["status"]) else "complete"
        report.update(status=overall, wall_seconds=perf_counter() - started,
                      token_usage=batch["token_usage"], dataset_status=dataset["status"],
                      records=len(dataset["records"]), entities=len(dataset["entities"]),
                      experiments=len(dataset.get("experiments", [])), relations=len(dataset.get("relations", [])),
                      total_pages=manifest["total_pages"], table_count=manifest["image_count"],
                      layout_status=manifest["status"], text_ocr_summary=json.loads((text_output / "summary.json").read_text()),
                      layout_scope=manifest.get("scope"), orientation=manifest["orientation"])
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
    if report.get("extraction_mode") == "dataagent2":
        raise ValueError("dataagent2 请在网页使用复用 OCR 重新解析，生成新的运行目录")
    if report.get("extraction_mode") == "dataagent":
        return resume_dataagent_pdf(run_dir, report, repair_rounds=repair_rounds,
                                   extract_sequences=template_agent,
                                   sequence_budget=template_agent_max_calls)
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
    root = Path(__file__).resolve().parents[2]
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
        if report.get("preprocessing_status") == "partial" and dataset["status"] == "complete":
            dataset["status"] = report["status"] = "partial"
            dataset["preprocessing"] = {"status": "partial", "document_file": report["structured_document"]}
            save(result / "dataset.json", dataset)
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


def resume_dataagent_pdf(run_dir, report, *, repair_rounds=2, extract_sequences=None, sequence_budget=None):
    """Resume the same OCR snapshot; preserve each native attempt and cumulative usage."""
    identity, result = report["document_id"], Path(report["result_dir"])
    checkpoint = json.loads((result / "dataagent_checkpoint.json").read_text())
    inputs, options = checkpoint["inputs"], checkpoint["options"]
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "src/graph.py"), "--extraction-mode", "dataagent",
        "--dataagent-resume", "--document-id", identity, "--image-dir", inputs["image_dir"],
        "--output-dir", str(result.parent), "--text-context-dir", inputs["data_dir"],
        "--text-pages-jsonl", inputs["text_pages_jsonl"], "--reuse-table-ocr-from", inputs["table_ocr"],
        "--document-repair-rounds", str(repair_rounds)]
    if inputs.get("structured_dir"):
        command.extend(["--structured-dir", inputs["structured_dir"]])
    for key in ("semantic_provider", "llm_model", "llm_effort", "llm_workers", "sequence_workers"):
        if options.get(key) is not None:
            command.extend(["--" + key.replace("_", "-"), str(options[key])])
    enabled = options.get("extract_sequences", True) if extract_sequences is None else extract_sequences
    if not enabled or sequence_budget == 0:
        command.append("--no-document-template-agent")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    save(run_dir / f"run_summary.before-resume-{stamp}.json", report)
    started = perf_counter()
    with (run_dir / f"resume-{stamp}.log").open("w") as log:
        code = subprocess.call(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    duration = perf_counter() - started
    report["phases"].append({"name": "document_resume", "seconds": duration, "returncode": code, "command": command})
    report["wall_seconds"] += duration
    report["wall_time_scope"] = "Sum of automated intervals; development pauses excluded."
    report["elapsed_since_first_start_seconds"] = (datetime.now(timezone.utc) - datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    report.update(resumed=True, cache_policy="复用同批全文和表格 OCR，校验输入身份后复用 dataagent 模型缓存；保留各次尝试和累计用量。")
    batch = json.loads((result.parent / "_batch_summary.json").read_text())
    report.update(token_usage=batch["token_usage"], status="failed" if code else batch["status"])
    if not code:
        dataset = json.loads((result / "dataset.json").read_text())
        if report.get("preprocessing_status") == "partial" and dataset["status"] == "complete":
            dataset["status"] = report["status"] = "partial"
        dataset["preprocessing"] = {"status": report.get("preprocessing_status"), "document_file": report.get("structured_document")}
        save(result / "dataset.json", dataset)
        report.update(records=len(dataset["records"]), entities=len(dataset["entities"]),
                      experiments=len(dataset.get("experiments", [])), relations=len(dataset.get("relations", [])),
                      dataset_status=dataset["status"])
        report.pop("error", None)
    else:
        report["error"] = batch.get("errors", {}).get(identity, "dataagent 续跑失败，查看运行日志")
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
    parser.add_argument("--text-workers", type=int, default=1)
    parser.add_argument("--text-backend", choices=("paddle", "vl"), default="paddle")
    parser.add_argument("--table-backend", choices=("paddle", "vl"), default="vl")
    parser.add_argument("--ocr-execution", choices=("parallel", "serial"), default="parallel")
    parser.add_argument("--extraction-mode", choices=("dataagent", "dataagent2", "document"), default="dataagent")
    parser.add_argument("--semantic-provider", choices=("codex", "rules"), default=None)
    parser.add_argument("--llm-workers", type=int, help="表格分类/原图复核并发数（0 全部并发；1–8 限制并发）")
    parser.add_argument("--sequence-workers", type=int,
                        help="dataagent2 默认所有 chunk 同时调用（0）；dataagent 序列并发数为 1–8")
    parser.add_argument("--reader-chunk-chars", type=int, default=24000)
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
                     table_ocr_workers=1 if args.table_ocr_workers is None else args.table_ocr_workers,
                     table_ocr_batch_size=1 if args.table_ocr_batch_size is None else args.table_ocr_batch_size,
                     text_backend=args.text_backend, table_backend=args.table_backend,
                     ocr_parallel=args.ocr_execution == "parallel", extraction_mode=args.extraction_mode,
                     semantic_provider=args.semantic_provider, llm_workers=args.llm_workers,
                     sequence_workers=args.sequence_workers, reader_chunk_chars=args.reader_chunk_chars)
    print(json.dumps({k: result[k] for k in ("status", "wall_seconds", "records", "experiments")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
