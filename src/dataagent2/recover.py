"""Recover failed consolidation in a new directory with exact Reader caches only."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil

from .contracts import SCHEMA, check_shape
from .inputs import load_input, make_chunks, read
from .pipeline import reader_request, run
from .provider import Provider, digest, write_json


RECOVERY_GUIDANCE = """The preceding complete consolidation failed validation. Regenerate the COMPLETE
dataset from the immutable initial records, Reader proposals and addressed source evidence.
Account for EVERY Reader action exactly once, including rejected/conflicting proposals.
Treat previous errors and proposals as diagnostic data, never as authoritative evidence.
Do not infer a SEQ ID from matching sequence characters; cite its literal printed number.
Use actual source table IDs, not printed captions such as 'Table 10'. Text-only experiments
may use a null table when the evidence supports them without an addressed result table.
Units must retain a literal spelling present in cited sources, including OCR math notation.
For sequences, value must equal the literal characters materialized from the supplied parts.
Prefer exact unchanged record references; never guess residues or substitute a full chain for
a variable domain. Domain reviews must point to final expanded array indices for the correct
entity and region. Mark unsupported or ambiguous assignments honestly and preserve the issue.
Preserve initial facts unless an explicit, source-supported seed_change accounts for a change.
Do not delete valid information to make validation pass.
"""


def reader_fingerprint(instructions, payload, manifest):
    return {"stage": "reader", "instructions": instructions, "payload": payload,
            "schema": SCHEMA, "model": manifest["model"], "effort": manifest["effort"]}


def prepare_recovery(source):
    """Read and validate every exact cache before permitting any model call."""
    source = Path(source).resolve()
    manifest = read(source / "manifest.json")
    if not manifest:
        raise ValueError("The original Reader manifest is missing")
    action = read(source / "action_agent.json", {})
    if action.get("status") != "failed":
        raise ValueError("Recovery requires a failed Action consolidation")
    data = load_input(Path(manifest["native_input"]))
    if data["document_id"] != manifest["document_id"] or data["pdf_sha256"] != manifest["pdf_sha256"]:
        raise ValueError("Original PDF or document identity changed")
    chunks = make_chunks(data, manifest["chunk_chars"])
    if chunks != manifest["chunks"]:
        raise ValueError("Reader chunks changed; refusing to reread or mix different evidence")
    cached = []
    for chunk in chunks:
        payload, instructions = reader_request(data, chunk, manifest.get("reader_source_format", "rows"))
        key = digest(reader_fingerprint(instructions, payload, manifest))
        folder = source / "requests" / key
        fingerprint = read(folder / "input.json")
        response = read(folder / "validated.json")
        if fingerprint is None or digest(fingerprint) != key or response is None:
            raise ValueError(f"No exact validated Reader cache for {chunk['id']}; no model call made")
        check_shape(response, SCHEMA)
        cached.append({"chunk": chunk["id"], "key": key, "response_hash": digest(response)})
    plan = {"source": str(source), "document_id": data["document_id"],
            "pdf_sha256": data["pdf_sha256"], "reused_reader_count": len(cached), "readers": cached,
            "source_manifest_hash": digest(manifest), "failed_action_hash": digest(action),
            "feedback": RECOVERY_GUIDANCE + "\nPrevious validation failure:\n" + action.get("error", ""),
            "policy": "Fresh output; no OCR, template or new Reader calls; unchanged full validation"}
    return data, manifest, plan


class RecoveryProvider(Provider):
    """Refuse network Reader calls even if a cache changes after preflight."""

    def __init__(self, output, source, manifest, plan, *, action_effort="high", action_timeout=900, transport="http"):
        super().__init__(output, model=manifest["model"], effort=manifest["effort"],
                         action_effort=action_effort, action_timeout=action_timeout, request_attempts=1, transport=transport,
                         answer_effort=manifest.get("answer_effort", "medium"), reuse_readers_from=source)
        self.expected_readers = {row["key"]: row["response_hash"] for row in plan["readers"]}
        self.action_feedback = plan["feedback"]

    def generate(self, stage, instructions, payload):
        if stage == "reader":
            fingerprint = reader_fingerprint(instructions, payload, {"model": self.model, "effort": self.effort})
            key = digest(fingerprint)
            folder = self.reuse_readers_from / "requests" / key
            response = read(folder / "validated.json")
            if (key not in self.expected_readers or response is None
                    or digest(response) != self.expected_readers[key]
                    or digest(read(folder / "input.json")) != key):
                raise ValueError("Reader cache changed after preflight; refusing a new Reader request")
        return super().generate(stage, instructions, payload)


def restore_validated_action(output, data):
    """Cache a fully validated, locally repaired response under its exact original request."""
    from .action_agent import validate_action_result
    output = Path(output)
    meta = read(output / "action_agent.json", {})
    if meta.get("status") != "validated":
        return None
    payload = read(output / "action_agent.input.json")
    response = read(output / "action_agent.response.json")
    key = meta.get("provider", {}).get("key", "")
    if (not re.fullmatch(r"[0-9a-f]{64}", key) or digest(payload) != meta.get("input_hash")
            or digest(response) != meta.get("response_hash")):
        raise ValueError("Validated Action input/response or request identity changed")
    request = read(output / "requests" / key / "input.json")
    if not request or digest(request) != key or request.get("stage") != "action":
        raise ValueError("Validated Action no longer has its exact original request")
    check_shape(response, request["schema"])
    validate_action_result(data, payload, response)
    audit = {"request_key": key, "input_hash": digest(payload), "response_hash": digest(response),
             "full_validation": True, "original_metadata": meta,
             "source": "complete Action response after full host validation; original raw calls retained"}
    write_json(output / "requests" / key / "validated.json", response)
    write_json(output / "requests" / key / "completed_action_cache.json", audit)
    return audit


def recover(source, output, *, dry_run=False, action_effort="high", action_timeout=900,
            transport="http", resume=False):
    if action_timeout < 1:
        raise ValueError("Action timeout must be positive")
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Recovery output must be separate from historical artifacts")
    if output.exists() and not resume:
        raise ValueError("Choose a new recovery output directory")
    data, manifest, plan = prepare_recovery(source)
    if resume:
        previous = read(output / "recovery.json", {})
        if (previous.get("status") != "failed" or previous.get("source") != str(source)
                or previous.get("source_manifest_hash") != plan["source_manifest_hash"]
                or previous.get("failed_action_hash") != plan["failed_action_hash"]
                or previous.get("action_effort") != action_effort):
            raise ValueError("Resume requires a finished failed recovery with the same evidence and Action effort")
        if dry_run:
            return {**plan, "status": "resume_preflight_validated"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = output / "resume_archive" / stamp
        archive.mkdir(parents=True)
        for path in output.glob("*.json"):
            shutil.copyfile(path, archive / path.name)
        restored = restore_validated_action(output, data)
        plan["resume_history"] = previous.get("resume_history", []) + [{
            "archive": str(archive), "previous_transport": previous.get("transport", "auto"),
            "action_cache": restored}]
    plan.update(action_effort=action_effort, action_timeout=action_timeout, transport=transport,
                request_attempts=1, output=str(output), status="preflight_validated")
    output.mkdir(parents=True, exist_ok=resume)
    write_json(output / "recovery.json", plan)
    print(f"Recovery preflight: {plan['reused_reader_count']}/{len(manifest['chunks'])} exact Readers; no new Reader calls", flush=True)
    if dry_run:
        return plan
    plan["status"] = "running"
    write_json(output / "recovery.json", plan)
    provider = RecoveryProvider(output, source, manifest, plan, action_effort=action_effort,
                                action_timeout=action_timeout, transport=transport)
    try:
        report = run(data, provider, output, chunk_chars=manifest["chunk_chars"],
                     workers=manifest.get("reader_workers", 0),
                     max_questions=manifest.get("max_questions", 8),
                     question_chars=manifest.get("question_chars", 8000),
                     answer_workers=manifest.get("answer_workers", 4),
                     reader_source_format=manifest.get("reader_source_format", "rows"))
        plan.update(status="validated" if report.get("benchmark_complete") else "failed",
                    action_complete=report.get("action_complete"), reader_complete=report.get("reader_complete"))
        if not report.get("benchmark_complete"):
            from .diagnostics import failure_details
            plan["failure_details"] = failure_details(report)
        return report
    except Exception as error:
        plan.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        plan["new_model_usage"] = provider.usage()
        write_json(output / "recovery.json", plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Failed dataagent2 output directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Verify all caches; make no model calls")
    parser.add_argument("--resume", action="store_true", help="Resume a failed recovery and reuse its validated Action/Answers")
    parser.add_argument("--action-effort", default="high")
    parser.add_argument("--action-timeout", type=int, default=900)
    parser.add_argument("--transport", choices=["http", "auto"], default="http")
    args = parser.parse_args()
    if args.action_timeout < 1:
        parser.error("Action timeout must be positive")
    report = recover(args.source, args.output, dry_run=args.dry_run,
                     action_effort=args.action_effort, action_timeout=args.action_timeout,
                     transport=args.transport, resume=args.resume)
    return 0 if args.dry_run or report.get("benchmark_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
