"""记录并发请求和流水线阶段耗时；区间并集用于避免并发时间重复累加。"""

import asyncio
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from units.document_fields import PATENT_ID
from units.token_usage import TokenUsageRecorder, usage_scope, write_usage_report


async def await_with_progress(awaitable, describe, *, interval=15):
    """等待任务时定期报告状态，不因报告间隔而取消模型请求。"""
    task = asyncio.ensure_future(awaitable)
    started = perf_counter()
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=interval)
            if not done:
                print(describe(perf_counter() - started), flush=True)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def union_seconds(intervals):
    merged, total = None, 0.0
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged is None:
            merged = (start, end)
        elif start <= merged[1]:
            merged = (merged[0], max(merged[1], end))
        else:
            total += merged[1] - merged[0]
            merged = (start, end)
    return total + (merged[1] - merged[0] if merged else 0.0)


def peak_concurrency(events):
    points = [(e["start_seconds"], 1) for e in events if e["end_seconds"] > e["start_seconds"]]
    points += [(e["end_seconds"], -1) for e in events if e["end_seconds"] > e["start_seconds"]]
    active, peak = 0, 0
    for _, change in sorted(points):
        active += change
        peak = max(peak, active)
    return peak


class TimingRecorder:
    def __init__(self, clock=perf_counter):
        self.clock = clock
        self.origin = clock()
        self.started_at_utc = datetime.now(timezone.utc).isoformat()
        self.events = []
        self.usage_recorder = TokenUsageRecorder()

    def add(self, stage, start, end, *, kind="operation", item_id=None, status="ok", **metadata):
        event = {"stage": stage, "kind": kind, "item_id": item_id, "status": status,
                 "start_seconds": start - self.origin, "end_seconds": end - self.origin,
                 "seconds": max(0.0, end - start), **metadata}
        self.events.append(event)
        return event

    @contextmanager
    def measure(self, stage, *, kind="operation", item_id=None, **metadata):
        start, status = self.clock(), "ok"
        detail = dict(metadata)
        document_ids = {match.group(0).upper() for match in PATENT_ID.finditer(str(item_id or ""))}
        # 多专利共用一个分类请求时，服务端只返回整批用量，不按比例猜分配。
        document_id = next(iter(document_ids)) if len(document_ids) == 1 else None
        with usage_scope(recorder=self.usage_recorder, stage=stage, item_id=item_id,
                         document_id=document_id, clear_document_id=len(document_ids) > 1,
                         attempt=metadata.get("attempt")):
            try:
                yield detail
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, (KeyboardInterrupt, GeneratorExit)) or type(exc).__name__ == "CancelledError" else "failed"
                detail["error_type"] = type(exc).__name__
                raise
            finally:
                self.add(stage, start, self.clock(), kind=kind, item_id=item_id, status=status, **detail)

    def report(self, *, item_prefix=None, wall_seconds=None, since=None):
        events = [e for e in self.events
                  if (item_prefix is None or (e["item_id"] or "").startswith(item_prefix))
                  and (since is None or e["start_seconds"] >= since - self.origin)]
        grouped, phases = defaultdict(list), []
        for event in events:
            if event["kind"] == "phase":
                phases.append({"stage": event["stage"], "seconds": event["seconds"], "status": event["status"]})
            elif event["kind"] != "task":
                grouped[event["stage"]].append(event)
        stages = []
        for stage, rows in grouped.items():
            requests = [e for e in rows if e["kind"] == "request"]
            active = [e for e in rows if e["kind"] in {"request", "operation"}]
            stages.append({
                "stage": stage,
                "active_wall_seconds": union_seconds((e["start_seconds"], e["end_seconds"]) for e in active),
                "request_seconds_sum": sum(e["seconds"] for e in requests),
                "operation_seconds_sum": sum(e["seconds"] for e in rows if e["kind"] == "operation"),
                "retry_sleep_seconds_sum": sum(e["seconds"] for e in rows if e["kind"] == "retry_sleep"),
                "queue_wait_seconds_sum": sum(e["seconds"] for e in rows if e["kind"] == "queue_wait"),
                "requests": len(requests), "failed_requests": sum(e["status"] == "failed" for e in requests),
                "max_request_seconds": max((e["seconds"] for e in requests), default=0.0),
                "peak_concurrent_requests": peak_concurrency(requests),
            })
        stages.sort(key=lambda s: s["active_wall_seconds"], reverse=True)
        phases.sort(key=lambda s: s["seconds"], reverse=True)
        return {"started_at_utc": self.started_at_utc,
                "wall_seconds": self.clock() - self.origin if wall_seconds is None else wall_seconds,
                "phase_ranking": phases, "stage_ranking": stages,
                "slowest_requests": sorted([e for e in events if e["kind"] == "request"],
                                            key=lambda e: e["seconds"], reverse=True)[:20],
                "events": sorted(events, key=lambda e: e["start_seconds"]),
                "token_usage": self.usage_recorder.report(item_prefix=item_prefix, since=since),
                "note": "phase为实际阶段耗时；active_wall为请求/操作区间并集。并发阶段会重叠，"
                        "request_seconds_sum、排队和退避累计值不能当成总耗时相加；仅记录本次运行。"}


def write_timing_report(output_dir, report):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "_timings.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    lines = [f"本次运行耗时：{report['wall_seconds']:.3f} 秒", report["note"]]
    if report.get("run_mode"):
        lines.append(f"运行范围：{report['run_mode']}")
    if report.get("cache_counts"):
        lines.append("本次缓存：" + json.dumps(report["cache_counts"], ensure_ascii=False))
    lines.extend(["", "实际阶段耗时（从高到低）"])
    lines.extend(f"{p['stage']}: {p['seconds']:.3f} 秒" for p in report["phase_ranking"])
    lines.extend(["", "请求/操作覆盖时间（从高到低）"])
    for row in report["stage_ranking"]:
        lines.append(f"{row['stage']}: 覆盖 {row['active_wall_seconds']:.3f} 秒，"
                     f"请求累计 {row['request_seconds_sum']:.3f} 秒，{row['requests']} 次请求，"
                     f"最长请求 {row['max_request_seconds']:.3f} 秒，"
                     f"峰值并发 {row['peak_concurrent_requests']}，"
                     f"排队累计 {row['queue_wait_seconds_sum']:.3f} 秒，"
                     f"重试退避累计 {row['retry_sleep_seconds_sum']:.3f} 秒")
    lines.extend(["", "最慢请求（包含失败和重试）"])
    for event in report["slowest_requests"]:
        lines.append(f"{event['stage']} | {event['item_id']} | {event['seconds']:.3f} 秒 | "
                     f"{event['status']} | 尝试 {event.get('attempt', 1)}")
    (output_dir / "_timings.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if "token_usage" in report:
        write_usage_report(output_dir, report["token_usage"])
    return path
