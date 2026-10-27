"""Condense collection diagnostics into source-scoped questions, never answers."""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from pydantic import Field

from .llm_io import Contract


class ReviewQuestion(Contract):
    task_ids: list[str] = Field(min_length=1)
    question: str = Field(min_length=5, max_length=180)
    question_en: str = Field(min_length=5, max_length=300)
    kind: Literal["name_equivalence", "collection_scope", "sequence", "assay", "source"]
    answer_type: Literal["yes_no", "text"]
    names: list[str]
    evidence_refs: list[str]


class QuestionPlan(Contract):
    questions: list[ReviewQuestion]


PROMPT = Path(__file__).resolve().parent / "prompt" / "collection_questions.txt"


def validate_questions(plan: dict, tasks: list[dict]) -> None:
    by_id = {t["id"]: t for t in tasks}
    covered = set()
    equivalences = set()
    texts = set()
    for question in plan["questions"]:
        ids = question["task_ids"]
        if len(ids) != len(set(ids)) or not set(ids) <= by_id.keys():
            raise ValueError("Questions must reference unique supplied task IDs")
        covered.update(ids)
        text = question["question"].strip()
        if not text.endswith(("?", "？")) or text in texts or re.match(r"Q\d+[.．:]", text):
            raise ValueError("Return distinct, unnumbered questions ending in a question mark")
        texts.add(text)
        refs = {ref for identity in ids for ref in by_id[identity]["evidence_refs"]}
        if not set(question["evidence_refs"]) <= refs or (refs and not question["evidence_refs"]):
            raise ValueError(f"Question {text!r} must cite nonempty evidence_refs copied from its task_ids. "
                             f"Allowed refs include: {sorted(refs)[:12]}; "
                             f"invalid refs: {sorted(set(question['evidence_refs']) - refs)}")
        if question["kind"] == "name_equivalence":
            if len(ids) != 1 or question["answer_type"] != "yes_no":
                raise ValueError("name_equivalence requires one task, two supplied names and yes_no. For an unknown name asking which antibody it denotes, use kind=source and answer_type=text.")
            issue = by_id[ids[0]].get("name_issue", {})
            aliases = {n for c in issue.get("candidates", []) for n in c["raw_names"]}
            names = question["names"]
            if (len(names) != 2 or names[0] != issue.get("raw_name") or names[1] not in aliases
                    or any(name not in text for name in names)):
                raise ValueError("Equivalence questions must preserve the observed name and a registered alias")
            equivalences.add(ids[0])
    if covered != by_id.keys():
        raise ValueError(f"Do not drop diagnostics; missing task IDs: {sorted(by_id.keys() - covered)}")
    required = {t["id"] for t in tasks if any(c["raw_names"] for c in t.get("name_issue", {}).get("candidates", []))}
    if not required <= equivalences:
        raise ValueError(f"Ask explicit name-equivalence questions for {sorted(required - equivalences)}")


class QuestionAgent:
    def __init__(self, provider, *, audience="human"):
        self.provider = provider
        self.audience = audience

    def run(self, document_id: str, tasks: list[dict], on_progress=None) -> dict:
        groups = [
            [t for t in tasks if t.get("name_issue", {}).get("candidates")],
            [t for t in tasks if t.get("name_issue") and not t["name_issue"]["candidates"]],
            [t for t in tasks if not t.get("name_issue")],
        ]
        batches = [group[offset:offset + 64] for group in groups for offset in range(0, len(group), 64)]
        completed, errors = {}, []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._batch, document_id, batch): i for i, batch in enumerate(batches)}
            for future in as_completed(futures):
                try:
                    completed[futures[future]] = future.result()
                except Exception as exc:
                    errors.append(exc)
                    continue
                if on_progress:
                    ordered = [completed[i] for i in sorted(completed)]
                    questions = [q for result in ordered for q in result["questions"]]
                    covered = {identity for q in questions for identity in q["task_ids"]}
                    on_progress({"questions": questions, "pending_diagnostics": len(tasks) - len(covered),
                        "agent": {"name": "QuestionAgent", "mode": "llm", "batch_count": len(batches),
                                  "batches": [r["metadata"] for r in ordered]}})
        if errors:
            raise errors[0]
        results = [completed[i] for i in sorted(completed)]
        questions = [q for result in results for q in result["questions"]]
        validate_questions({"questions": questions}, tasks)
        return {"questions": questions, "agent": {"name": "QuestionAgent", "mode": "llm",
            "batches": [r["metadata"] for r in results], "batch_count": len(results)}}

    def _batch(self, document_id: str, tasks: list[dict]) -> dict:
        # Repeated diagnoses travel once; each task retains its own scope and refs.
        messages, packed = [], []
        for task in tasks:
            indices = []
            for message in task["messages"]:
                if message not in messages:
                    messages.append(message)
                indices.append(messages.index(message))
            item = {k: task[k] for k in ("id", "category", "page", "table_ids", "codes")}
            item.update(message_indices=indices, evidence_refs=task["evidence_refs"][:12])
            if task.get("name_issue"):
                issue = task["name_issue"]
                item["name_issue"] = {"raw_name": issue["raw_name"], "candidates": issue["candidates"],
                                      "measurement_count": len(issue["measurements"])}
            if task.get("targets"):
                item["targets"] = task["targets"]
            packed.append(item)
        instructions = PROMPT.read_text(encoding="utf-8")
        if self.audience == "answer_agent":
            instructions += ("\nThese questions go to an evidence-reading AnswerAgent first. Ask what the original "
                "source establishes, including the exact VH/VL, SEQ ID, variable-domain boundary, OCR characters, "
                "method, species or system needed to resolve the diagnostic. Source images will be supplied to "
                "AnswerAgent. Do not ask for engineering approval or assume an answer. One shared damaged SEQ "
                "can be one question covering all its supplied entity/chain targets. Use targets.name in "
                "question text, never the internal entity_id.\n")
        if all(t.get("name_issue") and not t["name_issue"]["candidates"] for t in tasks):
            instructions += "\nThis batch contains ONLY unknown names without alias candidates. Use kind=source, answer_type=text and names=[the exact observed name]. NEVER use kind=name_equivalence for this batch. Ask which antibody each name refers to; do not invent an alternative name.\n"
        elif all(not t.get("name_issue") for t in tasks):
            instructions += "\nThis batch has NO name_issue candidate pairs. Do not use kind=name_equivalence. Incomplete antibody names require kind=source, answer_type=text. Each question MUST cite at least one evidence_ref copied verbatim from the evidence_refs of its own task_ids when any are supplied. Never construct cell refs from prose; use the supplied image/table ref if a specific cell ref is unavailable.\n"
        plan, meta = self.provider.generate("collection-questions-v1", instructions,
            {"document_id": document_id, "diagnoses": messages, "tasks": packed}, QuestionPlan,
            validate=lambda result: validate_questions(result, tasks))
        return {"questions": plan["questions"], "metadata": meta}


def question_tasks(questions: list[dict], tasks: list[dict]) -> list[dict]:
    """Keep original evidence behind each concise question and its saved answer."""
    by_id = {task["id"]: task for task in tasks}
    result = []
    for number, question in enumerate(questions, 1):
        sources = [by_id[identity] for identity in question["task_ids"]]
        task = {**sources[0]}
        task.update(category="question", question_number=number, question=question["question"],
                    question_en=question["question_en"], title=question["question"],
                    answer_type=question["answer_type"], question_kind=question["kind"],
                    source_task_ids=question["task_ids"], names=question["names"])
        task["source_categories"] = sorted({source["category"] for source in sources})
        for field in ("table_ids", "record_ids", "codes", "messages", "evidence_refs"):
            task[field] = list(dict.fromkeys(v for source in sources for v in source[field]))
        if len(sources) > 1:
            task.pop("name_issue", None)
        content = {**question, "sources": [s["fingerprint"] for s in sources]}
        task["fingerprint"] = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        task["id"] = "question-" + task["fingerprint"][:20]
        result.append(task)
    return result
