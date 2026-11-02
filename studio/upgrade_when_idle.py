"""Upgrade a legacy Studio server after its in-memory queue has drained."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=4182)
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    root = args.root.resolve()
    state = root / ".studio"
    state.mkdir(exist_ok=True)
    with (state / "queue-upgrade.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        previous = None
        while True:
            try:
                try:
                    with urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=3) as response:
                        upgraded = json.load(response).get("persistent_queue") is True
                except OSError:
                    upgraded = False
                jobs = json.loads((state / "jobs.json").read_text()) if (state / "jobs.json").exists() else {}
                active = [key for key, job in jobs.items() if job.get("status") in {"queued", "processing"}]
                status = {"state": "complete" if upgraded else "waiting_for_legacy_queue",
                          "pid": os.getpid(), "active_documents": active,
                          "checked_at": datetime.now(timezone.utc).isoformat()}
                temporary = state / "queue-upgrade.json.tmp"
                temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2))
                temporary.replace(state / "queue-upgrade.json")
                if upgraded:
                    print("Persistent queue is active.", flush=True)
                    return
                if active != previous:
                    print("Waiting for legacy tasks: " + ", ".join(active), flush=True)
                    previous = active
                if not active:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup = state / "backups" / ("before-gpu-queue-" + stamp)
                    backup.mkdir(parents=True, exist_ok=True)
                    if (state / "dataset.sqlite").exists():
                        source = sqlite3.connect(state / "dataset.sqlite")
                        target = sqlite3.connect(backup / "dataset.sqlite")
                        try:
                            source.backup(target)
                        finally:
                            target.close()
                            source.close()
                    if (state / "jobs.json").exists():
                        shutil.copyfile(state / "jobs.json", backup / "jobs.json")
                    result = subprocess.run(["bash", str(root / "studio/studio.sh"), "restart"],
                        cwd=root, env={**os.environ, "SDA_STUDIO_PORT": str(args.port)}, timeout=40)
                    if result.returncode:
                        print("Restart deferred; will recheck the queue.", flush=True)
            except Exception as exc:
                print(f"Upgrade waiting: {exc}", flush=True)
            threading.Event().wait(args.interval)


if __name__ == "__main__":
    main()
