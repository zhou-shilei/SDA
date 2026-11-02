import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from studio.review_queue import QueueConflict, ReviewQueue
from studio.server import StudioServer
from studio.sda_data import Workspace


class ReviewQueueTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = ReviewQueue(self.root)
        self.patents = [dict(document_id=f"DOC-{i}", filename=f"{i}.pdf", status="completed",
                             record_count=2, created_at=f"2026-11-0{i + 1}T00:00:00+00:00") for i in range(3)]

    def act(self, operation, task=None, name="张三", **kwargs):
        payload = dict(operation=operation, reviewer_name=name, **kwargs)
        if task:
            payload.update(document_id=task["document_id"], version=task["version"], token=task.get("token"))
        return self.queue.act(self.patents, payload)

    def draft(self, identity="DOC-0"):
        return {"corrections": {f"{identity}::R1::vh": {"document_id": identity, "record_id": "R1", "value": "EVQL", "reviewer_name": "张三"}},
                "reviews": {f"{identity}::R1::vl": {"document_id": identity, "record_id": "R1", "status": "accepted"}},
                "manualBoxes": [{"document_id": identity, "page": 2, "bbox": [1, 2, 3, 4]}],
                "cursor": {"record_id": "R1", "field": "vh", "page": 2}}

    def test_fifo_skips_processing_and_one_active_per_reviewer(self):
        self.patents[0]["status"] = "processing"
        first = self.act("next")
        self.assertEqual(first["document_id"], "DOC-1")
        self.assertEqual(self.act("next")["document_id"], "DOC-1")
        with self.assertRaises(QueueConflict):
            self.act("claim", document_id="DOC-2")
        self.assertEqual(self.act("next", name="李四")["document_id"], "DOC-2")

    def test_concurrent_claim_has_exactly_one_winner(self):
        barrier = threading.Barrier(8)

        def claim(i):
            queue = ReviewQueue(self.root)
            barrier.wait()
            try:
                return queue.act(self.patents, {"operation": "claim", "document_id": "DOC-0", "reviewer_name": f"成员{i}"})["owner"]
            except QueueConflict:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_release_restart_resume_and_stale_tab(self):
        task = self.act("next")
        draft = self.draft()
        saved = self.act("save", task, draft=draft)
        self.assertEqual(saved["reviewed_count"], 1)
        self.queue = ReviewQueue(self.root)
        resumed = self.act("resume", saved)
        self.assertEqual(resumed["draft"], draft)
        with self.assertRaises(QueueConflict):
            self.act("save", saved, draft=self.draft())
        released = self.act("release", resumed)
        self.assertEqual(released["status"], "ready")
        next_owner = self.act("claim", released, name="李四")
        self.assertEqual(next_owner["draft"], draft)

    def test_version_conflict_preserves_latest_draft(self):
        task = self.act("next")
        self.act("save", task, draft=self.draft())
        with self.assertRaises(QueueConflict):
            self.act("save", task, draft={})
        self.assertEqual(self.queue.list(self.patents, "DOC-0")[0]["draft"], self.draft())

    def test_review_requires_another_person_and_preserves_draft(self):
        saved = self.act("save", self.act("next"), draft=self.draft())
        pending = self.act("submit", saved)
        with self.assertRaises(QueueConflict):
            self.act("claim", pending)
        review = self.act("claim", pending, name="李四")
        self.assertEqual(review["status"], "reviewing")
        self.assertEqual(review["draft"], self.draft())
        released = self.act("release", review, name="李四")
        self.assertEqual(released["status"], "pending_review")
        review = self.act("claim", released, name="李四")
        done = self.act("approve", review, name="李四")
        self.assertEqual(done["status"], "completed")
        with self.assertRaises(QueueConflict):
            self.act("claim", done)

    def test_request_changes_and_unauthorized_writes(self):
        task = self.act("next")
        with self.assertRaises(QueueConflict):
            self.act("release", task, name="李四")
        pending = self.act("submit", task)
        reviewer = self.act("claim", pending, name="李四")
        returned = self.act("return", reviewer, name="李四")
        self.assertEqual(returned["status"], "ready")
        self.assertEqual(self.act("claim", returned)["owner"], "张三")

    def test_read_api_never_exposes_assignment_token(self):
        self.act("next")
        for task in self.queue.list(self.patents):
            self.assertNotIn("token", task)
            self.assertNotIn("draft", task)
        self.assertNotIn("token", self.queue.list(self.patents, "DOC-0")[0])

    def test_failed_processing_does_not_trap_assignment(self):
        task = self.act("next")
        task = self.act("save", task, draft=self.draft())
        self.patents[0]["status"] = "failed"
        resumed = self.act("resume", task)
        released = self.act("release", resumed)
        self.assertEqual(released["status"], "ready")
        self.assertEqual(released["draft"], self.draft())
        self.assertEqual(self.act("next")["document_id"], "DOC-1")

    def test_invalid_cross_document_draft_is_rejected_atomically(self):
        task = self.act("next")
        draft = self.draft("DOC-1")
        with self.assertRaises(ValueError):
            self.act("save", task, draft=draft)
        self.assertEqual(self.queue.list(self.patents, "DOC-0")[0]["version"], task["version"])

    def test_deleted_and_reimported_document_does_not_inherit_claim_or_draft(self):
        self.act("save", self.act("next"), draft=self.draft())
        self.patents[0]["created_at"] = "2026-11-20"
        reset = self.queue.list(self.patents, "DOC-0")[0]
        self.assertEqual((reset["owner"], reset["draft"]), ("", {}))
        self.patents = self.patents[1:]
        self.assertEqual(len(self.queue.list(self.patents)), 2)

    def test_http_status_validation_and_server_restart(self):
        workspace = Workspace(self.root)
        server = StudioServer(("127.0.0.1", 0), workspace)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/api/reviews/queue"

        def request(payload=None):
            req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                return json.load(response)

        with patch.object(workspace, "patents", return_value=self.patents):
            workspace.update_job("PDF-NEW", status="queued", run_id="new", submitted_by="李四")
            server.queue.submit("PDF-NEW", config={})
            snapshot = request()["processing_queue"]
            self.assertEqual(snapshot["tasks"][0]["document_id"], "PDF-NEW")
            self.assertEqual(snapshot["tasks"][0]["submitted_by"], "李四")
            with urlopen(url.replace("/api/reviews/queue", "/api/queue"), timeout=5) as response:
                self.assertEqual(json.load(response), snapshot)
            task = request({"operation": "next", "reviewer_name": "张三"})["task"]
            with self.assertRaises(HTTPError) as context:
                request({"operation": "claim", "document_id": task["document_id"], "reviewer_name": "李四"})
            self.assertEqual(context.exception.code, 409)
            with self.assertRaises(HTTPError) as context:
                request([])
            self.assertEqual(context.exception.code, 400)
            self.assertEqual(request()["tasks"][0]["owner"], "张三")


if __name__ == "__main__":
    unittest.main()
