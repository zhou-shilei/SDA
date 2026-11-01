"""LLM table structure, classification, document planning, and plan recovery."""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from .assays import (
    identified_regions,
    materialize_profiles,
    region_endpoints,
    table_profile_summaries,
)
from .llm_io import (
    AssayBindingPlan,
    ClassificationBatch,
    DocumentPlan,
    TableStructurePlan,
    TemplatePlan,
    build_evidence,
    cell_ref,
    clipped_text,
    contains_entity_name,
    find_image,
    normalize,
    metric_endpoints,
    pack_table,
    require_refs,
    table_id,
)
from .llm_runtime import CodexProvider, CodexProviderError, CodexUnavailable, json_write, log_llm_event
from .table_classification import TARGET_LABELS
from .table_structure import semantic_table, verified_header_links
from .table_payload import inherited_header_view, pack_semantic_table, semantic_cell_refs

# Table and document semantic agents

PROMPTS = Path(__file__).resolve().parent / "prompt"
PROMPT_VERSION = 6
TEMPLATE_PROMPT_VERSION = 8


class LLMTableStructureAgent:
    """One bounded image review; only existing OCR tokens may be rearranged."""
    def __init__(self, provider: CodexProvider) -> None:
        self.provider = provider

    def run(self, table: dict, image: Path) -> dict:
        from .table_structure import apply_structure_plan

        evidence = build_evidence([table])
        def validate(plan: dict) -> None:
            apply_structure_plan(table, plan)
            for finding in plan["unresolved"]:
                require_refs(finding["evidence_refs"], evidence)
        plan, meta = self.provider.generate(
            "table-structure-v1", (PROMPTS / "table_structure.txt").read_text(encoding="utf-8"),
            {"table": pack_table(table), "tokens": table.get("ocr_items", [])},
            TableStructurePlan, images=[image], validate=validate)
        return {**apply_structure_plan(table, plan), "structure_agent": meta}


def _metric_check(metric: dict[str, Any], evidence: dict[str, dict[str, Any]], identity: str, width: int, height: int, header_sources: list[str] | None = None, *, executable: bool = True) -> None:
    refs = metric["header_refs"]
    require_refs(refs + metric["unit_refs"], evidence)
    allowed = {identity, *(header_sources or [])}
    if (not refs and executable) or any(evidence[ref].get("table_id") not in allowed or evidence[ref].get("kind") != "cell" for ref in refs + metric["unit_refs"]):
        raise ValueError(f"{identity}: metric headers/units must cite this table or its verified continuation header")
    if metric["value_column"] is not None and any(
        evidence[ref].get("table_id") != identity and evidence[ref].get("col_index") != metric["value_column"]
        for ref in refs + metric["unit_refs"]
    ):
        raise ValueError(f"{identity}: inherited metric headers must align with their value column")
    header_text = " ".join(evidence[ref]["text"] for ref in refs)
    text = normalize(header_text)
    endpoint = metric["endpoint"]
    if refs and endpoint in {"IC50", "IC90", "EC50", "EC90"}:
        if endpoint not in metric_endpoints(header_text):
            raise ValueError(f"{identity}: {endpoint} absent from cited headers")
    if refs and endpoint == "KD" and "KD" not in metric_endpoints(header_text) and not re.search(r"affinity|亲和", text, re.I):
        raise ValueError(f"{identity}: KD absent from cited headers")
    for field, limit in (("value_column", width), ("value_row", height)):
        value = metric[field]
        if value is not None and not 0 <= value < limit:
            raise ValueError(f"{identity}: {field} {value} outside table")
    if executable and metric["value_column"] is None and metric["value_row"] is None:
        raise ValueError(f"{identity}: metric needs a row or column address")
    if metric["unit"]:
        unit = normalize(metric["unit"]).replace("μ", "µ")
        unit_text = normalize(" ".join(evidence[ref]["text"] for ref in metric["unit_refs"])).replace("μ", "µ")
        if not metric["unit_refs"] or unit not in unit_text:
            raise ValueError(f"{identity}: unit {metric['unit']} lacks supporting evidence")
        if endpoint == "KD" and re.search(r"(?:/s|s\^?-?1|1/s)", unit, re.I):
            raise ValueError(f"{identity}: dissociation rate units cannot be assigned to KD")


def validate_classifications(
    response: dict[str, Any],
    tables: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    header_links: dict[str, list[str]] | None = None,
) -> None:
    by_id = {table_id(table): table for table in tables}
    ids = [item["table_id"] for item in response["tables"]]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("Return exactly one classification for every input table_id")
    for item in response["tables"]:
        identity = item["table_id"]
        table = by_id[identity]
        height = len(table.get("rows", []))
        width = max((len(row) for row in table.get("rows", [])), default=0)
        if item["primary_label"] not in item["labels"]:
            raise ValueError(f"{identity}: primary label must appear in labels")
        require_refs(item["evidence_refs"], evidence)
        if not any(evidence[ref].get("table_id") == identity for ref in item["evidence_refs"]):
            raise ValueError(f"{identity}: classification must cite its own table evidence")
        for issue in item["issues"]:
            require_refs(issue["evidence_refs"], evidence)
        assay_regions = []
        for region in item["regions"]:
            if not 0 <= region["row_start"] <= region["row_end"] < height:
                raise ValueError(f"{identity}: invalid data row bounds")
            if any(not 0 <= index < height for index in region["header_rows"]):
                raise ValueError(f"{identity}: header row outside table")
            visible_rows = {source["row_index"] for source in evidence.values()
                            if source.get("kind") == "cell" and source.get("table_id") == identity}
            if not set(region["header_rows"]) <= visible_rows:
                raise ValueError(f"{identity}: header row was not supplied")
            columns = [*region["condition_columns"], *region["excluded_columns"]]
            if region["entity_column"] is not None:
                columns.append(region["entity_column"])
            columns.extend(region[key] for key in ("sequence_id_column", "sequence_column", "chain_column", "name_column") if region[key] is not None)
            if any(not 0 <= index < width for index in columns):
                raise ValueError(f"{identity}: column outside table")
            if region["kind"] == "assay":
                assay_regions.append(region)
                if item["decision"] == "extract" and region["entity_column"] is None and not region["entity_groups"]:
                    raise ValueError(f"{identity}: assay requires explicit entity addresses")
            metrics = list(region["metrics"])
            for group in region["entity_groups"]:
                require_refs([group["name_ref"]], evidence)
                source = evidence[group["name_ref"]]
                if source.get("table_id") != identity or source.get("kind") != "cell":
                    raise ValueError(f"{identity}: entity name must cite this table's OCR cell")
                if not normalize(group["raw_name"]) or normalize(group["raw_name"]) not in normalize(source["text"]):
                    raise ValueError(f"{identity}: entity name not present in cited cell")
                if re.search(r"\bratio\b|比值|比例", group["raw_name"], re.I):
                    raise ValueError(f"{identity}: ratio is not an antibody group")
                if group["value_column"] is not None and not 0 <= group["value_column"] < width:
                    raise ValueError(f"{identity}: entity value column outside table")
                metrics.extend(group["metrics"])
            for metric in metrics:
                _metric_check(metric, evidence, identity, width, height,
                              (header_links or {}).get(identity), executable=item["decision"] == "extract")
                if metric["value_column"] in region["excluded_columns"]:
                    raise ValueError(f"{identity}: metric uses an excluded ratio column")
            for comparison in region.get("comparisons", []):
                refs = [*comparison["header_refs"], comparison["numerator_ref"], comparison["denominator_ref"]]
                require_refs(refs, evidence)
                if any(evidence[ref].get("kind") != "cell" or evidence[ref].get("table_id") != identity for ref in refs):
                    raise ValueError("Comparison must cite original cells in this table")
                column = comparison["value_column"]
                if column not in region["excluded_columns"] or not 0 <= column < width:
                    raise ValueError("Comparison column must be excluded from antibody measurements")
                headers = " ".join(evidence[ref]["text"] for ref in comparison["header_refs"])
                if not re.search(r"ratio|比值|比例", headers, re.I):
                    raise ValueError("Comparison lacks ratio header")
                _metric_check({"endpoint": comparison["endpoint"], "value_column": column, "value_row": None,
                               "header_refs": comparison["header_refs"], "unit": None, "unit_refs": []}, evidence, identity, width, height)
                numerator, denominator = (normalize(evidence[comparison[k]]["text"]) for k in ("numerator_ref", "denominator_ref"))
                ratio = normalize(headers).split("ratio")[0]
                parts = re.split(r"[:：/]", ratio)
                if len(parts) != 2 or not numerator or numerator == denominator or not numerator.startswith(parts[0]) or not denominator.startswith(parts[1]):
                    raise ValueError("Comparison direction lacks explicit numerator:denominator header")
        if item["decision"] == "extract" and set(item["labels"]) & {"assay_ic50", "assay_ec50"}:
            if not assay_regions or not any(region["metrics"] or any(group["metrics"] for group in region["entity_groups"]) for region in assay_regions):
                raise ValueError(f"{identity}: assay extraction requires a measurement plan")



def finalize_table_plans(
    decisions: list[dict[str, Any]], batch: list[dict[str, Any]], scoped: dict[str, Any],
    meta: dict[str, Any], header_links: dict[str, list[str]], *, owner: str = "TableClassificationAgent",
) -> list[dict[str, Any]]:
    by_id = {table_id(table): table for table in batch}
    classified = []
    for decision in decisions:
        table = by_id[decision["table_id"]]
        model_decision = decision["decision"]
        structure_issues = list((table.get("structure_plan") or {}).get("unresolved", []))
        if (table.get("structure_agent") or {}).get("status") == "needs_review":
            structure_issues.append({"code": "structure_review_failed", "message": table["structure_agent"]["error"],
                                     "evidence_refs": [decision["table_id"]]})
        audit = table.get("token_audit", {})
        if audit.get("duplicate_indices") or audit.get("unknown_indices"):
            structure_issues.append({"code": "token_assignment_conflict", "message": "OCR tokens have duplicate or unknown cell assignments.",
                                     "evidence_refs": [decision["table_id"]]})
        if structure_issues:
            decision = {**decision, "issues": [*decision["issues"], *structure_issues]}
            if owner == "TemplateAgent" and decision["decision"] == "exclude":
                decision = {**decision, "decision": "needs_review"}
        if owner != "TemplateAgent" and not set(decision["labels"]) & TARGET_LABELS and decision["decision"] in {"extract", "context_only"}:
            decision = {**decision, "decision": "exclude"}
        regions = []
        for region in decision["regions"]:
            try:
                validate_classifications({"tables": [{**decision, "decision": "extract", "regions": [region]}]}, [table], scoped, header_links)
            except ValueError as exc:
                regions.append({**region, "execution_status": "needs_review", "validation_error": str(exc)})
            else:
                regions.append({**region, "execution_status": "ready"})
        retain = bool(set(decision["labels"]) & TARGET_LABELS) and decision["decision"] != "exclude" and (
            decision["decision"] in {"extract", "context_only"} or any(region["execution_status"] == "ready" for region in regions))
        if owner == "TemplateAgent":
            # Only an explicit exclusion drops evidence. Unknown/unsupported
            # structure remains visible to later readers and review tools.
            retain = decision["decision"] != "exclude"
        inherited_header = None
        inherited_parent_id = None
        for parent_id in header_links.get(decision["table_id"], []):
            parent = by_id.get(parent_id)
            if parent:
                inherited_header = inherited_header_view(parent, parent_id)
                if inherited_header:
                    inherited_parent_id = parent_id
                    break
        header_status = "inherited" if inherited_header else pack_semantic_table(table)["header_status"]
        inheritance_record = None
        if inherited_header:
            source_refs = [ref for row in inherited_header["rows"] for ref, text in row["cells"] if text]
            inheritance_record = {
                "status": "inherited",
                "source_table_id": inherited_parent_id,
                "source_header_row_indices": inherited_header["source_header_row_indices"],
                "source_refs": source_refs,
            }
        classified.append({
            **decision, "schema_version": 3, "image": table.get("image", ""),
            "regions": identified_regions(decision["table_id"], regions), "allowed_header_table_ids": header_links.get(decision["table_id"], []),
            "header_status": header_status,
            **({"header_inheritance": inheritance_record} if inheritance_record else {}),
            "structure_repair": table.get("structure_repair"),
            "structure_plan": table.get("structure_plan"), "structure_agent": table.get("structure_agent"),
            "token_audit": table.get("token_audit", {}),
            "model_decision": model_decision,
            "page": table.get("page"), "retain_for_pipeline": retain,
            "review_required": decision["decision"] == "needs_review" or bool(decision["issues"]),
            "understanding_status": "identified" if decision["primary_label"] != "ambiguous" else "uncertain",
            "extraction_status": "ready" if regions and all(r["execution_status"] == "ready" for r in regions) and decision["decision"] == "extract" and not decision["issues"] else "partial" if any(r["execution_status"] == "ready" for r in regions) else "needs_review",
            "evidence": [{"ref": ref, **scoped[ref]} for ref in decision["evidence_refs"]],
            "reasons": [decision["explanation"]],
            "classifier": {"name": owner, "version": 3,
                           "mode": "llm", "prompt_version": TEMPLATE_PROMPT_VERSION if owner == "TemplateAgent" else PROMPT_VERSION, **meta,
                           "input_scope": "all_rows" if owner == "TemplateAgent" else "headers_and_one_data_row"},
        })
    return classified


class LLMTableClassificationAgent:
    """Legacy standalone classifier for historical benchmarks; not a pipeline stage."""
    def __init__(
        self, provider: CodexProvider, *, image_roots: list[Path],
        batch_size: int = 4, max_workers: int = 6, coarse_triage: bool = False,
    ) -> None:
        if not 1 <= batch_size <= 8 or not 0 <= max_workers <= 8:
            raise ValueError("Classification batch_size must be 1–8; max_workers must be 0–8 (0 means all tables)")
        self.provider = provider
        self.image_roots = image_roots
        self.batch_size = 1 if max_workers == 0 else batch_size
        self.max_workers = max_workers
        self.coarse_triage = coarse_triage

    def run(
        self,
        ocr_results: list[dict[str, Any]],
        contexts: list[dict[str, Any]] | None = None,
        *, target_table_ids: set[str] | None = None,
        repair_findings: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        ocr_results = [semantic_table(table) for table in ocr_results]
        for index, table in enumerate(ocr_results):
            if target_table_ids is not None and table_id(table) not in target_table_ids:
                continue
            needs_structure = any(
                r.get("kind") == "recover_unassigned_token" for r in table.get("structure_repair", {}).get("repairs", [])) or any(
                re.fullmatch(r"[\d.]+(?:<br>\d+)+", str(cell.get("text", "")))
                for row in table.get("rows", []) for cell in row)
            image = find_image(table, self.image_roots)
            if needs_structure and table.get("ocr_items") and image and not table.get("structure_plan"):
                try:
                    ocr_results[index] = LLMTableStructureAgent(self.provider).run(table, image)
                except CodexUnavailable:
                    raise
                except (CodexProviderError, ValueError) as exc:
                    ocr_results[index] = {**table, "structure_agent": {"status": "needs_review", "error": str(exc)}}
        if len({table_id(table) for table in ocr_results}) != len(ocr_results):
            raise ValueError("Classification table identifiers must be unique")
        evidence = build_evidence(ocr_results, contexts)
        header_links = verified_header_links(ocr_results, contexts or [])
        all_tables = {table_id(table): table for table in ocr_results}
        instructions = (PROMPTS / "table_classification.txt").read_text(encoding="utf-8")

        def classify(batch: list[dict[str, Any]], *, allow_retry: bool = True) -> list[dict[str, Any]]:
            images: list[Path] = []
            packed = []
            support = []
            allowed_refs: set[str] = set()
            targets = {table_id(table) for table in batch}
            support_ids = {source for identity in targets for source in header_links.get(identity, [])} - targets
            for table in [*batch, *(all_tables[identity] for identity in sorted(support_ids))]:
                item = pack_semantic_table(table, sample_data_row=True)
                identity = table_id(table)
                item["allowed_header_table_ids"] = header_links.get(identity, [])
                item["context"] = [
                    {"ref": ref, "text": clipped_text(source["text"], 2400), "page": source.get("page")}
                    for ref, source in evidence.items()
                    if source.get("table_id") == identity and source.get("kind") == "context"
                ]
                allowed_refs.update(semantic_cell_refs(item))
                allowed_refs.update(source["ref"] for source in item["context"])
                allowed_refs.add(identity)
                (packed if identity in targets else support).append(item)
            scoped = {ref: evidence[ref] for ref in allowed_refs}
            try:
                response, meta = self.provider.generate(
                    f"table-classification-v{PROMPT_VERSION}", instructions,
                    {"tables": packed, "support_tables": support, "repair_findings": repair_findings or []}, ClassificationBatch, images=images,
                    validate=lambda response: validate_classifications(response, batch, scoped, header_links),
                    validation_attempts=1 if len(batch) > 1 else 2,
                )
            except CodexUnavailable:
                raise
            except CodexProviderError as exc:
                # Keep independently valid answers from the same completed model
                # response; retry only the rejected table, not the full batch.
                if not allow_retry:
                    identity = table_id(batch[0])
                    return [{"table_id": identity, "image": batch[0].get("image", ""),
                             "page": batch[0].get("page"), "primary_label": "ambiguous",
                             "labels": ["ambiguous"], "decision": "needs_review",
                             "confidence": 0, "regions": [], "evidence_refs": [identity],
                             "evidence": [{"ref": identity, **scoped[identity]}],
                             "explanation": str(exc), "reasons": [str(exc)],
                             "issues": [{"code": "classification_failed", "message": str(exc), "evidence_refs": [identity]}],
                             "retain_for_pipeline": False, "review_required": True,
                             "classifier": {"name": "TableClassificationAgent", "mode": "error",
                                            "model": self.provider.model, "reasoning_effort": self.provider.effort}}]
                recovered = []
                decisions = (exc.response or {}).get("tables", [])
                for table in batch:
                    identity = table_id(table)
                    matching = [item for item in decisions if item.get("table_id") == identity]
                    if len(matching) == 1:
                        try:
                            validate_classifications({"tables": matching}, [table], scoped, header_links)
                        except ValueError:
                            pass
                        else:
                            recovered.extend(finalize_table_plans(matching, [table], scoped, {
                                "model": self.provider.model, "reasoning_effort": self.provider.effort,
                                "status": "validated", "response_source": "individually_revalidated_batch",
                                "batch_error": str(exc),
                            }, header_links))
                            continue
                    recovered.extend(classify([table], allow_retry=False))
                return recovered
            return finalize_table_plans(response["tables"], batch, scoped, meta, header_links)


        ocr_results = [t for t in ocr_results if target_table_ids is None or table_id(t) in target_table_ids]
        identities = [table_id(table) for table in ocr_results]
        # The former coarse pass read every cell and the full image. Direct
        # sampled classification avoids that duplicate full-table LLM input.
        routing = False
        dispatch_size = self.batch_size
        batches = [ocr_results[start:start + dispatch_size] for start in range(0, len(ocr_results), dispatch_size)]
        workers = min(self.max_workers or len(batches), len(batches))
        ordered: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        started = time.monotonic()
        prefix = "repair_classification" if repair_findings else "classification"

        def checkpoint() -> None:
            nonlocal results
            results = [ordered[identity] for identity in identities if identity in ordered]
            if self.provider.audit_path:
                json_write(self.provider.audit_path.parent / f"{prefix}_progress.json", results)
            if identities:
                print(f"llm-classification {len(results)}/{len(identities)}", flush=True)

        checkpoint()
        print(f"llm-classification-runtime workers={workers} batch_size={self.batch_size} batches={len(batches)}", flush=True)
        completed_batches = 0
        batch_runs: list[dict[str, Any]] = []
        status = "error"

        def run_batch(index: int, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            run = batch_runs[index]
            batch_started = time.monotonic()
            run["started_seconds"] = round(batch_started - started, 3)
            run["status"] = "error"
            try:
                result = []
                for start in range(0, len(batch), self.batch_size):
                    result.extend(classify(batch[start:start + self.batch_size]))
                run["coarse_excluded_tables"] = 0
                run["detailed_tables"] = len(batch)
                run["status"] = "completed"
                run["cache_hit_tables"] = sum(bool(item.get("classifier", {}).get("cache_hit")) for item in result)
                run["review_tables"] = sum(bool(item.get("review_required")) for item in result)
                return result
            finally:
                run["elapsed_seconds"] = round(time.monotonic() - batch_started, 3)

        try:
            if batches:
                batch_runs = [{"batch_index": index + 1, "table_ids": [table_id(table) for table in batch],
                               "status": "not_started"} for index, batch in enumerate(batches)]
                remaining = iter(enumerate(batches))
                failure: Exception | None = None
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="table-classification") as pool:
                    pending = {pool.submit(run_batch, *next(remaining)) for _ in range(workers)}
                    while pending:
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in finished:
                            try:
                                classified = future.result()
                            except Exception as exc:
                                if failure is None:
                                    failure = exc
                            else:
                                ordered.update((item["table_id"], item) for item in classified)
                                completed_batches += 1
                                checkpoint()
                        if failure is None:
                            for _ in range(workers - len(pending)):
                                entry = next(remaining, None)
                                if entry is None:
                                    break
                                pending.add(pool.submit(run_batch, *entry))
                    if failure is not None:
                        raise failure
            status = "completed"
            return results
        finally:
            if self.provider.audit_path:
                json_write(self.provider.audit_path.parent / f"{prefix}_performance.json", {
                    "status": status, "workers": workers, "max_workers": self.max_workers, "batch_size": self.batch_size,
                    "coarse_triage": routing, "dispatch_size": dispatch_size,
                    "total_batches": len(batches), "completed_batches": completed_batches,
                    "total_tables": len(identities), "completed_tables": len(results),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "batches": batch_runs,
                })


def prepare_table_title_input(
    tables: list[dict[str, Any]],
    method_pages: list[dict[str, Any]] | None,
    contexts: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach one coordinate-linked table title and retain only title metadata."""
    from .template_agent import infer_table_captions

    contexts = contexts or []
    context_by_id = {str(item.get("table_id")): item for item in contexts if item.get("table_id")}
    inferred = infer_table_captions(tables, contexts, method_pages or [])
    titled_tables: list[dict[str, Any]] = []
    title_records: list[dict[str, Any]] = []
    title_contexts: list[dict[str, Any]] = []

    def descriptive(value: Any) -> bool:
        value = re.sub(r"^(?:table|表)\s*[\w.-]+\s*[.:：、]?\s*", "", str(value or ""), flags=re.I).strip()
        return bool(value)

    for table in tables:
        identity = table_id(table)
        context = context_by_id.get(identity, {})
        caption_obj = context.get("caption") or {}
        context_caption = caption_obj.get("text", "") if isinstance(caption_obj, dict) else str(caption_obj)
        inferred_caption = inferred.get(identity, {})
        table_caption = str(table.get("caption") or "")
        candidates = [
            (table_caption, table.get("caption_source") or "prepared_table.caption", table.get("caption_confidence")),
            (inferred_caption.get("caption", ""), inferred_caption.get("source", "page_ocr"), inferred_caption.get("caption_confidence")),
            (context_caption, caption_obj.get("source", "pdf_text") if isinstance(caption_obj, dict) else "pdf_text",
             caption_obj.get("confidence") if isinstance(caption_obj, dict) else None),
        ]
        # Prefer a complete title over a bare "Table N." marker. Prepared table
        # captions are already attached to that exact table; inferred captions
        # are selected by page and geometry; context captions are the fallback.
        selected = next((item for item in candidates if descriptive(item[0])), None)
        if selected is None:
            selected = next((item for item in candidates if item[0]), ("", "missing", None))
        title, source, confidence = selected
        titled_tables.append({**table, "caption": title})
        title_records.append({
            "table_id": identity,
            "page": table.get("page"),
            "title": title,
            "source": source,
            "confidence": confidence,
            "evidence_ref": identity,
        })

        # Keep only geometrical links needed for table/header structure and the
        # printed title. Nearby prose and footnotes never enter TemplateAgent.
        safe_context = {key: context[key] for key in (
            "table_id", "page", "bbox", "table_number", "logical_table_id",
            "continuation_of", "continuation_confidence",
        ) if key in context}
        if title:
            safe_context["caption"] = {
                "evidence_id": (caption_obj.get("evidence_id") if isinstance(caption_obj, dict) else None) or identity,
                "page": table.get("page"),
                "bbox": (caption_obj.get("bbox", []) if isinstance(caption_obj, dict) else []),
                "text": title,
                "source": source,
                "relation": "caption",
                "confidence": confidence,
            }
        title_contexts.append(safe_context)
    return titled_tables, title_records, title_contexts


def build_document_input(
    document_id: str,
    tables: list[dict[str, Any]],
    method_pages: list[dict[str, Any]] | None = None,
    contexts: list[dict[str, Any]] | None = None,
    *, plan_tables: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build a source payload from table titles, headers, and cells only."""
    if method_pages or contexts:
        tables, _, contexts = prepare_table_title_input(tables, method_pages, contexts)
    evidence = build_evidence(tables)
    packed_tables = []
    allowed: set[str] = set()
    omissions = []
    used_characters = 0
    # Assay regions have no later sequence-style retrieval loop. Reserve their
    # evidence before wide construct/CDR tables can exhaust the planning budget.
    priority = {"assay_ic50": 0, "binding_kd": 0, "sequence_mapping": 1, "construct_variant": 2, "sequence_definition": 4}
    def table_priority(table):
        classification = table.get("table_classification", {})
        return 0 if any(r.get("kind") == "assay" for r in classification.get("regions", [])) else priority.get(classification.get("primary_label"), 3)
    ordered = tables if plan_tables else sorted(tables, key=table_priority)
    header_links = verified_header_links(tables, contexts or [])
    by_id = {table_id(table): table for table in tables}
    for table in ordered:
        identity = table_id(table)
        classification = table.get("table_classification", {})
        inherited_header = None
        for parent_id in header_links.get(identity, []):
            parent = by_id.get(parent_id)
            if parent:
                inherited_header = inherited_header_view(parent, parent_id)
                if inherited_header:
                    break
        item = pack_semantic_table(table, inherited_header=inherited_header)
        classification = {**classification, "regions": identified_regions(identity, classification.get("regions", []))}
        if plan_tables:
            item["allowed_header_table_ids"] = header_links.get(identity, [])
        else:
            item["classification"] = {key: classification.get(key) for key in ("labels", "entity_axis", "measurement_axis", "regions")}
        size = len(json.dumps(item, ensure_ascii=False))
        if used_characters + size > 240000:
            omissions.append(identity)
            continue
        used_characters += size
        allowed.add(identity)
        allowed.update(semantic_cell_refs(item))
        packed_tables.append(item)
    payload = {"document_id": document_id, "tables": packed_tables}
    if omissions:
        payload["omitted_table_ids"] = omissions
    return payload, {ref: evidence[ref] for ref in allowed}


def validate_assay_profiles(profiles: list[dict], evidence: dict, table_ids: set[str], regions: dict[str, list[dict]] | None = None) -> None:
    assignments: set[tuple] = set()
    for profile in profiles:
        identity = profile["table_id"]
        if identity not in table_ids:
            raise ValueError("Assay profile references unknown table")
        require_refs(profile["evidence_refs"], evidence)
        if regions is not None:
            scopes = {r["region_id"]: r for r in identified_regions(identity, regions.get(identity, [])) if r.get("kind") == "assay"}
            scope = scopes.get(profile.get("region_id"))
            if scope is None:
                raise ValueError("Assay profile must select an existing assay region_id")
            if not profile["endpoints"] or not set(profile["endpoints"]) <= region_endpoints(scope):
                raise ValueError("Assay profile endpoints do not belong to selected region")
        for endpoint in profile["endpoints"]:
            key = (identity, profile.get("region_id"), endpoint)
            if key in assignments:
                raise ValueError("Overlapping assay profiles must remain unresolved")
            assignments.add(key)


def validate_document_plan(plan: dict[str, Any], document_id: str, evidence: dict[str, dict[str, Any]], table_ids: set[str], required_names: list[dict[str, Any]] | None = None, regions: dict[str, list[dict]] | None = None) -> None:
    if plan["document_id"] != document_id:
        raise ValueError("Document identity changed in model response")
    identities = [item["entity_id"] for item in plan["entities"]]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate entity_id")
    names: dict[str, str] = {}
    for entity in plan["entities"]:
        require_refs(entity["evidence_refs"], evidence)
        require_refs(entity["alias_evidence_refs"], evidence)
        if len({normalize(name) for name in entity["raw_names"]}) > 1:
            alias_text = " ".join(evidence[ref]["text"] for ref in entity["alias_evidence_refs"])
            compact_alias = normalize(alias_text)
            # Patent prose also explicitly defines aliases using apposition:
            # "BMK1 为阳性对照分子（Dupilumab 抗体）". Require the two names
            # in that precise identity clause, not mere co-occurrence.
            names_here = [normalize(name) for name in entity["raw_names"]]
            apposition = len(names_here) == 2 and any(re.search(
                rf"{re.escape(left)}(?:为|是)(?:(?:阳性|阴性|参考|对照|分子|抗体|单克隆|人源化|单抗|的)){{0,6}}"
                rf"[（(]{re.escape(right)}(?:抗体|分子)?[）)]", compact_alias)
                for left, right in (names_here, names_here[::-1]))
            definition = len(names_here) == 2 and any(re.search(
                rf'''(?<![a-z0-9])["“']?{re.escape(left)}["”']?(?:refers?to|denotes|means)(?:the)?["“']?{re.escape(right)}(?![a-z0-9])''',
                compact_alias) for left, right in (names_here, names_here[::-1]))
            if not apposition and not definition and not re.search(r"also\s+(?:known|called|referred)|same\s+(?:antibody|construct)|also\s+designated|又称|亦称|也称|即为|同一", alias_text, re.I):
                raise ValueError(f"Entity {entity['entity_id']} aliases {entity['raw_names']} require explicit alias-identity prose")
            if any(normalize(name) not in normalize(alias_text) for name in entity["raw_names"]):
                raise ValueError("Alias-identity prose must name every merged entity alias")
        if entity["parent_entity_id"] is not None and (entity["parent_entity_id"] not in identities or entity["parent_entity_id"] == entity["entity_id"]):
            raise ValueError("Entity parent must identify a different document entity")
        source = " ".join(evidence[ref]["text"] for ref in entity["evidence_refs"])
        for name in entity["raw_names"]:
            normalized = normalize(name)
            if not normalized or not contains_entity_name(source, name):
                raise ValueError(f"Entity {name!r} absent from its cited evidence")
            if normalized in names and names[normalized] != entity["entity_id"]:
                raise ValueError(f"Raw name {name!r} maps to conflicting entities")
            names[normalized] = entity["entity_id"]
    parents = {entity["entity_id"]: entity["parent_entity_id"] for entity in plan["entities"]}
    for identity in parents:
        visited = set()
        current = identity
        while current is not None:
            if current in visited:
                raise ValueError("Entity parent relationships contain a cycle")
            visited.add(current)
            current = parents[current]
    assignments: dict[tuple[str, str, str], int] = {}
    for link in plan["sequence_links"]:
        if link["entity_id"] not in identities:
            raise ValueError("Sequence link references unknown entity")
        require_refs(link["evidence_refs"], evidence)
        text = " ".join(evidence[ref]["text"] for ref in link["evidence_refs"])
        if not re.search(rf"(?<!\d){link['seq_id']}(?!\d)", text):
            raise ValueError(f"SEQ ID {link['seq_id']} absent from mapping evidence")
        entity = next(item for item in plan["entities"] if item["entity_id"] == link["entity_id"])
        from .sequence_caption_evidence import caption_sequence_relation
        sources = [evidence[ref] for ref in link["evidence_refs"]]
        caption_supported = caption_sequence_relation(link, entity, sources)
        if not any(contains_entity_name(text, name) for name in entity["raw_names"]) and not caption_supported:
            raise ValueError("Sequence mapping evidence does not include its construct name")
        chain_pattern = r"\bVH\b|heavy|重链" if link["chain"] == "VH" else r"\bVL\b|light|轻链"
        if not caption_supported and not re.search(chain_pattern, text, re.I) and not re.search(chain_pattern, normalize(text), re.I):
            raise ValueError("Sequence mapping needs explicit chain-header evidence")
        sources = [evidence[ref] for ref in link["evidence_refs"]]
        id_pattern = rf"(?<!\d){link['seq_id']}(?!\d)"
        opposite = r"\bVL\b|light|轻链" if link["chain"] == "VH" else r"\bVH\b|heavy|重链"
        seq_pattern = rf"SEQ\s*ID(?:\s*N[O0]\.?)?\s*[:：]?\s*{link['seq_id']}(?!\d)"
        between = rf"(?:(?!(?:{opposite}))[^,，;；。|\n]){{0,70}}?"

        def explicit_relation(source: dict[str, Any]) -> bool:
            value = re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " ", source["text"], flags=re.I))
            if source.get("kind") in {"method", "sequence_passage", "context"} and not any(
                contains_entity_name(value, name) for name in entity["raw_names"]
            ):
                return False
            matches = list(re.finditer(rf"(?:{chain_pattern}){between}{seq_pattern}", value, re.I))
            matches.extend(re.finditer(rf"{seq_pattern}{between}(?:{chain_pattern})", value, re.I))
            # Constant/CDR mentions cannot become VH/VL merely because an
            # entity name and a heavy/light word occur in the paragraph.
            return any(not re.search(r"constant|恒定|CDR\s*[123]", value[max(0, match.start() - 25):match.end()], re.I)
                       for match in matches)

        def sequence_block_relation(source: dict[str, Any]) -> bool:
            value = normalize(source["text"])
            # Ignore OCR line wrapping only inside an explicit named chain
            # definition followed by amino acids and its own SEQ ID annotation.
            for name in entity["raw_names"]:
                prefix = rf"(?<![a-z0-9]){re.escape(normalize(name))}(?:(?!(?:{opposite}|SEQID))[^,，;；。]){{0,40}}"
                prefix += rf"(?:{chain_pattern})(?:(?!(?:{opposite}|SEQID))[^,，;；。]){{0,40}}(?:序列为|sequenceis)[:：]?"
                if re.search(prefix + rf"[ACDEFGHIKLMNPQRSTVWYBOXZUJ*]{{5,}}[（(]?SEQID(?:NO\.?)?[:：]?{link['seq_id']}(?!\d)", value, re.I):
                    return True
            return False

        def wrapped_sequence_relation(source: dict[str, Any], header: dict[str, Any]) -> bool:
            """A named chain definition at a page end can finish on the next page."""
            if source.get("kind") not in {"cell", "method"} or header.get("kind") != "cell":
                return False
            if (source.get("kind") == "cell" and not str(source.get("table_id", "")).startswith("sequence-page-")) or not str(header.get("table_id", "")).startswith("sequence-page-"):
                return False
            if source.get("page") != (header.get("page") or 0) + 1 or source.get("row_index", 0) > 12:
                return False
            if not re.search(seq_pattern, source["text"], re.I) or not re.search(chain_pattern, header["text"], re.I):
                return False
            if not any(normalize(name) in normalize(header["text"]) for name in entity["raw_names"]):
                return False
            previous = [item for item in evidence.values() if item.get("kind") == "cell" and item.get("table_id") == header.get("table_id")]
            last_row = max((item.get("row_index", 0) for item in previous), default=999)
            if header.get("row_index", -1) < last_row - 12:
                return False
            intervening = [item for item in evidence.values() if item.get("kind") == "cell" and (
                (item.get("table_id") == header.get("table_id") and item.get("row_index", 0) > header["row_index"])
                or (source.get("kind") == "cell" and item.get("table_id") == source.get("table_id") and item.get("row_index", 0) <= source["row_index"]))]
            if source.get("kind") == "method":
                prefix = source["text"][:re.search(seq_pattern, source["text"], re.I).end()]
                prefix = "\n".join(line for line in prefix.splitlines() if not re.match(r"\s*(?:WO\s*\d|PCT[/／])", line, re.I))
                intervening.append({"text": prefix})
            for item in intervening:
                value = re.sub(r"\s+", "", item["text"])
                if item.get("row_index") == 0 and re.match(r"(?:WO\d|PCT[/／])", value, re.I):
                    continue
                if item.get("table_id") == header.get("table_id") and item.get("row_index") == last_row and re.fullmatch(r"\d{1,3}", value):
                    continue
                value = re.sub(r"[（(]?SEQID(?:N[O0]\.?)?[:：]?\d+[）)]?", "", value, flags=re.I)
                if value and not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWYBOXZUJ*]+", value):
                    return False
            return bool(intervening)

        row_definition = any(
            source.get("kind") == "cell"
            and re.fullmatch(rf"(?:SEQID(?:NO\.?)?[:：]?)?{link['seq_id']}", normalize(source["text"]), re.I)
            and any(header is not source and header.get("kind") == "cell"
                    and header.get("table_id") == source.get("table_id")
                    and header.get("row_index") == source.get("row_index")
                    and re.fullmatch(r"(?:V[HL]|(?:heavy|light)(?:chain)?(?:variableregion)?|[重轻]链(?:可变区)?)", normalize(header["text"]), re.I)
                    and re.search(chain_pattern, normalize(header["text"]), re.I)
                    for header in sources)
            for source in sources)

        def column_definition(source: dict[str, Any]) -> bool:
            if source.get("kind") != "cell" or not re.fullmatch(id_pattern, source["text"].strip()):
                return False
            headers = [s for s in sources if s.get("kind") == "cell"
                       and s.get("table_id") == source.get("table_id")
                       and s.get("row_index", 0) < source.get("row_index", 0)]
            id_headers = [s for s in headers if s.get("col_index") == source.get("col_index")
                          and re.search(r"seq(?:uence)?id|序列编号|序列号", normalize(s["text"]), re.I)]
            if not id_headers:
                return False
            # Description | Sequence | SEQ ID NO, with a named chain label
            # in the same row as its identifier.
            named_label = any(s.get("kind") == "cell" and s.get("table_id") == source.get("table_id")
                              and s.get("row_index") == source.get("row_index")
                              and re.search(chain_pattern, s["text"], re.I)
                              and not re.search(opposite, s["text"], re.I)
                              and any(contains_entity_name(s["text"], name) for name in entity["raw_names"])
                              and not re.search(r"constant|恒定|CDR\s*[123]", s["text"], re.I)
                              for s in sources)
            # Antibody | SEQ ID | VH | SEQ ID | VL. The ID header and
            # adjacent chain header must be cited together, with a same-row
            # entity name. A distant name from an assay cannot prove ownership.
            paired_header = any(h.get("row_index") == ih.get("row_index")
                                and h.get("col_index") == source.get("col_index", -2) + 1
                                and re.fullmatch(link["chain"], h["text"].strip(), re.I)
                                for h in headers for ih in id_headers)
            same_row_name = any(s.get("kind") == "cell" and s.get("table_id") == source.get("table_id")
                                and s.get("row_index") == source.get("row_index")
                                and any(normalize(name) == normalize(s["text"]) for name in entity["raw_names"])
                                for s in sources)
            return named_label or (paired_header and same_row_name)

        relation_supported = caption_supported or row_definition or any(column_definition(source) for source in sources) or any(
            source.get("kind") == "cell" and re.search(id_pattern, source["text"])
            and any(header is not source and header.get("kind") == "cell"
                    and header.get("table_id") == source.get("table_id")
                    and header.get("col_index") == source.get("col_index")
                    and header.get("row_index", 0) <= source.get("row_index", 0) + 6
                    and re.search(chain_pattern, header["text"], re.I)
                    for header in sources)
            for source in sources
        ) or any(
            source.get("kind") != "sequence_catalog"
            and explicit_relation(source)
            for source in sources
        ) or any(sequence_block_relation(source) for source in sources if source.get("kind") != "sequence_catalog") or any(wrapped_sequence_relation(source, header) for source in sources for header in sources if source is not header)
        if not relation_supported:
            raise ValueError(f"{link['entity_id']} {link['chain']} SEQ ID {link['seq_id']} must align with its cited chain header or explicit chain statement")
        if re.search(r"CDR\s*[123]", text, re.I) and not re.search(r"\bV[HL]\b|variable|可变", text, re.I):
            raise ValueError("CDR identifiers cannot be assigned as full VH/VL")
        key = (link["entity_id"], link["chain"], link["scope"])
        if key in assignments and assignments[key] != link["seq_id"]:
            raise ValueError("Conflicting sequence mapping must remain unresolved")
        assignments[key] = link["seq_id"]
    validate_assay_profiles(plan["assay_profiles"], evidence, table_ids, regions)
    for finding in plan["unresolved"]:
        require_refs(finding["evidence_refs"], evidence)
    unresolved_refs = {ref for finding in plan["unresolved"] for ref in finding["evidence_refs"]}
    missing = [item["raw_name"] for item in required_names or [] if normalize(item["raw_name"]) not in names and item["ref"] not in unresolved_refs]
    if missing:
        raise ValueError(f"Observed assay entities missing from plan: {missing[:12]}")


def expand_template_table(plan: dict[str, Any]) -> dict[str, Any]:
    """Derive compatibility fields instead of asking the model twice."""
    return {
        **plan, "primary_label": plan["labels"][0],
        "measurement_axis": {"rows": "columns", "columns": "rows"}.get(plan["entity_axis"], plan["entity_axis"]),
        "detected_endpoints": sorted({endpoint for region in plan["regions"] for endpoint in region_endpoints(region)}),
    }


def pending_template_table(identity: str, message: str) -> dict[str, Any]:
    return {"table_id": identity, "labels": ["ambiguous"], "confidence": 0,
            "entity_axis": "unknown", "decision": "needs_review", "regions": [],
            "evidence_refs": [identity], "explanation": message,
            "issues": [{"code": "template_table_needs_review", "message": message, "evidence_refs": [identity]}]}


def template_plan_context(response: dict, tables: list[dict], evidence: dict, header_links: dict) -> tuple:
    decisions = [expand_template_table(plan) for plan in response["tables"]]
    validate_classifications({"tables": decisions}, tables, evidence, header_links)
    for decision in decisions:
        if decision["decision"] == "exclude" and (
            set(decision["labels"]) & (TARGET_LABELS | {"ambiguous"}) or decision["regions"] or decision["issues"]
        ):
            raise ValueError("Target or uncertain evidence cannot be excluded; excluded tables have no regions")
    regions = {d["table_id"]: identified_regions(d["table_id"], d["regions"])
               for d in decisions if d["decision"] != "exclude"}
    required = []
    for identity, scopes in regions.items():
        for region in scopes:
            if region["kind"] != "assay":
                continue
            required.extend({"raw_name": group["raw_name"], "ref": group["name_ref"]}
                            for group in region["entity_groups"])
            if region["entity_column"] is not None:
                for row in range(region["row_start"], region["row_end"] + 1):
                    if row in region["header_rows"]:
                        continue
                    ref = cell_ref(identity, row, region["entity_column"])
                    if evidence.get(ref, {}).get("text", "").strip():
                        required.append({"raw_name": evidence[ref]["text"], "ref": ref})
    return decisions, regions, required


class LLMTemplateAgent:
    def __init__(self, provider: CodexProvider) -> None:
        self.provider = provider

    def run(
        self, document_id: str, ocr_results: list[dict[str, Any]],
        method_pages: list[dict[str, Any]] | None = None,
        table_contexts: list[dict[str, Any]] | None = None,
        *, repair_findings: list[dict] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from .template_agent import TemplateAgent

        started = time.monotonic()
        task = f"document-template-v{TEMPLATE_PROMPT_VERSION}"
        # Previous labels must never pre-filter or bias this planning pass.
        ocr_results = [semantic_table({k: v for k, v in table.items() if k != "table_classification"})
                       for table in ocr_results]
        if len({table_id(t) for t in ocr_results}) != len(ocr_results):
            raise ValueError("Template table identifiers must be unique")
        print("llm-template-start", flush=True)

        def log(event: str, **details: Any) -> None:
            log_llm_event(event, task=task, document_id=document_id,
                          elapsed_seconds=round(time.monotonic() - started, 3), **details)

        title_tables, title_records, title_contexts = prepare_table_title_input(
            ocr_results, method_pages, table_contexts
        )
        log("template_prepare_start", table_count=len(title_tables),
            title_count=sum(bool(item["title"]) for item in title_records))

        payload, evidence = build_document_input(document_id, title_tables, [], title_contexts, plan_tables=True)
        if repair_findings:
            payload["repair_findings"] = repair_findings
        supplied_ids = {t["table_id"] for t in payload["tables"]}
        supplied_tables = [t for t in title_tables if table_id(t) in supplied_ids]
        header_links = verified_header_links(title_tables, table_contexts or [])
        log("template_input_ready", table_count=len(payload["tables"]),
            cell_count=sum(len(semantic_cell_refs(t)) for t in payload["tables"]),
            titled_table_count=sum(bool(t.get("caption")) for t in payload["tables"]),
            evidence_count=len(evidence),
            omitted_table_count=len(payload.get("omitted_table_ids", [])),
            payload_chars=len(json.dumps(payload, ensure_ascii=False)))

        def retained_evidence(decisions: list[dict]) -> dict:
            excluded = {d["table_id"] for d in decisions if d["decision"] == "exclude"}
            return {ref: source for ref, source in evidence.items() if source.get("table_id") not in excluded}

        def validate(response: dict) -> None:
            response = TemplatePlan.model_validate(response).model_dump()
            decisions, regions, required = template_plan_context(response, supplied_tables, evidence, header_links)
            validate_document_plan(response, document_id, retained_evidence(decisions), set(regions), required, regions)

        def recover(response: dict) -> dict:
            # A bad region must not drop a table or invalidate independent plans.
            recovered_tables, rejected_tables = [], []
            for table in supplied_tables:
                identity = table_id(table)
                matches = [p for p in response["tables"] if p["table_id"] == identity]
                try:
                    if len(matches) != 1:
                        raise ValueError("Expected exactly one table plan")
                    template_plan_context({"tables": matches}, [table], evidence, header_links)
                except ValueError as exc:
                    recovered_tables.append(pending_template_table(identity, str(exc)))
                    rejected_tables.append({"kind": "table", "table_id": identity, "reason": str(exc)})
                else:
                    recovered_tables.extend(matches)
            decisions, regions, required = template_plan_context({"tables": recovered_tables}, supplied_tables, evidence, header_links)
            document_plan = {k: v for k, v in response.items() if k != "tables"}
            safe, rejected = recover_document_plan(document_plan, document_id, retained_evidence(decisions), set(regions), required, regions)
            rejected = [*rejected_tables, *rejected]
            log("template_recovery", rejected_count=len(rejected),
                retained_counts={key: len(safe[key]) for key in ("entities", "sequence_links", "assay_profiles")})
            if self.provider.audit_path:
                json_write(self.provider.audit_path.parent / "document_plan_recovery.json", {
                    "document_id": document_id, "status": "needs_review", "rejected": rejected,
                    "retained_counts": {key: len(safe[key]) for key in ("entities", "sequence_links", "assay_profiles")},
                    "unresolved": safe["unresolved"],
                })
            return {**safe, "tables": recovered_tables}

        response, meta = self.provider.generate(
            task,
            (PROMPTS / "document_template.txt").read_text(encoding="utf-8"),
            payload, TemplatePlan,
            validate=validate,
            recover=recover,
        )
        # Also validate scripted/cached providers before publishing routing decisions.
        validate(response)
        decisions, _, _ = template_plan_context(response, supplied_tables, evidence, header_links)
        classifications = finalize_table_plans(decisions, supplied_tables, evidence, meta, header_links, owner="TemplateAgent")
        # A budget omission is a pending table, never an implicit exclusion.
        for table in ocr_results:
            identity = table_id(table)
            if identity in supplied_ids:
                continue
            pending = expand_template_table(pending_template_table(identity, "Template input budget omitted this table; review required."))
            classifications.extend(finalize_table_plans([pending], [table], build_evidence([table]),
                {"mode": "pending", "status": "not_run"}, header_links, owner="TemplateAgent"))
        order = {table_id(t): index for index, t in enumerate(ocr_results)}
        classifications.sort(key=lambda c: order[c["table_id"]])
        document_plan = {k: v for k, v in response.items() if k != "tables"}
        from .table_classification import route_ocr_results
        # Materialize deterministic vocabulary/protocol evidence only after
        # triage; LLM entities replace inferred name grammars below.
        baseline, _ = TemplateAgent().run(document_id, route_ocr_results(title_tables, classifications), [], title_contexts)
        warning_evidence = {**build_evidence(title_tables), **evidence}
        warnings = [
            {"code": finding["code"], "severity": "medium", "message": finding["message"],
             "evidence_refs": finding["evidence_refs"],
             "table_ids": list(dict.fromkeys(warning_evidence[ref]["table_id"] for ref in finding["evidence_refs"] if warning_evidence.get(ref, {}).get("table_id")))}
            for finding in [*response["unresolved"], *(issue for c in classifications for issue in c["issues"])]
        ]
        warnings.extend({"code": "table_structure_needs_review", "severity": "medium",
                         "message": c["explanation"], "evidence_refs": c["evidence_refs"],
                         "table_ids": [c["table_id"]]}
                        for c in classifications if c["decision"] == "needs_review" and not c["issues"])
        if payload.get("omitted_table_ids"):
            warnings.append({"code": "template_input_bounded", "severity": "high",
                             "message": "Document input limit omitted tables; explicit review is required.",
                             "table_ids": payload["omitted_table_ids"]})
        scoped_profiles = materialize_profiles(response["assay_profiles"], evidence, meta)
        profiles = table_profile_summaries(scoped_profiles)
        status = "needs_review" if warnings else "validated"
        template = {
            **baseline, "version": 3, "status": status,
            "agent": {"name": "TemplateAgent", "version": 4, "mode": "llm", "prompt_version": TEMPLATE_PROMPT_VERSION,
                      "table_planning": "integrated", **meta},
            "entity_template": {"status": status, "patterns": [], "entities": response["entities"],
                                "reference_names": [], "candidate_count": len(response["entities"])},
            "sequence_links": response["sequence_links"], "document_plan": document_plan,
            "table_classifications": classifications,
            "table_titles": title_records,
            "sequence_source_passages": [],
            "assay_template": {**baseline.get("assay_template", {}), "table_profiles": profiles, "region_profiles": scoped_profiles},
            "warnings": warnings,
        }
        validation = {
            "status": status, "confidence": None, "coverage": None,
            "entity_count": len(response["entities"]), "sequence_link_count": len(response["sequence_links"]),
            "assay_profile_count": len(response["assay_profiles"]),
            "source_validation": "passed", "warnings": warnings, "llm": meta,
        }
        print(f"llm-template: {len(response['entities'])} entities, {len(response['sequence_links'])} sequence links", flush=True)
        log("template_complete", status=status, entity_count=len(response["entities"]),
            sequence_link_count=len(response["sequence_links"]), assay_profile_count=len(response["assay_profiles"]),
            warning_count=len(warnings), cache_hit=meta.get("cache_hit", False))
        return template, validation

    def repair_profiles(self, document_id: str, tables: list[dict], methods: list[dict], contexts: list[dict], target_ids: set[str], findings: list[dict]) -> tuple[list[dict], list[dict]]:
        """Replan only affected protocol bindings; entity/sequence plans are immutable."""
        selected = [t for t in tables if table_id(t) in target_ids]
        if {table_id(t) for t in selected} != target_ids:
            raise ValueError("Target table has no executable classification; cannot safely rebind protocols")
        support_ids = {identity for t in selected for identity in t.get("table_classification", {}).get("allowed_header_table_ids", [])} - target_ids
        inputs = [*selected, *(t for t in tables if table_id(t) in support_ids)]
        titled_inputs, _, title_contexts = prepare_table_title_input(inputs, methods, contexts)
        payload, evidence = build_document_input(document_id, titled_inputs, [], title_contexts)
        payload["target_table_ids"] = sorted(target_ids)
        payload["support_table_ids"] = sorted(support_ids)
        payload["repair_findings"] = findings
        instructions = (PROMPTS / "assay_repair.txt").read_text(encoding="utf-8")
        def validate(response: dict) -> None:
            validate_assay_profiles(response["assay_profiles"], evidence, target_ids,
                {table_id(t): t.get("table_classification", {}).get("regions", []) for t in selected})
            for finding in response["unresolved"]:
                require_refs(finding["evidence_refs"], evidence)
        response, meta = self.provider.generate(f"assay-binding-repair-v{PROMPT_VERSION}", instructions, payload, AssayBindingPlan, validate=validate)
        return materialize_profiles(response["assay_profiles"], evidence, meta), response["unresolved"]


# Partial document plan recovery

def recover_document_plan(
    plan: dict[str, Any], document_id: str, evidence: dict[str, dict[str, Any]],
    table_ids: set[str], required_names: list[dict[str, Any]] | None = None,
    regions: dict[str, list[dict]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # Import here so the strict validator remains the single source of truth.


    plan = DocumentPlan.model_validate(plan).model_dump()
    if plan["document_id"] != document_id:
        raise ValueError("Document identity changed in model response")
    safe: dict[str, Any] = {"document_id": document_id, "entities": [], "sequence_links": [],
                            "assay_profiles": [], "unresolved": []}
    rejected: list[dict[str, Any]] = []

    def reject(kind: str, item: dict, reason: str) -> None:
        refs = list(dict.fromkeys(ref for ref in [*item.get("evidence_refs", []),
                                                 *item.get("alias_evidence_refs", [])] if ref in evidence))
        identity = item.get("entity_id") or item.get("table_id") or item.get("code", "finding")
        rejected.append({"kind": kind, "item": item, "reason": reason})
        safe["unresolved"].append({"code": f"document_plan_rejected_{kind}",
                                   "message": f"{identity}: {reason}", "evidence_refs": refs})

    def check(candidate: dict) -> None:
        validate_document_plan(candidate, document_id, evidence, table_ids, regions=regions)

    # Conflicts are rejected symmetrically; source order must not choose a winner.
    id_counts = Counter(e["entity_id"] for e in plan["entities"])
    name_owners: dict[str, set[str]] = defaultdict(set)
    for entity in plan["entities"]:
        for name in entity["raw_names"]:
            name_owners[normalize(name)].add(entity["entity_id"])
    pending = []
    for entity in plan["entities"]:
        if id_counts[entity["entity_id"]] > 1:
            reject("entity", entity, "Duplicate entity_id")
        elif any(len(name_owners[normalize(name)]) > 1 for name in entity["raw_names"]):
            reject("entity", entity, "Raw name maps to conflicting entities")
        else:
            pending.append(entity)

    accepted: set[str] = set()
    # Validate parents before children without altering any relationship.
    while pending:
        remaining = []
        for entity in pending:
            if entity["parent_entity_id"] is not None and entity["parent_entity_id"] not in accepted:
                remaining.append(entity)
                continue
            candidate = {**safe, "entities": [*safe["entities"], entity]}
            try:
                check(candidate)
            except ValueError as exc:
                reject("entity", entity, str(exc))
            else:
                safe["entities"].append(entity)
                accepted.add(entity["entity_id"])
        if len(remaining) == len(pending):
            for entity in remaining:
                reject("entity", entity, "Parent entity is missing, rejected, or part of a cycle")
            break
        pending = remaining

    assignments: dict[tuple, set[int]] = defaultdict(set)
    for link in plan["sequence_links"]:
        assignments[(link["entity_id"], link["chain"], link["scope"])].add(link["seq_id"])
    for link in plan["sequence_links"]:
        if len(assignments[(link["entity_id"], link["chain"], link["scope"])]) > 1:
            reject("sequence_link", link, "Conflicting sequence mapping must remain unresolved")
            continue
        owner = next((entity for entity in safe["entities"] if entity["entity_id"] == link["entity_id"]), None)
        if owner is not None:
            from .sequence_caption_evidence import complete_caption_refs
            link = complete_caption_refs(link, owner, evidence)
        candidate = {**safe, "sequence_links": [*safe["sequence_links"], link]}
        try:
            check(candidate)
        except ValueError as exc:
            reject("sequence_link", link, str(exc))
        else:
            safe["sequence_links"].append(link)

    bindings = Counter((p["table_id"], p.get("region_id"), endpoint)
                       for p in plan["assay_profiles"] for endpoint in p["endpoints"])
    for profile in plan["assay_profiles"]:
        if any(bindings[(profile["table_id"], profile.get("region_id"), endpoint)] > 1
               for endpoint in profile["endpoints"]):
            reject("assay_profile", profile, "Overlapping assay profiles must remain unresolved")
            continue
        candidate = {**safe, "assay_profiles": [*safe["assay_profiles"], profile]}
        try:
            check(candidate)
        except ValueError as exc:
            reject("assay_profile", profile, str(exc))
        else:
            safe["assay_profiles"].append(profile)

    for finding in plan["unresolved"]:
        candidate = {**safe, "unresolved": [*safe["unresolved"], finding]}
        try:
            check(candidate)
        except ValueError as exc:
            reject("finding", finding, str(exc))
        else:
            safe["unresolved"].append(finding)

    represented = {normalize(name) for entity in safe["entities"] for name in entity["raw_names"]}
    unresolved_refs = {ref for finding in safe["unresolved"] for ref in finding["evidence_refs"]}
    for item in required_names or []:
        if normalize(item["raw_name"]) not in represented and item["ref"] not in unresolved_refs:
            if item["ref"] not in evidence:
                raise ValueError("Required entity source is unavailable")
            safe["unresolved"].append({"code": "document_plan_missing_entity",
                "message": f"Observed assay entity requires review: {item['raw_name']}", "evidence_refs": [item["ref"]]})
            unresolved_refs.add(item["ref"])

    safe = DocumentPlan.model_validate(safe).model_dump()
    validate_document_plan(safe, document_id, evidence, table_ids, required_names, regions)
    return safe, rejected
