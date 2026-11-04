"""Detached single-slot Studio queue consumer. SIGTERM drains the current task."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.sda_data import Workspace, now
from studio.experience import ExperienceWorkspace
from studio.task_queue import TaskQueue
from studio.pipeline_executor import PipelineExecutor


class QueueWorker:
    def __init__(self, workspace, executor_factory=None):
        self.workspace = workspace
        self.queue = TaskQueue(workspace)
        self.executor_factory = executor_factory or PipelineExecutor

    def run_one(self):
        self.queue.recover()
        task = self.queue.claim()
        if task is None:
            return False
        identity = task["document_id"]
        run = self.workspace.state / "patents" / identity / "runs" / task["run_id"]
        try:
            run.mkdir(parents=True, exist_ok=True)
            executor = self.executor_factory(self.workspace, **task["config"])
            if task["job"].get("operation") == "compare":
                from studio.comparison import run_comparison
                run_comparison(executor, identity, run)
            else:
                executor.run_pipeline(identity, run, task["job"].get("operation", "pipeline"))
        except Exception as exc:
            self.workspace.update_job(identity, status="failed", stage="failed", error=str(exc), finished_at=now())
            with (run / "pipeline.log").open("a") as log:
                traceback.print_exc(file=log)
        finally:
            self.queue.finish(task)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    workspace = ExperienceWorkspace(args.root)
    with (workspace.state / "queue-worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        pid = workspace.state / "queue-worker.pid"
        pid.write_text(str(os.getpid()))
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        worker = QueueWorker(workspace)
        print(json.dumps({"event": "queue-worker-started", "pid": os.getpid(), "concurrency": 1}), flush=True)
        try:
            while not stopping.is_set():
                try:
                    if not worker.run_one():
                        stopping.wait(1)
                except Exception:
                    traceback.print_exc()
                    stopping.wait(5)
        finally:
            pid.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
