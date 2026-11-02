"""One bounded document pass, a host fact ledger, and targeted gap questions."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from pathlib import Path
import re
import time

from .contracts import ANSWER_INSTRUCTIONS, INSTRUCTIONS
from .export_rows import export as export_rows
from .inputs import CHUNKING_REVISION, make_chunks, read, source_row, source_ocr_status
from .ledger import Ledger, normalized
from .provider import compact, digest, write_json
from .quality import audit_dataset
from .structure import TAG_CATEGORIES, TAG_VOCABULARY
from .timing import PhaseTimings
from .action_agent import ACTION_INSTRUCTIONS, REVISION as ACTION_REVISION, run_action_agent
from .followup import (REVISION as FOLLOWUP_REVISION, ANSWER_INSTRUCTIONS as FOCUSED_ANSWER_INSTRUCTIONS,
                       ANSWER_ACTION_INSTRUCTIONS, run_followup)
from .source_transport import (FORMATS as SOURCE_FORMATS, REVISION as SOURCE_REVISION,
                               encode_payload, instructions as source_instructions)


READER_INPUT_REVISION = 2


def initial_dataset(data):
    """All initial business facts, isolated from callers and other chunks."""
    dataset = copy.deepcopy(data["seed"])
    if data.get("style_definitions"):
        definitions = dataset.setdefault("sequences", [])
        for definition in data["style_definitions"]:
            if definition not in definitions:
                definitions.append(copy.deepcopy(definition))
    return dataset


def reader_payload(data, chunk):
    ids = chunk["context"] + chunk["core"]
    payload = {"document": data["document_id"], "chunk": chunk["id"],
            "initial_dataset": initial_dataset(data),
            "tag_categories": list(TAG_CATEGORIES), "tag_vocabulary": TAG_VOCABULARY,
            "existing_observation_sources": sorted({o["s"] for o in data["seed"]["observations"] if o["s"] in ids}),
            "context_only_sources": chunk["context"],
            "sources": [source_row(data["sources"][sid]) for sid in ids]}
    styles = {sid: data["sources"][sid]["text_styles"] for sid in ids if data["sources"][sid].get("text_styles")}
    reviewed = {sid: {k: data["sources"][sid]["sequence_style"][k] for k in ("method", "spans", "domain", "annotation_quote")}
                for sid in ids if data["sources"][sid].get("sequence_style")}
    if styles:
        payload["source_text_styles"] = styles
    if reviewed:
        payload["source_image_styles"] = reviewed
    if status := source_ocr_status(data["sources"], ids):
        payload["source_sequence_ocr"] = status
    return payload


def reader_request(data, chunk, source_format="rows"):
    """Model wire input; canonical payload stays available to old runs and audits."""
    return encode_payload(reader_payload(data, chunk), source_format), source_instructions(INSTRUCTIONS, source_format)


def question_tasks(dataset, data):
    """One shared missing definition becomes one question for all affected users."""
    grouped = {}
    for e in dataset["entities"]:
        for chain in e["missing"]:
            current = e["sequences"][chain]
            ids = current.get("seq_ids", []) or current.get("source_chain_seq_ids", [])
            scope = current.get("seq_id_scope", "as_recorded")
            key = ("seq", tuple(ids), chain, scope) if ids else ("item", e["id"])
            question = grouped.setdefault(key, {"id": "q" + digest(key)[:12], "targets": [],
                "seq_ids": ids, "seq_id_scope": scope, "kind": "boundary_or_definition" if ids else "identity_or_mapping"})
            question["targets"].append({"item": e["id"], "chain": chain})
            if current.get("failure_kind") == "ocr_failure":
                question.update(kind="ocr_failure", recovery_action="rerun_table_sequence_ocr")
                question["failed_source_ids"] = sorted(set(question.get("failed_source_ids", []) + current["failed_source_ids"]))
    return sorted(grouped.values(), key=lambda q: (-len(q["targets"]), bool(not q["seq_ids"]), q["id"]))


def question_payload(question, ledger, max_chars):
    """Host search and exact-address retrieval; never feed the whole article again."""
    target_ids = {t["item"] for t in question["targets"]}
    names = {name for k in target_ids for name in [ledger.entities[k]["name"], *ledger.entities[k]["names"]]}
    # Similar names are retrieval hints only. Only an explicit, cited model
    # relationship may transfer ownership; no fuzzy identity merging occurs.
    from difflib import SequenceMatcher
    related_ids = set(target_ids)
    for key, entity in ledger.entities.items():
        if any(SequenceMatcher(None, normalized(n), normalized(name)).ratio() >= .72
               for n in [entity["name"], *entity["names"]] for name in names):
            related_ids.add(key)
    seed_refs = set(r for l in ledger.facts["links"] if l["item"] in target_ids for r in l["refs"])
    related_refs = {r for l in ledger.facts["links"] if l["item"] in related_ids for r in l["refs"]}
    seed_refs.update(r for d in ledger.facts["sequences"] if d["seq"] in question["seq_ids"] for r in d["refs"])
    seed_refs.update(r for i in ledger.facts["issues"] if set(i["items"]) & target_ids for r in i["refs"])
    target_chains = {t["chain"] for t in question["targets"]}
    direct_refs = {r for d in ledger.facts["sequences"] if d["seq"] in question["seq_ids"] for r in d["refs"]}
    boundary_definitions = [d for d in ledger.facts["sequences"] if d["region"] in target_chains]
    boundary_refs = {r for d in boundary_definitions for r in d["refs"]}
    constant_regions = {"CH" if chain == "VH" else "CL" for chain in target_chains}
    constant_definitions = [d for d in ledger.facts["sequences"] if d["region"] in constant_regions]
    constant_refs = ({r for d in constant_definitions for r in d["refs"]}
                     if question.get("seq_id_scope") == "full_chain" else set())
    pages = {ledger.sources[r]["page"] for r in seed_refs}
    tokens = [normalized(n) for n in names if len(normalized(n)) >= 3]
    scores = []
    for sid, source in ledger.sources.items():
        text = source["text"]
        score = 100 if sid in seed_refs else 0
        score += 200 * int(sid in direct_refs)
        score += 120 * int(sid in related_refs)
        score += 160 * int(sid in boundary_refs)
        score += 35 * int(any(name in normalized(text) for name in tokens))
        score += 30 * int(any(re.search(rf"SEQ\s*ID(?:\s*NO[.:]?)?\s*[:：]?\s*{n}(?!\d)", text, re.I) for n in question["seq_ids"]))
        if source["kind"] == "cell" and text.strip() in {str(n) for n in question["seq_ids"]}:
            score += 30
        if source["page"] in pages:
            score += 2
        if score:
            scores.append((score, sid))
    # Exact definitions and known domain evidence precede surrounding rows.
    selected = sorted(direct_refs) + sorted(constant_refs - direct_refs) + sorted(seed_refs - direct_refs - constant_refs)
    if question["kind"] == "boundary_or_definition":
        selected += sorted(boundary_refs - set(selected))
    expanded = set(selected)
    # Include a hit's local lines/row headers instead of only isolated sequence numbers.
    by_page = defaultdict(list)
    by_table = defaultdict(list)
    for sid, s in ledger.sources.items():
        by_page[s["page"]].append(sid)
        if s["table_id"]:
            by_table[s["table_id"]].append(sid)
    for _, sid in sorted(scores, key=lambda x: (-x[0], x[1])):
        source = ledger.sources[sid]
        neighbors = [sid]
        if source["table_id"]:
            neighbors += [r for r in by_table[source["table_id"]]
                if ledger.sources[r]["row"] in (0, 1, source["row"], (source["row"] or 0) - 1, (source["row"] or 0) + 1)]
        else:
            page = by_page[source["page"]]
            i = page.index(sid)
            neighbors += page[max(0, i - 4):i + 7]
        for r in neighbors:
            if r not in expanded:
                expanded.add(r)
                selected.append(r)
    admitted, used = [], 0
    for sid in selected:
        size = len(compact(source_row(ledger.sources[sid])))
        if used + size <= max_chars:
            admitted.append(sid)
            used += size
    payload = {"document": ledger.data["document_id"], "question": question,
        "items": [[e["id"], list(dict.fromkeys([e["name"], *e["names"]]))] for e in ledger.entities.values()],
        "known_links": [[l["item"], l["region"], l["seq"], l.get("name")] for l in ledger.facts["links"]
                        if l["item"] in related_ids and set(l["refs"]) <= set(admitted)],
        "identity_rule": "Similar names were retrieved as context only. Mutants, isotypes and parents stay separate unless cited evidence explicitly establishes identity or component ownership.",
        "known_variable_definitions": [[d["seq"], d["region"], d.get("name"), d["refs"]]
            for d in boundary_definitions if set(d["refs"]) <= set(admitted)],
        "sources": [source_row(ledger.sources[s]) for s in admitted],
        "omitted_matching_sources": len(selected) - len(admitted)}
    if constant_refs:
        payload["known_constant_definitions"] = [[d["seq"], d["region"], d.get("name"), d["refs"]]
            for d in constant_definitions if set(d["refs"]) <= set(admitted)]
    styles = {sid: {k: ledger.sources[sid]["sequence_style"][k] for k in ("method", "spans", "domain", "annotation_quote")}
              for sid in admitted if ledger.sources[sid].get("sequence_style")}
    if styles:
        payload["source_image_styles"] = styles
    if status := source_ocr_status(ledger.sources, admitted):
        payload["source_sequence_ocr"] = status
    return payload, admitted


def compare_reference(dataset, reference):
    by_name = defaultdict(list)
    for entity in reference.get("entities", []):
        by_name[normalized(entity["name"])].append(entity)
    rows = []
    counts = Counter()
    for e in dataset["entities"]:
        matches = by_name.get(normalized(e["name"]), [])
        if len(matches) != 1:
            counts["unmatched_entities"] += 1
            continue
        for chain in ("VH", "VL"):
            old = matches[0].get(chain, {}).get("sequence", {})
            old_value = old.get("value") if old.get("status") == "present" else None
            new_value = e["sequences"][chain]["value"]
            status = ("same" if new_value and new_value == old_value else "different" if new_value and old_value else
                      "new_only" if new_value else "reference_only" if old_value else "both_missing")
            counts[status] += 1
            rows.append({"item": e["id"], "name": e["name"], "chain": chain, "comparison": status,
                         "new_length": len(new_value or ""), "reference_length": len(old_value or "")})
    return {"counts": dict(counts), "rows": rows,
            "note": "Exact-name comparison to later repaired outputs, NOT accuracy. Reference answers never enter prompts."}


def run(data, provider, output, *, chunk_chars=24000, workers=None, max_questions=8, question_chars=8000,
        max_chunks=None, answer_workers=4, reader_source_format="rows"):
    if reader_source_format not in SOURCE_FORMATS:
        raise ValueError("Unknown Reader source format")
    if answer_workers < 1 or question_chars < 1 or (max_questions is not None and max_questions < 0):
        raise ValueError("Invalid Answer concurrency, evidence budget or question limit")
    if workers is not None and workers < 0:
        raise ValueError("Reader workers must be nonnegative; 0 means all chunks")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timings = PhaseTimings(output / "phase_timings.json")
    with timings.measure("dataagent2_prepare"):
        chunks = make_chunks(data, chunk_chars)
        scheduled = chunks[:max_chunks] if max_chunks else chunks
        # One worker per scheduled chunk by default; do not inherit executor or
        # legacy sequence-agent concurrency caps. Explicit positive limits opt in.
        workers = workers or max(1, len(scheduled))
        settings = {"input": data["signature"], "instructions": INSTRUCTIONS,
                            "answer_instructions": ANSWER_INSTRUCTIONS, "chunk_chars": chunk_chars,
                            "model": provider.model, "effort": provider.effort, "chunking_revision": CHUNKING_REVISION,
                            "reader_input_revision": READER_INPUT_REVISION,
                            "reader_initial_dataset_hash": digest(initial_dataset(data)),
                            "action_agent_revision": ACTION_REVISION, "action_instructions": ACTION_INSTRUCTIONS,
                            "followup_revision": FOLLOWUP_REVISION,
                            "focused_answer_instructions": FOCUSED_ANSWER_INSTRUCTIONS,
                            "answer_action_instructions": ANSWER_ACTION_INSTRUCTIONS,
                            "answer_effort": getattr(provider, "answer_effort", None) or provider.effort,
                            "question_chars": question_chars, "max_questions": max_questions}
        if reader_source_format != "rows":
            settings.update(reader_source_format=reader_source_format, reader_source_revision=SOURCE_REVISION,
                            reader_source_instructions=source_instructions(INSTRUCTIONS, reader_source_format))
        signature = digest(settings)
        existing = read(output / "manifest.json", {})
        if existing and existing.get("signature") != signature:
            raise ValueError("Output belongs to different inputs/settings; choose a fresh output directory")
        manifest = {"version": 1, "signature": signature, "document_id": data["document_id"],
            "pdf_sha256": data["pdf_sha256"], "native_input": data["native_dir"], "model": provider.model,
            "effort": provider.effort, "chunk_chars": chunk_chars, "chunks": chunks,
            "request_interval_seconds": getattr(provider, "request_interval", None), "answer_retrieval_revision": 5,
            "followup_revision": FOLLOWUP_REVISION, "answer_workers": answer_workers, "question_chars": question_chars,
            "reader_source_format": reader_source_format,
            "reader_source_revision": SOURCE_REVISION,
            "reader_source_policy": ("Original seven-column source rows" if reader_source_format == "rows" else
                                     "Lossless consecutive source grouping; same IDs, text, seed, chunks and evidence scope"),
            "answer_effort": getattr(provider, "answer_effort", None) or provider.effort,
            "followup_policy": "Action domain reviews -> parallel focused Answer proposals on fixed Dataset2 -> LLM Action -> complete Dataset3",
            "unique_sources": len(data["sources"]), "unique_source_text_characters": sum(len(s["text"]) for s in data["sources"].values()),
            "source_transmissions_planned": sum(len(c["core"]) + len(c["context"]) for c in chunks),
            "seed_entities": len(data["seed"]["entities"]), "seed_observations": len(data["seed"]["observations"]),
            "chunking_revision": CHUNKING_REVISION, "reader_workers": workers,
            "reader_seed_policy": "Full initial business seed; fixed initial snapshot across parallel chunks; no name or SEQ-ID filtering",
            "reader_input_revision": READER_INPUT_REVISION,
            "reader_initial_dataset_hash": digest(initial_dataset(data)),
            "action_agent_revision": ACTION_REVISION,
            "action_effort": getattr(provider, "action_effort", None) or provider.effort,
            "action_seed_policy": "Complete initial dataset + all Reader actions + their cited original OCR evidence",
            "baseline": data["baseline"], "max_questions": max_questions,
            "seed_policy": "Only original table/template output. sequence_extraction, resolved_sequences, repaired exports, answers and human inheritance are excluded."}
        write_json(output / "manifest.json", manifest)
        write_json(output / "sources.json", data["sources"])
        write_json(output / "seed.json", data["seed"])
        write_json(output / "reader_initial_dataset.json", initial_dataset(data))
        ledger, completed, failed, skipped = Ledger(data), {}, {}, {}
        merge_index, merged_successes = 0, 0

    def read_chunk(chunk):
        payload, instructions = reader_request(data, chunk, reader_source_format)
        response, meta = provider.generate("reader", instructions, payload)
        return response, meta

    print(f"{data['document_id']}: sources={len(data['sources'])} chunks={len(chunks)} scheduled={len(scheduled)}", flush=True)
    print("sda-dataagent2-progress " + compact({"phase": "reader", "completed": 0, "total": len(chunks)}), flush=True)
    with timings.measure("dataagent2_reader"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(read_chunk, c): c for c in scheduled}
            for future in as_completed(pending):
                chunk = pending[future]
                try:
                    response, meta = future.result()
                    completed[chunk["id"]] = {"response": response, "meta": meta}
                except Exception as exc:
                    if "service circuit open" in str(exc).lower():
                        skipped[chunk["id"]] = str(exc)
                    else:
                        failed[chunk["id"]] = str(exc)
                # Buffer out-of-order completions, then persist the growing ledger
                # in document order without another model request.
                with timings.measure("dataagent2_merge"):
                    previous_index = merge_index
                    while merge_index < len(scheduled):
                        current = scheduled[merge_index]
                        current_id = current["id"]
                        if current_id in completed:
                            ledger.apply(completed[current_id]["response"], current["context"] + current["core"], current_id)
                            merged_successes += 1
                        elif current_id not in failed and current_id not in skipped:
                            break
                        merge_index += 1
                    if merge_index != previous_index:
                        partial = ledger.assemble()
                        partial["status"] = "partial"
                        partial["reader_coverage"] = {"total_chunks": len(chunks), "completed_chunks": merged_successes,
                            "failed_chunks": failed.copy(), "skipped_chunks": skipped.copy(),
                            "core_sources_read": sum(len(c["core"]) for c in scheduled[:merge_index] if c["id"] in completed),
                            "total_sources": len(data["sources"]), "scope": "Incremental source-supported facts; not semantic accuracy",
                            "pending_completed_chunks": len(completed) - merged_successes}
                        write_json(output / "dataset.partial.json", partial)
                        write_json(output / "facts.partial.json", ledger.facts)
                        export_rows(output / "dataset.partial.json", output / "review.partial")
                write_json(output / "progress.json", {"document": data["document_id"], "phase": "reader",
                    "completed_chunks": len(completed), "failed_chunks": failed, "skipped_chunks": skipped,
                    "total_chunks": len(chunks),
                    "usage": provider.usage(), "elapsed_seconds": round(time.monotonic() - started, 2)})
                print("sda-dataagent2-progress " + compact({"phase": "reader", "completed": len(completed),
                    "failed": len(failed), "skipped": len(skipped), "total": len(chunks)}), flush=True)
                if chunk["id"] not in skipped or len(completed) + len(failed) + len(skipped) == len(scheduled):
                    print(f"{data['document_id']}: read {len(completed)}/{len(chunks)} failed={len(failed)} skipped={len(skipped)} tokens={provider.usage()['total_tokens']}", flush=True)
    # The growing ledger above is a provisional review view. The LLM ActionAgent
    # now produces the authoritative complete business dataset across all chunks.
    action_status = {"status": "not_run_incomplete_read", "revision": ACTION_REVISION}
    with timings.measure("dataagent2_action"):
        if len(completed) == len(chunks) and not failed and not skipped:
            print("sda-dataagent2-progress " + compact({"phase": "action", "completed": 0, "total": 1}), flush=True)
            write_json(output / "progress.json", {"document": data["document_id"], "phase": "action",
                "completed_chunks": len(completed), "total_chunks": len(chunks),
                "action_agent": {"status": "running"}, "usage": provider.usage(),
                "elapsed_seconds": round(time.monotonic() - started, 2)})
            try:
                ledger, action_status = run_action_agent(data, chunks, completed, provider, output)
            except Exception as exc:
                action_status = {"status": "failed", "revision": ACTION_REVISION, "error": str(exc),
                                 "error_type": type(exc).__name__,
                                 "validation_paths": getattr(exc, "paths", [])}
                write_json(output / "action_agent.json", action_status)
            print("sda-dataagent2-progress " + compact({"phase": "action", "completed": int(action_status["status"] != "failed"), "total": 1}), flush=True)
        else:
            write_json(output / "action_agent.json", action_status)
    action_complete = action_status["status"] in {"validated", "not_needed"}
    ledger.action_metadata = action_status
    with timings.measure("dataagent2_gap_detection"):
        first = ledger.assemble()
        write_json(output / "dataset.first_pass.json", first)
        write_json(output / "facts.first_pass.json", ledger.facts)
    followup = {"revision": FOLLOWUP_REVISION, "integration": {"status": "not_run_incomplete_read"},
                "questions": [], "remaining": [], "answers": [], "workers": answer_workers}

    def emit_followup(phase, current, total):
        print("sda-dataagent2-progress " + compact({"phase": phase, "completed": current, "total": total}), flush=True)
        write_json(output / "progress.json", {"document": data["document_id"], "phase": phase,
            "completed": current, "total": total, "usage": provider.usage(),
            "elapsed_seconds": round(time.monotonic() - started, 2)})

    if len(completed) == len(chunks) and not failed and not skipped and action_complete:
        ledger, followup = run_followup(data, ledger, provider, output, max_questions=max_questions,
            question_chars=question_chars, workers=answer_workers, timings=timings, emit=emit_followup)
    questions, answers = followup["questions"], followup["answers"]
    image_questions = [q for q in questions if q["kind"] == "ocr_failure"]
    followup_complete = followup["integration"]["status"] in {"validated", "not_needed"}
    with timings.measure("dataagent2_export"):
        final = ledger.assemble()
        unresolved = followup["remaining"] if action_complete else question_tasks(final, data)
        write_json(output / "questions.json", unresolved)
        final["reader_coverage"] = {"total_chunks": len(chunks), "completed_chunks": len(completed),
            "failed_chunks": failed, "skipped_chunks": skipped,
            "core_sources_read": sum(len(c["core"]) for c in chunks if c["id"] in completed),
            "total_sources": len(data["sources"]), "scope": "All supplied PDF OCR lines and table cells, not PDF recall or semantic accuracy"}
        final["status"] = "partial" if failed or len(completed) < len(chunks) or not action_complete or not followup_complete or any(e["missing"] for e in final["entities"]) or final["conflicts"] else "source_supported"
        write_json(output / "dataset.json", final)
        write_json(output / "quality_audit.json", audit_dataset(final, ledger.sources))
        export_rows(output / "dataset.json", output / "review.formal")
        write_json(output / "facts.json", ledger.facts)
        write_json(output / "answers.json", answers)
        comparison = compare_reference(final, data["reference_for_scoring_only"])
        write_json(output / "reference_comparison.json", comparison)
    usage = provider.usage()
    shared = data["baseline"]["shared_stage_tokens"]
    complete_read = len(completed) == len(chunks) and not failed
    answer_failures = sum(a["status"] == "failed" for a in answers)
    complete_benchmark = complete_read and action_complete and followup_complete and not answer_failures
    # A recovery reports its actual new usage, but cannot claim a full-run
    # token reduction without the historical cost of imported Readers.
    total = shared + usage["total_tokens"] if complete_benchmark and not usage.get("reused_reader_requests") else None
    old = data["baseline"]["total_tokens"]
    report = {"document_id": data["document_id"], "status": final["status"],
        "new_model_usage": usage, "reused_stage_historical_tokens": shared,
        "reader_complete": complete_read, "benchmark_complete": complete_benchmark,
        "action_complete": action_complete, "action_agent": action_status,
        "followup_revision": FOLLOWUP_REVISION, "answer_action": followup["integration"],
        "followup_complete": followup_complete, "answer_workers": answer_workers,
        "question_api_failures": answer_failures,
        "comparable_model_total_tokens": total,
        "historical_baseline_tokens": old,
        "known_token_reduction_fraction": 1 - total / old if old and total is not None else None,
        "first_pass": first["summary"], "final": final["summary"],
        "coverage": final["reader_coverage"], "question_count": len(questions),
        "unresolved_question_count": len(unresolved),
        "questions_attempted": sum(a["status"] not in {"requires_image_ocr", "deferred_budget"} for a in answers),
        "questions_requiring_image_ocr": len(image_questions), "questions_remaining": len(unresolved),
        "reference_comparison": comparison["counts"], "baseline": data["baseline"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "limitations": ["Comparison reuses historical table classification/template costs; no new OCR or table-template calls.",
            "Model and reasoning effort are recorded; unknown attempt usage makes totals lower bounds.",
            "Reference outputs contain later repairs and are used only after extraction, not as ground truth.",
            "No image re-OCR or sequence correction; uncertain characters/boundaries remain pending.",
            "Fewer tokens with lower extraction coverage is not an equal-quality speedup."],
        "output": str(output.resolve())}
    write_json(output / "report.json", report)
    write_json(output / "progress.json", {"phase": "finished", **report})
    print("sda-dataagent2-progress " + compact({"phase": "export", "completed": 0, "total": 0}), flush=True)
    return report
