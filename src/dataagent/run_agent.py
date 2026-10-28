#!/usr/bin/env python3
"""CLI entrypoint for the LangGraph patent dataset agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent.graph import build_graph, initial_state
from .agent.schemas import AgentConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the LangGraph patent table agent")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--antigen-target", help="antigen keyword-library ID or alias; otherwise resolve from evidence")
    parser.add_argument("--keyword-library-dir", type=Path, help="directory of maintained per-antigen keyword libraries")
    parser.add_argument("--source-pdf", type=Path)
    parser.add_argument("--existing-ocr-json", type=Path)
    parser.add_argument("--existing-context-dir", type=Path, help="reuse existing caption/method evidence without rerunning OCR")
    parser.add_argument("--semantic-provider", choices=("codex", "rules"), default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-effort", choices=("low", "medium", "high", "xhigh", "max"), default=None)
    parser.add_argument("--llm-batch-size", type=int, default=4)
    parser.add_argument("--no-classification-triage", action="store_true", help="Use detailed classification for every crop")
    parser.add_argument("--llm-workers", type=int, help="0: all tables concurrently; 1-8: bounded batches; default: DATASETAGENT_LLM_WORKERS or 6")
    parser.add_argument("--sequence-workers", type=int, help="concurrent sequence batches (1-8); default: DATASETAGENT_SEQUENCE_WORKERS or 3")
    parser.add_argument("--llm-timeout", type=int, default=600)
    parser.add_argument("--max-repair-rounds", type=int, default=2, help="bounded evidence-repair rounds; 0 disables automatic repair")
    parser.add_argument("--max-repair-actions", type=int, default=2, help="total local table actions, excluding provider transport/schema retries")
    parser.add_argument("--max-answer-questions", type=int, default=12)
    parser.add_argument("--no-question-answers", action="store_true")
    parser.add_argument("--no-sequence-evaluation", action="store_true")
    parser.add_argument("--backend", choices=("auto", "paddleocr", "tesseract"), default="auto")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--device", default=None)
    parser.add_argument("--max-side", type=int, default=2200)
    parser.add_argument("--ocr-workers", type=int, help="independent OCR crop processes; overrides local runtime")
    parser.add_argument("--ocr-threads-per-worker", type=int, help="native CPU threads per OCR process")
    parser.add_argument("--ocr-python", help="isolated Python interpreter for table OCR workers")
    parser.add_argument("--ocr-device", help="table OCR device; overrides --device and local table_device")
    parser.add_argument("--ocr-rec-batch-size", type=int, help="text lines per recognition batch (1-512)")
    parser.add_argument("--ocr-cpu-fallback-workers", type=int, help="CPU workers to recheck rotated/empty GPU crops; 0 disables")
    parser.add_argument("--selection-mode", choices=("heuristic", "all"), default="heuristic")
    parser.add_argument(
        "--target-roles",
        nargs="+",
        default=["sequence_reference", "sequence_variant", "binding_assay_ic50"],
    )
    parser.add_argument("--selection-threshold", type=float, default=0.45)
    parser.add_argument("--run-ocr", action="store_true", help="run OCR on selected crops")
    parser.add_argument("--no-page-index", action="store_true", help="disable background full-document page search")
    parser.add_argument("--page-index-workers", type=int, help="concurrent single-threaded page OCR processes (default 6)")
    parser.add_argument("--page-index-max-side", type=int, default=1600)
    parser.add_argument("--page-detail-max-side", type=int, default=2400)
    parser.add_argument("--page-detail-limit", type=int, default=32, help="maximum retrieved pages to read; overflow remains pending review")
    parser.add_argument(
        "--no-sequence-page-ocr",
        action="store_true",
        help="skip targeted full-page OCR for FASTA-style sequence definitions",
    )
    parser.add_argument("--codex-decisions", type=Path, help="JSON decisions supplied by Codex")
    parser.add_argument("--no-copy-images", action="store_true")
    parser.add_argument("--markdown-name", help="Markdown filename; defaults to <document-id>.md")
    parser.add_argument("--reference-dataset", type=Path, help="optional CSV used as a seed or entity whitelist")
    parser.add_argument(
        "--reference-entities-only",
        action="store_true",
        help="keep OCR records whose construct matches an entity in --reference-dataset; do not add CSV rows",
    )
    args = parser.parse_args()
    from .ocr_runtime import load_runtime
    ocr_runtime = load_runtime()
    config = AgentConfig(
        document_id=args.document_id,
        antigen_target=args.antigen_target,
        keyword_library_dir=str(args.keyword_library_dir.resolve()) if args.keyword_library_dir else None,
        input_dir=str(args.input_dir.resolve()),
        output_dir=str(args.output_dir.resolve()),
        source_pdf=str(args.source_pdf.resolve()) if args.source_pdf else None,
        existing_ocr_json=str(args.existing_ocr_json.resolve()) if args.existing_ocr_json else None,
        existing_context_dir=str(args.existing_context_dir.resolve()) if args.existing_context_dir else None,
        **{key: value for key, value in {
            "semantic_provider": args.semantic_provider, "llm_model": args.llm_model,
            "llm_effort": args.llm_effort,
            "llm_workers": args.llm_workers,
            "sequence_workers": args.sequence_workers,
            "page_index_workers": args.page_index_workers,
            "page_index_enabled": False if args.no_page_index else None,
        }.items() if value is not None},
        llm_batch_size=args.llm_batch_size,
        classification_triage=not args.no_classification_triage,
        llm_timeout=args.llm_timeout,
        max_repair_rounds=args.max_repair_rounds,
        max_repair_actions=args.max_repair_actions,
        question_answer_enabled=not args.no_question_answers,
        evaluation_enabled=not args.no_sequence_evaluation,
        max_answer_questions=args.max_answer_questions,
        backend=args.backend,
        lang=args.lang,
        device=args.device or ocr_runtime.get("device"),
        max_side=args.max_side,
        ocr_workers=args.ocr_workers if args.ocr_workers is not None else int(ocr_runtime.get("workers", 1)),
        ocr_threads_per_worker=args.ocr_threads_per_worker if args.ocr_threads_per_worker is not None else int(ocr_runtime.get("threads_per_worker", 2)),
        ocr_python=args.ocr_python or ocr_runtime.get("python"),
        ocr_device=args.ocr_device or args.device or ocr_runtime.get("table_device"),
        ocr_rec_batch_size=args.ocr_rec_batch_size if args.ocr_rec_batch_size is not None else ocr_runtime.get("rec_batch_size"),
        ocr_cpu_fallback_workers=args.ocr_cpu_fallback_workers if args.ocr_cpu_fallback_workers is not None else int(ocr_runtime.get("cpu_fallback_workers", 0)),
        selection_mode=args.selection_mode,
        target_roles=args.target_roles,
        selection_threshold=args.selection_threshold,
        run_ocr=args.run_ocr,
        page_index_max_side=args.page_index_max_side,
        page_detail_max_side=args.page_detail_max_side,
        page_detail_limit=args.page_detail_limit,
        run_sequence_page_ocr=not args.no_sequence_page_ocr,
        copy_images=not args.no_copy_images,
        codex_decisions=str(args.codex_decisions.resolve()) if args.codex_decisions else None,
        markdown_name=args.markdown_name or f"{args.document_id}.md",
        reference_dataset=str(args.reference_dataset.resolve()) if args.reference_dataset else None,
        reference_entities_only=args.reference_entities_only,
    )
    try:
        result = build_graph().invoke(initial_state(config))
    except Exception as exc:
        from .llm_runtime import json_write

        json_write(Path(config.output_dir) / "run_error.json", {
            "type": type(exc).__name__, "message": str(exc),
            "error_kind": getattr(exc, "error_kind", "pipeline"),
        })
        raise
    classification_labels = sorted({
        str(item.get("primary_label", "ambiguous"))
        for item in result.get("table_classifications", [])
    })
    print(json.dumps({
        "run_id": result["run_id"],
        "candidate_count": len(result.get("candidates", [])),
        "selected_count": len(result.get("selected_table_ids", [])),
        "document_block_count": len(result.get("document_blocks", [])),
        "table_context_count": len(result.get("table_contexts", [])),
        "table_classification_counts": {
            label: sum(
                item.get("primary_label") == label
                for item in result.get("table_classifications", [])
            )
            for label in classification_labels
        },
        "retained_table_count": sum(
            item.get("retain_for_pipeline", False)
            for item in result.get("table_classifications", [])
        ),
        "semantic_provider": config.semantic_provider,
        "llm_model": config.llm_model if config.semantic_provider == "codex" else None,
        "llm_effort": config.llm_effort if config.semantic_provider == "codex" else None,
        "llm_workers": config.llm_workers if config.semantic_provider == "codex" else None,
        "sequence_workers": config.sequence_workers if config.semantic_provider == "codex" else None,
        "llm_call_count": len(result.get("llm_runs", [])),
        "llm_cache_hit_count": sum(item.get("cache_hit", False) for item in result.get("llm_runs", [])),
        "context_linked_count": sum(
            context.get("context_status") == "linked"
            for context in result.get("table_contexts", [])
        ),
        "sequence_count": len(result.get("sequences", [])),
        "template_status": result.get("template_validation", {}).get("status"),
        "template_confidence": result.get("template_validation", {}).get("confidence"),
        "construct_validation_count": len(result.get("construct_validations", [])),
        "construct_consistent_count": sum(
            item.get("pair_status") == "consistent"
            for item in result.get("construct_validations", [])
        ),
        "agent_review_counts": {
            status: sum(
                record.get("agent_review_status") == status
                for record in result.get("dataset_records", [])
            )
            for status in ("passed", "error", "manual_review")
        },
        "anarci_review_counts": {
            status: sum(
                record.get("anarci_review_status") == status
                for record in result.get("dataset_records", [])
            )
            for status in ("passed", "error", "manual_review")
        },
        "esm2_review_counts": {
            status: sum(
                record.get("esm2_review_status") == status
                for record in result.get("dataset_records", [])
            )
            for status in ("passed", "error", "manual_review")
        },
        "review_count": len(result.get("review_tasks", [])),
        "quality_warning_count": len(result.get("quality_warnings", [])),
        "dataset_record_count": len(result.get("dataset_records", [])),
        "record_verification": result.get("record_verification", {}).get("summary", {}),
        "repair_state": result.get("repair_state", {}),
        "artifacts": result.get("artifacts", {}),
        "context_errors": result.get("context_errors", []),
        "errors": result.get("errors", []),
    }, ensure_ascii=False, indent=2))
    return 0 if not result.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
