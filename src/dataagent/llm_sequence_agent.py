"""Agent-owned sequence interpretation and transcription, with literal grounding.

The validator checks references and copied characters. It does not infer VH/VL,
recognize sequence table layouts, or choose variable-domain boundaries.
"""

from __future__ import annotations

import hashlib
import copy
import json
import re
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .sequence_evidence import source_digest, EvidenceAccess, fingerprint, prompt_source
from .llm_io import SequenceDecision, SequenceExtractionStep, build_evidence, require_refs
from document.cell_geometry import geometry_fields


PROMPT_VERSION = 4
AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")


def residue_text(quote: str) -> str:
    # Never uppercase, substitute OCR glyphs, or discard nonformatting symbols.
    return re.sub(r"\s+", "", re.sub(r"<br\s*/?>", "", quote, flags=re.I))


def decision_refs(decision: dict) -> set[str]:
    return (set(decision["evidence_refs"]) | {f["source_ref"] for f in decision["fragments"]}
            | {c[key] for c in decision.get("source_conflicts", []) for key in ("table_ref", "text_ref")})


def load_sequence_inheritance(document_id: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", document_id):
        return []
    path = Path(__file__).resolve().parents[2] / ".studio" / "patents" / document_id / "sequence_inheritance.json"
    if not path.is_file():
        return []
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("document_id") != document_id:
        raise ValueError("Sequence inheritance belongs to another document")
    return saved.get("assignments", [])


def apply_confirmed_sequence_inheritance(bundle: dict, assignments: list[dict],
                                         sources: dict, entities: list[dict]) -> dict:
    """Apply explicit user-confirmed VH/VL relationships; keep literal residue grounding."""
    if not assignments:
        return bundle
    result = copy.deepcopy(bundle)
    originals = {(d["entity_id"], d["chain"]): d for d in bundle["decisions"]}
    by_id = {e["entity_id"]: e for e in entities}
    replacements = {}
    for assignment in assignments:
        target, parent = assignment["entity_id"], assignment["parent_entity_id"]
        if (assignment.get("basis") != "user_confirmation" or not assignment.get("note", "").strip()
                or assignment.get("scope") != "variable_regions" or target == parent
                or target not in by_id or parent not in by_id):
            raise ValueError("Variable-region inheritance requires explicit, scoped user confirmation")
        for chain in assignment["chains"]:
            if chain not in {"VH", "VL"} or (target, chain) in replacements:
                raise ValueError("Invalid or duplicate inherited chain")
            if (parent, chain) not in originals or (target, chain) not in originals:
                raise ValueError("Inheritance requires existing chain decisions for both entities")
            item = copy.deepcopy(originals[parent, chain])
            item["entity_id"] = target
            item["evidence_refs"] = list(dict.fromkeys(
                item["evidence_refs"] + by_id[target].get("evidence_refs", [])))
            item["rationale"] = (f"用户确认 {by_id[target].get('canonical_name', target)} 与 "
                f"{by_id[parent].get('canonical_name', parent)} 的 {chain} 相同；"
                f"该关联仅适用于可变区。{item['rationale']}")
            clean = {k: v for k, v in item.items() if k != "grounded_fragments"}
            item["grounded_fragments"] = ground_decision(clean, sources)
            result["sources"].update({ref: sources[ref] for ref in decision_refs(item)})
            replacements[target, chain] = item
    result["decisions"] = [replacements.get((d["entity_id"], d["chain"]), d) for d in result["decisions"]]
    result["sequence_inheritance"] = copy.deepcopy(assignments)
    result["status"] = "validated" if all(d["status"] == "resolved" and not d.get("source_conflicts")
                                         for d in result["decisions"]) else "needs_review"
    return result


def ground_decision(decision: dict, sources: dict[str, dict]) -> list[dict]:
    decision = SequenceDecision.model_validate(decision).model_dump()
    require_refs(decision["evidence_refs"], sources)
    for ref in {f["source_ref"] for f in decision["fragments"]}:
        if ref in sources and sources[ref].get("image_review"):
            from .answer_agent import validate_reviewed_source
            validate_reviewed_source(sources[ref], sources)
        if ref in sources and sources[ref].get("sequence_style"):
            from document.sequence_styles import validate_style_source
            validate_style_source(sources[ref], sources)
    for conflict in decision["source_conflicts"]:
        require_refs([conflict["table_ref"], conflict["text_ref"]], sources)
        table, prose = sources[conflict["table_ref"]], sources[conflict["text_ref"]]
        if table.get("kind") != "cell" or prose.get("kind") in {"cell", "table", "image"}:
            raise ValueError("Source conflict requires a table cell and a text source")
        for name in ("table", "text"):
            if conflict[name + "_quote"] not in sources[conflict[name + "_ref"]].get("text", ""):
                raise ValueError("Source conflict quotes must occur verbatim in their sources")
        if residue_text(conflict["table_quote"]) == residue_text(conflict["text_quote"]):
            raise ValueError("Source conflict quotes must differ beyond formatting")
        if decision["status"] == "resolved":
            fragment_refs = {f["source_ref"] for f in decision["fragments"]}
            fragment_refs.update(sources[ref].get("image_review", {}).get("original_ref")
                                 for ref in list(fragment_refs) if ref in sources)
            # A variable-domain table can corroborate the same residues in a
            # full-chain table. Both references need not contribute duplicate
            # residues to the output. Keep the table priority and literal checks.
            table_only = bool(decision["fragments"]) and all(
                sources.get(f["source_ref"], {}).get("kind") == "cell" for f in decision["fragments"])
            gold, prose_value = residue_text(conflict["table_quote"]), residue_text(conflict["text_quote"])
            corroborated = (table_only and gold and gold in (decision["value"] or "")
                            and prose_value not in (decision["value"] or ""))
            if (conflict["table_ref"] not in fragment_refs and not corroborated) or conflict["text_ref"] in fragment_refs:
                raise ValueError("Resolve table/text conflicts using the gold-standard table cell")
    if decision["status"] != "resolved":
        if decision["value"] is not None or decision["fragments"]:
            raise ValueError("Unresolved/ambiguous decisions cannot export a value or fragments")
        return []
    value = decision["value"]
    if not value or set(value) - AMINO_ACIDS:
        raise ValueError("Resolved value must contain only literal uppercase amino acid characters")
    if not decision["fragments"] or not decision["evidence_refs"]:
        raise ValueError("Resolved value requires source fragments and ownership/chain evidence")
    if decision["source_scope"] == "unknown":
        raise ValueError("Resolved value requires an explicit scope decision")
    if decision["seq_id"] is not None and not any(
        re.search(rf"(?<!\d){decision['seq_id']}(?!\d)", sources[ref].get("text", ""))
        for ref in decision["evidence_refs"]
    ):
        raise ValueError("SEQ ID is absent from cited evidence")
    segments, covered = [], {}
    offset = 0
    for fragment in decision["fragments"]:
        ref, quote = fragment["source_ref"], fragment["quote"]
        require_refs([ref], sources)
        if sources[ref].get("sequence_ocr", {}).get("status") not in {None, "success"}:
            raise ValueError("Table sequence re-recognition failed; original PPVL text is audit-only")
        text = str(sources[ref].get("text", ""))
        matches = [match.start() for match in re.finditer(re.escape(quote), text)]
        if fragment["occurrence"] >= len(matches):
            raise ValueError(f"Quote does not occur verbatim at the requested occurrence: {ref}")
        start = matches[fragment["occurrence"]]
        end = start + len(quote)
        if any(start < b and a < end for a, b in covered.get(ref, [])):
            raise ValueError("Source fragments overlap; residues cannot be duplicated")
        covered.setdefault(ref, []).append((start, end))
        residues = residue_text(quote)
        if not residues or set(residues) - AMINO_ACIDS:
            raise ValueError("Quote contains nonresidue characters beyond whitespace/br formatting")
        source = sources[ref]
        segments.append({"source_ref": ref, "quote": quote, "char_start": start, "char_end": end,
                         "sequence_start": offset, "sequence_end": offset + len(residues), "text": residues,
                         "source_sha256": source_digest(source), "page": source.get("page"),
                         "table_id": source.get("table_id"), "row_index": source.get("row_index"),
                         "col_index": source.get("col_index"), "bbox": source.get("bbox", []),
                         **geometry_fields(source)})
        offset += len(residues)
    if "".join(segment["text"] for segment in segments) != value:
        raise ValueError("Agent value does not equal the ordered verbatim source fragments")
    return segments


def validate_step(response: dict, document_id: str, entities: list[dict], sources: dict,
                  known_refs: set[str], *, validate_grounding: bool = True) -> None:
    if response["document_id"] != document_id:
        raise ValueError("Wrong document_id")
    require_refs(response["request_refs"], {ref: {} for ref in known_refs})
    if len(response["search_queries"]) > 12 or any(not q.strip() or len(q) > 200 for q in response["search_queries"]):
        raise ValueError("Search queries must contain 1..200 characters, at most 12 queries")
    if not response["complete"]:
        if response["decisions"] or not (response["request_refs"] or response["search_queries"]):
            raise ValueError("Retrieval requires requests/queries and no partial final decisions")
        return
    if response["request_refs"] or response["search_queries"]:
        raise ValueError("Completed extraction cannot contain pending requests")
    expected = Counter((entity["entity_id"], chain) for entity in entities for chain in ("VH", "VL"))
    actual = Counter((item["entity_id"], item["chain"]) for item in response["decisions"])
    if actual != expected:
        raise ValueError("Return exactly one VH and one VL decision for every supplied entity")
    if validate_grounding:
        for item in response["decisions"]:
            ground_decision(item, sources)


def validate_decisions_individually(response: dict, document_id: str, entities: list[dict],
                                  sources: dict, known_refs: set[str]) -> list[dict]:
    """Preserve supported chains when another chain in the batch fails."""
    response = SequenceExtractionStep.model_validate(response).model_dump()
    validate_step(response, document_id, entities, sources, known_refs, validate_grounding=False)
    if not response["complete"]:
        raise ValueError("Only completed decisions can be recovered")
    decisions = []
    for item in response["decisions"]:
        try:
            ground_decision(item, sources)
        except ValueError as exc:
            item = next(d for d in unresolved_decisions([{"entity_id": item["entity_id"]}],
                f"This chain failed source validation: {exc}") if d["chain"] == item["chain"])
        decisions.append(item)
    return decisions


def unresolved_decisions(entities: list[dict], reason: str) -> list[dict]:
    return [{"entity_id": entity["entity_id"], "chain": chain, "status": "unresolved", "value": None,
             "seq_id": None, "source_scope": "unknown", "fragments": [], "evidence_refs": [],
             "rationale": reason} for entity in entities for chain in ("VH", "VL")]


class SequenceExtractionAgent:
    def __init__(self, provider, *, batch_size: int = 8, max_turns: int = 6, source_budget: int = 180_000,
                 max_workers: int = 3):
        if min(batch_size, max_turns, source_budget) <= 0:
            raise ValueError("Sequence extraction budgets must be positive")
        if not 1 <= max_workers <= 8:
            raise ValueError("Sequence workers must be between 1 and 8")
        self.provider, self.batch_size = provider, batch_size
        self.max_turns, self.source_budget = max_turns, source_budget
        self.max_workers = max_workers

    def run(self, graph: dict, template: dict, *, initial_source_refs: list[str] | None = None) -> dict:
        from .llm_runtime import PROVIDER_VERSION, CodexProviderError, CodexUnavailable, json_write
        from .llm_runtime import GUIDANCE_INSTRUCTIONS, scoped_guidance

        started = time.monotonic()

        sources = graph["sources"]
        entities = template.get("entity_template", {}).get("entities", [])
        inheritance = template.get("sequence_inheritance") or load_sequence_inheritance(graph["document_id"])
        if hasattr(self.provider, "guidance_entities"):
            self.provider.guidance_entities = entities
        access = EvidenceAccess(graph)
        nodes, index = access.nodes, access.index
        all_size = sum(len(ref) + len(json.dumps(prompt_source(source), ensure_ascii=False))
                       for ref, source in sources.items())
        initial = sources if all_size <= self.source_budget else None
        if initial_source_refs is not None:
            require_refs(initial_source_refs, sources)
            initial = {ref: sources[ref] for ref in initial_source_refs}
            initial_size = sum(len(ref) + len(json.dumps(prompt_source(source), ensure_ascii=False))
                               for ref, source in initial.items())
            if initial_size > self.source_budget:
                raise ValueError("Explicit initial sequence evidence exceeds the source budget")
        instructions = (Path(__file__).resolve().parent / "prompt" / "sequence_extraction.txt").read_text(encoding="utf-8")
        task = f"sequence-extraction-v{PROMPT_VERSION}"
        structure_edges = access.continuations
        cache_root = getattr(self.provider, "cache_dir", None) if getattr(self.provider, "cache_enabled", True) else None
        audit_path = getattr(self.provider, "audit_path", None)
        output_dir = Path(audit_path).parent if audit_path else None
        # Evidence, layout, prompt, contract and runtime choices all invalidate
        # checkpoints. Worker count does not change semantic inputs.
        common = {"version": 3, "task": task, "provider_version": PROVIDER_VERSION,
                  "model": getattr(self.provider, "model", None), "effort": getattr(self.provider, "effort", None),
                  "profile": getattr(self.provider, "profile", None), "instructions": instructions,
                  "schema": SequenceExtractionStep.model_json_schema(), "document_id": graph["document_id"],
                  "index": index, "edges": structure_edges,
                  "max_turns": self.max_turns, "source_budget": self.source_budget,
                  "sequence_inheritance": inheritance}
        common_digest = hashlib.sha256(json.dumps(common, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        known_refs = set(nodes) | set(sources)

        def run_batch(start: int, batch: list[dict]) -> dict:
            batch_started = time.monotonic()
            links = [link for link in template.get("sequence_links", [])
                     if link["entity_id"] in {e["entity_id"] for e in batch}]
            seeds = access.seed_refs(batch, links)
            prefetched, seed_omissions = (dict(initial), []) if initial is not None else access.prefetch(seeds, self.source_budget // 2)
            key_input = {"common": common_digest, "entities": batch, "links": links,
                         "seed_refs": seeds, "prefetched": fingerprint(prefetched), "seed_omissions": seed_omissions,
                         "guidance": scoped_guidance(task, {"entities": batch}, all_entities=entities)}
            if key_input["guidance"]:
                key_input["guidance_instructions"] = GUIDANCE_INSTRUCTIONS
            key = hashlib.sha256(json.dumps(key_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache_path = Path(cache_root) / "sequence_batches" / f"{key}.json" if cache_root else None
            if cache_path and cache_path.is_file():
                try:
                    saved = json.loads(cache_path.read_text(encoding="utf-8"))
                    if (saved["input_hash"] != key or saved["batch"]["status"] != "completed"
                            or not access.unchanged(saved.get("dependencies"))):
                        raise ValueError("Incomplete sequence checkpoint")
                    values = [{k: v for k, v in item.items() if k != "grounded_fragments"} for item in saved["decisions"]]
                    response = SequenceExtractionStep.model_validate({"document_id": graph["document_id"],
                        "complete": True, "request_refs": [], "search_queries": [], "decisions": values}).model_dump()
                    validate_step(response, graph["document_id"], batch, sources, known_refs)
                    # Rebuild grounding from current source bytes, never trust
                    # serialized coordinates/fragments from a checkpoint.
                    saved["decisions"] = [{**item, "grounded_fragments": ground_decision(item, sources)}
                                          for item in response["decisions"]]
                    refs = {ref for item in values for ref in decision_refs(item)}
                    saved["sources"] = {ref: sources[ref] for ref in refs}
                    saved["cache_hit"] = True
                    saved["elapsed_seconds"] = round(time.monotonic() - batch_started, 3)
                    return saved
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            decisions, used_sources = [], {}
            loaded, hits, omitted, turns = dict(prefetched), [], seed_omissions, []
            seen_sources, evicted = dict(loaded), []
            requested_refs, searched_queries = [], []
            response = None
            failure = None
            last_completed = None
            for turn in range(self.max_turns):
                payload = {"document_id": graph["document_id"], "entities": batch,
                           "structure_index": index, "structure_edges": structure_edges,
                           "loaded_sources": {ref: prompt_source(source) for ref, source in loaded.items()},
                           "search_hits_not_citable": hits,
                           "evicted_source_refs": evicted,
                           "omitted_source_refs": omitted, "remaining_turns_including_this": self.max_turns - turn,
                           "source_coverage": {"available": len(sources), "loaded": len(loaded)},
                           "prior_sequence_links_are_hypotheses": links}
                print(f"llm-sequence: batch {start // self.batch_size + 1}, turn {turn + 1}, sources {len(loaded)}/{len(sources)}", flush=True)
                def validate_response(value):
                    nonlocal last_completed
                    validate_step(value, graph["document_id"], batch, loaded, known_refs, validate_grounding=False)
                    if value["complete"]:
                        last_completed = copy.deepcopy(value)
                    validate_step(value, graph["document_id"], batch, loaded, known_refs)
                try:
                    response, meta = self.provider.generate(f"sequence-extraction-v{PROMPT_VERSION}", instructions,
                        payload, SequenceExtractionStep,
                        validate=validate_response)
                    # Validate even providers used by embedding applications/test harnesses.
                    response = SequenceExtractionStep.model_validate(response).model_dump()
                    validate_response(response)
                except CodexUnavailable:
                    raise
                except (CodexProviderError, ValueError) as exc:
                    failure = f"Sequence agent response could not be source-validated: {exc}"
                    break
                turns.append({"turn": turn + 1, "llm": meta, "request_refs": response["request_refs"],
                              "search_queries": response["search_queries"], "complete": response["complete"],
                              "loaded_source_count": len(loaded)})
                if response["complete"]:
                    break
                requested_refs.extend(response["request_refs"])
                searched_queries.extend(response["search_queries"])
                loaded, omitted, evicted = access.read_window(response["request_refs"], loaded, self.source_budget)
                seen_sources.update(loaded)
                hits = []
                for query in response["search_queries"]:
                    matches = [(ref, source) for ref, source in sources.items() if query.casefold() in source.get("text", "").casefold()]
                    hits.append({"query": query, "total": len(matches), "omitted_count": max(0, len(matches) - 80),
                                 "matches": [{"ref": ref, "page": source.get("page"), "table_id": source.get("table_id"),
                                              "preview_not_citable": source["text"][max(0, source["text"].casefold().find(query.casefold()) - 60):][:250]}
                                             for ref, source in matches[:80]]})
            if failure or not response or not response["complete"]:
                reason = failure or "Evidence retrieval turn budget exhausted; no sequence value was accepted."
                batch_decisions = (validate_decisions_individually(last_completed, graph["document_id"], batch, loaded, known_refs)
                                   if failure and last_completed else unresolved_decisions(batch, reason))
            else:
                batch_decisions = response["decisions"]
            for item in batch_decisions:
                segments = ground_decision(item, loaded)
                refs = decision_refs(item)
                used_sources.update({ref: loaded[ref] for ref in refs})
                decisions.append({**item, "grounded_fragments": segments})
            batch_result = {"entity_ids": [e["entity_id"] for e in batch], "turns": turns,
                            "status": ("partial" if any(d["status"] == "resolved" for d in decisions) else "failed") if failure
                                      else "completed" if response and response["complete"] else "budget_exhausted",
                            "error": failure, "loaded_source_count": len(loaded), "total_source_count": len(sources),
                            "omitted_source_refs": omitted, "prefetched_source_count": len(prefetched),
                            "index_characters": len(json.dumps(index, ensure_ascii=False))}
            result = {"input_hash": key, "dependencies": access.dependencies(seen_sources, requested_refs, searched_queries), "decisions": decisions, "sources": used_sources, "batch": batch_result,
                      "cache_hit": False, "elapsed_seconds": round(time.monotonic() - batch_started, 3)}
            return {**result, "cache_path": cache_path}

        ordered_entities = sorted(entities, key=lambda e: e["entity_id"])
        buckets = {}
        for entity in ordered_entities:
            bucket = int(hashlib.sha256(entity["entity_id"].encode()).hexdigest()[:8], 16) % 8 if len(entities) > self.batch_size else 0
            buckets.setdefault(bucket, []).append(entity)
        groups = [values[start:start + self.batch_size] for _, values in sorted(buckets.items())
                  for start in range(0, len(values), self.batch_size)]
        # For small documents preserve the established batching convention.
        if len(entities) <= 16:
            groups = [ordered_entities[start:start + self.batch_size] for start in range(0, len(entities), self.batch_size)]
        entries = [(i * self.batch_size, batch) for i, batch in enumerate(groups)]
        workers = min(self.max_workers, len(entries))
        completed, status = {}, "error"

        def checkpoint() -> None:
            ordered = [completed[start] for start, _ in entries if start in completed]
            if output_dir:
                json_write(output_dir / "sequence_progress.json", {
                    "document_id": graph["document_id"], "status": status,
                    "completed_batches": len(ordered), "total_batches": len(entries),
                    "decisions": [d for item in ordered for d in item["decisions"]],
                    "batches": [{**item["batch"], "cache_hit": item["cache_hit"]} for item in ordered]})
                json_write(output_dir / "sequence_performance.json", {
                    "status": status, "workers": workers, "max_workers": self.max_workers,
                    "batch_size": self.batch_size, "total_batches": len(entries), "completed_batches": len(ordered),
                    "cache_hit_batches": sum(item["cache_hit"] for item in ordered),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "batches": [{"batch_index": start // self.batch_size + 1, "status": completed[start]["batch"]["status"],
                                 "cache_hit": completed[start]["cache_hit"], "elapsed_seconds": completed[start]["elapsed_seconds"],
                                 "turns": 0 if completed[start]["cache_hit"] else len(completed[start]["batch"]["turns"])}
                                for start, _ in entries if start in completed]})
            print(f"llm-sequence-progress {len(ordered)}/{len(entries)}", flush=True)

        status = "running"
        checkpoint()
        print(f"llm-sequence-runtime workers={workers} batch_size={self.batch_size} batches={len(entries)}", flush=True)
        try:
            if entries:
                remaining, failure = iter(entries), None
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sequence-extraction") as pool:
                    pending = {}

                    def dispatch() -> None:
                        while failure is None and len(pending) < workers:
                            entry = next(remaining, None)
                            if entry is None:
                                break
                            pending[pool.submit(run_batch, *entry)] = entry[0]

                    dispatch()
                    while pending:
                        finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in finished:
                            start = pending.pop(future)
                            try:
                                result = future.result()
                                path = result.pop("cache_path", None)
                                if path and result["batch"]["status"] == "completed":
                                    json_write(path, result)
                                completed[start] = result
                                checkpoint()
                            except Exception as exc:
                                failure = failure or exc
                        dispatch()
                    if failure:
                        raise failure
            status = "completed"
        finally:
            if status != "completed":
                status = "error"
            checkpoint()
        ordered = [completed[start] for start, _ in entries]
        decisions = [d for item in ordered for d in item["decisions"]]
        entity_order = {entity["entity_id"]: i for i, entity in enumerate(entities)}
        decisions.sort(key=lambda d: (entity_order[d["entity_id"]], d["chain"] != "VH"))
        batches = [{**item["batch"], "cache_hit": item["cache_hit"]} for item in ordered]
        used_sources = {ref: source for item in ordered for ref, source in item["sources"].items()}
        bundle = {"schema_version": 1, "mode": "agent_values", "document_id": graph["document_id"],
                "agent": {"name": "SequenceExtractionAgent", "mode": "llm", "prompt_version": PROMPT_VERSION},
                "status": "validated" if all(item["status"] == "resolved" and not item.get("source_conflicts")
                                              for item in decisions) else "needs_review",
                "source_priority": "table_cells_gold_standard",
                "source_graph_digest": graph["source_digest"], "decisions": decisions,
                "sources": used_sources, "batches": batches,
                "validation_scope": "Literal source grounding; chain identity and boundaries are agent judgments, biological checks are separate."}
        bundle = apply_confirmed_sequence_inheritance(bundle, inheritance, sources, entities)
        from .sequence_identity import review_missing_variable_regions
        return review_missing_variable_regions(self.provider, graph, template, bundle)


def resolve_agent_sequences(tables: list[dict], template: dict) -> dict[str, dict]:
    """Execute agent values after rechecking their literal sources. No heuristic fallback."""
    bundle = template["sequence_extraction"]
    entities = template.get("entity_template", {}).get("entities", [])
    ledger = bundle.get("sources", {})
    current = build_evidence(tables)
    output = {}
    counts = Counter((item["entity_id"], item["chain"]) for item in bundle.get("decisions", []))
    for entity in entities:
        key = entity["entity_id"]
        result = {"construct_key": key, "family": None, "vh": "", "vl": "", "vh_seq_id": None, "vl_seq_id": None,
                  "sequence_resolution": {}, "sequence_table_ids": [], "sequence_rows": [], "mapping_evidence": [],
                  "supporting_sequence_table_ids": [], "supporting_sequence_rows": [],
                  "constant_region_seq_ids": {"VH": [], "VL": []}, "cdr_seq_ids": {}, "ambiguities": [],
                  "source_conflicts": [], "status": "review"}
        for chain, field in (("VH", "vh"), ("VL", "vl")):
            if counts[key, chain] != 1:
                result["ambiguities"].append({"chain": chain, "reason": "agent_decision_missing_or_duplicate"})
                continue
            item = next(item for item in bundle["decisions"] if (item["entity_id"], item["chain"]) == (key, chain))
            result["sequence_resolution"][field] = {"source": "agent_extracted_value", "status": item["status"],
                "source_scope": item["source_scope"], "seq_id": item["seq_id"], "rationale": item["rationale"],
                "evidence_refs": item["evidence_refs"], "source_conflicts": item.get("source_conflicts", [])}
            inherited = next((a for a in bundle.get("sequence_inheritance", [])
                              if a["entity_id"] == key and chain in a["chains"]), None)
            if inherited:
                result["sequence_resolution"][field]["inheritance"] = inherited
            result["source_conflicts"].extend({**conflict, "chain": chain}
                                              for conflict in item.get("source_conflicts", []))
            if item["status"] != "resolved":
                result["ambiguities"].append({"chain": chain, "reason": "agent_" + item["status"], "message": item["rationale"]})
                continue
            try:
                clean = {k: v for k, v in item.items() if k != "grounded_fragments"}
                refs = decision_refs(item)
                require_refs(list(refs), ledger)
                for ref in refs:
                    source = ledger[ref]
                    if source.get("image_review"):
                        from .answer_agent import validate_reviewed_source
                        validate_reviewed_source(source, current)
                    elif source.get("kind") == "cell" and (ref not in current or current[ref]["text"] != source["text"]):
                        raise ValueError(f"Current raw source no longer matches the agent's evidence: {ref}")
                segments = ground_decision(clean, ledger)
            except ValueError as exc:
                result["sequence_resolution"][field]["status"] = "source_invalid"
                result["ambiguities"].append({"chain": chain, "reason": "agent_source_invalid", "message": str(exc)})
                continue
            result[field], result[field + "_seq_id"] = item["value"], item["seq_id"]
            result["sequence_resolution"][field]["segments"] = segments
            for segment in segments:
                tid = segment.get("table_id")
                if tid:
                    result["sequence_table_ids"].append(tid)
                    result["sequence_rows"].append({"table_id": tid, "row_index": segment["row_index"],
                        "col_index": segment["col_index"], "seq_id": item["seq_id"], "chain": chain,
                        "sequence_scope": item["source_scope"], "evidence_role": "primary"})
            evidence = [ledger[ref] for ref in item["evidence_refs"]]
            cells = [s for s in evidence if s.get("kind") == "cell"]
            result["mapping_evidence"].append({"construct_key": key, "assignments": {chain: item["seq_id"]},
                "chain_decisions": {chain: "source_grounded"}, "evidence_refs": item["evidence_refs"],
                "table_id": cells[0]["table_id"] if cells else None,
                "row_index": max((s["row_index"] for s in cells), default=0),
                "raw_cells": [s["text"] for s in cells], "header_cells": [],
                "header_mapping_status": "agent_value_source_grounded", "rationale": item["rationale"],
                "passages": [s for s in evidence if s.get("kind") != "cell"]})
        result["sequence_table_ids"] = list(dict.fromkeys(result["sequence_table_ids"]))
        result["status"] = "linked" if result["vh"] and result["vl"] and not result["ambiguities"] else "review"
        output[key] = result
    return output
