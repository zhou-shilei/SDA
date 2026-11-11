"""Real case navigation remains usable while publication/history requests stall."""
import functools
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class CaseExamplesBrowserTest(unittest.TestCase):
    def test_cases_load_without_dataset_or_history_and_switch_documents(self):
        from playwright.sync_api import expect, sync_playwright

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            QuietHandler, directory=str(Path(__file__).resolve().parents[1])))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pending = {}
        writes, errors = [], []
        stall_background = True

        def api(route):
            request = route.request
            if request.method != "GET":
                writes.append(request.url)
            path = urlsplit(request.url).path
            if stall_background and path in ("/api/dataset/records", "/api/reviews/human"):
                pending[path] = route
                return
            if path == "/api/patents":
                payload = {"patents": [dict(document_id=identity, filename=identity + ".pdf",
                    status="completed", stage="completed", record_count=count, page_count=0, pdf_form={"kind": "text"},
                    patent_text={"status": "missing"},
                    artifacts={key: f"api/patents/{identity}/{key}.json" for key in ("review", "pages")})
                    for identity, count in (("DOC-A", 1), ("DOC-B", 1), ("DOC-EMPTY", 0))]}
            elif path.endswith("/review.json"):
                identity = path.split("/")[-2]
                payload = dict(records=[dict(document_id=identity, record_id="R1",
                    antibody_id=identity + "-Ab", vh="EVQL", vl="DIQL", ic50=1, ic50_unit="nM",
                    link_status="linked", source={}, evidence=[], assay_keywords=[])],
                    pages={"page_count": 0, "pages": []}, contexts=[], candidates=[],
                    classifications=[], ocr=[], runtime={}, template={}, validations=[])
            elif path == "/api/dataset/records":
                payload = {"records": [], "folders": []}
            elif path == "/api/reviews/human":
                payload = {"reviews": []}
            else:
                payload = {"tasks": [], "processing_queue": None}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        try:
            with sync_playwright() as p:
                if not Path(p.chromium.executable_path).exists():
                    self.skipTest("Chromium is not installed")
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.add_init_script("sessionStorage.setItem('sda.studio.reviewerName','案例测试');"
                        "localStorage.setItem('sda.studio.onboarding.v1','dismissed')")
                    page.route("**/api/**", api)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/studio/?document_id=DOC-A")
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    expect(page.locator("#record-rows")).to_contain_text("DOC-A-Ab")
                    self.assertIn("/api/dataset/records", pending)
                    self.assertIn("/api/reviews/human", pending)
                    nav = page.locator(".primary-nav button")
                    self.assertEqual(nav.nth(0).get_attribute("data-app-page"), "cases")
                    self.assertEqual(nav.nth(1).get_attribute("data-app-page"), "dataset")
                    page.locator('[data-app-page="cases"]').click()
                    expect(page.locator("#cases-page")).to_be_visible()
                    expect(page.locator(".case-example-card")).to_have_count(2)
                    expect(page.locator("#case-examples-count")).to_have_text("2 个案例")
                    page.locator("#case-examples-search").fill("doc-b.pdf")
                    expect(page.locator(".case-example-card")).to_have_count(1)
                    page.locator("#case-examples-search").fill("no-such-case")
                    expect(page.locator(".case-example-card")).to_have_count(0)
                    expect(page.locator("#case-example-list")).to_contain_text("未找到匹配的案例")
                    page.locator("#case-examples-search").fill("")
                    page.locator('[data-case-document="DOC-B"]').click()
                    expect(page.locator("#cases-page")).not_to_be_visible()
                    expect(page.locator("#dataset-title")).to_have_text("DOC-B")
                    expect(page.locator("#record-rows")).to_contain_text("DOC-B-Ab")
                    pending["/api/dataset/records"].fulfill(status=503,
                        content_type="application/json", body='{"error":"unavailable"}')
                    pending["/api/reviews/human"].fulfill(status=200,
                        content_type="application/json", body='{"reviews":[]}')
                    stall_background = False
                    expect(page.locator("#record-rows")).to_contain_text("DOC-B-Ab")
                    page.locator("#language-toggle").click()
                    expect(page.locator('[data-app-page="cases"]')).to_have_text("Cases")
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.locator('[data-app-page="cases"]').click()
                    expect(page.locator("#case-examples-title")).to_have_text("Cases")
                    rect = page.locator("#cases-page").bounding_box()
                    self.assertGreaterEqual(rect["x"], 0)
                    self.assertLessEqual(rect["x"] + rect["width"], 390)
                    for card in page.locator(".case-example-card").all():
                        rect = card.bounding_box()
                        self.assertGreaterEqual(rect["x"], 0)
                        self.assertLessEqual(rect["x"] + rect["width"], 390)
                    page.locator('[data-app-page="dataset"]').click()
                    expect(page.locator("#cases-page")).not_to_be_visible()
                    expect(page.locator("#dataset-page")).to_be_visible()
                    page.evaluate("localStorage.removeItem('sda.studio.appPage')")
                    page.goto(f"http://127.0.0.1:{server.server_port}/studio/")
                    expect(page.locator("#cases-page")).to_be_visible()
                    expect(page.locator('[data-app-page="cases"]')).to_have_attribute("aria-current", "page")
                    self.assertEqual(errors, [])
                    self.assertEqual(writes, [])
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
