"""Actual model token usage, preserving unknown counts and concurrent request context."""

import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "uncached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
_CONTEXT = ContextVar("sda_token_usage", default={})


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first(*values):
    return next((n for value in values if (n := _count(value)) is not None), None)


def normalize_usage(usage):
    """Cached input/reasoning output are subsets, never added to the total."""
    if not isinstance(usage, dict):
        return None
    if "_coverage" in usage:
        return dict(usage)
    incoming = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    outgoing = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    incoming = incoming if isinstance(incoming, dict) else {}
    outgoing = outgoing if isinstance(outgoing, dict) else {}
    result = dict.fromkeys(FIELDS)
    result["input_tokens"] = _first(usage.get("input_tokens"), usage.get("prompt_tokens"))
    result["output_tokens"] = _first(usage.get("output_tokens"), usage.get("completion_tokens"))
    result["cached_input_tokens"] = _first(usage.get("cached_input_tokens"), incoming.get("cached_tokens"))
    result["cache_write_input_tokens"] = _first(usage.get("cache_write_input_tokens"), incoming.get("cache_write_tokens"))
    result["reasoning_output_tokens"] = _first(usage.get("reasoning_output_tokens"), outgoing.get("reasoning_tokens"))
    input_count, cached = result["input_tokens"], result["cached_input_tokens"]
    if input_count is not None and cached is not None and cached <= input_count:
        result["uncached_input_tokens"] = input_count - cached
    if input_count is not None and result["output_tokens"] is not None:
        result["total_tokens"] = input_count + result["output_tokens"]
    else:
        result["total_tokens"] = _count(usage.get("total_tokens"))
    result["raw_usage"] = usage
    return result


def _aggregate(usages):
    result, coverage = {}, {}
    for field in FIELDS:
        values, known, unknown = [], 0, 0
        for usage in usages:
            value = usage.get(field) if usage else None
            detail = (usage or {}).get("_coverage", {}).get(field)
            known += detail["known"] if detail else int(value is not None)
            unknown += detail["unknown"] if detail else int(value is None)
            if value is not None:
                values.append(value)
        result[field] = sum(values) if values else None
        coverage[field] = {"known": known, "unknown": unknown}
    result["_coverage"] = coverage
    return result


def parse_codex_events(stdout):
    """Parse codex exec --json JSONL; only terminal turn usage is counted."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    reply, thread_id, warnings, usages, failures = None, None, [], [], 0
    for number, line in enumerate((stdout or "").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            warnings.append(f"Non-JSON event at line {number}")
            continue
        if not isinstance(event, dict):
            warnings.append(f"Non-object event at line {number}")
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id", thread_id)
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if (isinstance(item, dict) and item.get("type") == "agent_message"
                    and item.get("phase") in {None, "final_answer"} and isinstance(item.get("text"), str)):
                reply = item["text"]
        elif event_type == "turn.completed":
            usages.append(normalize_usage(event.get("usage")))
        elif event_type == "turn.failed":
            failures += 1
    usage = _aggregate(usages) if any(u is not None for u in usages) else None
    if usage is not None:
        usage["raw_usage"] = [u.get("raw_usage") if u else None for u in usages]
    return {"reply": reply, "usage": usage, "thread_id": thread_id,
            "warnings": warnings, "terminal_events": len(usages), "failed_events": failures}


@contextmanager
def usage_scope(*, recorder=None, stage=None, item_id=None, document_id=None,
                attempt=None, clear_document_id=False):
    """Nested scopes inherit identifiers; ContextVar isolates concurrent tasks."""
    context = dict(_CONTEXT.get())
    if clear_document_id:
        context.pop("document_id", None)
    values = {"recorder": recorder, "stage": stage, "item_id": item_id,
              "document_id": document_id, "attempt": attempt}
    context.update({k: v for k, v in values.items() if v is not None})
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def get_usage_context():
    return dict(_CONTEXT.get())


def record_current_usage(usage=None, *, recorder=None, **metadata):
    context = get_usage_context()
    active = recorder if recorder is not None else context.pop("recorder", None)
    context.pop("recorder", None)
    context.update(metadata)
    return active.record(usage, **context) if active is not None else None


class TokenUsageRecorder:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.started_at_utc = datetime.now(timezone.utc).isoformat()

    def record(self, usage=None, *, stage=None, item_id=None, document_id=None,
               attempt=None, status="ok", **metadata):
        event = {"stage": stage, "item_id": item_id, "document_id": document_id,
                 "attempt": attempt, "status": status, **metadata,
                 "recorded_at_monotonic": perf_counter(),
                 "usage": normalize_usage(usage)}
        self.events.append(event)
        return event

    @staticmethod
    def _summary(events):
        summary = _aggregate([e["usage"] for e in events])
        if not events:
            summary.update(dict.fromkeys(FIELDS, 0))
        summary["field_coverage"] = summary.pop("_coverage")
        known = sum(any(e["usage"].get(f) is not None for f in FIELDS)
                    if e["usage"] else False for e in events)
        summary.update({"requests": len(events),
                        "requests_with_usage": known,
                        "requests_without_usage": len(events) - known,
                        "failed_requests": sum(e["status"] in {"failed", "timeout"} for e in events),
                        "timeout_requests": sum(e["status"] == "timeout" for e in events),
                        "cancelled_requests": sum(e["status"] == "cancelled" for e in events),
                        "retry_requests": sum((e.get("attempt") or 1) > 1 for e in events)})
        return summary

    def report(self, *, item_prefix=None, document_id=None, since=None, **metadata):
        events = [e for e in self.events
                  if (item_prefix is None or (e.get("item_id") or "").startswith(item_prefix))
                  and (document_id is None or e.get("document_id") == document_id)
                  and (since is None or e.get("recorded_at_monotonic", 0) >= since)]
        report = {"started_at_utc": self.started_at_utc, **metadata,
                  "totals": self._summary(events), "events": events}
        for key in ("stage", "item_id", "document_id"):
            groups = defaultdict(list)
            for event in events:
                value = event.get(key)
                if key == "document_id" and value is None:
                    value = "_shared_or_unknown"
                groups[value].append(event)
            report["by_" + key] = [{key: value, **self._summary(rows)} for value, rows in groups.items()]
        report["note"] = ("仅统计本次模型实际返回的用量，失败、取消及重试也计入。缺失用量保持未知，不估算为零。"
                          "总量=input+output；cached input与reasoning output为子集，不重复相加。"
                          "字段值为已知部分之和，field_coverage标明未知部分；"
                          "本地结果缓存命中不产生新模型请求，服务端缓存输入仍随请求计数。"
                          "本地 PaddleOCR-VL 推理未纳入此模型服务 token 统计。")
        return report


def format_tokens(value):
    """展示用十进制 K/M；JSON 计数仍保留精确整数。"""
    if _count(value) is None:
        return "未知"
    # 四舍五入后接近 1000K 时直接显示 1M。
    if value >= 999_995:
        number, suffix = value / 1_000_000, "M"
    elif value >= 1_000:
        number, suffix = value / 1_000, "K"
    else:
        return str(value)
    return f"{number:.2f}".rstrip("0").rstrip(".") + suffix


def render_usage_report(report):
    """可独立刷新文本展示，不改写原始 JSON 用量。"""

    def describe(row):
        detail = (f"已知总量 {format_tokens(row['total_tokens'])} tokens，"
                  f"输入 {format_tokens(row['input_tokens'])} / 输出 {format_tokens(row['output_tokens'])}，"
                  f"{row['requests']} 次请求")
        unknown = row["field_coverage"]["total_tokens"]["unknown"]
        if row["requests_without_usage"] or unknown:
            detail += f"，{row['requests_without_usage']} 次无用量，总量字段未知 {unknown} 个事件"
        for key, label in (("failed_requests", "失败"), ("cancelled_requests", "取消"), ("retry_requests", "重试")):
            if row[key]:
                detail += f"，{label} {row[key]}"
        return detail

    totals = report["totals"]
    lines = [describe(totals),
             f"输入缓存 {format_tokens(totals['cached_input_tokens'])}，非缓存输入 {format_tokens(totals['uncached_input_tokens'])}；"
             f"推理输出 {format_tokens(totals['reasoning_output_tokens'])}（包含在输出中）",
             "单位：K = 千 tokens，M = 百万 tokens；展示值四舍五入，精确整数见 _token_usage.json。",
             report["note"], "", "按阶段"]
    lines.extend(f"{row['stage'] or '未标记'}: {describe(row)}" for row in report["by_stage"])
    return "\n".join(lines) + "\n"


def write_usage_report(output_dir, report):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_token_usage.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    (output_dir / "_token_usage.txt").write_text(render_usage_report(report), encoding="utf-8")
    return path
