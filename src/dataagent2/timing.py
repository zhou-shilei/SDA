"""Persist phase measurements and recover explicitly scoped historical timings."""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import time


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def read_object(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


class PhaseTimings:
    """Main-thread phases; snapshots also survive an interrupted run."""

    def __init__(self, path):
        self.path = Path(path)
        self.origin = time.monotonic()
        self.events = []
        self.active = []

    def save(self):
        now = time.monotonic()
        value = {"events": self.events, "active": [
            {"stage": stage, "seconds": now - start, "status": "running"}
            for stage, start in self.active]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)

    @contextmanager
    def measure(self, stage):
        start, status = time.monotonic(), "completed"
        active = (stage, start)
        self.active.append(active)
        self.save()
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            end = time.monotonic()
            self.active.remove(active)
            self.events.append({"stage": stage, "seconds": end - start,
                "start_seconds": start - self.origin, "end_seconds": end - self.origin,
                "status": status})
            self.save()


def measured_phases(directory):
    saved = read_object(Path(directory) / "phase_timings.json")
    grouped = {}
    for event in saved.get("events", []) + saved.get("active", []):
        if not isinstance(event, dict) or number(event.get("seconds")) is None:
            continue
        stage = event.get("stage")
        if not isinstance(stage, str):
            continue
        row = grouped.setdefault(stage, {"stage": stage, "seconds": 0.0,
                                         "basis": "phase", "status": "completed"})
        row["seconds"] += event["seconds"]
        if event.get("status") in {"running", "failed"}:
            row["status"] = event["status"]
    return list(grouped.values())


def request_coverage(directory):
    """Union request intervals, including failures/retries, never sum parallel work.

    Old Provider attempt IDs embed time.time_ns() at admission. New calls also
    store the actual request start explicitly. Neither measures host work.
    """
    groups, seen = {}, set()
    try:
        lines = (Path(directory) / "calls.jsonl").read_text().splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        try:
            call = json.loads(line)
        except ValueError:
            continue  # A live JSONL tail can be incomplete.
        if not isinstance(call, dict) or call.get("stage") not in {"reader", "action", "answer", "answer_action", "action_repair"}:
            continue
        identity = call.get("attempt_id")
        if not isinstance(identity, str) or identity in seen or number(call.get("seconds")) is None:
            continue
        seen.add(identity)
        start = number(call.get("started_at_unix"))
        if start is None:
            try:
                stamp = identity.rsplit("-", 1)[1]
                start = int(stamp) / 1e9 if len(stamp) == 19 and stamp.isdigit() else None
            except (ValueError, IndexError):
                start = None
        if start is not None:
            stage = call.get("parent_stage", "action") if call["stage"] == "action_repair" else call["stage"]
            if stage not in {"reader", "action", "answer", "answer_action"}:
                continue
            groups.setdefault(stage, []).append((start, start + call["seconds"]))
    result = []
    for stage, spans in groups.items():
        total, end = 0.0, 0.0
        for start, finish in sorted(spans):
            total += max(0.0, finish - max(start, end))
            end = max(end, finish)
        result.append({"stage": {"reader": "dataagent2_reader", "action": "dataagent2_action", "answer": "dataagent2_questions",
                                  "answer_action": "dataagent2_answer_action"}[stage],
                       "seconds": total, "basis": "request_coverage", "requests": len(spans)})
    return result


def extraction_stages(directory, runtime=None):
    """Prefer measured phases; historical fallbacks retain their actual scope."""
    directory = Path(directory)
    runtime = runtime or {}
    rows = runtime.get("phase_ranking") or measured_phases(directory)
    if rows:
        result = [dict(row, basis=row.get("basis", "phase")) for row in rows
                  if isinstance(row, dict) and number(row.get("seconds")) is not None]
    else:
        result = []
    known = {row["stage"] for row in result}
    for row in read_object(directory / "shared_seed/pipeline_performance.json").get("stages", []):
        if not isinstance(row, dict) or row.get("stage") in known or number(row.get("elapsed_seconds")) is None:
            continue
        result.append({"stage": row["stage"], "seconds": row["elapsed_seconds"],
                       "basis": "phase", "status": row.get("status")})
        known.add(row["stage"])
    reader = directory / "dataagent2"
    precise = measured_phases(reader)
    for row in precise + request_coverage(reader):
        if row["stage"] not in known:
            result.append(row)
            known.add(row["stage"])
    # Show missing host phases explicitly for old Reader runs, without assigning
    # the unexplained residual to a particular operation.
    if (reader / "manifest.json").is_file():
        for stage in ("sequence_ocr_refresh", "dataagent2_input", "dataagent2_prepare", "dataagent2_reader",
                      "dataagent2_merge", "dataagent2_action", "dataagent2_gap_detection", "dataagent2_questions",
                      "dataagent2_export", "result_export"):
            if stage not in known:
                result.append({"stage": stage, "seconds": None, "basis": "unrecorded"})
    order = ["sequence_ocr_refresh", "discover", "context", "triage", "select", "extract",
             "index_document", "classify_tables", "assay_method_context", "template",
             "dataagent2_input", "dataagent2_prepare", "dataagent2_reader", "dataagent2_merge",
             "dataagent2_action", "dataagent2_gap_detection", "dataagent2_questions", "dataagent2_answer_action",
             "dataagent2_export", "result_export"]
    result.sort(key=lambda row: order.index(row["stage"]) if row["stage"] in order else len(order))
    return result
