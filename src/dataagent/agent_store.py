#!/usr/bin/env python3
"""Persist an auditable LangGraph run and its review queue."""

from __future__ import annotations

import json
import csv
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _write_json(path: Path, value: Any) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _write_dataset_csv(path: Path, records: list[dict[str, Any]]) -> str:
    fields = [
        "document_id", "record_id", "antibody_id", "raw_antibody_id",
        "canonical_antibody_id", "canonical_entity_key", "antibody_variant", "entity_role",
        "experiment_group", "experiment_id", "measurement_id", "donor_id",
        "species", "assay_system", "assay_method", "assay_condition", "assay_protocol_id",
        "assay_region_id", "assay_binding_status", "record_verification_status",
        "assay_keywords", "antibody_keywords", "sequence_source_conflicts",
        "antigen_target", "assay_keyword_status", "assay_keyword_library", "assay_keyword_unmatched",
        "assay_cell_host_species", "assay_experiment",
        "assay_protocol_summary_zh", "assay_protocol_summary_en",
        "vh", "vl", "ic50", "ic50_operator", "ic50_unit", "assay_evidence_count",
        "agent_review_status", "anarci_review_status", "esm2_review_status", "link_status", "source",
        "sequence_table_id", "assay_table_id",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({
                field: (
                    json.dumps(record.get(field), ensure_ascii=False)
                    if isinstance(record.get(field), (list, dict))
                    else record.get(field, "")
                )
                for field in fields
            })
    return str(path)


def _markdown_text(value: Any) -> str:
    return str(value or "").replace("|", r"\|").replace("\n", " ").strip()


def _write_context_markdown(path: Path, contexts: list[dict[str, Any]]) -> str:
    lines = ["# Table context evidence", ""]
    for context in contexts:
        table_id = _markdown_text(context.get("table_id"))
        number = context.get("table_number")
        title = f"Table {number}" if number else table_id
        lines.extend(
            [
                f"## {title}",
                "",
                f"- Physical table: `{table_id}`",
                f"- Logical table: `{_markdown_text(context.get('logical_table_id'))}`",
                f"- Page: {context.get('page') or ''}",
                f"- Status: `{_markdown_text(context.get('context_status'))}`",
            ]
        )
        if context.get("continuation_of"):
            lines.append(
                f"- Continuation of: `{_markdown_text(context.get('continuation_of'))}` "
                f"(confidence {context.get('continuation_confidence')})"
            )
        image_path = context.get("context_image")
        if image_path:
            relative = os.path.relpath(str(image_path), path.parent)
            lines.extend(["", f"![{table_id} context]({quote(Path(relative).as_posix(), safe='/_-.()')})"])
        caption = context.get("caption")
        if caption:
            lines.extend(
                [
                    "",
                    "### Caption",
                    "",
                    f"> {_markdown_text(caption.get('text'))}",
                    "",
                    f"Page {caption.get('page')}, confidence {caption.get('confidence')}",
                ]
            )
        references = context.get("referring_sentences", [])
        if references:
            lines.extend(["", "### Referring sentences", ""])
            for evidence in references:
                lines.append(
                    f"- Page {evidence.get('page')} ({evidence.get('relation')}, "
                    f"{evidence.get('confidence')}): {_markdown_text(evidence.get('text'))}"
                )
        nearby = context.get("nearby_context", [])
        if nearby:
            lines.extend(["", "### Nearby context", ""])
            for evidence in nearby:
                lines.append(
                    f"- Page {evidence.get('page')} ({evidence.get('relation')}, "
                    f"{evidence.get('confidence')}): {_markdown_text(evidence.get('text'))}"
                )
        footnotes = context.get("footnotes", [])
        if footnotes:
            lines.extend(["", "### Footnotes", ""])
            for evidence in footnotes:
                lines.append(f"- {_markdown_text(evidence.get('text'))}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def publish_run(
    output_dir: Path,
    run_id: str,
    state: dict[str, Any],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_records = state.get("dataset_records", [])
    from .table_structure import semantic_table
    from .records import export_measurements

    by_class = {c["table_id"]: c for c in state.get("table_classifications", [])}
    semantic_tables = [semantic_table({**table, "table_classification": by_class.get(str(table.get("source_id") or table.get("image")), {})})
                       for table in state.get("ocr_results", [])]
    measurements = export_measurements(state.get("config", {}).get("document_id", ""), state.get("ocr_results", []),
                                      state.get("table_classifications", []), state.get("document_template"), dataset_records)
    failed_tables = [item["table_id"] for item in state.get("table_classifications", [])
                     if item.get("classifier", {}).get("mode") == "error"]
    pending_regions = sum(region.get("execution_status") == "needs_review"
                          for item in state.get("table_classifications", []) for region in item.get("regions", []))
    pending_tables = [item["table_id"] for item in state.get("table_classifications", [])
                      if item.get("decision") == "needs_review"]
    verification = state.get("record_verification", {})
    unresolved_records = any(r.get("record_verification_status") != "passed" for r in dataset_records)
    sequence_summary = verification.get("sequence_coverage", {}).get("summary", {})
    page_evidence_pending = bool(state.get("page_retrieval", {}).get("warnings"))
    artifacts = {
        "measurements": _write_json(output_dir / "assay_measurements.json", measurements["measurements"]),
        "comparisons": _write_json(output_dir / "assay_comparisons.json", measurements["comparisons"]),
        "measurement_review": _write_json(output_dir / "measurement_review.json", {"summary": measurements["summary"], "issues": measurements["issues"]}),
        "state": _write_json(output_dir / "agent_state.json", state),
        "candidates": _write_json(output_dir / "table_candidates.json", state.get("candidates", [])),
        "document_blocks": _write_json(
            output_dir / "document_blocks.json", state.get("document_blocks", [])
        ),
        "table_contexts": _write_json(
            output_dir / "table_contexts.json", state.get("table_contexts", [])
        ),
        "classifications": _write_json(
            output_dir / "table_classifications.json",
            state.get("table_classifications", []),
        ),
        "llm_runs": _write_json(output_dir / "llm_runs.json", state.get("llm_runs", [])),
        "document_structure": _write_json(output_dir / "document_structure.json", state.get("document_structure", {})),
        "sequence_extraction": _write_json(output_dir / "sequence_extraction.json", state.get("sequence_extraction", {})),
        "semantic_ocr": _write_json(output_dir / "semantic_ocr_results.json", semantic_tables),
        "table_repairs": _write_json(output_dir / "table_repairs.json", [
            {"table_id": table.get("source_id") or table.get("image"), **table["structure_repair"]}
            for table in semantic_tables if table.get("structure_repair")]),
        "agent_runtime": _write_json(output_dir / "agent_runtime.json", {
            "semantic_provider": state.get("config", {}).get("semantic_provider", "rules"),
            "model": state.get("config", {}).get("llm_model"),
            "reasoning_effort": state.get("config", {}).get("llm_effort"),
            "classification_agent": {"mode": "llm" if any(item.get("classifier", {}).get("mode") == "llm" for item in state.get("table_classifications", [])) else "rules",
                                     "table_count": len(state.get("table_classifications", []))},
            "template_agent": state.get("document_template", {}).get("agent", {}),
            "sequence_agent": {**state.get("sequence_extraction", {}).get("agent", {}),
                               "status": state.get("sequence_extraction", {}).get("status", "not_run")},
            "calls": len(state.get("llm_runs", [])),
            "cache_hits": sum(item.get("cache_hit", False) for item in state.get("llm_runs", [])),
            "semantic_status": "partial" if failed_tables or pending_regions or pending_tables or unresolved_records or verification.get("findings") or sequence_summary.get("missing_chain_count") or page_evidence_pending or state.get("question_review", {}).get("status") == "needs_review" or state.get("sequence_evaluation", {}).get("status") == "partial" else "completed",
            "record_verification": verification.get("summary", {}),
            "sequence_coverage": sequence_summary,
            "page_index": state.get("document_index", {}),
            "page_evidence_status": state.get("page_retrieval", {}).get("status", "disabled"),
            "repair_state": state.get("repair_state", {}),
            "failed_table_ids": failed_tables,
            "pending_table_ids": pending_tables,
            "pending_region_count": pending_regions,
        }),
        "table_context_report": _write_context_markdown(
            output_dir / "table_contexts.md", state.get("table_contexts", [])
        ),
        "context_errors": _write_json(
            output_dir / "context_errors.json", state.get("context_errors", [])
        ),
        "assay_method_pages": _write_json(
            output_dir / "assay_method_pages.json", state.get("assay_method_pages", [])
        ),
        "page_retrieval": _write_json(output_dir / "page_retrieval.json", state.get("page_retrieval", {})),
        "sequence_reference_passages": _write_json(
            output_dir / "sequence_reference_passages.json", state.get("sequence_reference_passages", [])
        ),
        "document_template": _write_json(
            output_dir / "document_template.json", state.get("document_template", {})
        ),
        "template_validation": _write_json(
            output_dir / "template_validation.json", state.get("template_validation", {})
        ),
        "quality_warnings": _write_json(
            output_dir / "quality_warnings.json", state.get("quality_warnings", [])
        ),
        "sequences": _write_json(output_dir / "sequence_candidates.json", state.get("sequences", [])),
        "resolved_sequences": _write_json(
            output_dir / "resolved_sequences.json", state.get("resolved_sequences", [])
        ),
        "construct_validations": _write_json(
            output_dir / "construct_validations.json", state.get("construct_validations", [])
        ),
        "record_verification": _write_json(output_dir / "record_verification.json", verification),
        "sequence_coverage": _write_json(output_dir / "sequence_coverage.json", verification.get("sequence_coverage", {})),
        "gap_report": _write_json(output_dir / "gap_report.json", {
            "records": [{"record_id": r.get("record_id"), "field_states": r.get("record_verification", {}).get("field_states", {})} for r in dataset_records],
            "coverage_gaps": [f for f in verification.get("findings", []) if f["code"] in {"ic50_coverage_gap", "ic50_cells_unparsed"} or f["code"].startswith("sequence_")],
            "sequence_summary": sequence_summary,
        }),
        "repair_plan": _write_json(output_dir / "repair_plan.json", state.get("repair_plan", {})),
        "repair_history": _write_json(output_dir / "repair_history.json", state.get("repair_history", [])),
        "question_review": _write_json(output_dir / "question_review.json", state.get("question_review", {})),
        "sequence_evaluation": _write_json(output_dir / "sequence_evaluation.json", state.get("sequence_evaluation", {})),
        "assays": _write_json(output_dir / "assay_candidates.json", state.get("assays", [])),
        "review_queue": _write_json(output_dir / "review_queue.json", state.get("review_tasks", [])),
        "dataset": _write_json(output_dir / "dataset_records.json", dataset_records),
        "dataset_csv": _write_dataset_csv(output_dir / "dataset_records.csv", dataset_records),
    }
    db_path = output_dir / "agent_run.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS table_candidates (
                run_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                image TEXT NOT NULL,
                page INTEGER,
                role_json TEXT NOT NULL,
                selection_score REAL NOT NULL,
                selected INTEGER NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                PRIMARY KEY(run_id, table_id)
            );
            CREATE TABLE IF NOT EXISTS sequence_candidates (
                run_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                image TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                col_index INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                sequence TEXT NOT NULL,
                chain TEXT NOT NULL,
                length INTEGER NOT NULL,
                validation_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS document_blocks (
                run_id TEXT NOT NULL,
                block_id TEXT NOT NULL,
                page INTEGER NOT NULL,
                bbox_json TEXT NOT NULL,
                text TEXT NOT NULL,
                block_type TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY(run_id, block_id)
            );
            CREATE TABLE IF NOT EXISTS table_contexts (
                run_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                logical_table_id TEXT NOT NULL,
                table_number TEXT,
                page INTEGER,
                bbox_json TEXT NOT NULL,
                caption_json TEXT,
                nearby_context_json TEXT NOT NULL,
                referring_sentences_json TEXT NOT NULL,
                footnotes_json TEXT NOT NULL,
                continuation_of TEXT,
                continuation_confidence REAL,
                context_status TEXT NOT NULL,
                context_image TEXT,
                PRIMARY KEY(run_id, table_id)
            );
            CREATE TABLE IF NOT EXISTS table_classifications (
                run_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                primary_label TEXT NOT NULL,
                labels_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                entity_axis TEXT NOT NULL,
                measurement_axis TEXT NOT NULL,
                retain_for_pipeline INTEGER NOT NULL,
                review_required INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                classification_json TEXT NOT NULL,
                PRIMARY KEY(run_id, table_id)
            );
            CREATE TABLE IF NOT EXISTS resolved_sequences (
                run_id TEXT NOT NULL,
                construct_key TEXT NOT NULL,
                vh_seq_id INTEGER,
                vl_seq_id INTEGER,
                vh TEXT NOT NULL,
                vl TEXT NOT NULL,
                status TEXT NOT NULL,
                sequence_table_ids_json TEXT NOT NULL,
                sequence_rows_json TEXT NOT NULL,
                PRIMARY KEY(run_id, construct_key)
            );
            CREATE TABLE IF NOT EXISTS construct_validations (
                run_id TEXT NOT NULL,
                construct_key TEXT NOT NULL,
                family TEXT,
                pair_status TEXT NOT NULL,
                validation_level TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                PRIMARY KEY(run_id, construct_key)
            );
            CREATE TABLE IF NOT EXISTS review_queue (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                table_id TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id)
            );
            CREATE TABLE IF NOT EXISTS dataset_records (
                run_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                antibody_id TEXT NOT NULL,
                raw_antibody_id TEXT,
                canonical_antibody_id TEXT,
                canonical_entity_key TEXT,
                antibody_variant TEXT,
                entity_role TEXT,
                experiment_group TEXT,
                experiment_id TEXT,
                measurement_id TEXT,
                donor_id TEXT,
                species_json TEXT,
                assay_system TEXT,
                assay_method TEXT,
                assay_protocol_id TEXT,
                assay_keywords_json TEXT,
                antibody_keywords_json TEXT,
                assay_protocol_summary_zh TEXT,
                assay_protocol_summary_en TEXT,
                vh TEXT,
                vl TEXT,
                ic50 REAL,
                ic50_unit TEXT,
                agent_review_status TEXT,
                anarci_review_status TEXT,
                esm2_review_status TEXT,
                link_status TEXT NOT NULL,
                source TEXT NOT NULL,
                sequence_table_id TEXT,
                assay_table_id TEXT,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY(run_id, record_id)
            );
            """
        )
        existing_dataset_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(dataset_records)")
        }
        for column in (
            "raw_antibody_id",
            "canonical_antibody_id",
            "canonical_entity_key",
            "antibody_variant",
            "entity_role",
            "experiment_id",
            "measurement_id",
            "donor_id",
            "species_json",
            "assay_system",
            "assay_method",
            "assay_protocol_id",
            "assay_keywords_json",
            "antigen_target",
            "assay_keyword_status",
            "assay_keyword_library_json",
            "assay_keyword_unmatched_json",
            "antibody_keywords_json",
            "assay_protocol_summary_zh",
            "assay_protocol_summary_en",
            "agent_review_status",
            "anarci_review_status",
            "esm2_review_status",
        ):
            if column not in existing_dataset_columns:
                connection.execute(f"ALTER TABLE dataset_records ADD COLUMN {column} TEXT")
        connection.execute(
            "INSERT OR REPLACE INTO runs(run_id, document_id, state_json) VALUES (?, ?, ?)",
            (run_id, state.get("config", {}).get("document_id", ""), json.dumps(state, ensure_ascii=False)),
        )
        for candidate in state.get("candidates", []):
            connection.execute(
                """INSERT OR REPLACE INTO table_candidates
                   (run_id, table_id, image, page, role_json, selection_score,
                    selected, status, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    candidate.get("table_id", ""),
                    candidate.get("image", ""),
                    candidate.get("page"),
                    json.dumps(candidate.get("roles", []), ensure_ascii=False),
                    candidate.get("selection_score", 0.0),
                    int(candidate.get("selected", False)),
                    candidate.get("status", "candidate"),
                    json.dumps(candidate.get("reasons", []), ensure_ascii=False),
                ),
            )
        for block in state.get("document_blocks", []):
            connection.execute(
                """INSERT OR REPLACE INTO document_blocks
                   (run_id, block_id, page, bbox_json, text, block_type, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    block.get("block_id", ""),
                    block.get("page", 0),
                    json.dumps(block.get("bbox", []), ensure_ascii=False),
                    block.get("text", ""),
                    block.get("block_type", "paragraph"),
                    block.get("source", "pdf_text"),
                ),
            )
        for context in state.get("table_contexts", []):
            connection.execute(
                """INSERT OR REPLACE INTO table_contexts
                   (run_id, table_id, logical_table_id, table_number, page,
                    bbox_json, caption_json, nearby_context_json,
                    referring_sentences_json, footnotes_json, continuation_of,
                    continuation_confidence, context_status, context_image)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    context.get("table_id", ""),
                    context.get("logical_table_id", context.get("table_id", "")),
                    context.get("table_number"),
                    context.get("page"),
                    json.dumps(context.get("bbox", []), ensure_ascii=False),
                    json.dumps(context.get("caption"), ensure_ascii=False),
                    json.dumps(context.get("nearby_context", []), ensure_ascii=False),
                    json.dumps(context.get("referring_sentences", []), ensure_ascii=False),
                    json.dumps(context.get("footnotes", []), ensure_ascii=False),
                    context.get("continuation_of"),
                    context.get("continuation_confidence"),
                    context.get("context_status", "missing"),
                    context.get("context_image"),
                ),
            )
        for classification in state.get("table_classifications", []):
            connection.execute(
                """INSERT OR REPLACE INTO table_classifications
                   (run_id, table_id, primary_label, labels_json, confidence,
                    entity_axis, measurement_axis, retain_for_pipeline,
                    review_required, evidence_json, reasons_json,
                    classification_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    classification.get("table_id", ""),
                    classification.get("primary_label", "ambiguous"),
                    json.dumps(classification.get("labels", []), ensure_ascii=False),
                    classification.get("confidence", 0.0),
                    classification.get("entity_axis", "unknown"),
                    classification.get("measurement_axis", "unknown"),
                    int(classification.get("retain_for_pipeline", False)),
                    int(classification.get("review_required", False)),
                    json.dumps(classification.get("evidence", []), ensure_ascii=False),
                    json.dumps(classification.get("reasons", []), ensure_ascii=False),
                    json.dumps(classification, ensure_ascii=False),
                ),
            )
        for sequence in state.get("sequences", []):
            connection.execute(
                """INSERT INTO sequence_candidates
                   (run_id, table_id, image, row_index, col_index, raw_text,
                    sequence, chain, length, validation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    sequence.get("table_id", ""),
                    sequence.get("image", ""),
                    sequence.get("row_index", 0),
                    sequence.get("col_index", 0),
                    sequence.get("raw_text", ""),
                    sequence.get("normalized_sequence", ""),
                    sequence.get("chain", "unknown"),
                    sequence.get("length", 0),
                    sequence.get("validation_status", "review"),
                ),
            )
        for task in state.get("review_tasks", []):
            connection.execute(
                """INSERT OR REPLACE INTO review_queue
                   (run_id, task_id, kind, severity, message, table_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    task.get("task_id", ""),
                    task.get("kind", "ocr"),
                    task.get("severity", "medium"),
                    task.get("message", ""),
                    task.get("table_id"),
                    task.get("status", "open"),
                ),
            )
        for resolved in state.get("resolved_sequences", []):
            connection.execute(
                """INSERT OR REPLACE INTO resolved_sequences
                   (run_id, construct_key, vh_seq_id, vl_seq_id, vh, vl,
                    status, sequence_table_ids_json, sequence_rows_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    resolved.get("construct_key", ""),
                    resolved.get("vh_seq_id"),
                    resolved.get("vl_seq_id"),
                    resolved.get("vh", ""),
                    resolved.get("vl", ""),
                    resolved.get("status", "review"),
                    json.dumps(resolved.get("sequence_table_ids", []), ensure_ascii=False),
                    json.dumps(resolved.get("sequence_rows", []), ensure_ascii=False),
                ),
            )
        for validation in state.get("construct_validations", []):
            connection.execute(
                """INSERT OR REPLACE INTO construct_validations
                   (run_id, construct_key, family, pair_status,
                    validation_level, validation_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    validation.get("construct_key", ""),
                    validation.get("family"),
                    validation.get("pair_status", "review"),
                    validation.get("validation_level", "rule_based"),
                    json.dumps(validation, ensure_ascii=False),
                ),
            )
        for record in state.get("dataset_records", []):
            connection.execute(
                """INSERT OR REPLACE INTO dataset_records
                   (run_id, record_id, antibody_id, raw_antibody_id,
                    canonical_antibody_id, canonical_entity_key,
                    antibody_variant, entity_role, experiment_group,
                    experiment_id, measurement_id, donor_id, species_json,
                    assay_system, assay_method, assay_protocol_id,
                    assay_keywords_json, antibody_keywords_json,
                    assay_protocol_summary_zh, assay_protocol_summary_en,
                    vh, vl, ic50, ic50_unit, agent_review_status,
                    anarci_review_status, link_status, source,
                    sequence_table_id, assay_table_id, evidence_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    record.get("record_id", ""),
                    record.get("antibody_id", ""),
                    record.get("raw_antibody_id"),
                    record.get("canonical_antibody_id"),
                    record.get("canonical_entity_key"),
                    record.get("antibody_variant"),
                    record.get("entity_role"),
                    record.get("experiment_group", ""),
                    record.get("experiment_id"),
                    record.get("measurement_id"),
                    record.get("donor_id"),
                    json.dumps(record.get("species", []), ensure_ascii=False),
                    record.get("assay_system"),
                    record.get("assay_method"),
                    record.get("assay_protocol_id"),
                    json.dumps(record.get("assay_keywords", []), ensure_ascii=False),
                    json.dumps(record.get("antibody_keywords", []), ensure_ascii=False),
                    record.get("assay_protocol_summary_zh"),
                    record.get("assay_protocol_summary_en"),
                    record.get("vh", ""),
                    record.get("vl", ""),
                    record.get("ic50"),
                    record.get("ic50_unit", "nM"),
                    record.get("agent_review_status", "manual_review"),
                    record.get("anarci_review_status", "manual_review"),
                    record.get("link_status", "review"),
                    record.get("source", "ocr"),
                    record.get("sequence_table_id"),
                    record.get("assay_table_id"),
                    json.dumps(record.get("evidence", []), ensure_ascii=False),
                ),
            )
            connection.execute(
                "UPDATE dataset_records SET antigen_target=?, assay_keyword_status=?, "
                "assay_keyword_library_json=?, assay_keyword_unmatched_json=?, esm2_review_status=? WHERE run_id=? AND record_id=?",
                (record.get("antigen_target"), record.get("assay_keyword_status"),
                 json.dumps(record.get("assay_keyword_library"), ensure_ascii=False),
                 json.dumps(record.get("assay_keyword_unmatched", []), ensure_ascii=False),
                 record.get("esm2_review_status", "manual_review"),
                 run_id, record.get("record_id", "")),
            )
        connection.commit()
    artifacts["database"] = str(db_path)
    return artifacts
