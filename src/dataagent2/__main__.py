"""Run with PYTHONPATH=src python -m dataagent2 --help."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .inputs import discover, load_input, make_chunks
from .pipeline import run
from .provider import Provider, write_json


def main():
    parser = argparse.ArgumentParser(description="One-pass patent evidence extraction benchmark")
    parser.add_argument("--documents", nargs="+", default=["WO2026086808A1", "WO2024020591A9"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path, help="Explicit original native run; requires one document")
    parser.add_argument("--reference-result", type=Path)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--chunk-chars", type=int, default=24000)
    parser.add_argument("--workers", type=int, default=0,
                        help="Reader concurrency; 0 (default) starts all chunks together")
    parser.add_argument("--document-workers", type=int, default=1)
    parser.add_argument("--max-questions", type=int, default=8)
    parser.add_argument("--question-chars", type=int, default=14000)
    parser.add_argument("--max-chunks", type=int, help="Smoke test only, not a full cost comparison")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--request-interval", type=float, default=0,
                        help="Minimum seconds between model attempt starts, shared across documents")
    parser.add_argument("--plan-only", action="store_true", help="Inspect input sizes; no model calls/token estimates")
    args = parser.parse_args()
    if (args.workers < 0 or min(args.document_workers, args.timeout) < 1
            or args.max_questions < 0 or args.request_interval < 0):
        parser.error("Reader workers/question budget/interval must be nonnegative; document workers/timeout must be positive")
    if args.native_dir and len(args.documents) != 1:
        parser.error("--native-dir requires exactly one document")
    args.output.mkdir(parents=True, exist_ok=True)

    def execute(doc):
        native, reference = ((args.native_dir, args.reference_result) if args.native_dir else discover(args.root, doc))
        data = load_input(native, reference)
        if data["document_id"] != doc:
            raise ValueError("Requested document differs from native inputs")
        if args.plan_only:
            chunks = make_chunks(data, args.chunk_chars)
            plan = {"document": doc, "sources": len(data["sources"]), "chunks": len(chunks),
                "source_text_characters": sum(len(s["text"]) for s in data["sources"].values()),
                "seed_entities": len(data["seed"]["entities"]), "seed_observations": len(data["seed"]["observations"]),
                "baseline": data["baseline"], "native": str(native),
                "note": "Character sizes are not token estimates. Live usage is required."}
            write_json(args.output / doc / "plan.json", plan)
            return plan
        output = args.output / doc
        output.mkdir(exist_ok=True)
        provider = Provider(output, model=args.model, effort=args.effort, timeout=args.timeout,
                            request_interval=args.request_interval)
        return run(data, provider, output, chunk_chars=args.chunk_chars, workers=args.workers,
                   max_questions=args.max_questions, question_chars=args.question_chars, max_chunks=args.max_chunks)

    reports, errors = [], {}
    with ThreadPoolExecutor(max_workers=args.document_workers) as pool:
        futures = {pool.submit(execute, doc): doc for doc in args.documents}
        for future in as_completed(futures):
            doc = futures[future]
            try:
                report = future.result()
                reports.append(report)
                if args.plan_only or report.get("benchmark_complete"):
                    print(f"FINISHED {doc}: {report.get('comparable_model_total_tokens', 'plan only')}", flush=True)
                else:
                    coverage = report["coverage"]
                    errors[doc] = (f"Incomplete benchmark: {coverage['completed_chunks']}/"
                                   f"{coverage['total_chunks']} reader chunks, "
                                   f"{report.get('question_api_failures', 0)} failed answers; "
                                   "resume the same output directory")
                    print(f"PARTIAL {doc}: {errors[doc]}", flush=True)
            except Exception as exc:
                errors[doc] = f"{type(exc).__name__}: {exc}"
                print(f"FAILED {doc}: {errors[doc]}", flush=True)
            write_json(args.output / "comparison.json", {"reports": reports, "errors": errors})
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
