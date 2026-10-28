"""Run unchanged dataagent2 assembly with a fixed question budget, cache only.

Every request must match the original model, prompt, schema and complete payload
hash. A cache miss fails; this provider cannot launch a model or repair answers.
"""

import argparse
import hashlib
from pathlib import Path

from dataagent2.contracts import INSTRUCTIONS, SCHEMA, check_shape
from dataagent2.inputs import load_input, read
from dataagent2.pipeline import run
from dataagent2.provider import Provider, digest, write_json


class SavedResponses:
    def __init__(self, source):
        self.source = Path(source)
        self.manifest = read(self.source / "manifest.json")
        self.model, self.effort = self.manifest["model"], self.manifest["effort"]
        self.request_interval = self.manifest.get("request_interval_seconds")
        self.used = set()
        self.original = Provider(self.source, model=self.model, effort=self.effort)

    def generate(self, stage, instructions, payload):
        fingerprint = {"stage": stage, "instructions": instructions, "payload": payload,
                       "schema": SCHEMA, "model": self.model, "effort": self.effort}
        key = digest(fingerprint)
        request = self.source / "requests" / key
        saved_input, response = read(request / "input.json"), read(request / "validated.json")
        if saved_input is None or response is None or digest(saved_input) != key:
            raise RuntimeError(f"Saved request/response unavailable or changed: {stage} {key}; no model call allowed")
        check_shape(response)
        self.used.add(key)
        return response, {"key": key, "cache_hit": True, "original_request": str(request.resolve())}

    def usage(self):
        selected = Provider(self.source, model=self.model, effort=self.effort)
        selected.calls = [call for call in self.original.calls if call["key"] in self.used]
        usage = selected.usage()
        usage.update(new_model_calls=0, source_run_actual_usage=self.original.usage(),
                     note="Historical usage of selected requests and retries. Replay makes zero model calls; original collection also includes extended questions.")
        return usage


def replay_bounded(source, output, questions=8):
    source, output = Path(source).resolve(), Path(output).resolve()
    provider = SavedResponses(source)
    manifest = provider.manifest
    data = load_input(manifest["native_input"])
    original_signature = digest({"input": data["signature"], "instructions": INSTRUCTIONS,
                                 "chunk_chars": manifest["chunk_chars"], "model": provider.model,
                                 "effort": provider.effort})
    if original_signature != manifest["signature"]:
        raise ValueError("Original extraction inputs, prompt or settings changed")
    output.mkdir(parents=True, exist_ok=True)
    result = run(data, provider, output, chunk_chars=manifest["chunk_chars"], workers=1,
                 max_questions=questions)
    write_json(output / "source_cache_replay.json", {
        "source_native": str(source), "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "question_budget": questions, "new_model_calls": 0, "used_request_keys": sorted(provider.used),
        "reader_complete": result["reader_complete"], "benchmark_complete": result["benchmark_complete"],
        "quality_scope": "Complete unchanged Reader and first budgeted questions, using exact original model responses.",
        "cost_scope": "Original source run may contain extra diagnostic questions; physical collection costs are retained separately.",
    })
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-questions", type=int, default=8)
    args = parser.parse_args()
    result = replay_bounded(args.source, args.output, args.max_questions)
    if not result["benchmark_complete"]:
        raise SystemExit("Cache-only benchmark is incomplete; missing responses are not scored")
