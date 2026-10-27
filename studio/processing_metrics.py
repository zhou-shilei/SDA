"""Compact, source-backed processing statistics for the intake list."""

from collections import OrderedDict
import json
import math
from pathlib import Path


TOKEN_FIELDS = ("total_tokens", "input_tokens", "output_tokens", "cached_input_tokens")


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value if not integer or isinstance(value, int) else None


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _token_row(row):
    result = {key: _number(row.get(key), integer=True) for key in TOKEN_FIELDS}
    result["requests"] = _number(row.get("requests"), integer=True)
    coverage = row.get("field_coverage", {}).get("total_tokens", {})
    result["unknown_requests"] = max(_number(row.get("requests_without_usage"), integer=True) or 0,
                                     _number(coverage.get("unknown"), integer=True) or 0)
    return result


def processing_metrics(root, document_id, result_dir, job, run, runtime):
    """Choose one usage source; never sum overlapping reports or infer missing counts."""
    root, result_dir = Path(root).resolve(), Path(result_dir).resolve()

    def local(value):
        path = Path(value).resolve()
        return path if path.is_relative_to(root) else None

    report = run if isinstance(run, dict) else {}
    if not report:
        parent_report = local(result_dir.parent.parent / "run_summary.json")
        candidate = _read(parent_report) if parent_report else {}
        if candidate.get("document_id") == document_id:
            report = candidate
    runtime = runtime if isinstance(runtime, dict) else {}
    running = job.get("status") == "processing"
    seconds = _number(report.get("wall_seconds"))
    if report.get("resumed"):
        scope = "cumulative"
    elif report.get("run_kind") in {"ocr_reanalysis", "printed_style_repair"} or job.get("operation") == "compare":
        scope = "reanalysis"
    else:
        scope = "full_run"
    if seconds is None and not running:
        seconds = _number(runtime.get("wall_seconds"))
        if seconds is not None:
            scope = "stage_only"
    started_at = (job.get("started_at") or report.get("started_at_utc")) if running else None

    grouped = OrderedDict()
    for phase in report.get("phases", []):
        if not isinstance(phase, dict) or (duration := _number(phase.get("seconds"))) is None:
            continue
        name = phase.get("name") or "unknown"
        row = grouped.setdefault(name, {"stage": name, "seconds": 0.0, "count": 0, "level": 0})
        row["seconds"] += duration
        row["count"] += 1

    structured_value = job.get("structured_dir") or report.get("structured_dir")
    structured = local(structured_value) if structured_value else None
    structured_run = _read(structured / "run_summary.json") if structured else {}
    recognition = _read(structured / "recognition_summary.json") if structured else {}
    stages = []
    for name, row in grouped.items():
        stages.append(row)
        if name == "structured_document":
            for phase in structured_run.get("phases", []):
                if not isinstance(phase, dict) or (duration := _number(phase.get("seconds"))) is None:
                    continue
                stages.append({"stage": phase.get("name") or "unknown", "seconds": duration, "level": 1, "count": 1})
                if phase.get("name") == "recognition":
                    for branch in ("text", "table"):
                        details = recognition.get("runtime", {}).get(branch, {})
                        start, finish = _number(details.get("started")), _number(details.get("finished"))
                        if start is not None and finish is not None and finish >= start:
                            stages.append({"stage": "text_ocr" if branch == "text" else "table_ocr",
                                           "seconds": finish - start, "level": 2, "count": 1})
    operations = [{"stage": row.get("stage") or "unknown", "seconds": duration}
                  for row in runtime.get("stage_ranking", []) if isinstance(row, dict)
                  and (duration := _number(row.get("active_wall_seconds"))) is not None]

    # Finished run reports retain cumulative usage across resumes. A later
    # cache-only check may overwrite the local usage file with zero requests.
    # For an active run, prefer its live snapshot; never add duplicate copies.
    live_usage = _read(result_dir / "_token_usage.json")
    choices = (live_usage, report.get("token_usage"), runtime.get("token_usage")) if running or report.get("status") == "running" else (
        report.get("token_usage"), live_usage, runtime.get("token_usage"))
    usage = next((row for row in choices if isinstance(row, dict) and isinstance(row.get("totals"), dict)), {})
    available = isinstance(usage, dict) and isinstance(usage.get("totals"), dict)
    tokens = {"available": available, "totals": _token_row(usage["totals"]) if available else _token_row({}),
              "stages": [{"stage": row.get("stage") or "unknown", **_token_row(row)}
                         for row in usage.get("by_stage", []) if isinstance(row, dict)] if available else []}
    return {"time": {"seconds": seconds, "running": running, "started_at": started_at,
                     "scope": scope, "stages": stages, "operations": operations}, "tokens": tokens}
