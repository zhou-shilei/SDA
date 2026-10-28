"""Bounded automatic resume after service rate limits, with a single-run lock."""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .inputs import read
from .provider import write_json
from .summarize import summarize


def rate_limited(text):
    return any(term in text.lower() for term in ("429", "too many requests", "rate limit", "service circuit open"))


def supervise(output, documents, *, rounds=4, cooldown=300, initial_delay=300, interval=90):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / "supervisor.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("A supervisor already owns this output directory")
    try:
        return _supervise(output, documents, rounds=rounds, cooldown=cooldown,
                          initial_delay=initial_delay, interval=interval)
    finally:
        lock.close()


def _supervise(output, documents, *, rounds, cooldown, initial_delay, interval):
    command = [sys.executable, "-m", "dataagent2", "--documents", *documents,
               "--output", str(output), "--workers", "0", "--document-workers", "1",
               "--request-interval", str(interval), "--max-questions", "8"]
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    previous = read(output / "supervisor.json", {})
    history = previous.get("history", []) if previous.get("documents") == documents else []
    state = {"pid": os.getpid(), "documents": documents, "max_rounds": rounds,
             "command": command, "history": history}

    def save(phase, **fields):
        state.update(phase=phase, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
        write_json(output / "supervisor.json", state)
        print(f"supervisor: {phase} round={state.get('round', 0)}/{rounds}", flush=True)

    def wait(seconds):
        deadline = time.monotonic() + seconds
        save("waiting", next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
        while time.monotonic() < deadline:
            if (output / "supervisor.stop").exists():
                save("stopped")
                return False
            time.sleep(min(30, max(0, deadline - time.monotonic())))
        return True

    save("starting", round=len(history))
    if len(history) >= rounds:
        save("blocked", reason="Resume round budget already exhausted; increase --rounds to authorize more rounds")
        return 1
    if initial_delay and not wait(initial_delay):
        return 1
    for index in range(len(history) + 1, rounds + 1):
        if (output / "supervisor.stop").exists():
            save("stopped")
            return 1
        log = output / f"supervisor-round-{time.time_ns()}.log"
        def reader_complete(doc):
            report = read(output / doc / "report.json", {})
            coverage = report.get("coverage", {})
            return bool(report.get("reader_complete") or report.get("benchmark_complete") or
                        coverage.get("total_chunks") and coverage.get("completed_chunks") == coverage["total_chunks"])

        # An unfinished document gets its first pass before another document's
        # remaining gap questions consume the available service window.
        ordered = sorted(documents, key=reader_complete)
        command[command.index("--documents") + 1:command.index("--output")] = ordered
        prior_interval = history[-1].get("request_interval_seconds", interval) if history else interval
        effective_interval = max(interval, min(300, prior_interval * 1.5)) if history else interval
        command[command.index("--request-interval") + 1] = str(effective_interval)
        save("running", round=index, current_log=str(log), next_retry_at=None,
             scheduled_documents=ordered, request_interval_seconds=effective_interval)
        with log.open("w") as stream:
            code = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
        reports = [read(output / doc / "report.json", {}) for doc in documents]
        answer_errors = [a.get("error", "") for doc in documents
                         for a in read(output / doc / "answers.json", []) if a.get("status") == "failed"]
        complete = code == 0 and all(r.get("benchmark_complete") for r in reports) and not answer_errors
        state["history"].append({"round": index, "returncode": code, "log": str(log),
            "request_interval_seconds": effective_interval,
            "completed_chunks": {r.get("document_id", d): r.get("coverage", {}).get("completed_chunks", 0)
                                 for d, r in zip(documents, reports)},
            "known_new_tokens": sum(r.get("new_model_usage", {}).get("total_tokens", 0) for r in reports)})
        summarize(output)
        if complete:
            save("finished")
            return 0
        if not rate_limited(log.read_text() + "\n" + "\n".join(answer_errors)):
            save("failed", reason="Non-rate-limit failure; inspect round log instead of repeating model input")
            return 1
        if index == rounds:
            save("blocked", reason="Persistent model service rate limits exhausted the bounded resume rounds")
            return 1
        if not wait(cooldown * index):
            return 1
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents", nargs="+", default=["WO2026086808A1", "WO2024020591A9"])
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--cooldown", type=float, default=300)
    parser.add_argument("--initial-delay", type=float, default=300)
    parser.add_argument("--request-interval", type=float, default=90)
    args = parser.parse_args()
    if args.rounds < 1 or min(args.cooldown, args.initial_delay, args.request_interval) < 0:
        parser.error("Rounds must be positive; timing settings nonnegative")
    raise SystemExit(supervise(args.output, args.documents, rounds=args.rounds, cooldown=args.cooldown,
                              initial_delay=args.initial_delay, interval=args.request_interval))
