"""Dataset deletion is scoped to published snapshots and validates each batch."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from studio.sda_data import Workspace
from studio.server import StudioServer


class DatasetDeleteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Workspace(Path(self.directory.name))
        self.folder = self.workspace.create_folder("IL-4")["dataset_id"]
        with self.workspace.connection() as db:
            for folder, doc, record in [("SDA", "DOC1", "r1"), (self.folder, "DOC1", "r1"),
                                        (self.folder, "DOC2", "r1"), (self.folder, "DOC1", "r2")]:
                db.execute("INSERT INTO publications VALUES (?, ?, ?, ?)", (folder, doc, record,
                           json.dumps(dict(document_id=doc, record_id=record, antibody_id=doc + record,
                                           vh="EVQL", vl="DIQL", ic50=1, ic50_unit="nM", source={}, evidence=[]))))
            db.execute("INSERT INTO reviews VALUES (?, ?, ?, ?)", ("DOC1", "r1", "张三", '{}'))
        self.source = Path(self.directory.name) / "source.pdf"
        self.source.write_bytes(b"source sentinel")

    def test_record_identity_scope_and_atomic_validation(self):
        workspace = self.workspace
        before = workspace.published()
        with self.assertRaises(ValueError):
            workspace.delete_dataset_items(dataset_id=self.folder, records=[
                dict(document_id="DOC1", record_id="r1"), dict(document_id="DOC1", record_id="missing")])
        self.assertEqual(workspace.published(), before)
        result = workspace.delete_dataset_items(dataset_id=self.folder, records=[dict(document_id="DOC1", record_id="r1")] * 2)
        self.assertEqual(result["deleted_record_count"], 1)
        remaining = {(r['_dataset_id'], r['_patent_id'], r['record_id']) for r in workspace.published()}
        self.assertIn(("SDA", "DOC1", "r1"), remaining)
        self.assertIn((self.folder, "DOC2", "r1"), remaining)
        self.assertEqual(len(remaining), 3)
        self.assertEqual(workspace.reviews(), [{}])
        self.assertEqual(self.source.read_bytes(), b"source sentinel")

    def test_folders_batch_and_restart(self):
        with self.assertRaises(ValueError):
            self.workspace.delete_dataset_items(dataset_ids=["SDA", "missing"])
        self.assertEqual(len(self.workspace.published()), 4)
        result = self.workspace.delete_dataset_items(dataset_ids=["SDA", self.folder])
        self.assertEqual(result["deleted_record_count"], 4)
        reopened = Workspace(Path(self.directory.name))
        self.assertEqual(reopened.folders(), [])
        self.assertEqual(reopened.published(), [])
        self.assertEqual(reopened.reviews(), [{}])
        self.assertTrue(self.source.exists())
        reopened.create_folder("New target")

    def test_http_validation_and_delete(self):
        server = StudioServer(("127.0.0.1", 0), self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/dataset/delete"
            def request(payload):
                return urlopen(Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}))
            for payload in ({}, {"dataset_ids": "SDA"}, {"dataset_ids": ["SDA"], "dataset_id": self.folder},
                            {"dataset_id": self.folder, "records": [None]}):
                with self.assertRaises(HTTPError) as error:
                    request(payload)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            self.assertEqual(len(self.workspace.published()), 4)
            with request({"dataset_ids": [self.folder]}) as response:
                self.assertEqual(json.load(response)["deleted_record_count"], 3)
            self.assertEqual(len(self.workspace.published()), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
    def test_browser_selection_cancel_filter_delete_and_reload(self):
        from playwright.sync_api import sync_playwright, expect
        with patch.object(self.workspace, "patents", return_value=[]):
            server = StudioServer(("127.0.0.1", 0), self.workspace)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    if not Path(playwright.chromium.executable_path).exists():
                        self.skipTest("Chromium is not installed")
                    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page(viewport=dict(width=1500, height=1000))
                        page.add_init_script("sessionStorage.setItem('sda.studio.reviewerName', '测试'); localStorage.setItem('sda.studio.onboarding.v1', 'completed');")
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}/studio/?view=dataset")
                        expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                        delete = page.locator("#dataset-delete-selected")
                        expect(delete).to_be_disabled()
                        page.set_viewport_size(dict(width=430, height=900))
                        expect(delete).to_be_in_viewport()
                        page.set_viewport_size(dict(width=1500, height=1000))
                        page.locator('.dataset-folder-select').first.check()
                        expect(page.locator("#dataset-selection-count")).to_have_text("已选择 1 个文件夹")
                        self.assertTrue(page.locator('#dataset-select-all').evaluate('(el) => el.indeterminate'))
                        # Checking a folder does not open it; opening clears the selection.
                        page.locator(f'button[data-dataset-id="{self.folder}"]').click()
                        expect(delete).to_be_disabled()
                        expect(page.locator('#dataset-overview-rows tr')).to_have_count(3)
                        page.locator('#dataset-select-all').check()
                        expect(page.locator('#dataset-selection-count')).to_have_text('已选择 3 条记录')
                        page.locator('#dataset-search').fill('DOC1r2')
                        expect(page.locator('#dataset-overview-rows tr')).to_have_count(1)
                        expect(page.locator('#dataset-selection-count')).to_have_text('已选择 1 条记录')
                        page.once('dialog', lambda dialog: dialog.dismiss())
                        delete.click()
                        self.assertEqual(len(self.workspace.published(self.folder)), 3)
                        page.route('**/api/dataset/delete', lambda route: route.fulfill(
                            status=400, content_type='application/json', body='{"error":"test failure"}'))
                        page.once('dialog', lambda dialog: dialog.accept())
                        delete.click()
                        expect(delete).to_be_enabled()
                        expect(page.locator('#dataset-selection-count')).to_have_text('已选择 1 条记录')
                        self.assertEqual(len(self.workspace.published(self.folder)), 3)
                        page.unroute('**/api/dataset/delete')
                        page.once('dialog', lambda dialog: dialog.accept())
                        delete.click()
                        expect(page.locator('#dataset-selection-count')).to_have_text('已选择 0 条记录')
                        page.locator('#dataset-search').fill('')
                        expect(page.locator('#dataset-overview-rows tr')).to_have_count(2)
                        page.locator('#dataset-back').click()
                        page.locator('#dataset-select-all').check()
                        expect(page.locator('#dataset-selection-count')).to_have_text('已选择 2 个文件夹')
                        page.locator('#language-toggle').click()
                        expect(delete).to_have_text('Delete selected')
                        page.once('dialog', lambda dialog: dialog.accept())
                        delete.click()
                        expect(page.locator('.dataset-folder-card')).to_have_count(0)
                        page.reload()
                        expect(page.locator('#app')).to_have_attribute('aria-busy', 'false')
                        expect(page.locator('.dataset-folder-card')).to_have_count(0)
                        expect(delete).to_be_disabled()
                        self.assertEqual(self.workspace.reviews(), [{}])
                        self.assertTrue(self.source.exists())
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
