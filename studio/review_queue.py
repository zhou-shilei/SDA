"""Persistent, transactional document assignment and shared annotation drafts."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class QueueConflict(FileExistsError):
    pass


def reviewer_name(value):
    name = " ".join(str(value or "").split())
    if len(name) > 40 or sum(c.isalpha() for c in name) < 2:
        raise ValueError("请输入至少两个文字的真实姓名")
    return name


class ReviewQueue:
    def __init__(self, project_root: Path):
        self.path = project_root / ".studio/dataset.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS review_tasks (
                    document_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready', owner TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL DEFAULT '', last_reviewer TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 0, draft_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_task_events (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL, action TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def sync(self, db, patents):
        """Call with the registry lock held, so delete/reimport cannot race claims."""
        for patent in patents:
            identity = patent["document_id"]
            generation = patent.get("created_at") or "builtin"
            current = db.execute("SELECT generation FROM review_tasks WHERE document_id=?", (identity,)).fetchone()
            if current and current[0] != generation:
                db.execute("DELETE FROM review_tasks WHERE document_id=?", (identity,))
            db.execute("INSERT OR IGNORE INTO review_tasks (document_id,generation,created_at,updated_at) VALUES (?,?,?,?)",
                       (identity, generation, patent.get("created_at") or "1970-01-01", self.now()))
        live = {p["document_id"] for p in patents}
        for row in db.execute("SELECT document_id FROM review_tasks").fetchall():
            if row[0] not in live:
                db.execute("DELETE FROM review_tasks WHERE document_id=?", (row[0],))

    @staticmethod
    def public(row, patent, *, draft=False):
        result = {key: row[key] for key in ("document_id", "status", "owner", "last_reviewer", "version", "created_at", "updated_at")}
        result.update(filename=patent.get("filename", ""), pipeline_status=patent.get("status"),
                      submitted_by=patent.get("submitted_by", ""),
                      pipeline_progress=patent.get("progress", 0), error=patent.get("error"),
                      record_count=patent.get("record_count", 0))
        saved = json.loads(row["draft_json"])
        reviewed = {item.get("record_id") for group in ("corrections", "reviews")
                    for item in saved.get(group, {}).values() if isinstance(item, dict)}
        result["reviewed_count"] = len(reviewed - {None, ""})
        if draft:
            result["draft"] = saved
        return result

    def list(self, patents, document_id=None):
        with self.transaction() as db:
            self.sync(db, patents)
            by_id = {p["document_id"]: p for p in patents}
            return [self.public(row, by_id[row["document_id"]], draft=bool(document_id)) for row in db.execute(
                "SELECT * FROM review_tasks ORDER BY created_at, document_id") if not document_id or row["document_id"] == document_id]

    def act(self, patents, payload):
        operation = payload.get("operation")
        name = reviewer_name(payload.get("reviewer_name"))
        by_id = {p["document_id"]: p for p in patents}
        with self.transaction() as db:
            self.sync(db, patents)
            identity = payload.get("document_id")
            row = db.execute("SELECT * FROM review_tasks WHERE document_id=?", (identity,)).fetchone()
            if operation in {"claim", "next", "resume"}:
                owned = db.execute("SELECT * FROM review_tasks WHERE owner=? ORDER BY created_at LIMIT 1", (name,)).fetchone()
                if operation == "next":
                    row = owned or next((r for r in db.execute("SELECT * FROM review_tasks WHERE status='ready' AND owner='' ORDER BY created_at,document_id")
                                         if by_id[r["document_id"]].get("status") == "completed"), None)
                if row is None:
                    raise QueueConflict("暂无可领取任务，请等待文件处理完成")
                identity = row["document_id"]
                if by_id[identity].get("status") != "completed" and not (operation == "resume" and row["owner"] == name):
                    raise QueueConflict("此文件尚未处理完成，暂时不能标注")
                if row["owner"] and row["owner"] != name:
                    raise QueueConflict(f"任务已由 {row['owner']} 领取")
                if owned and owned["document_id"] != identity:
                    raise QueueConflict("请先完成或退回您当前领取的任务")
                if row["status"] == "completed":
                    raise QueueConflict("任务已完成")
                if operation == "resume" and row["owner"] != name:
                    raise QueueConflict("只能恢复自己已领取的任务")
                if row["status"] == "pending_review" and row["last_reviewer"] == name:
                    raise QueueConflict("请由另一位成员领取复核")
                status = "reviewing" if row["status"] in {"pending_review", "reviewing"} else "in_progress"
                token = uuid.uuid4().hex
                db.execute("UPDATE review_tasks SET owner=?,token=?,status=?,version=version+1,updated_at=? WHERE document_id=?",
                           (name, token, status, self.now(), identity))
            else:
                if row is None:
                    raise ValueError("任务不存在")
                self.require_owner(row, payload)
                if by_id[identity].get("status") != "completed" and operation != "release":
                    raise QueueConflict("文件正在处理，请完成后再保存或提交")
                if payload.get("version") != row["version"]:
                    raise QueueConflict("任务已在其他页面更新，请重新打开任务")
                if operation == "save":
                    draft = payload.get("draft")
                    self.validate_draft(draft, identity)
                    db.execute("UPDATE review_tasks SET draft_json=? WHERE document_id=?", (json.dumps(draft, ensure_ascii=False), identity))
                    status, owner, token, last = row["status"], name, row["token"], row["last_reviewer"]
                elif operation == "release":
                    status, owner, token, last = ("pending_review" if row["status"] == "reviewing" else "ready"), "", "", row["last_reviewer"]
                elif operation == "submit" and row["status"] == "in_progress":
                    status, owner, token, last = "pending_review", "", "", name
                elif operation == "approve" and row["status"] == "reviewing":
                    status, owner, token, last = "completed", "", "", row["last_reviewer"]
                elif operation == "return" and row["status"] == "reviewing":
                    status, owner, token, last = "ready", "", "", row["last_reviewer"]
                else:
                    raise ValueError("无效的任务操作")
                db.execute("UPDATE review_tasks SET status=?,owner=?,token=?,last_reviewer=?,version=version+1,updated_at=? WHERE document_id=?",
                           (status, owner, token, last, self.now(), identity))
            row = db.execute("SELECT * FROM review_tasks WHERE document_id=?", (identity,)).fetchone()
            db.execute("INSERT INTO review_task_events (document_id,reviewer,action,version,created_at) VALUES (?,?,?,?,?)",
                       (identity, name, operation, row["version"], self.now()))
            result = self.public(row, by_id[identity], draft=True)
            result["token"] = token
            return result

    @staticmethod
    def require_owner(row, payload):
        if not row["owner"] or row["owner"] != payload.get("reviewer_name") or not row["token"] or row["token"] != payload.get("token"):
            raise QueueConflict("请先从任务队列领取或继续此任务；其他页面恢复后，当前页面需要重新打开")

    @staticmethod
    def validate_draft(draft, identity):
        if not isinstance(draft, dict):
            raise ValueError("草稿格式无效")
        for key in ("corrections", "reviews"):
            if not isinstance(draft.get(key, {}), dict):
                raise ValueError("草稿字段格式无效")
            for item in draft.get(key, {}).values():
                if not isinstance(item, dict) or item.get("document_id") != identity:
                    raise ValueError("草稿包含其他文件的记录")
        if not isinstance(draft.get("manualBoxes", []), list) or any(not isinstance(b, dict) or b.get("document_id") != identity for b in draft.get("manualBoxes", [])):
            raise ValueError("标注框格式无效")

    def authorize(self, identity, payload, *, require_claim=False):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM review_tasks WHERE document_id=?", (identity,)).fetchone()
            if row and (row["owner"] or require_claim):
                self.require_owner(row, payload)
