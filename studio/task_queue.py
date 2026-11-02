"""Durable FIFO queue; one detached worker owns the workspace GPU slot."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from studio.sda_data import now


def process_token(pid):
    """Linux start time distinguishes a live task from a reused process ID."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, TypeError, ValueError, IndexError):
        return None


def process_alive(pid, token=None):
    current = process_token(pid)
    return current is not None and (token is None or str(token) == current)


class TaskQueue:
    def __init__(self, workspace):
        self.workspace = workspace
        with workspace.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    payload TEXT NOT NULL,
                    UNIQUE(document_id, run_id));
                CREATE INDEX IF NOT EXISTS task_queue_state ON task_queue(state, id);
            """)

    def submit(self, identity, *, config):
        workspace = self.workspace
        with workspace.lock, workspace.connection() as db:
            job = workspace.jobs()[identity]
            if job.get("status") != "queued" or not job.get("run_id"):
                raise ValueError("只有已提交且等待执行的任务可以入队")
            payload = {"job": job, "config": config}
            db.execute("INSERT OR IGNORE INTO task_queue(document_id,run_id,state,queued_at,payload) VALUES(?,?,'queued',?,?)",
                       (identity, job["run_id"], job.get("queued_at") or now(), json.dumps(payload, ensure_ascii=False)))
            return db.execute("SELECT id FROM task_queue WHERE document_id=? AND run_id=?",
                              (identity, job["run_id"])).fetchone()[0]

    def recover(self, *, config=None):
        """Import old waiting jobs and retire interrupted runs without repeating GPU work."""
        workspace = self.workspace
        with workspace.lock:
            jobs = workspace.jobs()
            for identity, job in sorted(jobs.items(), key=lambda item: item[1].get("queued_at") or item[1].get("started_at") or item[1].get("created_at") or ""):
                if job.get("status") == "queued" and job.get("run_id"):
                    self.submit(identity, config=job.get("execution_config") or config or {})
            with workspace.connection() as db:
                rows = db.execute("SELECT id,document_id,run_id,state FROM task_queue WHERE state IN ('queued','running')").fetchall()
                for task_id, identity, run_id, state in rows:
                    job = jobs.get(identity, {})
                    if job.get("run_id") != run_id or job.get("status") not in {"queued", "processing"}:
                        db.execute("UPDATE task_queue SET state=?,finished_at=? WHERE id=?",
                                   ("completed" if job.get("status") == "completed" and job.get("run_id") == run_id else "failed", now(), task_id))
                    elif state == "running" and not self.job_alive(job):
                        workspace.update_job(identity, status="failed", stage="failed", worker_pid=None,
                            runner_pid=None, finished_at=now(),
                            error="解析执行进程曾中断；原始 PDF、日志和已有产物保留，请确认后手动重试")
                        db.execute("UPDATE task_queue SET state='failed',finished_at=? WHERE id=?", (now(), task_id))
                # Legacy in-memory executors have no queue row. Wait for their
                # live processes; never start another GPU task beside them.
                for identity, job in jobs.items():
                    if job.get("status") == "processing" and not self.job_alive(job):
                        workspace.update_job(identity, status="failed", stage="failed", worker_pid=None,
                            runner_pid=None, finished_at=now(), error="此前解析进程已中断；已有产物保留，可手动重试")

    @staticmethod
    def job_alive(job):
        return any(process_alive(job.get(key), job.get(key + "_token")) for key in ("runner_pid", "worker_pid"))

    def claim(self):
        workspace = self.workspace
        with workspace.lock, workspace.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM task_queue WHERE state='running' LIMIT 1").fetchone():
                return None
            if any(job.get("status") == "processing" for job in workspace.jobs().values()):
                return None
            row = db.execute("SELECT id,document_id,run_id,payload FROM task_queue WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            task_id, identity, run_id, payload = row
            job = workspace.jobs().get(identity, {})
            if job.get("run_id") != run_id or job.get("status") != "queued":
                db.execute("UPDATE task_queue SET state='failed',finished_at=? WHERE id=?", (now(), task_id))
                return None
            started = now()
            db.execute("UPDATE task_queue SET state='running',started_at=? WHERE id=?", (started, task_id))
            workspace.update_job(identity, status="processing", started_at=started,
                runner_pid=os.getpid(), runner_pid_token=process_token(os.getpid()),
                stage="starting", progress=0)
            return {"id": task_id, "document_id": identity, "run_id": run_id, **json.loads(payload)}

    def finish(self, task):
        workspace = self.workspace
        with workspace.lock, workspace.connection() as db:
            identity = task["document_id"]
            job = workspace.jobs()[identity]
            if job.get("status") not in {"completed", "failed"}:
                workspace.update_job(identity, status="failed", stage="failed", finished_at=now(),
                                     error="执行器未生成最终状态；已有产物保留，可查看日志后重试")
                job = workspace.jobs()[identity]
            db.execute("UPDATE task_queue SET state=?,finished_at=? WHERE id=?",
                       (job["status"], now(), task["id"]))
            workspace.update_job(identity, runner_pid=None, runner_pid_token=None)

    def snapshot(self):
        workspace = self.workspace
        with workspace.lock, workspace.connection() as db:
            jobs = workspace.jobs()
            rows = db.execute("SELECT id,document_id,run_id,queued_at FROM task_queue WHERE state='queued' ORDER BY id").fetchall()
            running = sorted((identity for identity, job in jobs.items() if job.get("status") == "processing"),
                             key=lambda identity: (jobs[identity].get("started_at") or "", identity))
            waiting = [row for row in rows if jobs.get(row[1], {}).get("run_id") == row[2]
                       and jobs.get(row[1], {}).get("status") == "queued"]
            lock_path = workspace.state / "queue-worker.lock"
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    worker_online = False
                except BlockingIOError:
                    worker_online = True
            positions = {row[1]: {"queue_id": row[0], "queue_position": index + 1,
                                  "queue_ahead": len(running) + index, "queued_at": row[3]}
                         for index, row in enumerate(waiting)}
            tasks = []
            for identity in [*running, *(row[1] for row in waiting)]:
                job = jobs[identity]
                tasks.append({"document_id": identity, "run_id": job.get("run_id"),
                              "filename": job.get("filename") or f"{identity}.pdf",
                              "submitted_by": job.get("submitted_by", ""),
                              "status": job["status"], "stage": job.get("stage"),
                              "progress": job.get("progress", 0), "operation": job.get("operation", "pipeline"),
                              "queued_at": job.get("queued_at"), "started_at": job.get("started_at"),
                              **positions.get(identity, {}), "position": len(tasks) + 1})
            return {"concurrency": 1, "running": len(running), "waiting": len(waiting),
                    "worker_online": worker_online,
                    "tasks": tasks, "positions": positions}


def ensure_worker(workspace, *, python=None):
    """Only launchers spawn; the worker itself holds the singleton lock."""
    with (workspace.state / "queue-launch.lock").open("a+") as launch:
        fcntl.flock(launch, fcntl.LOCK_EX)
        with (workspace.state / "queue-worker.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        # Rate-limit starts while a child is still importing modules.
        import time
        marker = workspace.state / "queue-launch.time"
        if marker.exists() and time.time() - marker.stat().st_mtime < 10:
            return
        marker.touch()
        with (workspace.state / "queue-worker.log").open("a") as log:
            subprocess.Popen([python or sys.executable, str(Path(__file__).with_name("queue_worker.py")),
                              "--root", str(workspace.root)], cwd=workspace.root, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
