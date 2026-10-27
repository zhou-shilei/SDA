"""Evidence-backed answers and staged patches; raw OCR is never overwritten."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from .llm_io import AssayProfile, Contract, SequenceDecision, SourceQuote, find_image, require_refs
from .llm_runtime import CodexProviderError
from .sequence_evidence import prompt_source, source_digest


class CellImageReview(Contract):
    source_ref: str
    transcription: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class QuestionAnswer(Contract):
    document_id: str
    question_id: str
    status: Literal["answered", "uncertain"]
    answer: str = Field(min_length=1)
    evidence: list[SourceQuote]
    source_reviews: list[CellImageReview]
    sequence_updates: list[SequenceDecision]
    assay_updates: list[AssayProfile]


def reviewed_ref(ref: str, source: dict, image_hash: str) -> str:
    digest = hashlib.sha256((ref + source_digest(source) + image_hash).encode()).hexdigest()[:24]
    return f"image-review:{digest}:{ref}"


def validate_reviewed_source(source: dict, current: dict) -> None:
    """Recheck immutable OCR and the exact image bytes at every subsequent link."""
    review = source.get("image_review", {})
    original = current.get(review.get("original_ref"))
    if (not original or original.get("kind") != "cell"
            or source_digest(original) != review.get("original_sha256")
            or source.get("table_id") != original.get("table_id")
            or source.get("row_index") != original.get("row_index")
            or source.get("col_index") != original.get("col_index")):
        raise ValueError("Reviewed source no longer matches its original OCR cell")
    path = Path(review.get("image_path", ""))
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != review.get("image_sha256"):
        raise ValueError("Reviewed source image is missing or changed")
    if (source_digest(source) != review.get("transcription_sha256") or not review.get("question_id")
            or not review.get("answer_sha256") or review.get("agent") != "AnswerAgent"):
        raise ValueError("Reviewed transcription audit is missing or changed")
    if source.get("source_ref") != reviewed_ref(review["original_ref"], original, review["image_sha256"]):
        raise ValueError("Reviewed source identity does not match its image/OCR provenance")


def answer_sources(answer: dict, sources: dict, review_sources: dict, *, question_id: str) -> dict:
    ledger = copy.deepcopy(sources)
    seen = set()
    for review in answer["source_reviews"]:
        ref = review["source_ref"]
        if ref in seen or ref not in review_sources:
            raise ValueError("Image review must select unique cells with an attached table image")
        seen.add(ref)
        original, manifest = sources[ref], review_sources[ref]
        derived = {**copy.deepcopy(original), "kind": "cell", "text": review["transcription"],
                   "source_ref": manifest["reviewed_ref"]}
        derived["image_review"] = {
            "original_ref": ref, "original_sha256": source_digest(original),
            "image_path": manifest["image_path"], "image_sha256": manifest["image_sha256"],
            "transcription_sha256": source_digest(derived), "question_id": question_id,
            "answer_sha256": hashlib.sha256(json.dumps(answer, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "agent": "AnswerAgent", "rationale": review["rationale"]}
        validate_reviewed_source(derived, sources)
        ledger[manifest["reviewed_ref"]] = derived
    return ledger


def validate_answer(answer: dict, document_id: str, question: dict, sources: dict,
                    review_sources: dict, decisions: list[dict], regions: dict) -> dict:
    from .llm_sequence_agent import ground_decision
    from .llm_table_agents import validate_assay_profiles

    answer = QuestionAnswer.model_validate(answer).model_dump()
    if answer["document_id"] != document_id or answer["question_id"] != question["id"]:
        raise ValueError("Answer changed the document or question identity")
    changes = answer["sequence_updates"] or answer["source_reviews"] or answer["assay_updates"]
    if answer["status"] == "uncertain" and changes:
        raise ValueError("Uncertain answers must not modify data")
    if answer["status"] == "answered" and not answer["evidence"]:
        raise ValueError("Answered questions require source quotations")
    for quote in answer["evidence"]:
        require_refs([quote["source_ref"]], sources)
        matches = list(re.finditer(re.escape(quote["quote"]), sources[quote["source_ref"]].get("text", "")))
        if quote["occurrence"] >= len(matches):
            raise ValueError("Answer quote must occur verbatim in the supplied evidence")
    ledger = answer_sources(answer, sources, review_sources, question_id=question["id"])
    allowed = {(t["entity_id"], t["chain"]) for t in question.get("targets", [])}
    originals = {(d["entity_id"], d["chain"]): d for d in decisions}
    seen = set()
    for update in answer["sequence_updates"]:
        key = update["entity_id"], update["chain"]
        if key not in allowed or key in seen or key not in originals:
            raise ValueError("Sequence update is duplicate or outside this question's targets")
        seen.add(key)
        original = originals[key]
        if original["status"] == "resolved" or update["status"] != "resolved":
            raise ValueError("Answers may only fill unresolved sequences")
        if original["seq_id"] is not None and update["seq_id"] != original["seq_id"]:
            raise ValueError("An explicit SEQ assignment cannot be bypassed")
        if not set(original["evidence_refs"]) <= set(update["evidence_refs"]):
            raise ValueError("Sequence update discarded original ownership evidence")
        if update["source_conflicts"] != original.get("source_conflicts", []):
            raise ValueError("Sequence update discarded or altered existing source conflicts")
        ground_decision(update, ledger)
    used = {f["source_ref"] for d in answer["sequence_updates"] for f in d["fragments"]}
    if any(review_sources[r["source_ref"]]["reviewed_ref"] not in used for r in answer["source_reviews"]):
        raise ValueError("Image transcriptions must be used by a validated sequence update")
    table_ids = set(question.get("table_ids", []))
    validate_assay_profiles(answer["assay_updates"], sources, table_ids, regions)
    if answer["assay_updates"]:
        from units.domain_knowledge import load_domain_knowledge
        vocabulary = load_domain_knowledge()["groups"]
        def supported(value, group, text):
            if value.casefold() in text.casefold():
                return True
            for term in vocabulary.get(group, []):
                labels = [term.get(k, "").casefold() for k in ("key", "label_zh", "label_en")]
                if value.casefold() in labels and any(re.search(p, text, re.I) for p in term.get("patterns", [])):
                    return True
            return False
        for profile in answer["assay_updates"]:
            text = "\n".join(q["quote"] for q in answer["evidence"] if q["source_ref"] in profile["evidence_refs"])
            if not text:
                raise ValueError("Assay changes require quotations from their own evidence_refs")
            terms = [(v, "species") for v in profile["species"]]
            terms += [(profile[k], group) for k, group in (("method", "methods"), ("system", "systems")) if profile[k]]
            if any(not supported(value, group, text) for value, group in terms):
                raise ValueError("Assay method, species or system lacks quoted terminology evidence")
    return ledger


class AnswerAgent:
    def __init__(self, provider, *, image_roots: list[Path], source_budget=96_000):
        self.provider, self.image_roots, self.source_budget = provider, image_roots, source_budget

    def run(self, document_id: str, questions: list[dict], graph: dict, tables: list[dict],
            template: dict) -> list[dict]:
        from .llm_sequence_agent import decision_refs

        sources = graph.get("sources", {})
        extraction = template.get("sequence_extraction", {})
        decisions = extraction.get("decisions", [])
        by_id = {e["entity_id"]: e for e in template.get("entity_template", {}).get("entities", [])}
        regions = {t["table_id"]: t.get("regions", []) for t in graph.get("classifications", [])}
        # Actual regions are supplied by the workflow, never reconstructed from names.
        regions.update(graph.get("assay_regions", {}))
        instructions = (Path(__file__).parent / "prompt" / "answer_questions.txt").read_text()
        results = []
        for index, question in enumerate(questions, 1):
            refs = list(question.get("evidence_refs", []))
            targets = {(t["entity_id"], t["chain"]) for t in question.get("targets", [])}
            target_decisions = [d for d in decisions if (d["entity_id"], d["chain"]) in targets]
            for d in target_decisions:
                refs.extend(decision_refs(d))
                refs.extend(by_id.get(d["entity_id"], {}).get("evidence_refs", []))
            owner_tables = set(question.get("table_ids", [])) | {
                sources[r].get("table_id") for r in refs if r in sources}
            pages = {sources[r].get("page") for r in refs if r in sources}
            # Read the complete scoped tables and same-page definitions before secondary prose.
            extra = [r for r, s in sources.items() if s.get("table_id") in owner_tables
                     or (s.get("page") in pages and s.get("kind") != "cell")]
            loaded, size, omitted = {}, 0, []
            for ref in dict.fromkeys([*refs, *extra]):
                if ref not in sources:
                    continue
                length = len(json.dumps(sources[ref], ensure_ascii=False))
                if size + length > self.source_budget:
                    omitted.append(ref)
                else:
                    loaded[ref], size = sources[ref], size + length
            images, manifests, review_sources = [], [], {}
            for table in tables:
                tid = str(table.get("source_id") or table.get("image"))
                if tid not in owner_tables or len(images) >= 4:
                    continue
                image = find_image(table, self.image_roots)
                if image is None:
                    continue
                sha = hashlib.sha256(image.read_bytes()).hexdigest()
                images.append(image)
                manifests.append({"index": len(images), "table_id": tid, "image_path": str(image), "image_sha256": sha})
                for ref, source in loaded.items():
                    if source.get("kind") == "cell" and source.get("table_id") == tid:
                        review_sources[ref] = {**manifests[-1], "reviewed_ref": reviewed_ref(ref, source, sha)}
            payload = {"document_id": document_id, "question_id": question["id"], "question": question,
                "current_decisions": target_decisions,
                "current_assay_profiles": template.get("assay_template", {}).get("region_profiles", []),
                "assay_regions": regions, "loaded_sources": {r: prompt_source(s) for r, s in loaded.items()},
                "omitted_evidence_refs": omitted, "attached_images": manifests, "review_sources": review_sources}
            print(f"llm-answer: question {index}/{len(questions)} {question['id']}", flush=True)
            try:
                answer, meta = self.provider.generate("answer-questions-v1", instructions, payload, QuestionAnswer,
                    images=images, validate=lambda a: validate_answer(a, document_id, question, loaded,
                        review_sources, decisions, regions))
                ledger = validate_answer(answer, document_id, question, loaded, review_sources, decisions, regions)
                results.append({"question_id": question["id"], "status": "validated", "answer": answer,
                    "reviewed_sources": {r: s for r, s in ledger.items() if r not in loaded},
                    "loaded_refs": list(loaded), "images": manifests, "llm": meta})
            except (CodexProviderError, ValueError) as exc:
                results.append({"question_id": question["id"], "status": "failed", "error": str(exc)})
        return results
