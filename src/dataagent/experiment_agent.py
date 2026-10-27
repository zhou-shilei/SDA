"""Observation-scoped experiment interpretation with bounded, lossless retrieval."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from pydantic import Field

from .assays import select_assay_profile, annotate_assay, keyword_context
from .llm_runtime import json_write, provider_from_env
from .llm_io import Contract, build_evidence, table_id
from .table_structure import semantic_table


class Citation(Contract):
    ref: str
    quote: str = Field(min_length=1)


class ExperimentFact(Contract):
    field: Literal["target", "antigen_species", "cell_host_species", "method", "system", "cell_model", "sample", "readout"]
    value: str = Field(min_length=1)
    evidence: list[Citation] = Field(min_length=1)


class ExperimentIssue(Contract):
    field: str
    reason: str
    detail_zh: str
    detail_en: str
    evidence_refs: list[str]


class ExperimentDecision(Contract):
    scope_id: str
    status: Literal["matched", "needs_review"]
    facts: list[ExperimentFact]
    binding_evidence: list[Citation]
    summary_zh: str
    summary_en: str
    unresolved: list[ExperimentIssue]
    request_pages: list[int]
    search_queries: list[str]


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def search_pages(query: str, pages: dict[int, dict]) -> list[int]:
    # Models may return several quoted phrases; treat these as a conjunction,
    # rather than searching for the quotation marks in the patent text.
    phrases = re.findall(r'"([^"]+)"|“([^”]+)”', query)
    terms = [compact(a or b) for a, b in phrases] if phrases else [compact(query)]
    terms = [term for term in terms if term]
    return [number for number, page in pages.items()
            if terms and all(term in compact(page["text"]) for term in terms)]


def source_pages(output_dir: Path, source_pdf: Path | None) -> dict[int, dict]:
    """Prefer the complete native/OCR page index; preserve full text, never clip it."""
    pages = {int(p["page"]): p for p in read_json(output_dir / "page_index.json", {}).get("pages", []) if p.get("text")}
    blocks = defaultdict(list)
    for block in read_json(output_dir / "document_blocks.json", []):
        if block.get("page") and block.get("text"):
            blocks[int(block["page"])].append(block)
    for number, items in blocks.items():
        pages.setdefault(number, {"page": number, "source": "document_blocks", "text": "\n".join(b["text"] for b in items)})
    if source_pdf and source_pdf.is_file():
        import pymupdf
        with pymupdf.open(source_pdf) as pdf:
            for i, page in enumerate(pdf):
                existing = pages.get(i + 1)
                if existing and existing.get("source") != "document_blocks":
                    continue
                text = page.get_text()
                if text.strip() and (not existing or len(text) > len(existing["text"])):
                    pages[i + 1] = {"page": i + 1, "source": "pdf_text", "text": text}
    for page in read_json(output_dir / "assay_method_pages.json", []):
        if page.get("text") and page.get("page"):
            pages.setdefault(int(page["page"]), page)
    return pages


def observation(record: dict, evidence: dict) -> dict:
    measurement = next((e for e in record.get("evidence", []) if e.get("type") == "assay_measurement"), {})
    return {"table_id": measurement.get("table_id") or record.get("assay_table_id"),
            "region_id": record.get("assay_region_id") or measurement.get("region_id"),
            "endpoint": record.get("assay_endpoint") or measurement.get("endpoint") or "IC50",
            "condition": record.get("assay_condition") or measurement.get("condition_label", ""),
            "measurement_label": record.get("assay_measurement_label") or " · ".join(
                evidence.get(ref, {}).get("text", "") for ref in measurement.get("header_refs", []))}


def validate_decision(decision: dict, scope: dict, evidence: dict) -> None:
    if decision["scope_id"] != scope["scope_id"]:
        raise ValueError("Experiment scope changed")
    def cite(citation):
        source = evidence.get(citation["ref"])
        if not source or not compact(citation["quote"]) or compact(citation["quote"]) not in compact(source["text"]):
            raise ValueError(f"Experiment citation is not verbatim: {citation['ref']}")
    for citation in decision["binding_evidence"]:
        cite(citation)
    seen = set()
    for fact in decision["facts"]:
        if (fact["field"], fact["value"]) in seen:
            raise ValueError("Duplicate experiment fact")
        seen.add((fact["field"], fact["value"]))
        for citation in fact["evidence"]:
            cite(citation)
    for field in ("target", "method", "system"):
        if sum(f["field"] == field for f in decision["facts"]) > 1:
            raise ValueError(f"An observation cannot silently combine multiple {field} assignments")
    for issue in decision["unresolved"]:
        if any(ref not in evidence for ref in issue["evidence_refs"]):
            raise ValueError("Unresolved experiment issue references evidence outside its input")
    if decision["facts"]:
        refs = [e["ref"] for e in decision["binding_evidence"]]
        if not any(evidence[ref].get("table_id") == scope["table_id"] for ref in refs):
            raise ValueError("Experiment binding must cite the target result table")
    if decision["status"] == "matched":
        fields = {f["field"] for f in decision["facts"]}
        if not {"target", "antigen_species", "method", "system"} <= fields or decision["unresolved"]:
            raise ValueError("Matched experiment requires all core dimensions without unresolved issues")
        if not any(evidence[c["ref"]].get("kind") in {"method", "context"} for c in decision["binding_evidence"]):
            raise ValueError("Matched experiment requires method/context evidence")


class ExperimentAgent:
    def __init__(self, provider, *, max_rounds: int = 3, text_budget: int = 120000):
        self.provider = provider
        self.max_rounds = max_rounds
        self.text_budget = text_budget

    def run(self, scope: dict, group: list[dict], tables: list[dict], contexts: list[dict],
            pages: dict[int, dict], template: dict, libraries: dict, audit: Path) -> dict:
        context = next((c for c in contexts if c.get("table_id") == scope["table_id"]), {})
        related_ids = {scope["table_id"]} | {c["table_id"] for c in contexts
            if context.get("logical_table_id") and c.get("logical_table_id") == context["logical_table_id"]}
        related = [t for t in tables if table_id(t) in related_ids]
        if scope["table_id"] not in {table_id(t) for t in related}:
            raise ValueError(f"Missing experiment table {scope['table_id']}")
        local = build_evidence(related, [c for c in contexts if c.get("table_id") in related_ids])
        # Source cells and paragraphs are complete; redundant OCR tokens/images are not sent.
        local = {ref: value for ref, value in local.items() if value["kind"] in {"cell", "context", "table"}}
        centers = {int(t["page"]) for t in related if t.get("page")}
        centers.update(int(c["page"]) for c in contexts if c.get("table_id") in related_ids and c.get("page"))
        requested = {p + offset for p in centers for offset in (-2, -1, 0, 1)}
        for record in group:
            profile = select_assay_profile(template, scope["table_id"], scope["region_id"], scope["endpoint"], record_id=record["record_id"])
            requested.update(profile.get("source_pages", []))
        headings = []
        for number, page in pages.items():
            lines = [line.strip() for line in page["text"].splitlines()
                     if re.match(r"\s*(?:Example\s+\d+|实施例\s*\d+|[A-H]\.\s+[A-Z])", line, re.I)]
            if lines:
                headings.append({"page": number, "headings": lines})
        local_text = "\n".join(pages[p]["text"] for p in requested if p in pages)
        examples = set(re.findall(r"\bExample\s+(\d+)\b", local_text, re.I))
        for item in headings:
            if any(re.match(rf"Example\s+{n}(?!\d)", h, re.I) for n in examples for h in item["headings"]):
                requested.update([item["page"], item["page"] + 1])
        loaded, retrieval_issues, searches, explicit_requests = {}, [], [], set()
        instructions = (Path(__file__).resolve().parent / "prompt/experiment.txt").read_text(encoding="utf-8")
        decision = None
        for round_index in range(self.max_rounds):
            used = sum(len(v["text"]) for v in [*local.values(), *loaded.values()])
            if used > self.text_budget:
                raise ValueError("Experiment table evidence exceeds budget; split the selected experiment")
            for number in sorted(requested, key=lambda p: min((abs(p - c) for c in centers), default=p)):
                ref = f"method:p{number}"
                if ref in loaded:
                    continue
                if number not in pages:
                    if number in explicit_requests:
                        retrieval_issues.append({"page": number, "reason": "missing_page_text"})
                    continue
                text = pages[number]["text"]
                if used + len(text) > self.text_budget:
                    retrieval_issues.append({"page": number, "reason": "evidence_budget"})
                    continue
                loaded[ref] = {"page": number, "kind": "method", "source": pages[number].get("source"), "text": text}
                used += len(text)
            evidence = {**local, **loaded}
            payload = {"scope": scope, "observations": [{"record_id": r["record_id"],
                "measurement": next((e for e in r.get("evidence", []) if e.get("type") == "assay_measurement"), {})} for r in group],
                "evidence": evidence, "page_catalogue": headings, "keyword_libraries": libraries["libraries"],
                "retrieval": {"round": round_index + 1, "max_rounds": self.max_rounds,
                              "unavailable": retrieval_issues, "searches": searches}}
            json_write(audit / f"input-{round_index + 1}.json", payload)
            decision, meta = self.provider.generate("experiment-v1", instructions, payload, ExperimentDecision,
                validate=lambda d: validate_decision(d, scope, evidence))
            json_write(audit / f"decision-{round_index + 1}.json", {"decision": decision, "agent": meta})
            if not decision["request_pages"] and not decision["search_queries"]:
                break
            requested = set(decision["request_pages"])
            explicit_requests.update(requested)
            searches = []
            for query in decision["search_queries"][:6]:
                hits = search_pages(query, pages)
                # Spend follow-up rounds on unseen evidence, not the same four
                # already-loaded result pages that repeat the assay heading.
                hits.sort(key=lambda p: (f"method:p{p}" in loaded,
                                        min((abs(p - c) for c in centers), default=p)))
                searches.append({"query": query, "total_hits": len(hits), "pages": hits[:4]})
                for p in hits[:4]:
                    requested.update([p - 1, p, p + 1])
        assert decision is not None
        if decision["request_pages"] or decision["search_queries"] or retrieval_issues:
            decision["status"] = "needs_review"
            decision["unresolved"].append({"field": "evidence", "reason": "retrieval_incomplete",
                "detail_zh": "仍有请求的实验原文未完成核查", "detail_en": "Requested experiment evidence remains unresolved", "evidence_refs": []})
        if decision["status"] == "needs_review" and not decision["unresolved"]:
            decision["unresolved"].append({"field": "experiment", "reason": "missing_evidence",
                "detail_zh": "实验信息尚不完整", "detail_en": "Experiment interpretation is incomplete", "evidence_refs": []})
        facts = decision["facts"]
        values = lambda field: [f["value"] for f in facts if f["field"] == field]
        refs = list(dict.fromkeys(c["ref"] for c in [*decision["binding_evidence"], *(c for f in facts for c in f["evidence"])]))
        profile = {"table_id": scope["table_id"], "region_id": scope["region_id"], "endpoints": [scope["endpoint"]],
            "protocol_id": f"experiment:{scope['scope_id']}", "protocol_ids": [f"experiment:{scope['scope_id']}"],
            "status": decision["status"], "label": " · ".join(values("target") + values("method")),
            "antigen_target": next(iter(values("target")), None), "antigen_species": values("antigen_species"),
            "cell_host_species": values("cell_host_species"), "species": values("antigen_species"),
            "assay_method": next(iter(values("method")), None), "assay_system": next(iter(values("system")), None),
            "experiment_facts": facts, "experiment_unresolved": decision["unresolved"],
            "summary_zh": decision["summary_zh"], "summary_en": decision["summary_en"],
            "evidence_refs": refs, "source_pages": sorted({evidence[r]["page"] for r in refs if evidence[r].get("page")}),
            "protocol_evidence": [{**evidence[r], "evidence_ref": r} for r in refs],
            "match_confidence": None, "match_reason": "experiment_agent_source_quotes", "llm": meta}
        json_write(audit / "result.json", profile)
        return {"profile": profile, "pages": {int(ref.split("p")[-1]): pages[int(ref.split("p")[-1])] for ref in loaded}}


def refresh_experiments(records: list[dict], template: dict, tables: list[dict], output_dir: Path,
                        source_pdf: Path | None = None, *, provider=None) -> tuple[list[dict], dict]:
    """Interpret selected experiments, applying only experiment metadata to their rows."""
    contexts = read_json(output_dir / "table_contexts.json", [])
    tables = [semantic_table(t) for t in tables]
    evidence = build_evidence(tables)
    pages = source_pages(output_dir, source_pdf)
    libraries = keyword_context()
    provider = provider or provider_from_env(output_dir)
    agent = ExperimentAgent(provider)
    groups = {}
    for record in records:
        scope = observation(record, evidence)
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:20]
        groups.setdefault(key, ({**scope, "scope_id": key}, []))[1].append(record)
    audit = output_dir / "experiment_runs" / uuid.uuid4().hex
    workers = max(1, min(4, int(os.environ.get("DATASETAGENT_EXPERIMENT_WORKERS", "3"))))
    def process(item):
        key, (scope, group) = item
        return key, agent.run(scope, group, tables, contexts, pages, template, libraries, audit / key)
    results = {}
    print(f"experiment-agent 0/{len(groups)}", flush=True)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="experiment") as pool:
        pending = [pool.submit(process, item) for item in groups.items()]
        for future in as_completed(pending):
            key, result = future.result()
            results[key] = result
            print(f"experiment-agent {len(results)}/{len(groups)}", flush=True)
    updated_template = copy.deepcopy(template)
    profiles = updated_template.setdefault("assay_template", {}).setdefault("record_profiles", {})
    updated_template["assay_keyword_context"] = libraries
    updated, used_pages = {}, {}
    for key, (_, group) in groups.items():
        profile = results[key]["profile"]
        used_pages.update(results[key]["pages"])
        for original in group:
            record = copy.deepcopy(original)
            identity = record["record_id"]
            scope = groups[key][0]
            annotation = annotate_assay(profile, scope["measurement_label"], scope["condition"], libraries,
                                        endpoint=scope["endpoint"], table_id=scope["table_id"])
            record.update(annotation, assay_method=profile["assay_method"], assay_system=profile["assay_system"],
                assay_cell_host_species=profile["cell_host_species"], assay_protocol_id=profile["protocol_id"],
                assay_protocol_ids=profile["protocol_ids"], assay_protocol_summary_zh=profile["summary_zh"],
                assay_protocol_summary_en=profile["summary_en"], assay_protocol_pages=profile["source_pages"],
                assay_protocol_match_reason=profile["match_reason"], assay_protocol_confidence=None,
                assay_protocol_evidence=profile["protocol_evidence"], assay_experiment={"agent": profile["llm"],
                    "scope_id": key, "facts": profile["experiment_facts"], "unresolved": profile["experiment_unresolved"]})
            record["evidence"] = [e for e in record.get("evidence", []) if e.get("type") != "assay_protocol"]
            record["evidence"].append({"type": "assay_protocol", "protocol_id": profile["protocol_id"],
                "pages": profile["source_pages"], "evidence": profile["protocol_evidence"],
                "summary_zh": profile["summary_zh"], "summary_en": profile["summary_en"]})
            for item in record["evidence"]:
                if item.get("type") == "assay_measurement":
                    for field in ("species", "assay_method", "assay_system", "assay_keywords", "assay_keyword_status", "antigen_target"):
                        item[field] = copy.deepcopy(record[field])
            profiles[identity] = profile
            updated[identity] = record
    methods = {p["page"]: p for p in read_json(output_dir / "assay_method_pages.json", [])}
    methods.update(used_pages)
    report = {"name": "ExperimentAgent", "mode": "llm", "scope_count": len(groups), "record_count": len(records),
        "matched_count": sum(r["assay_keyword_status"] == "matched" for r in updated.values()),
        "review_count": sum(r["assay_keyword_status"] == "needs_review" for r in updated.values()),
        "record_ids": list(updated), "source_pages": sorted(used_pages), "audit_dir": str(audit), "calls": provider.calls}
    return [updated[r["record_id"]] for r in records], {"document_template": updated_template,
        "assay_method_pages": [methods[p] for p in sorted(methods)], "experiment_agent": report,
        "llm_runs": [*read_json(output_dir / "llm_runs.json", []), *provider.calls]}
