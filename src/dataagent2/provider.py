"""Audited Codex CLI calls, attempt-level usage and content-addressed resume."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .contracts import SCHEMA, check_shape

SERVICE_STOP = threading.Event()
ADMISSION_LOCK = threading.Lock()
NEXT_REQUEST_AT = 0.0


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def redact(value):
    return re.sub(r"Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+", "[redacted]", value)


def event_usage(stdout):
    """Sum terminal turns only. Never count item-level running totals twice."""
    turns = []
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "turn.completed":
            turns.append(e.get("usage") or {})
    keys = ["input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"]
    result = {}
    for k in keys:
        values = [u.get(k) for u in turns]
        result[k] = (sum(values) if values and all(type(x) is int and x >= 0 for x in values)
                     else None)
    result["total_tokens"] = (result["input_tokens"] + result["output_tokens"]
                              if result["input_tokens"] is not None and result["output_tokens"] is not None else None)
    return result


def event_error(stdout):
    """Prefer the service failure over incidental CLI startup warnings."""
    messages = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            messages.append(event.get("message", ""))
        elif event.get("type") == "turn.failed":
            messages.append((event.get("error") or {}).get("message", ""))
    return "; ".join(dict.fromkeys(m for m in messages if m))


class Provider:
    def __init__(self, output, *, model="gpt-6.1-sol", effort="xhigh", timeout=600, request_interval=65):
        self.output = Path(output)
        self.model, self.effort, self.timeout = model, effort, timeout
        self.request_interval = max(0, request_interval)
        self.lock = threading.RLock()
        self.calls = []
        log = self.output / "calls.jsonl"
        if log.is_file():
            self.calls = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    def admit(self):
        """Pace actual attempts across documents; cached responses never wait."""
        global NEXT_REQUEST_AT
        if SERVICE_STOP.is_set():
            raise RuntimeError("Model service circuit open; resume this command later")
        with ADMISSION_LOCK:
            now = time.monotonic()
            start = max(now, NEXT_REQUEST_AT)
            NEXT_REQUEST_AT = start + self.request_interval
        delay = start - now
        if delay > 0:
            print(f"model admission: waiting {delay:.1f}s before next request", flush=True)
            time.sleep(delay)
        if SERVICE_STOP.is_set():
            raise RuntimeError("Model service circuit open; request was not sent")

    def record(self, call):
        with self.lock:
            self.calls.append(call)
            with (self.output / "calls.jsonl").open("a", encoding="utf-8") as f:
                f.write(compact(call) + "\n")
            write_json(self.output / "token_usage.json", self.usage())

    def usage(self):
        with self.lock:
            unique = {c["attempt_id"]: c for c in self.calls}
            result = {k: sum((c.get("usage") or {}).get(k) or 0 for c in unique.values())
                      for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens", "total_tokens")}
            result.update(requests=len(unique), unknown_requests=sum(
                (c.get("usage") or {}).get("total_tokens") is None for c in unique.values()),
                failed_requests=sum(c["status"] != "ok" for c in unique.values()),
                model=self.model, effort=self.effort)
            result["by_stage"] = {}
            for stage in sorted({c["stage"] for c in unique.values()}):
                rows = [c for c in unique.values() if c["stage"] == stage]
                result["by_stage"][stage] = {k: sum((c.get("usage") or {}).get(k) or 0 for c in rows)
                    for k in ("input_tokens", "output_tokens", "total_tokens")}
                result["by_stage"][stage].update(requests=len(rows), unknown_requests=sum(
                    (c.get("usage") or {}).get("total_tokens") is None for c in rows))
            result["note"] = "Actual terminal usage; input includes cached input, output includes reasoning. Unknown attempts are not zero. Resume retains prior attempts; cache hits add no call."
            return result

    def generate(self, stage, instructions, payload):
        fingerprint = {"stage": stage, "instructions": instructions, "payload": payload,
                       "schema": SCHEMA, "model": self.model, "effort": self.effort}
        key = digest(fingerprint)
        folder = self.output / "requests" / key
        saved = folder / "validated.json"
        if saved.is_file():
            response = json.loads(saved.read_text())
            check_shape(response)
            return response, {"key": key, "cache_hit": True}
        if SERVICE_STOP.is_set():
            raise RuntimeError("Model service circuit open after persistent rate/auth limit; resume this command later")
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "input.json", fingerprint)
        write_json(folder / "schema.json", SCHEMA)
        prompt = instructions + "\nINPUT_JSON:\n" + compact(payload)
        # One retry, not an unbounded whole-document correction loop.
        errors = []
        for attempt in range(2):
            self.admit()
            attempt_id = f"{key}-{time.time_ns()}"
            run = folder / attempt_id
            run.mkdir()
            answer = run / "answer.json"
            command = ["codex", "--ask-for-approval", "never", "exec", "--model", self.model,
                "-c", f'model_reasoning_effort="{self.effort}"', "--sandbox", "read-only",
                "--ephemeral", "--json", "--disable", "shell_tool", "--disable", "multi_agent",
                "--disable", "skill_search", "-c", 'web_search="disabled"', "--skip-git-repo-check",
                "--cd", str(run.resolve()), "--output-schema", str((folder / "schema.json").resolve()),
                "--output-last-message", str(answer.resolve()), "--ignore-user-config", "-"]
            started = time.monotonic()
            stdout, stderr, error = "", "", None
            try:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, start_new_session=True, env=os.environ.copy())
                try:
                    stdout, stderr = proc.communicate(prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate()
                    raise RuntimeError("Model timeout")
                (run / "events.jsonl").write_text(redact(stdout), encoding="utf-8")
                (run / "stderr.log").write_text(redact(stderr), encoding="utf-8")
                usage = event_usage(stdout)
                if proc.returncode or usage["total_tokens"] is None:
                    raise RuntimeError(redact(event_error(stdout) or stderr[-1200:] or stdout[-1200:]))
                response = json.loads(answer.read_text())
                check_shape(response)
            except (OSError, ValueError, RuntimeError) as exc:
                error = redact(str(exc))[-1800:]
            finally:
                (run / "events.jsonl").write_text(redact(stdout), encoding="utf-8")
                (run / "stderr.log").write_text(redact(stderr), encoding="utf-8")
                call = {"attempt_id": attempt_id, "stage": stage, "key": key,
                        "attempt": attempt + 1, "status": "failed" if error else "ok",
                        "usage": event_usage(stdout), "seconds": round(time.monotonic() - started, 3),
                        "input_characters": len(prompt), "error": error, "audit_dir": str(run)}
                self.record(call)
            if error is None:
                write_json(saved, response)
                return response, {"key": key, "cache_hit": False}
            errors.append(error)
            if any(x in error.lower() for x in ("quota", "usage limit", "unauthorized", "not authenticated")):
                SERVICE_STOP.set()
                break
            if attempt == 0 and any(x in error.lower() for x in ("429", "too many requests", "rate limit")):
                print(f"{stage}: service rate limited; retrying once after 45 seconds", flush=True)
                time.sleep(45)
            elif attempt == 1 and any(x in error.lower() for x in ("429", "too many requests", "rate limit")):
                SERVICE_STOP.set()
        raise RuntimeError("; ".join(errors))
