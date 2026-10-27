"""LangGraph workflow for patent table triage and dataset preparation.

The semantic agents use a LangChain Codex adapter by default. OCR,
source validation and biological checks remain ordinary reproducible tools.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agent_store import publish_run
from ..page_ocr import extract_assay_method_pages
from ..construct_validation import apply_construct_validations, validate_construct_pairs
from ..dataset_linker import link_dataset
from ..records import verify_records, assess_candidate, plan_repairs
from ..document_context import (
    context_text,
    extract_document_blocks,
    link_table_contexts,
    render_context_images,
)
from ..sequence_validation import validate_ocr_results, apply_sequence_links
from ..sequence_resolver import resolve_sequences
from ..sequence_evidence import collect_sequence_reference_passages, attach_sequence_locations
from ..table_classification import (
    TableClassificationAgent,
    route_ocr_results,
    load_ocr_text,
    parse_crop_metadata,
    triage_text,
)
from ..template_agent import TemplateAgent, enrich_table_contexts_with_assay_template

from .decision_provider import get_decision_provider
from .schemas import AgentConfig, ReviewTask, TableCandidate


OCR_CHECKPOINT_VERSION = 3


def _image_roots(config, state):
    roots = [Path(config.output_dir) / "tables", Path(config.input_dir)]
    if config.existing_ocr_json:
        roots.append(Path(config.existing_ocr_json).parent / "tables")
    if config.sda_input_file:
        roots.extend(Path(table["image_path"]).parent for table in state.get("ocr_results", [])
                     if table.get("image_path"))
    return list(dict.fromkeys(roots))


class AgentState(TypedDict, total=False):
    run_id: str
    config: dict[str, Any]
    candidates: list[dict[str, Any]]
    selected_table_ids: list[str]
    document_blocks: list[dict[str, Any]]
    table_contexts: list[dict[str, Any]]
    context_errors: list[str]
    ocr_results: list[dict[str, Any]]
    table_classifications: list[dict[str, Any]]
    llm_runs: list[dict[str, Any]]
    assay_method_pages: list[dict[str, Any]]
    document_index: dict[str, Any]
    page_retrieval: dict[str, Any]
    sequence_reference_passages: list[dict[str, Any]]
    document_structure: dict[str, Any]
    sequence_extraction: dict[str, Any]
    question_review: dict[str, Any]
    sequence_evaluation: dict[str, Any]
    sequences: list[dict[str, Any]]
    assays: list[dict[str, Any]]
    document_template: dict[str, Any]
    template_validation: dict[str, Any]
    resolved_sequences: list[dict[str, Any]]
    construct_validations: list[dict[str, Any]]
    dataset_records: list[dict[str, Any]]
    record_verification: dict[str, Any]
    repair_plan: dict[str, Any]
    repair_state: dict[str, Any]
    repair_history: list[dict[str, Any]]
    review_tasks: list[dict[str, Any]]
    quality_warnings: list[dict[str, Any]]
    artifacts: dict[str, str]
    errors: list[str]


def discover_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    if config.sda_input_file:
        from ..sda_inputs import read_prepared
        prepared = read_prepared(config.sda_input_file, config.document_id)
        return {"candidates": prepared["candidates"], "ocr_results": prepared["tables"]}
    input_dir = Path(config.input_dir)
    metadata = parse_crop_metadata(input_dir)
    images = sorted(input_dir.glob("table-*.png"), key=lambda path: path.name)
    ocr_json_path = Path(config.existing_ocr_json) if config.existing_ocr_json else None
    ocr_text = load_ocr_text(ocr_json_path)
    existing_ocr_results: list[dict[str, Any]] = []
    if ocr_json_path and ocr_json_path.is_file():
        try:
            payload = json.loads(ocr_json_path.read_text(encoding="utf-8"))
            existing_ocr_results = payload if isinstance(payload, list) else []
        except (OSError, json.JSONDecodeError):
            existing_ocr_results = []
    candidates: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        item = metadata.get(image.name, {})
        page = item.get("page")
        match = re.search(r"page-(\d+)", image.stem)
        if page is None and match:
            page = int(match.group(1))
        table_id = str(item.get("id") or image.stem)
        candidates.append(
            TableCandidate(
                table_id=table_id,
                image=image.name,
                image_path=str(image),
                page=int(page) if page is not None else None,
                bbox=item.get("bbox", []),
                crop_confidence=item.get("confidence"),
                ocr_text=ocr_text.get(image.name, ""),
            ).model_dump()
        )
    return {"candidates": candidates, "ocr_results": existing_ocr_results}


def context_node(state: AgentState) -> dict[str, Any]:
    """Attach coordinate-backed captions and referring prose to each table."""

    config = AgentConfig.model_validate(state["config"])
    if config.sda_input_file:
        from ..sda_inputs import read_prepared
        prepared = read_prepared(config.sda_input_file, config.document_id)
        return {"document_blocks": prepared["document_blocks"],
                "table_contexts": prepared["table_contexts"], "context_errors": []}
    if config.existing_context_dir:
        root = Path(config.existing_context_dir)
        try:
            blocks = json.loads((root / "document_blocks.json").read_text(encoding="utf-8"))
            contexts = json.loads((root / "table_contexts.json").read_text(encoding="utf-8"))
            if not isinstance(blocks, list) or not isinstance(contexts, list):
                raise ValueError("Existing document evidence must contain lists")
            return {"document_blocks": blocks, "table_contexts": contexts, "context_errors": []}
        except (OSError, ValueError):
            pass
    if not config.source_pdf:
        return {"document_blocks": [], "table_contexts": [], "context_errors": []}
    pdf_path = Path(config.source_pdf)
    if not pdf_path.is_file():
        return {
            "document_blocks": [],
            "table_contexts": [],
            "context_errors": [f"Source PDF not found: {pdf_path}"],
        }
    try:
        blocks = extract_document_blocks(pdf_path)
        contexts = link_table_contexts(state.get("candidates", []), blocks)
        render_context_images(pdf_path, contexts, Path(config.output_dir) / "context_images")
        by_id = {str(context.get("table_id")): context for context in contexts}
        candidates: list[dict[str, Any]] = []
        for raw in state.get("candidates", []):
            candidate = TableCandidate.model_validate(raw)
            context = by_id.get(candidate.table_id, {})
            candidate.logical_table_id = context.get("logical_table_id")
            candidate.table_number = context.get("table_number")
            candidates.append(candidate.model_dump())
        return {
            "candidates": candidates,
            "document_blocks": blocks,
            "table_contexts": contexts,
            "context_errors": [],
        }
    except Exception as exc:
        # Context is additive evidence.  A malformed text layer must not stop
        # the existing OCR/sequence pipeline from producing a reviewable run.
        return {
            "document_blocks": [],
            "table_contexts": [],
            "context_errors": [f"Context extraction failed: {type(exc).__name__}: {exc}"],
        }


def triage_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    decision_provider = get_decision_provider(config.codex_decisions)
    contexts = {
        str(context.get("table_id")): context
        for context in state.get("table_contexts", [])
    }
    updated: list[dict[str, Any]] = []
    for raw in state.get("candidates", []):
        candidate = TableCandidate.model_validate(raw)
        linked_context = context_text(contexts.get(candidate.table_id, {}))
        triage_input = " ".join(value for value in (candidate.ocr_text, linked_context) if value)
        roles, score, reasons = triage_text(triage_input, config.target_roles)
        if linked_context:
            reasons.append("coordinate-linked table context")
        decision = decision_provider.decide(
            table_id=candidate.table_id,
            image=candidate.image,
            default_roles=roles,
            default_score=score,
            default_reasons=reasons,
        )
        roles = decision["roles"]
        score = decision["selection_score"]
        reasons = decision["reasons"]
        candidate.roles = roles
        candidate.selection_score = score
        candidate.reasons = reasons
        updated.append(candidate.model_dump())
    return {"candidates": updated}


def select_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    contexts = {
        str(context.get("table_id")): context
        for context in state.get("table_contexts", [])
    }
    selected: list[str] = []
    candidates: list[dict[str, Any]] = []
    for raw in state.get("candidates", []):
        candidate = TableCandidate.model_validate(raw)
        selection_text = " ".join(
            value
            for value in (candidate.ocr_text, context_text(contexts.get(candidate.table_id, {})))
            if value
        )
        has_sequence_text = bool(re.search(r"seq\s*id|amino\s*acid|\bvh\b|\bvl\b|sequence", selection_text, re.I))
        has_assay_text = bool(re.search(r"ic\s*50|ec\s*50|\bkd\b|\bnm\b|\bpm\b", selection_text, re.I))
        roles_are_targeted = (
            "sequence_reference" in candidate.roles
            or has_assay_text
            or ("sequence_variant" in candidate.roles and has_sequence_text)
        )
        if config.selection_mode == "all":
            candidate.selected = True
        else:
            candidate.selected = (
                candidate.selection_score >= config.selection_threshold
                and any(role in config.target_roles for role in candidate.roles)
                and roles_are_targeted
            )
        if candidate.selected:
            candidate.status = "selected"
            selected.append(candidate.table_id)
        elif candidate.selection_score > 0:
            candidate.status = "review"
        else:
            candidate.status = "candidate"
        candidates.append(candidate.model_dump())
    if not selected and config.selection_mode == "heuristic":
        # Empty selection is a reviewable result, not an implicit full OCR run.
        pass
    return {"candidates": candidates, "selected_table_ids": selected}


def extract_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    if not config.run_ocr:
        return {}
    try:
        from ..table_ocr import TableResult, _crop_metadata, extract_one, make_backend, write_artifacts

        backend = None
        input_dir = Path(config.input_dir)
        metadata = _crop_metadata(input_dir)
        by_id = {candidate["table_id"]: candidate for candidate in state.get("candidates", [])}
        output_dir = Path(config.output_dir)
        image_root = output_dir / "tables" if config.copy_images else input_dir
        image_root.mkdir(parents=True, exist_ok=True)
        checkpoint_path = output_dir / ".ocr_checkpoint.json"
        selected_table_ids = state.get("selected_table_ids", [])
        # Changing the inference runtime must not reuse a different runtime's
        # partial output. Source fingerprints also detect replaced crops.
        checkpoint_key = {
            "backend": config.backend, "lang": config.lang,
            "device": config.ocr_device or config.device, "python": config.ocr_python,
            "max_side": config.max_side, "rec_batch_size": config.ocr_rec_batch_size,
            "cpu_fallback": bool(config.ocr_cpu_fallback_workers),
            "sources": [{"table_id": key, "path": str(Path(by_id[key]["image_path"]).resolve()),
                         "size": Path(by_id[key]["image_path"]).stat().st_size,
                         "mtime_ns": Path(by_id[key]["image_path"]).stat().st_mtime_ns}
                        for key in selected_table_ids],
        }

        restore_result = TableResult.from_dict

        restored: list[Any] = []
        if checkpoint_path.is_file():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if (
                    checkpoint.get("schema_version") == OCR_CHECKPOINT_VERSION
                    and checkpoint.get("selected_table_ids") == selected_table_ids
                    and checkpoint.get("key") == checkpoint_key
                ):
                    restored = [restore_result(raw) for raw in checkpoint.get("results", [])]
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                restored = []
        restored_by_id = {
            str(result.source_id or result.image): result
            for result in restored
        }

        def save_checkpoint(values: list[Any]) -> None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = checkpoint_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": OCR_CHECKPOINT_VERSION,
                        "key": checkpoint_key,
                        "selected_table_ids": selected_table_ids,
                        "results": [value.as_dict() for value in values],
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            temporary.replace(checkpoint_path)

        results: list[dict[str, Any]] = []
        table_results: list[Any] = []
        total_tables = len(selected_table_ids)
        by_result: dict[str, Any] = {}
        pending: list[dict[str, Any]] = []
        for number, table_id in enumerate(selected_table_ids, start=1):
            candidate = by_id[table_id]
            image_path = Path(candidate["image_path"])
            if config.copy_images:
                shutil.copy2(image_path, image_root / image_path.name)
            result = restored_by_id.get(str(table_id)) or restored_by_id.get(image_path.name)
            if result is not None:
                by_result[table_id] = result
            else:
                pending.append({"table_id": table_id, "image_path": str(image_path), "number": number})

        def accept(table_id: str, result: Any) -> None:
            by_result[table_id] = result
            # Completion order may differ; checkpoint and final export always
            # follow the original selection order, preserving source ordinals.
            ordered = [by_result[key] for key in selected_table_ids if key in by_result]
            save_checkpoint(ordered)
            print(f"ocr-table {len(by_result)}/{total_tables}: {result.image}", flush=True)

        if by_result:
            print(f"ocr-table {len(by_result)}/{total_tables}: restored checkpoint", flush=True)
        if pending and (config.ocr_workers > 1 or config.ocr_python or config.ocr_cpu_fallback_workers):
            from ..ocr_runtime import extract_parallel
            for table_id, result in extract_parallel(
                pending, metadata, output_dir, workers=config.ocr_workers,
                threads=config.ocr_threads_per_worker, backend=config.backend,
                lang=config.lang, device=config.ocr_device or config.device, max_side=config.max_side,
                python=config.ocr_python, rec_batch_size=config.ocr_rec_batch_size,
                cpu_fallback_workers=config.ocr_cpu_fallback_workers,
            ):
                accept(table_id, result)
        else:
            for task in pending:
                if backend is None:
                    backend = make_backend(config.backend, config.lang, config.ocr_device or config.device,
                                           config.max_side, config.ocr_rec_batch_size)
                accept(task["table_id"], extract_one(Path(task["image_path"]), metadata, backend, task["number"]))
        table_results = [by_result[key] for key in selected_table_ids]
        results = [result.as_dict() for result in table_results]
        if config.run_sequence_page_ocr and config.source_pdf:
            try:
                from ..page_ocr import extract_sequence_page_results  # type: ignore

                def sequence_backend():
                    nonlocal backend
                    if backend is None:
                        backend = make_backend(config.backend, config.lang, config.device, config.max_side)
                    return backend
                sequence_page_results = extract_sequence_page_results(
                    Path(config.source_pdf),
                    image_root,
                    output_dir,
                    backend,
                    table_results,
                    max_side=max(config.max_side, 2600),
                    backend_factory=sequence_backend,
                )
                table_results.extend(sequence_page_results)
                results.extend(result.as_dict() for result in sequence_page_results)
                print(f"ocr-sequence-pages: {len(sequence_page_results)} matched", flush=True)
            except Exception as exc:
                # Sequence-page OCR is additive. Keep the table extraction
                # usable and make the missing evidence visible in the log.
                print(
                    f"warning: sequence-page OCR failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
        artifact_paths = write_artifacts(
            table_results,
            output_dir,
            title=input_dir.name.removesuffix("_tables"),
            markdown_name=config.markdown_name or f"{config.document_id}.md",
            input_dir=input_dir,
            image_root=image_root,
            backend_name=(backend.name if backend is not None else table_results[0].backend),
            source_pdf=Path(config.source_pdf) if config.source_pdf else None,
        )
        performance = output_dir / "ocr_performance.json"
        if performance.is_file():
            artifact_paths["ocr_performance"] = str(performance)
        sequence_page_artifact = output_dir / "sequence_page_ocr.json"
        if sequence_page_artifact.is_file():
            artifact_paths["sequence_page_ocr"] = str(sequence_page_artifact)
        return {"ocr_results": results, "artifacts": artifact_paths}
    except Exception as exc:  # keep the graph publishable with a review task
        return {
            "errors": [f"OCR failed: {type(exc).__name__}: {exc}"],
            "review_tasks": [
                ReviewTask(
                    task_id=f"ocr-{uuid.uuid4().hex[:10]}",
                    kind="ocr",
                    severity="high",
                    message=str(exc),
                ).model_dump()
            ],
        }


def validate_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    routed_results = route_ocr_results(
        state.get("ocr_results", []),
        state.get("table_classifications", []),
    )
    sequences, assays, issues = validate_ocr_results(
        config.document_id,
        routed_results,
        state.get("document_template"),
    )
    # The generic validator sees wrapped sequence fragments and can flag them
    # even when the patent's explicit SEQ-ID resolver has reconstructed a
    # complete VH/VL pair.  Once the resolver has authoritative full-chain
    # evidence, do not create duplicate false-positive review tasks for those
    # rows.  In reference-entity mode, ignore unrelated sequence tables too.
    resolved = resolve_sequences(
        state.get("ocr_results", []) if "sequence_extraction" in state.get("document_template", {}) else routed_results,
        state.get("document_template"),
    )
    authoritative_tables = {
        table_id
        for item in resolved.values()
        for table_id in item.get("sequence_table_ids", [])
    }
    if config.reference_entities_only:
        issues = [issue for issue in issues if issue.get("table_id") in authoritative_tables]
    issues = [issue for issue in issues if issue.get("table_id") not in authoritative_tables]
    review_tasks = list(state.get("review_tasks", []))
    review_tasks.extend(ReviewTask(
        task_id=f"sequence-{index}-{uuid.uuid4().hex[:8]}",
        kind="sequence",
        severity=issue.get("severity", "high"),
        message=issue.get("message", "Sequence requires review."),
        table_id=issue.get("table_id"),
    ).model_dump() for index, issue in enumerate(issues))
    return {"sequences": sequences, "assays": assays, "review_tasks": review_tasks}


def link_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    routed_results = route_ocr_results(
        state.get("ocr_results", []),
        state.get("table_classifications", []),
    )
    resolved = resolve_sequences(
        state.get("ocr_results", []) if "sequence_extraction" in state.get("document_template", {}) else routed_results,
        state.get("document_template"),
    )
    records = link_dataset(
        config.document_id,
        routed_results,
        config.reference_dataset,
        config.reference_entities_only,
        state.get("document_template"),
        sequence_sources=state.get("ocr_results", []) if "sequence_extraction" in state.get("document_template", {}) else None,
    )
    attach_sequence_locations(resolved, records, state.get("ocr_results", []) if "sequence_extraction" in state.get("document_template", {}) else routed_results, state.get("assay_method_pages", []),
                              state.get("candidates", []), config.source_pdf,
                              sources={**state.get("document_structure", {}).get("sources", {}),
                                       **state.get("sequence_extraction", {}).get("sources", {})})
    return {"resolved_sequences": list(resolved.values()), "dataset_records": records}


def template_node(state: AgentState) -> dict[str, Any]:
    """Infer and validate a document-level semantic template after OCR."""

    config = AgentConfig.model_validate(state["config"])
    routed_results = route_ocr_results(
        state.get("ocr_results", []),
        state.get("table_classifications", []),
    )
    provider = None
    sequence_passages = collect_sequence_reference_passages(state.get("document_blocks", []))
    if config.semantic_provider == "codex":
        from ..llm_runtime import provider_from_env
        from ..llm_table_agents import LLMTemplateAgent

        provider = provider_from_env(Path(config.output_dir), model=config.llm_model,
                                     effort=config.llm_effort, timeout=config.llm_timeout)
        agent = LLMTemplateAgent(provider)
    else:
        agent = TemplateAgent()
    template, validation = agent.run(
        config.document_id,
        routed_results,
        state.get("assay_method_pages", []),
        state.get("table_contexts", []),
        **({"sequence_passages": sequence_passages} if provider else {}),
        antigen_target=config.antigen_target,
        keyword_library_dir=config.keyword_library_dir,
    )
    return {
        "document_template": template,
        "template_validation": validation,
        "sequence_reference_passages": sequence_passages,
        "llm_runs": [*state.get("llm_runs", []), *(provider.calls if provider else [])],
        "table_contexts": enrich_table_contexts_with_assay_template(
            state.get("table_contexts", []), template
        ),
    }


def sequence_extract_node(state: AgentState) -> dict[str, Any]:
    """Expose all raw evidence, then let an agent choose chains and actual values."""
    import copy
    from ..llm_runtime import json_write, provider_from_env
    from ..sequence_evidence import build_document_structure, write_structure_artifacts
    from ..llm_sequence_agent import SequenceExtractionAgent
    from ..llm_runtime import excluded_tables

    config = AgentConfig.model_validate(state["config"])
    excluded = excluded_tables()
    available_tables = [table for table in state.get("ocr_results", [])
                        if str(table.get("source_id") or table.get("image")) not in excluded]
    graph = build_document_structure(config.document_id, available_tables,
        blocks=state.get("document_blocks", []), contexts=state.get("table_contexts", []),
        methods=state.get("assay_method_pages", []), passages=state.get("sequence_reference_passages", []),
        classifications=state.get("table_classifications", []))
    root = Path(config.output_dir)
    artifacts = {**state.get("artifacts", {}), **write_structure_artifacts(graph, root)}
    if config.semantic_provider != "codex" or not config.extract_sequences:
        return {"document_structure": graph, "artifacts": artifacts}
    provider = provider_from_env(root, model=config.llm_model, effort=config.llm_effort, timeout=config.llm_timeout)
    extraction = SequenceExtractionAgent(provider, max_workers=config.sequence_workers).run(graph, state.get("document_template", {}))
    extraction["agent"].update(model=config.llm_model, reasoning_effort=config.llm_effort)
    json_write(root / "sequence_extraction.json", extraction)
    artifacts["sequence_extraction"] = str(root / "sequence_extraction.json")
    artifacts["sequence_performance"] = str(root / "sequence_performance.json")
    artifacts["sequence_progress"] = str(root / "sequence_progress.json")
    if extraction.get("identity_review"):
        artifacts["sequence_identity_review"] = str(root / "sequence_identity_review.json")
    template = copy.deepcopy(state.get("document_template", {}))
    template["sequence_extraction"] = extraction
    validation = copy.deepcopy(state.get("template_validation", {}))
    validation["sequence_extraction_status"] = extraction["status"]
    warnings = [{"code": "sequence_agent_" + item["status"], "severity": "medium",
                 "message": f"{item['entity_id']} {item['chain']}: {item['rationale']}",
                 "evidence_refs": item["evidence_refs"], "table_ids": []}
                for item in extraction["decisions"] if item["status"] != "resolved"]
    warnings.extend({"code": "table_text_mismatch", "severity": "medium",
                     "message": f"{item['entity_id']} {item['chain']}: 表格和文字不一致；以表格单元格为金标准。",
                     "evidence_refs": [conflict["table_ref"], conflict["text_ref"]],
                     "entity_id": item["entity_id"], "chain": item["chain"], "table_ids": []}
                    for item in extraction["decisions"] for conflict in item.get("source_conflicts", []))
    if extraction.get("identity_review", {}).get("status") == "needs_review":
        warnings.append({"code": "sequence_identity_review_incomplete", "severity": "medium",
                         "message": "部分 VH/VL 同一性问题尚未完成 Codex 核验，保留原有待复核状态。",
                         "evidence_refs": [], "table_ids": []})
    validation["warnings"] = [*validation.get("warnings", []), *warnings]
    if warnings:
        validation["status"] = "needs_review"
    return {"document_structure": graph, "sequence_extraction": extraction, "document_template": template,
            "template_validation": validation, "artifacts": artifacts,
            "llm_runs": [*state.get("llm_runs", []), *provider.calls]}


def index_document_node(state: AgentState) -> dict[str, Any]:
    """Build a local search index concurrently with semantic classification."""
    from ..page_index import build_page_index

    config = AgentConfig.model_validate(state["config"])
    if config.sda_input_file:
        from ..sda_inputs import read_prepared
        return {"document_index": read_prepared(config.sda_input_file, config.document_id)["index_summary"]}
    if not config.page_index_enabled or not config.source_pdf:
        return {"document_index": {"status": "disabled"}}
    try:
        summary = build_page_index(Path(config.source_pdf), Path(config.output_dir),
            workers=config.page_index_workers, max_side=config.page_index_max_side, lang=config.lang)
    except Exception as exc:
        summary = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return {"document_index": summary}


def assay_method_context_node(state: AgentState) -> dict[str, Any]:
    """Retrieve detail evidence, retaining the legacy method OCR as fallback."""

    config = AgentConfig.model_validate(state["config"])
    routed_results = route_ocr_results(state.get("ocr_results", []), state.get("table_classifications", []))
    if config.sda_input_file:
        from ..sda_inputs import retrieve_existing_pages
        return retrieve_existing_pages(config, routed_results, state.get("table_contexts", []))
    index = state.get("document_index", {})
    fallback: dict[str, Any] = {}
    if index.get("status") == "ready" and config.source_pdf:
        from ..page_index import retrieve_page_evidence

        try:
            evidence = retrieve_page_evidence(Path(config.source_pdf), Path(config.output_dir),
                index, routed_results, workers=config.page_index_workers,
                max_side=config.page_detail_max_side, limit=config.page_detail_limit, lang=config.lang,
                contexts=state.get("table_contexts", []))
            # Replace only pages read successfully; never duplicate native and OCR text.
            new_blocks = evidence["document_blocks"]
            replaced = {block["page"] for block in new_blocks}
            evidence["document_blocks"] = sorted(
                [b for b in state.get("document_blocks", []) if b.get("page") not in replaced] + new_blocks,
                key=lambda b: (b.get("page", 0), (b.get("bbox") or [0, 0])[1]))
            evidence["artifacts"] = {**state.get("artifacts", {}), **evidence["artifacts"]}
            return evidence
        except Exception as exc:
            index = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if index.get("status") == "failed":
        message = "Page search unavailable; using legacy method window: " + index["error"]
        fallback = {"page_retrieval": {"status": "fallback", "warnings": [
            {"code": "page_index_failed", "severity": "medium", "message": message, "table_ids": []}]},
            "context_errors": [*state.get("context_errors", []), message]}
    if config.existing_context_dir:
        path = Path(config.existing_context_dir) / "assay_method_pages.json"
        if path.is_file():
            pages = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(pages, list):
                return {**fallback, "assay_method_pages": pages}
    routed_results = route_ocr_results(
        state.get("ocr_results", []),
        state.get("table_classifications", []),
    )
    if not config.source_pdf or not routed_results:
        return {**fallback, "assay_method_pages": []}
    source_pdf = Path(config.source_pdf)
    if not source_pdf.is_file():
        return {**fallback, "assay_method_pages": []}
    try:
        pages = extract_assay_method_pages(
            source_pdf,
            Path(config.output_dir),
            routed_results,
            lang=config.lang,
            device=config.device,
        )
        return {**fallback, "assay_method_pages": pages}
    except Exception as exc:
        return {
            **fallback,
            "assay_method_pages": [],
            "context_errors": [
                *fallback.get("context_errors", state.get("context_errors", [])),
                f"Assay-method OCR failed: {type(exc).__name__}: {exc}",
            ],
        }


def construct_validation_node(state: AgentState) -> dict[str, Any]:
    """Check that each plausible VH/VL pair matches its construct evidence."""

    validations = validate_construct_pairs(
        state.get("resolved_sequences", []),
        state.get("dataset_records", []),
    )
    records = apply_construct_validations(state.get("dataset_records", []), validations)
    return {
        "construct_validations": validations,
        "dataset_records": records,
        "review_tasks": [*state.get("review_tasks", []), *_construct_review_tasks(validations)],
    }


def _construct_review_tasks(validations: list[dict]) -> list[dict]:
    review_tasks = []
    for validation in validations:
        if validation.get("overall_review_status") == "passed":
            continue
        issues = validation.get("issues", [])
        statuses = {
            source: validation.get(f"{source}_review", {}).get("status", "manual_review")
            for source in ("agent", "anarci", "esm2")
        }
        review_tasks.append(
            {**ReviewTask(
                task_id=f"construct-{validation['construct_key']}-{uuid.uuid4().hex[:8]}",
                kind="construct_pair",
                severity=(
                    "high"
                    if "error" in statuses.values()
                    or validation["pair_status"] in {"conflict", "incomplete"}
                    else "medium"
                ),
                message=(
                    f"{validation['construct_key']} review is "
                    f"Agent={statuses['agent']}, ANARCI={statuses['anarci']}, "
                    f"ESM-2={statuses['esm2']}: "
                    + ("; ".join(issues) if issues else "manual confirmation required")
                ),
            ).model_dump(), "construct_key": validation["construct_key"]}
        )
    return review_tasks


def verify_records_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    records, report = verify_records(state.get("dataset_records", []), state.get("ocr_results", []),
        state.get("document_template"), state.get("table_contexts"), state.get("assay_method_pages"), state.get("table_classifications"),
        state.get("sequence_reference_passages", []))
    repair = state.get("repair_state", {})
    plan = plan_repairs(report, state.get("repair_history", []), enabled=config.semantic_provider == "codex",
        rounds=repair.get("rounds", 0), max_rounds=config.max_repair_rounds,
        actions_used=repair.get("actions_used", 0), max_actions=config.max_repair_actions,
        stopped=bool(repair.get("stop_reason")))
    sequence_incomplete = report.get("sequence_coverage", {}).get("summary", {}).get("missing_chain_count", 0) > 0
    if not report["findings"] and sequence_incomplete:
        plan = {"tasks": [], "stop_reason": "sequence_evidence_missing"}
    return {"dataset_records": records, "record_verification": report, "repair_plan": plan,
            "repair_state": {**repair, "status": "completed" if not report["findings"] and not sequence_incomplete else "repair_pending" if plan["tasks"] else "review_required",
                             "stop_reason": repair.get("stop_reason") or plan["stop_reason"]}}


def question_answer_node(state: AgentState) -> dict[str, Any]:
    """Ask about diagnosed gaps, answer from sources, then validate staged changes."""
    from ..llm_runtime import provider_from_env
    from ..question_workflow import run_question_workflow

    config = AgentConfig.model_validate(state["config"])
    # Repair-loop revisits must not re-ask the same unresolved questions forever.
    if (config.semantic_provider != "codex" or not config.question_answer_enabled
            or state.get("question_review", {}).get("rounds")):
        return {}
    provider = provider_from_env(Path(config.output_dir) / "question_review", model=config.llm_model,
                                 effort=config.llm_effort, timeout=config.llm_timeout)
    return run_question_workflow(state, provider, image_roots=_image_roots(config, state),
                                 max_questions=config.max_answer_questions)


def repair_sequence_evidence(state: AgentState, task: dict, audit_dir: Path) -> dict[str, Any]:
    """Stage exact table-backed links and recheck residues and biological typing."""
    import copy
    from ..llm_runtime import json_write
    from ..llm_io import build_evidence
    from ..llm_table_agents import validate_document_plan
    from ..records import assess_sequence_candidate
    from ..table_structure import semantic_table

    config = AgentConfig.model_validate(state["config"])
    entity_id = task["entity_id"]
    template = copy.deepcopy(state.get("document_template", {}))
    proposals = [c for c in state["record_verification"]["sequence_coverage"]["repair_candidates"] if c["entity_id"] == entity_id]
    links = [{k: c[k] for k in ("entity_id", "chain", "seq_id", "scope", "evidence_refs")} for c in proposals]
    changes = {}
    try:
        if "sequence_extraction" in template:
            raise ValueError("Agent-value extraction cannot be changed by legacy rule-based sequence repair")
        if not links or template.get("agent", {}).get("mode") != "llm":
            raise ValueError("Sequence repair requires an explicit document entity plan and unique source links")
        tables = [semantic_table(t) for t in state.get("ocr_results", [])]
        passages = state.get("sequence_reference_passages", [])
        evidence = build_evidence(tables, state.get("table_contexts", []), state.get("assay_method_pages", []), passages)
        validate_document_plan({"document_id": config.document_id, "entities": template["entity_template"]["entities"],
            "sequence_links": links, "assay_profiles": [], "unresolved": []}, config.document_id, evidence,
            {str(t.get("source_id") or t.get("image")) for t in tables})
        for link in links:
            for existing in template.setdefault("sequence_links", []):
                if all(existing[k] == link[k] for k in ("entity_id", "chain", "scope")) and existing["seq_id"] != link["seq_id"]:
                    raise ValueError("Sequence repair may not overwrite a conflicting assignment")
            if link not in template["sequence_links"]:
                template["sequence_links"].append(link)
        if "document_plan" in template:
            template["document_plan"]["sequence_links"] = copy.deepcopy(template["sequence_links"])
        resolved_map = resolve_sequences(tables, template)
        candidate_records = apply_sequence_links(state.get("dataset_records", []), resolved_map, entity_id)
        target_records = [r for r in candidate_records if str(r.get("canonical_entity_key") or r.get("antibody_id")) == entity_id]
        complete_records = [r for r in target_records if all(r.get(field) and r[field] == resolved_map[entity_id].get(field) for field in ("vh", "vl"))]
        validations = validate_construct_pairs([resolved_map[entity_id]], complete_records)
        checked = apply_construct_validations(complete_records, validations)
        checked_by_id = {r["record_id"]: r for r in checked}
        candidate_records = [checked_by_id.get(r["record_id"], r) for r in candidate_records]
        attach_sequence_locations({entity_id: resolved_map[entity_id]}, [checked_by_id.get(r["record_id"], r) for r in target_records], tables,
            [*state.get("assay_method_pages", []), *template.get("sequence_source_passages", [])],
            state.get("candidates", []), config.source_pdf,
            sources={**state.get("document_structure", {}).get("sources", {}),
                     **template.get("sequence_extraction", {}).get("sources", {})})
        candidate_records, report = verify_records(candidate_records, tables, template,
            state.get("table_contexts"), state.get("assay_method_pages"), state.get("table_classifications"), passages)
        decision = assess_sequence_candidate(state.get("dataset_records", []), candidate_records,
            state.get("record_verification", {}), report, entity_id)
        # Biological runtime absence remains pending; an actual typing/pair
        # conflict rejects the automatic fill.
        if any(v.get("agent_review", {}).get("status") == "error" or v.get("anarci_review", {}).get("status") == "error" for v in validations):
            decision = {"accepted": False, "reason": "biological_sequence_conflict"}
        json_write(audit_dir / "candidate.json", {"links": links, "records": candidate_records,
            "verification": report, "construct_validations": validations, "template": template})
        if decision["accepted"]:
            changes = {"dataset_records": candidate_records, "document_template": template,
                "resolved_sequences": [r for r in state.get("resolved_sequences", []) if r.get("construct_key") != entity_id] + [resolved_map[entity_id]],
                "construct_validations": [v for v in state.get("construct_validations", []) if v.get("construct_key") != entity_id] + validations,
                "review_tasks": [t for t in state.get("review_tasks", []) if not (t.get("kind") == "construct_pair" and t.get("construct_key") == entity_id)] + _construct_review_tasks(validations)}
    except Exception as exc:
        decision = {"accepted": False, "reason": "sequence_repair_failed", "error": f"{type(exc).__name__}: {exc}"}
    json_write(audit_dir / "decision.json", decision)
    control = state.get("repair_state", {})
    round_number = control.get("rounds", 0) + 1
    return {**changes, "repair_history": [*state.get("repair_history", []), {"round": round_number, **task, **decision, "audit_dir": str(audit_dir)}],
        "repair_state": {"rounds": round_number, "actions_used": control.get("actions_used", 0) + 1,
                         "stop_reason": None if decision["accepted"] else decision["reason"]}}


def repair_evidence_node(state: AgentState) -> dict[str, Any]:
    """Stage a local plan revision and retain it only after independent checks."""
    import copy
    from ..assays import table_profile_summaries
    from ..llm_runtime import json_write, provider_from_env
    from ..llm_table_agents import LLMTableClassificationAgent, LLMTemplateAgent

    config = AgentConfig.model_validate(state["config"])
    task = state["repair_plan"]["tasks"][0]
    target_ids = {task["table_id"]}
    control = state.get("repair_state", {})
    round_number = control.get("rounds", 0) + 1
    audit_dir = Path(config.output_dir) / "repairs" / f"attempt-{round_number:03d}"
    audit_dir.mkdir(parents=True, exist_ok=True)
    json_write(audit_dir / "request.json", task)
    json_write(audit_dir / "before.json", {"records": state.get("dataset_records", []), "verification": state.get("record_verification", {}),
        "classifications": state.get("table_classifications", []), "template": state.get("document_template", {})})
    if task["action"] == "relink_sequences":
        return repair_sequence_evidence(state, task, audit_dir)
    provider = provider_from_env(audit_dir, model=config.llm_model, effort=config.llm_effort, timeout=config.llm_timeout)
    changes: dict[str, Any] = {}
    try:
        classifications = copy.deepcopy(state.get("table_classifications", []))
        if task["action"] == "reclassify_table":
            roots = _image_roots(config, state)
            revised = LLMTableClassificationAgent(provider, image_roots=roots, batch_size=1, max_workers=1).run(
                state.get("ocr_results", []), state.get("table_contexts", []), target_table_ids=target_ids, repair_findings=task["findings"])
            classifications = [c for c in classifications if c["table_id"] not in target_ids] + revised
        routed = route_ocr_results(state.get("ocr_results", []), classifications)
        template = copy.deepcopy(state.get("document_template", {}))
        profiles, unresolved = LLMTemplateAgent(provider).repair_profiles(config.document_id, routed,
            state.get("assay_method_pages", []), state.get("table_contexts", []), target_ids, task["findings"])
        assay = template.setdefault("assay_template", {})
        assay["region_profiles"] = [p for p in assay.get("region_profiles", []) if p["table_id"] not in target_ids] + profiles
        assay["table_profiles"] = table_profile_summaries(assay["region_profiles"])
        if "document_plan" in template:
            template["document_plan"]["assay_profiles"] = [p for p in template["document_plan"].get("assay_profiles", []) if p["table_id"] not in target_ids] + [
                {key: p[key] for key in ("table_id", "region_id", "protocol_id", "label", "method", "system", "species", "endpoints", "summary_zh", "summary_en", "evidence_refs")} for p in profiles]
        candidate = {**state, "table_classifications": classifications, "document_template": template}
        linked = link_node(candidate)
        # This iteration repairs measurements/protocols. Sequence changes need
        # a dedicated biological review, so they cannot slip through this tool.
        def sequence_values(items: list[dict]) -> dict:
            return {str(item.get("construct_key")): tuple(item.get(key) for key in
                ("vh", "vl", "vh_seq_id", "vl_seq_id", "full_heavy_seq_id", "full_light_seq_id")) for item in items}
        if sequence_values(linked.get("resolved_sequences", [])) != sequence_values(state.get("resolved_sequences", [])):
            raise ValueError("Repair would change sequence resolution; manual biological review required")
        candidate_records = apply_construct_validations(linked["dataset_records"], state.get("construct_validations", []))
        candidate_records, report = verify_records(candidate_records, state.get("ocr_results", []), template,
            state.get("table_contexts"), state.get("assay_method_pages"), classifications, state.get("sequence_reference_passages", []))
        decision = assess_candidate(state.get("dataset_records", []), candidate_records, state.get("record_verification", {}), report, target_ids)
        json_write(audit_dir / "candidate.json", {"records": candidate_records, "verification": report,
            "classifications": classifications, "template": template, "unresolved": unresolved})
        if decision["accepted"]:
            changes = {**linked, "dataset_records": candidate_records, "table_classifications": classifications, "document_template": template}
            changes["table_contexts"] = [{**context, "experiment_summaries": [
                {"region_id": p.get("region_id"), "method": p.get("assay_method"), "metric": ", ".join(p.get("endpoints", [])),
                 "summary_zh": p.get("summary_zh", ""), "summary_en": p.get("summary_en", "")}
                for p in assay["region_profiles"] if p["table_id"] == context.get("table_id")]}
                if context.get("table_id") in target_ids else context for context in state.get("table_contexts", [])]
    except Exception as exc:
        decision = {"accepted": False, "reason": "repair_tool_failed", "error": f"{type(exc).__name__}: {exc}"}
    json_write(audit_dir / "decision.json", decision)
    history = [*state.get("repair_history", []), {"round": round_number, **task, **decision, "audit_dir": str(audit_dir)}]
    return {**changes, "repair_history": history,
            "repair_state": {"rounds": round_number, "actions_used": control.get("actions_used", 0) + 1,
                             "stop_reason": None if decision["accepted"] else decision["reason"]},
            "llm_runs": [*state.get("llm_runs", []), *provider.calls]}


def route_after_verification(state: AgentState) -> str:
    return "repair_evidence" if state.get("repair_plan", {}).get("tasks") else "quality"


def sequence_evaluation_node(state: AgentState) -> dict[str, Any]:
    """Evaluate final sequences after repairs, using independent ANARCI/ESM-2 workers."""
    from eval.pipeline import evaluate_sequences, merge_evaluations

    config = AgentConfig.model_validate(state["config"])
    if not config.evaluation_enabled or config.semantic_provider != "codex":
        return {}
    root = Path(config.output_dir) / "eval"
    report = evaluate_sequences(state.get("resolved_sequences", []), root)
    validations = merge_evaluations(state.get("construct_validations", []), report)
    records = apply_construct_validations(state.get("dataset_records", []), validations)
    records, verification = verify_records(records, state.get("ocr_results", []), state.get("document_template"),
        state.get("table_contexts"), state.get("assay_method_pages"), state.get("table_classifications"),
        state.get("sequence_reference_passages", []))
    return {"sequence_evaluation": report, "construct_validations": validations, "dataset_records": records,
        "record_verification": verification,
        "review_tasks": [t for t in state.get("review_tasks", []) if t.get("kind") != "construct_pair"] + _construct_review_tasks(validations),
        "artifacts": {**state.get("artifacts", {}), "sequence_evaluation": str(root / "sequence_evaluation.json"),
            "anarci_evaluation": str(root / "anarci_results.json"), "esm2_evaluation": str(root / "esm2_results.json"),
            "sequence_evaluation_csv": str(root / "sequence_evaluation.csv")}}


def quality_node(state: AgentState) -> dict[str, Any]:
    """Turn template uncertainty and empty semantic output into visible work."""

    warnings = list(state.get("template_validation", {}).get("warnings", []))
    warnings.extend(state.get("page_retrieval", {}).get("warnings", []))
    if state.get("sequence_evaluation", {}).get("status") == "partial":
        warnings.append({"code": "sequence_evaluation_incomplete", "severity": "medium",
            "message": "ANARCI/ESM-2 部分评估未运行完成，具体运行原因已保留。", "table_ids": []})
    if state.get("question_review", {}).get("status") == "needs_review":
        warnings.append({"code": "question_answers_pending", "severity": "medium",
            "message": "QuestionAgent / AnswerAgent 仍有证据不足、未处理或未通过修改校验的问题。",
            "table_ids": [], "evidence_refs": []})
    keyword_review = [record for record in state.get("dataset_records", [])
                      if record.get("assay_keyword_status") == "needs_review"]
    if keyword_review:
        warnings.append({"code": "assay_keywords_need_review", "severity": "medium",
                         "message": f"{len(keyword_review)} observations have missing or ambiguous antigen keyword labels.",
                         "table_ids": sorted({str(record.get("assay_table_id")) for record in keyword_review}),
                         "record_ids": [record["record_id"] for record in keyword_review]})
    warnings.extend({**finding, "severity": "high" if finding["severity"] == "error" else "medium",
                     "code": finding["finding_id"], "validation_code": finding["code"], "table_ids": [finding["table_id"]]}
                    for finding in state.get("record_verification", {}).get("findings", []))
    ocr_results = route_ocr_results(
        state.get("ocr_results", []),
        state.get("table_classifications", []),
    )
    endpoint_tables = []
    for table in ocr_results:
        text = " ".join(
            str(cell.get("text", ""))
            for row in table.get("rows", [])
            for cell in row if isinstance(cell, dict)
        )
        if re.search(r"IC\s*50|IC₅₀", text, re.I):
            endpoint_tables.append(str(table.get("source_id") or table.get("image") or "unknown"))
    if endpoint_tables and not state.get("dataset_records") and not state.get("record_verification"):
        warnings.append({
            "code": "target_endpoint_but_zero_records",
            "severity": "high",
            "message": (
                f"Detected IC50 evidence in {len(endpoint_tables)} table(s), "
                "but the semantic assembler produced zero dataset records."
            ),
            "table_ids": endpoint_tables,
        })

    review_tasks = list(state.get("review_tasks", []))
    existing_codes = {
        str(task.get("quality_code"))
        for task in review_tasks
        if task.get("quality_code")
    }
    for index, warning in enumerate(warnings):
        code = str(warning.get("code", f"quality-{index}"))
        if code in existing_codes:
            continue
        review_tasks.append({
            **ReviewTask(
                task_id=f"quality-{code}-{uuid.uuid4().hex[:8]}",
                kind=(
                    "record_validation" if warning.get("validation_code") else "template"
                    if code in {
                        "no_stable_entity_naming_pattern",
                        "unmatched_entity_names",
                        "unsafe_entity_pattern",
                    }
                    else "quality_gate"
                ),
                severity=str(warning.get("severity", "medium")),
                message=str(warning.get("message", "Semantic output requires review.")),
                table_id=(warning.get("table_ids") or [None])[0],
            ).model_dump(),
            "quality_code": code,
            "details": warning,
        })
        existing_codes.add(code)
    return {"quality_warnings": warnings, "review_tasks": review_tasks}


def classify_tables_node(state: AgentState) -> dict[str, Any]:
    """Classify post-OCR evidence and expose the routing decision."""

    config = AgentConfig.model_validate(state["config"])
    provider = None
    if config.semantic_provider == "codex":
        from ..llm_runtime import provider_from_env
        from ..llm_table_agents import LLMTableClassificationAgent

        provider = provider_from_env(Path(config.output_dir), model=config.llm_model,
                                     effort=config.llm_effort, timeout=config.llm_timeout)
        roots = _image_roots(config, state)
        classifications = LLMTableClassificationAgent(
            provider, image_roots=roots, batch_size=config.llm_batch_size, max_workers=config.llm_workers,
            coarse_triage=config.classification_triage,
        ).run(state.get("ocr_results", []), state.get("table_contexts", []))
        if classifications and not any(item.get("classifier", {}).get("mode") == "llm" for item in classifications):
            raise RuntimeError("No table classification passed validation; see classification_progress.json and llm_calls.jsonl")
    else:
        classifications = TableClassificationAgent().run(state.get("ocr_results", []))
    # Persist independently source-checked measurements before document assembly,
    # so a later entity/sequence planning failure does not hide table understanding.
    from ..records import export_measurements
    from ..llm_runtime import json_write
    measurements = export_measurements(config.document_id, state.get("ocr_results", []), classifications)
    for name, values in (("assay_measurements", measurements["measurements"]),
                         ("assay_comparisons", measurements["comparisons"]),
                         ("measurement_review", {"summary": measurements["summary"], "issues": measurements["issues"]})):
        json_write(Path(config.output_dir) / f"{name}.json", values)
    by_id = {
        str(item.get("table_id")): item
        for item in classifications
        if item.get("table_id")
    }
    candidates: list[dict[str, Any]] = []
    for raw in state.get("candidates", []):
        candidate = dict(raw)
        classification = by_id.get(str(candidate.get("table_id")))
        if classification:
            candidate["classification_label"] = classification.get("primary_label")
            candidate["classification_labels"] = classification.get("labels", [])
            candidate["classification_confidence"] = classification.get("confidence")
            candidate["retain_for_pipeline"] = classification.get(
                "retain_for_pipeline", False
            )
        candidates.append(candidate)
    review_tasks = list(state.get("review_tasks", []))
    for classification in classifications:
        if not classification.get("review_required"):
            continue
        review_tasks.append(
            ReviewTask(
                task_id=(
                    f"table-classification-{classification['table_id']}-"
                    f"{uuid.uuid4().hex[:8]}"
                ),
                kind="table_classification",
                severity="medium",
                message=(
                    f"Table {classification['table_id']}: type={classification.get('primary_label')}; "
                    f"extraction={classification.get('extraction_status', 'needs_review')}. "
                    + "; ".join(issue.get("message", "") for issue in classification.get("issues", []))
                ),
                table_id=classification.get("table_id"),
            ).model_dump()
        )
    return {
        "table_classifications": classifications,
        "candidates": candidates,
        "review_tasks": review_tasks,
        "llm_runs": [*state.get("llm_runs", []), *(provider.calls if provider else [])],
    }


def publish_node(state: AgentState) -> dict[str, Any]:
    config = AgentConfig.model_validate(state["config"])
    output_dir = Path(config.output_dir)
    run_state = dict(state)
    artifacts = publish_run(output_dir, state["run_id"], run_state)
    artifacts.update(state.get("artifacts", {}))
    return {"artifacts": artifacts}


def route_after_select(state: AgentState) -> str:
    config = AgentConfig.model_validate(state["config"])
    return "extract" if config.run_ocr and not config.sda_input_file and state.get("selected_table_ids") else "classify_tables"


def build_seed_graph():
    """Shared table/template stages only; dataagent2 owns all later extraction."""
    from ..llm_runtime import StageTimings
    builder, timings = StateGraph(AgentState), StageTimings()
    for name, node in (("discover", discover_node), ("context", context_node),
                       ("triage", triage_node), ("select", select_node),
                       ("classify_tables", classify_tables_node), ("index_document", index_document_node),
                       ("assay_method_context", assay_method_context_node), ("template", template_node)):
        builder.add_node(name, timings.wrap(name, node))
    builder.add_edge(START, "discover")
    builder.add_edge("discover", "context")
    builder.add_edge("context", "triage")
    builder.add_edge("triage", "select")
    builder.add_edge("select", "classify_tables")
    builder.add_edge("select", "index_document")
    builder.add_edge(["classify_tables", "index_document"], "assay_method_context")
    builder.add_edge("assay_method_context", "template")
    builder.add_edge("template", END)
    return builder.compile()


def build_graph():
    """Compile OCR, semantic LLM agents, deterministic checks and publication."""

    from ..llm_runtime import StageTimings

    builder = StateGraph(AgentState)
    timings = StageTimings()

    # 节点按处理职责拆分：每一步独立更新 AgentState，并记录耗时与失败阶段。
    # 默认 codex 模式下，LLM 判断位于 classify_tables、template、sequence_extract、question_answer；
    # 其余节点负责工具执行、规则检查或调度，repair_evidence 按需再次调用语义 Agent。
    # 本图从已裁剪的表格开始；PDF 预处理在上游，Studio 人工复核与正式入库在下游。

    # 1. 输入与 OCR：发现候选 → 关联上下文 → 初筛 → 选择 → 识别。
    # 读取表格图片、页码、坐标，以及可复用的已有 OCR。
    builder.add_node("discover", timings.wrap("discover", discover_node))
    # 从 PDF 文本层提取正文，将标题和引用段落关联到表格。
    builder.add_node("context", timings.wrap("context", context_node))
    # 用关键词规则评分；可由外部决策文件覆盖，尚未进行 LLM 表格分类。
    builder.add_node("triage", timings.wrap("triage", triage_node))
    # 按 all / heuristic 模式确定需要 OCR 的表格。
    builder.add_node("select", timings.wrap("select", select_node))
    # 执行表格 OCR，按需补充序列页 OCR；支持检查点恢复。
    builder.add_node("extract", timings.wrap("extract", extract_node))

    # 2. 表格理解与证据检索：分类和全文索引并行，完成后汇合读取详细证据。
    # 空操作调度节点：统一新 OCR 与已有 OCR 两条入口，再启动两个分支。
    builder.add_node("prepare_semantics", timings.wrap("prepare_semantics", lambda state: {}))
    # 工具分支：建立全文检索索引，用于定位相关页面。
    builder.add_node("index_document", timings.wrap("index_document", index_document_node))
    # LLM 分支：判断表格类型、划分区域，给出实体列、数值列、单位等提取计划。
    builder.add_node("classify_tables", timings.wrap("classify_tables", classify_tables_node))
    # 根据索引与分类结果精读方法、实体和序列相关页面；索引失败时回退方法窗口 OCR。
    builder.add_node("assay_method_context", timings.wrap("assay_method_context", assay_method_context_node))

    # 3. 文档规划与序列提取：两个语义 Agent 依次工作。
    # LLM 建立实体/别名、SEQ ID 关联假设，以及实验区域与方法的绑定。
    builder.add_node("template", timings.wrap("template", template_node))
    # LLM 按实体检索原文，判断 VH/VL 归属与边界，输出实际序列及逐字引用。
    # 多轮检索循环封装在 SequenceExtractionAgent 内部；本图只调度一次节点。
    builder.add_node("sequence_extract", timings.wrap("sequence_extract", sequence_extract_node))

    # 4. 记录组装：按语义计划解析数据，连接实体、序列和实验测量。
    # 基础规则校验并提取序列/实验候选，将解析问题加入复核任务。
    builder.add_node("validate", timings.wrap("validate", validate_node))
    # 组装 VH/VL 与 IC50 记录，并挂接原始页码、表格和坐标证据。
    builder.add_node("link", timings.wrap("link", link_node))

    # 5. 校验与有限修复：生物学检查 → 独立证据核验 → 按需局部修复。
    # 构建体规则、ANARCI 和 ESM-2 分别检查序列配对、链型与合理性。
    builder.add_node("validate_constructs", timings.wrap("validate_constructs", construct_validation_node))
    # 核对记录与来源的一致性，并按剩余轮次/动作预算生成修复任务。
    builder.add_node("verify_records", timings.wrap("verify_records", verify_records_node))
    builder.add_node("question_answer", timings.wrap("question_answer", question_answer_node))
    # 暂存局部计划修改、重新组装并核验；仅在问题减少且无回退时接受候选。
    builder.add_node("repair_evidence", timings.wrap("repair_evidence", repair_evidence_node))
    builder.add_node("sequence_evaluation", timings.wrap("sequence_evaluation", sequence_evaluation_node))

    # 6. 结果输出：汇总未解决问题，保存可审计的本次运行结果。
    # 将规划不确定性、检索缺口及核验发现统一转成质量警告和人工复核任务。
    builder.add_node("quality", timings.wrap("quality", quality_node))
    # 写入本次运行的 JSON、CSV、SQLite 和证据文件；正式数据集发布由 Studio 处理。
    builder.add_node("publish", timings.wrap("publish", publish_node))

    # 以下定义执行顺序；上面的 add_node 只注册节点，不决定先后。
    # 阶段 1：顺序准备输入；仅在启用 OCR 且选中表格时执行 extract。
    builder.add_edge(START, "discover")
    builder.add_edge("discover", "context")
    builder.add_edge("context", "triage")
    builder.add_edge("triage", "select")
    builder.add_conditional_edges(
        "select",
        route_after_select,
        # 路由返回值沿用 classify_tables 名称，实际进入统一的并行调度入口。
        {"extract": "extract", "classify_tables": "prepare_semantics"},
    )
    builder.add_edge("extract", "prepare_semantics")

    # 阶段 2：同时启动分类与索引；列表形式的边要求两个分支都完成后再精读。
    builder.add_edge("prepare_semantics", "classify_tables")
    builder.add_edge("prepare_semantics", "index_document")
    builder.add_edge(["classify_tables", "index_document"], "assay_method_context")

    # 阶段 3：先建立文档级关联假设，再让序列 Agent 根据原文验证和提取。
    builder.add_edge("assay_method_context", "template")
    builder.add_edge("template", "sequence_extract")

    # 阶段 4：基础校验后组装记录。
    builder.add_edge("sequence_extract", "validate")
    builder.add_edge("validate", "link")

    # 阶段 5：修复节点内部重新组装候选，再回到独立核验；预算耗尽或无任务则退出。
    builder.add_edge("link", "validate_constructs")
    builder.add_edge("validate_constructs", "verify_records")
    builder.add_edge("verify_records", "question_answer")
    builder.add_conditional_edges("question_answer", route_after_verification,
        {"repair_evidence": "repair_evidence", "quality": "sequence_evaluation"})
    builder.add_edge("repair_evidence", "verify_records")
    builder.add_edge("sequence_evaluation", "quality")

    # 阶段 6：保留待复核项并保存结果，流程完成并不意味着所有记录均通过校验。
    builder.add_edge("quality", "publish")
    builder.add_edge("publish", END)
    return builder.compile()


def initial_state(config: AgentConfig, run_id: str | None = None) -> AgentState:
    return {
        "run_id": run_id or f"run-{uuid.uuid4().hex[:12]}",
        "config": config.model_dump(),
        "candidates": [],
        "selected_table_ids": [],
        "document_blocks": [],
        "table_contexts": [],
        "context_errors": [],
        "ocr_results": [],
        "table_classifications": [],
        "llm_runs": [],
        "assay_method_pages": [],
        "document_index": {},
        "page_retrieval": {},
        "sequence_reference_passages": [],
        "document_structure": {},
        "sequence_extraction": {},
        "question_review": {},
        "sequence_evaluation": {},
        "sequences": [],
        "assays": [],
        "document_template": {},
        "template_validation": {},
        "resolved_sequences": [],
        "construct_validations": [],
        "dataset_records": [],
        "record_verification": {},
        "repair_plan": {},
        "repair_state": {},
        "repair_history": [],
        "review_tasks": [],
        "quality_warnings": [],
        "artifacts": {},
        "errors": [],
    }
