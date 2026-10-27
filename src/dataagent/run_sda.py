"""Run the migrated dataagent using SDA's table and full-page OCR outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent.schemas import AgentConfig
from .sda_inputs import prepare_sda_inputs


def run_from_ocr(document_id, table_ocr, pdf_ocr, output_dir, *, layout_manifest=None,
                 image_dir=None, source_pdf=None, expected_input=None, seed_only=False, **options):
    """Semantic pipeline over existing OCR; optionally validate a resume snapshot."""
    from .agent.graph import build_graph, initial_state
    from .llm_runtime import json_write

    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Use a new output directory to preserve existing results")
    config = AgentConfig(document_id=document_id, input_dir=str(Path(image_dir).resolve()) if image_dir else str(output_dir),
                         output_dir=str(output_dir), source_pdf=str(Path(source_pdf).resolve()) if source_pdf else None,
                         **{**options, "selection_mode": "all", "run_ocr": False,
                            "run_sequence_page_ocr": False, "copy_images": False})
    prepared = prepare_sda_inputs(document_id, table_ocr, pdf_ocr, output_dir,
        layout_manifest=layout_manifest, image_dir=image_dir, source_pdf=source_pdf)
    if expected_input:
        from .sda_inputs import read_prepared
        previous = read_prepared(expected_input, document_id)
        current = read_prepared(prepared, document_id)
        keys = ("document_id", "pdf_sha256", "tables", "candidates", "document_blocks", "table_contexts", "unstructured_tables")
        def original(value, key):
            if key == "tables":
                return [{k: v for k, v in table.items() if k != "sequence_style_sources"} for table in value.get(key, [])]
            return value.get(key, [])
        if any(original(previous, key) != original(current, key) for key in keys):
            raise ValueError("续跑的表格/全文 OCR 与原输入不一致，拒绝复用模型结果")
    config.sda_input_file = str(prepared)
    try:
        # Both engines receive the same separate image-backed style evidence.
        # Rules mode stays entirely offline; no image request without a style cue.
        inputs = json.loads(prepared.read_text())
        from document.sequence_styles import style_candidates, review_prepared
        if config.semantic_provider != "rules" and any(style_candidates(inputs, t)[0] for t in inputs["tables"]):
            from .llm_runtime import CodexProvider
            provider = CodexProvider(cache_dir=output_dir / "style_requests", model=config.llm_model,
                effort=config.llm_effort, timeout=config.llm_timeout, audit_path=output_dir / "llm_calls.jsonl")
            review = review_prepared(inputs, provider, output_dir)
            inputs["sequence_style_review"] = review["summary"]
            inputs["warnings"].extend({"code": "sequence_style_review_pending", **item} for item in review["failures"])
            json_write(prepared, inputs)
        if seed_only:
            from .agent.graph import build_seed_graph
            from .records import export_measurements
            state = build_seed_graph().invoke(initial_state(config), {"recursion_limit": 100})
            measurements = export_measurements(document_id, state["ocr_results"],
                state["table_classifications"], state["document_template"], [])
            for name, value in (("agent_state.json", state), ("document_template.json", state["document_template"]),
                                ("assay_measurements.json", measurements["measurements"])):
                json_write(output_dir / name, value)
            return state
        state = build_graph().invoke(initial_state(config), {"recursion_limit": 100})
        runtime = json.loads((output_dir / "agent_runtime.json").read_text())
        measurements = json.loads((output_dir / "assay_measurements.json").read_text())
        inputs = json.loads(prepared.read_text())
        dataset = {"schema_version": "dataagent-migrated-1", "document_id": document_id,
                   "status": "complete" if runtime["semantic_status"] == "completed" else "partial",
                   "semantic_provider": config.semantic_provider,
                   "records": state.get("dataset_records", []), "measurements": measurements,
                   "document_template": state.get("document_template", {}),
                   "record_verification": state.get("record_verification", {}),
                   "review_tasks": state.get("review_tasks", []), "warnings": inputs["warnings"],
                   "question_review": state.get("question_review", {}),
                   "sequence_evaluation": state.get("sequence_evaluation", {}),
                   "provenance_file": "sda_input.json", "artifacts": state.get("artifacts", {})}
        # Rule mode is an offline integration check; it does not run semantic agents.
        if config.semantic_provider == "rules" or inputs.get("unstructured_tables"):
            dataset["status"] = "partial"
        json_write(output_dir / "dataset.json", dataset)
        return dataset
    except Exception as exc:
        json_write(output_dir / "run_error.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structured-dir", type=Path, help="SDA pdf2doc output directory; derives both OCR inputs and layout")
    parser.add_argument("--document-id")
    parser.add_argument("--table-ocr", type=Path, help="SDA table OCR JSONL, JSON or directory of table results")
    parser.add_argument("--pdf-ocr", type=Path, help="SDA full-page OCR pages.jsonl")
    parser.add_argument("--layout-manifest", type=Path)
    parser.add_argument("--image-dir", type=Path)
    parser.add_argument("--source-pdf", type=Path, help="Optional working PDF, with the same sha256 as PDF OCR")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--semantic-provider", choices=("codex", "rules"), default=None)
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--llm-workers", type=int)
    parser.add_argument("--sequence-workers", type=int)
    parser.add_argument("--llm-timeout", type=int)
    parser.add_argument("--max-repair-rounds", type=int)
    parser.add_argument("--max-answer-questions", type=int)
    parser.add_argument("--no-question-answers", action="store_true")
    parser.add_argument("--no-sequence-evaluation", action="store_true")
    parser.add_argument("--antigen-target")
    parser.add_argument("--keyword-library-dir")
    args = parser.parse_args(argv)
    if args.structured_dir:
        root = args.structured_dir.expanduser().resolve()
        metadata = json.loads((root / "document.json").read_text(encoding="utf-8"))
        identity = metadata["document_id"]
        if args.document_id and args.document_id != identity:
            parser.error("--document-id differs from structured/document.json")
        args.document_id = identity
        args.table_ocr = args.table_ocr or root / "table_ocr/tables.jsonl"
        args.pdf_ocr = args.pdf_ocr or root / "page_ocr/pages.jsonl"
        args.layout_manifest = args.layout_manifest or root / "layout" / f"{identity}_manifest.json"
        args.image_dir = args.image_dir or root / "layout"
    if not args.document_id or args.table_ocr is None or args.pdf_ocr is None:
        parser.error("Supply --structured-dir or --document-id, --table-ocr and --pdf-ocr")
    options = {k: getattr(args, k) for k in ("semantic_provider", "llm_model", "llm_effort", "llm_workers",
               "sequence_workers", "llm_timeout", "max_repair_rounds", "max_answer_questions", "antigen_target", "keyword_library_dir")
               if getattr(args, k) is not None}
    if args.no_question_answers:
        options["question_answer_enabled"] = False
    if args.no_sequence_evaluation:
        options["evaluation_enabled"] = False
    result = run_from_ocr(args.document_id, args.table_ocr, args.pdf_ocr, args.output_dir,
        layout_manifest=args.layout_manifest, image_dir=args.image_dir, source_pdf=args.source_pdf, **options)
    print(json.dumps({"document_id": result["document_id"], "status": result["status"],
                      "records": len(result["records"]), "measurements": len(result["measurements"]),
                      "dataset": str(args.output_dir.resolve() / "dataset.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
