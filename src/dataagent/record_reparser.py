#!/usr/bin/env python3
"""Reparse selected dataset fields from existing OCR evidence.

The review UI sends record IDs and field names.  This command rebuilds table
structure from the preserved OCR boxes, refreshes the small sequence-page OCR
neighbourhood, resolves construct-to-SEQ-ID links, and only applies requested
fields to the selected records.  Existing records remain unchanged until the
whole operation succeeds.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


from .construct_validation import apply_construct_validations, validate_construct_pairs
from .dataset_linker import link_dataset, scope_entity_key
from .page_ocr import extract_sequence_page_results
from .sequence_resolver import resolve_sequences
from .table_ocr import (
    Cell,
    OCRItem,
    TableResult,
    _alignment_cells,
    _grid_cells,
    _outside_caption,
    complete_vertical_boundaries,
    make_backend,
)


METADATA_FIELDS = {"link_status", "assay_keywords"}
ALLOWED_FIELDS = {"antibody_id", "vh", "vl", "ic50", "ic50_unit"} | METADATA_FIELDS
SEQUENCE_FIELDS = {"vh", "vl"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_dataset_csv_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    from .agent_store import _write_dataset_csv

    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_dataset_csv(temporary, records)
    temporary.replace(path)


def scope_verification_report(report: dict, records: list[dict], record_ids: set[str], output_dir: Path) -> None:
    """Keep saved record findings aligned with a selected-record refresh."""
    path = output_dir / "record_verification.json"
    prior = _read_json(path) if path.is_file() else {}
    report["findings"] = [f for f in prior.get("findings", [])
                          if f.get("record_id") and f["record_id"] not in record_ids] + [
        f for f in report["findings"] if not f.get("record_id") or f["record_id"] in record_ids]
    report["records"] = [{"record_id": r["record_id"], **r.get("record_verification", {
        "status": r.get("record_verification_status", "manual_review")})} for r in records]
    counts = Counter(r.get("record_verification_status", "manual_review") for r in records)
    report["summary"].update({status: counts[status] for status in ("passed", "error", "manual_review")})
    report["summary"]["finding_count"] = len(report["findings"])
    report["updated_record_ids"] = sorted(record_ids)


def refresh_selected_metadata(
    records: list[dict], record_ids: set[str], fields: set[str],
    output_dir: Path, tables: list[dict], *, template: dict | None = None,
    artifacts: dict | None = None, source_pdf: Path | None = None,
) -> tuple[list[dict], dict]:
    """Recheck stored values or retag assays without reconstructing observations."""
    def read(name: str, default: Any) -> Any:
        if artifacts and name in artifacts:
            return artifacts[name]
        path = output_dir / f"{name}.json"
        return _read_json(path) if path.is_file() else default

    template = template if template is not None else read("document_template", {})
    selected = [copy.deepcopy(r) for r in records if r.get("record_id") in record_ids]
    updates: dict[str, Any] = {}
    if "assay_keywords" in fields:
        from .experiment_agent import refresh_experiments

        selected, experiment_updates = refresh_experiments(selected, template, tables, output_dir, source_pdf)
        if artifacts and "llm_runs" in artifacts:
            experiment_updates["llm_runs"] = [*artifacts["llm_runs"], *experiment_updates["experiment_agent"]["calls"]]
        updates.update(experiment_updates)
        template = updates["document_template"]
        artifacts = {**(artifacts or {}), **experiment_updates}
    if "link_status" in fields:
        from .records import verify_records

        resolved = resolve_sequences(tables, template)
        # Batch identical pairs, but never validate one row using another row's
        # differing sequences, or sequences that were not selected for update.
        groups: dict[tuple, list[dict]] = {}
        for record in selected:
            key = str(record.get("canonical_entity_key") or scope_entity_key(record.get("antibody_id", ""), template))
            groups.setdefault((key, record.get("vh") or "", record.get("vl") or ""), []).append(record)
        pending = list(groups.items())
        checked, validations = [], []
        while pending:
            batch, remaining, keys = [], [], set()
            for pair, group in pending:
                if pair[0] in keys:
                    remaining.append((pair, group))
                else:
                    keys.add(pair[0])
                    batch.append((pair, group))
            candidates = [{**resolved.get(key, {}), "construct_key": key, "vh": vh, "vl": vl}
                          for (key, vh, vl), _ in batch]
            batch_records = [r for _, group in batch for r in group]
            fresh_validations = validate_construct_pairs(candidates, batch_records)
            checked.extend(apply_construct_validations(batch_records, fresh_validations))
            validations.extend({**v, "record_ids": [r["record_id"] for pair, group in batch
                                                   if pair[0] == v["construct_key"] for r in group]}
                               for v in fresh_validations)
            pending = remaining
        by_id = {r["record_id"]: r for r in checked}
        all_records = [by_id.get(r.get("record_id"), r) for r in records]
        verified, report = verify_records(
            all_records, tables, template, read("table_contexts", []), read("assay_method_pages", []),
            read("table_classifications", []), read("sequence_reference_passages", []),
        )
        selected = [r for r in verified if r.get("record_id") in record_ids]
        for record in selected:
            record["link_status"] = "linked" if all(record.get(field) == "passed" for field in (
                "agent_review_status", "anarci_review_status", "esm2_review_status", "record_verification_status",
            )) and record.get("sequence_coverage_status") == "complete" else "review"
        changed_keys = {v["construct_key"] for v in validations}
        updates["construct_validations"] = [v for v in read("construct_validations", [])
                                            if v.get("construct_key") not in changed_keys] + validations
        updates["record_verification"] = report
        updates["sequence_coverage"] = report["sequence_coverage"]
    by_id = {r["record_id"]: r for r in selected}
    merged = [by_id.get(r.get("record_id"), r) for r in records]
    if "record_verification" in updates:
        scope_verification_report(updates["record_verification"], merged, record_ids, output_dir)
    return merged, updates


def save_reparse_history(output_dir: Path, document_id: str, record_ids: set[str], fields: set[str],
                         records: list[dict], audit: list[dict], missing: list[str], replace_dataset: bool = False) -> dict:
    path = output_dir / "reparse_history.json"
    try:
        history = _read_json(path) if path.is_file() else []
    except (OSError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    summary = {"completed_at": utc_now(), "document_id": document_id, "requested_record_ids": sorted(record_ids),
               "fields": sorted(fields), "updated_count": len(audit), "output_record_count": len(records),
               "replace_dataset": replace_dataset, "missing_record_ids": missing, "changes": audit}
    history.append(summary)
    _write_json_atomic(path, history)
    return summary


def restore_result(raw: dict[str, Any], *, rebuild_structure: bool = True) -> TableResult:
    """Restore a serialized OCR result and optionally rebuild its cell grid."""

    items = [
        OCRItem(
            text=str(item.get("text", "")),
            score=float(item.get("score", 0.0)),
            bbox=[float(value) for value in item.get("bbox", [])],
            backend=str(item.get("backend", raw.get("backend", "unknown"))),
            item_index=int(item.get("item_index", index)),
        )
        for index, item in enumerate(raw.get("ocr_items", []))
        if len(item.get("bbox", [])) >= 4
    ]
    for index, item in enumerate(items):
        item.item_index = index
    rows = [
        [
            Cell(
                row_index=int(cell.get("row_index", row_index)),
                col_index=int(cell.get("col_index", col_index)),
                text=str(cell.get("text", "")),
                confidence=float(cell.get("confidence", 0.0)),
                bbox=[float(value) for value in cell.get("bbox", [])],
                item_indices=[int(value) for value in cell.get("item_indices", [])],
            )
            for col_index, cell in enumerate(row)
        ]
        for row_index, row in enumerate(raw.get("rows", []))
    ]
    horizontal = [float(value) for value in raw.get("horizontal_lines", [])]
    vertical = [float(value) for value in raw.get("vertical_lines", [])]
    mode = str(raw.get("mode", "alignment"))
    caption = str(raw.get("caption", ""))
    width = int(raw.get("width", 0))
    if rebuild_structure and mode != "sequence_page" and items:
        completed_vertical = complete_vertical_boundaries(vertical, width, items)
        if len(horizontal) >= 2 and len(completed_vertical) >= 2:
            rows, outside, mode = _grid_cells(items, horizontal, completed_vertical)
            caption = _outside_caption(outside, horizontal)
            vertical = completed_vertical
        elif not rows:
            rows = _alignment_cells(items)
            mode = "alignment"
    return TableResult(
        table_index=int(raw.get("table_index", 0)),
        image=str(raw.get("image", "")),
        page=int(raw["page"]) if raw.get("page") is not None else None,
        source_id=str(raw["source_id"]) if raw.get("source_id") else None,
        crop_confidence=(
            float(raw["crop_confidence"])
            if raw.get("crop_confidence") is not None
            else None
        ),
        width=width,
        height=int(raw.get("height", 0)),
        backend=str(raw.get("backend", "unknown")),
        caption=caption,
        items=items,
        rows=rows,
        horizontal_lines=horizontal,
        vertical_lines=vertical,
        mode=mode,
    )


def rebuild_ocr_results(
    raw_results: Iterable[dict[str, Any]],
    *,
    drop_sequence_pages: bool = True,
) -> list[TableResult]:
    """Rebuild table grids and optionally drop sequence pages before refresh."""

    results: list[TableResult] = []
    for raw in raw_results:
        source_id = str(raw.get("source_id") or "")
        is_sequence_page = raw.get("mode") == "sequence_page" or source_id.startswith("sequence-page-")
        if is_sequence_page and drop_sequence_pages:
            continue
        results.append(restore_result(raw, rebuild_structure=not is_sequence_page))
    return results


def _record_match_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("antibody_id", "")).strip().casefold(),
        str(record.get("experiment_group", "")).strip().casefold(),
        str(record.get("ic50", "")).strip(),
    )


def _fresh_record(
    source: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    direct = by_id.get(str(source.get("record_id", ""))) or by_key.get(_record_match_key(source))
    if direct:
        return direct
    # Protocol wording and model entity IDs may change during reanalysis;
    # the original value cell remains a stable observation address.
    source_cells = {(item.get("table_id"), item.get("row_index"), item.get("col_index"))
                    for item in source.get("evidence", []) if item.get("type") == "assay_measurement"}
    matches = []
    for fresh in by_id.values():
        if str(fresh.get("antibody_id", "")).casefold() != str(source.get("antibody_id", "")).casefold():
            continue
        if fresh.get("ic50") != source.get("ic50"):
            continue
        cells = {(item.get("table_id"), item.get("row_index"), item.get("col_index"))
                 for item in fresh.get("evidence", []) if item.get("type") == "assay_measurement"}
        if source_cells & cells:
            matches.append(fresh)
    return matches[0] if len(matches) == 1 else None


def preserve_existing_sequences(
    records: list[dict[str, Any]],
    fresh_records: list[dict[str, Any]],
    resolved_sequences: dict[str, dict[str, Any]],
) -> None:
    """Keep verified sequence values while refreshing their evidence graph.

    A focused OCR window may recover a construct mapping, CDR rows and full
    chains without seeing the standalone variable-region definition. In that
    case the existing sequence value is retained, while IDs and provenance
    are replaced by the newly parsed structured evidence.
    """

    by_id = {str(item.get("record_id", "")): item for item in fresh_records}
    by_key = {_record_match_key(item): item for item in fresh_records}
    for source in records:
        fresh = _fresh_record(source, by_id, by_key)
        if fresh is None:
            continue
        construct_key = str(fresh.get("canonical_entity_key") or scope_entity_key(str(source.get("antibody_id", ""))))
        resolved = resolved_sequences.get(construct_key)
        for field in ("vh", "vl"):
            preserved = source.get(field)
            if not preserved or fresh.get(field):
                continue
            fresh[field] = preserved
            if (resolved is not None and not resolved.get(field)
                    and resolved.get("sequence_resolution", {}).get(field, {}).get("source") != "agent_extracted_value"):
                resolved[field] = preserved
                resolved.setdefault("sequence_resolution", {})[field] = {
                    "source_scope": "preserved_record",
                    "source_record_id": source.get("record_id"),
                }
        if fresh.get("vh") and fresh.get("vl"):
            fresh["link_status"] = "review" if resolved and resolved.get("ambiguities") else "linked"
        if resolved is not None and resolved.get("vh") and resolved.get("vl"):
            resolved["status"] = "linked"
        if resolved is not None:
            sequence_index = next(
                (
                    item
                    for item in fresh.get("evidence", [])
                    if item.get("type") == "sequence_index"
                ),
                None,
            )
            if sequence_index is not None:
                sequence_index["resolution"] = resolved.get("sequence_resolution", {})


def merge_selected_records(
    records: list[dict[str, Any]],
    fresh_records: list[dict[str, Any]],
    record_ids: set[str],
    fields: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Apply fresh values only to selected records and return an audit delta."""

    by_id = {str(item.get("record_id", "")): item for item in fresh_records}
    by_key = {_record_match_key(item): item for item in fresh_records}
    updated: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    missing: list[str] = []
    for source in records:
        record_id = str(source.get("record_id", ""))
        if record_id not in record_ids:
            updated.append(source)
            continue
        fresh = _fresh_record(source, by_id, by_key)
        if fresh is None:
            missing.append(record_id)
            updated.append(source)
            continue
        merged = dict(source)
        changes: dict[str, dict[str, Any]] = {}
        refreshed_sequence_fields: set[str] = set()
        for field in fields - METADATA_FIELDS:
            before = merged.get(field)
            after = fresh.get(field)
            # A targeted reparse must not erase a previously populated chain
            # merely because the refreshed OCR window cannot resolve it.
            if field in SEQUENCE_FIELDS and before and not after:
                continue
            merged[field] = after
            if field in SEQUENCE_FIELDS and after:
                refreshed_sequence_fields.add(field)
            if before != after:
                changes[field] = {"before": before, "after": after}
        if refreshed_sequence_fields:
            for field in ("sequence_table_id", "construct_validation_status", "evidence"):
                if field in fresh:
                    merged[field] = fresh[field]
            merged["link_status"] = (
                fresh.get("link_status", "review")
                if merged.get("vh") and merged.get("vl")
                else "review"
            )
        if fields & {"ic50", "ic50_unit", "antibody_id"}:
            for field in (
                "assay_table_id",
                "assay_endpoint",
                "assay_evidence_count",
                "experiment_group",
                "experiment_id",
                "evidence",
            ):
                if field in fresh:
                    merged[field] = fresh[field]
        if merged.get("evidence") is fresh.get("evidence"):
            previous_index = next((e for e in source.get("evidence", []) if e.get("type") == "sequence_index"), {})
            safe_evidence = []
            for item in merged.get("evidence", []):
                if item.get("type") == "sequence_index" and "locations" in item:
                    locations = {}
                    for chain_field in ("vh", "vl"):
                        candidate = (item if merged.get(chain_field) == fresh.get(chain_field) else previous_index)
                        if chain_field in candidate.get("locations", {}):
                            locations[chain_field] = candidate["locations"][chain_field]
                    item = {**item, "locations": locations}
                safe_evidence.append(item)
            merged["evidence"] = safe_evidence
        updated.append(merged)
        audit.append({"record_id": record_id, "changes": changes})
    return updated, audit, missing


def run_reparse(
    *,
    document_id: str,
    source_pdf: Path,
    output_dir: Path,
    request_path: Path,
    backend_name: str = "auto",
    lang: str = "en",
    device: str | None = None,
    max_side: int = 2600,
) -> dict[str, Any]:
    request = _read_json(request_path)
    record_ids = {str(value) for value in request.get("record_ids", []) if value}
    fields = {str(value) for value in request.get("fields", [])}
    if not fields or not fields <= ALLOWED_FIELDS:
        raise ValueError("请选择有效的重新解析字段")

    records_path = output_dir / "dataset_records.json"
    ocr_path = output_dir / "ocr_results.json"
    records = _read_json(records_path)
    raw_ocr = _read_json(ocr_path)
    if not isinstance(records, list) or not isinstance(raw_ocr, list):
        raise ValueError("现有 Agent 产物格式无效")
    if request.get("all_records"):
        record_ids = {
            str(record.get("record_id", ""))
            for record in records
            if record.get("record_id")
        }
    if not record_ids:
        raise ValueError("至少选择一条记录")
    if record_ids - {str(r.get("record_id", "")) for r in records}:
        raise ValueError("部分记录不属于当前专利")

    if fields <= METADATA_FIELDS:
        if request.get("replace_dataset"):
            raise ValueError("更新复核状态或实验关键词不能替换整个数据集")
        merged, updates = refresh_selected_metadata(records, record_ids, fields, output_dir, raw_ocr, source_pdf=source_pdf)
        original = {r["record_id"]: r for r in records}
        audit = [{"record_id": r["record_id"], "changes": {
            key: {"before": original[r["record_id"]].get(key), "after": value}
            for key, value in r.items() if original[r["record_id"]].get(key) != value
        }} for r in merged if r.get("record_id") in record_ids]
        for name, value in updates.items():
            _write_json_atomic(output_dir / f"{name}.json", value)
        _write_json_atomic(records_path, merged)
        _write_dataset_csv_atomic(output_dir / "dataset_records.csv", merged)
        state_path = output_dir / "agent_state.json"
        if state_path.is_file():
            state = _read_json(state_path)
            state.update(dataset_records=merged, **updates)
            _write_json_atomic(state_path, state)
        runtime_path = output_dir / "agent_runtime.json"
        if "record_verification" in updates or "experiment_agent" in updates:
            runtime = _read_json(runtime_path) if runtime_path.is_file() else {}
            if "record_verification" in updates:
                runtime.update(record_verification=updates["record_verification"]["summary"],
                               sequence_coverage=updates["sequence_coverage"]["summary"])
            if "experiment_agent" in updates:
                runtime["experiment_agent"] = updates["experiment_agent"]
                runtime["calls"] = runtime.get("calls", 0) + len(updates["experiment_agent"]["calls"])
            _write_json_atomic(runtime_path, runtime)
        return save_reparse_history(output_dir, document_id, record_ids, fields, merged, audit, [])

    table_results = rebuild_ocr_results(
        raw_ocr,
        drop_sequence_pages=bool(fields & SEQUENCE_FIELDS),
    )
    if fields & SEQUENCE_FIELDS:
        page_results = extract_sequence_page_results(
            source_pdf,
            output_dir / "tables",
            output_dir,
            None,
            table_results,
            max_side=max_side,
            backend_factory=lambda: make_backend(backend_name, lang, device, max_side),
        )
        table_results.extend(page_results)

    refreshed_ocr = [result.as_dict() for result in table_results]
    semantic_ocr = refreshed_ocr
    template = None
    contexts_path = output_dir / "table_contexts.json"
    methods_path = output_dir / "assay_method_pages.json"
    contexts = _read_json(contexts_path) if contexts_path.is_file() else []
    methods = _read_json(methods_path) if methods_path.is_file() else []
    from .sequence_evidence import collect_sequence_reference_passages
    blocks_path = output_dir / "document_blocks.json"
    sequence_passages = collect_sequence_reference_passages(_read_json(blocks_path) if blocks_path.is_file() else [])
    semantic_updates: dict[str, Any] = {"sequence_reference_passages": sequence_passages}
    provider = None
    if os.environ.get("DATASETAGENT_SEMANTIC_PROVIDER", "codex") == "codex":
        from .llm_runtime import provider_from_env
        from .llm_table_agents import LLMTableClassificationAgent, LLMTemplateAgent
        from .llm_sequence_agent import SequenceExtractionAgent
        from .sequence_evidence import build_document_structure, write_structure_artifacts
        from .table_classification import route_ocr_results

        contexts_path = output_dir / "table_contexts.json"
        methods_path = output_dir / "assay_method_pages.json"
        contexts = _read_json(contexts_path) if contexts_path.is_file() else []
        methods = _read_json(methods_path) if methods_path.is_file() else []
        provider = provider_from_env(output_dir)
        classifications = LLMTableClassificationAgent(
            provider, image_roots=[output_dir / "tables", source_pdf.parent / "tables"],
        ).run(refreshed_ocr, contexts)
        semantic_ocr = route_ocr_results(refreshed_ocr, classifications)
        template, template_validation = LLMTemplateAgent(provider).run(document_id, semantic_ocr, methods, contexts, sequence_passages)
        structure = build_document_structure(document_id, refreshed_ocr,
            blocks=_read_json(blocks_path) if blocks_path.is_file() else [], contexts=contexts, methods=methods,
            passages=sequence_passages, classifications=classifications)
        write_structure_artifacts(structure, output_dir)
        extraction = SequenceExtractionAgent(provider).run(structure, template)
        extraction["agent"].update(model=provider.model, reasoning_effort=provider.effort)
        template["sequence_extraction"] = extraction
        semantic_updates.update(document_structure=structure, sequence_extraction=extraction)
        semantic_updates.update(table_classifications=classifications, document_template=template,
                                template_validation=template_validation, llm_runs=provider.calls)
    sequence_sources = refreshed_ocr if "sequence_extraction" in (template or {}) else semantic_ocr
    fresh_records = link_dataset(document_id, semantic_ocr, document_template=template, sequence_sources=sequence_sources)
    resolved_map = resolve_sequences(sequence_sources, template)
    preserve_existing_sequences(records, fresh_records, resolved_map)
    from .sequence_evidence import attach_sequence_locations
    attach_sequence_locations(
        resolved_map, fresh_records, sequence_sources,
        (template or {}).get("sequence_source_passages", []),
        _read_json(output_dir / "table_candidates.json") if (output_dir / "table_candidates.json").is_file() else [],
        source_pdf,
        sources=(template or {}).get("sequence_extraction", {}).get("sources", {}),
    )
    resolved = list(resolved_map.values())
    validations = validate_construct_pairs(resolved, fresh_records)
    fresh_records = apply_construct_validations(fresh_records, validations)
    replace_dataset = bool(request.get("replace_dataset"))
    if replace_dataset:
        merged = fresh_records
        missing = []
        audit = [{
            "record_id": str(record.get("record_id", "")),
            "changes": {"dataset_record": {"before": None, "after": "rebuilt"}},
        } for record in merged]
    else:
        merged, audit, missing = merge_selected_records(records, fresh_records, record_ids, fields)

    from .records import verify_records
    merged, verification = verify_records(merged, refreshed_ocr, template, contexts, methods,
        semantic_updates.get("table_classifications", []), sequence_passages)
    if not replace_dataset:
        originals = {r["record_id"]: r for r in records}
        merged = [r if r.get("record_id") in record_ids else originals[r["record_id"]] for r in merged]
        scope_verification_report(verification, merged, record_ids, output_dir)
    if fields & METADATA_FIELDS:
        before_metadata = {r["record_id"]: copy.deepcopy(r) for r in merged}
        merged, metadata_updates = refresh_selected_metadata(
            merged, record_ids, fields, output_dir, refreshed_ocr, template=template, source_pdf=source_pdf,
            artifacts={**semantic_updates, "record_verification": verification,
                       "construct_validations": validations},
        )
        semantic_updates.update(metadata_updates)
        template = metadata_updates.get("document_template", template)
        validations = metadata_updates.get("construct_validations", validations)
        verification = metadata_updates.get("record_verification", verification)
        audit_by_id = {item["record_id"]: item for item in audit}
        for record in merged:
            identity = record["record_id"]
            if identity not in record_ids:
                continue
            entry = audit_by_id.setdefault(identity, {"record_id": identity, "changes": {}})
            entry["changes"].update({key: {"before": before_metadata[identity].get(key), "after": value}
                                     for key, value in record.items() if before_metadata[identity].get(key) != value})
        audit = list(audit_by_id.values())
    semantic_updates["record_verification"] = verification
    _write_json_atomic(output_dir / "record_verification.json", verification)
    _write_json_atomic(output_dir / "sequence_coverage.json", verification["sequence_coverage"])
    _write_json_atomic(output_dir / "sequence_reference_passages.json", sequence_passages)

    _write_json_atomic(ocr_path, refreshed_ocr)
    _write_json_atomic(output_dir / "resolved_sequences.json", resolved)
    _write_json_atomic(output_dir / "construct_validations.json", validations)
    _write_json_atomic(records_path, merged)
    _write_dataset_csv_atomic(output_dir / "dataset_records.csv", merged)
    for key, filename in (("table_classifications", "table_classifications.json"),
                          ("sequence_extraction", "sequence_extraction.json"),
                          ("document_template", "document_template.json"),
                          ("template_validation", "template_validation.json"), ("llm_runs", "llm_runs.json"),
                          ("experiment_agent", "experiment_agent.json"), ("assay_method_pages", "assay_method_pages.json")):
        if key in semantic_updates:
            _write_json_atomic(output_dir / filename, semantic_updates[key])
    if template and provider is not None:
        calls = [*provider.calls, *semantic_updates.get("experiment_agent", {}).get("calls", [])]
        _write_json_atomic(output_dir / "agent_runtime.json", {
            "semantic_provider": "codex", "model": provider.model,
            "reasoning_effort": provider.effort, "classification_agent": {"mode": "llm"},
            "template_agent": template.get("agent", {}), "calls": len(calls),
            "experiment_agent": semantic_updates.get("experiment_agent", {}),
            "sequence_agent": {**semantic_updates.get("sequence_extraction", {}).get("agent", {}),
                               "status": semantic_updates.get("sequence_extraction", {}).get("status", "not_run")},
            "cache_hits": sum(item.get("cache_hit", False) for item in calls),
            "record_verification": verification["summary"],
            "sequence_coverage": verification["sequence_coverage"]["summary"],
            "semantic_status": "partial" if verification["findings"] or verification["sequence_coverage"]["summary"]["missing_chain_count"] else "completed",
        })
    elif "experiment_agent" in semantic_updates:
        runtime_path = output_dir / "agent_runtime.json"
        runtime = _read_json(runtime_path) if runtime_path.is_file() else {}
        runtime.update(semantic_provider="rules", experiment_agent=semantic_updates["experiment_agent"],
                       record_verification=verification["summary"],
                       sequence_coverage=verification["sequence_coverage"]["summary"])
        runtime["calls"] = runtime.get("calls", 0) + len(semantic_updates["experiment_agent"]["calls"])
        _write_json_atomic(runtime_path, runtime)

    state_path = output_dir / "agent_state.json"
    if state_path.is_file():
        state = _read_json(state_path)
        if isinstance(state, dict):
            state.update({**semantic_updates, "ocr_results": refreshed_ocr,
                          "resolved_sequences": resolved, "construct_validations": validations,
                          "dataset_records": merged})
            _write_json_atomic(state_path, state)

    return save_reparse_history(output_dir, document_id, record_ids, fields, merged, audit, missing, replace_dataset)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reparse selected patent dataset records")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-pdf", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--request-json", required=True, type=Path)
    parser.add_argument("--backend", choices=("auto", "paddleocr", "tesseract"), default="auto")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--device")
    parser.add_argument("--max-side", type=int, default=2600)
    args = parser.parse_args()
    summary = run_reparse(
        document_id=args.document_id,
        source_pdf=args.source_pdf.resolve(),
        output_dir=args.output_dir.resolve(),
        request_path=args.request_json.resolve(),
        backend_name=args.backend,
        lang=args.lang,
        device=args.device,
        max_side=args.max_side,
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
