"""Inline tag expansion survives unrelated clicks and table redraws, without data writes."""
import functools
import importlib.util
import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class TagExpansionBrowserTest(unittest.TestCase):
    def test_single_row_expansion_state_widths_and_dataset(self):
        from playwright.sync_api import expect, sync_playwright

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            QuietHandler, directory=str(Path(__file__).resolve().parents[1])))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        writes = []
        tags = [dict(category="target" if i < 6 else "method", key=f"tag-{i}",
                     label_zh=f"很长的实验标签-{i}-需要展开查看完整内容", label_en=f"Long experiment tag {i} with full details")
                for i in range(12)]

        def records(identity):
            return [dict(document_id=identity, record_id=f"R{i}", antibody_id=f"Ab{i}", vh="EVQL" * 30,
                         vl="DIQL" * 27, ic50=i, ic50_unit="nM", link_status="linked", source={}, evidence=[],
                         experiment_group="IC50 · " + "实验说明 " * 12, assay_keywords=tags,
                         h_chain="EVQLFULL", l_chain="DIQLFULL",
                         ophiuchus_review_status="manual_review",
                         ophiuchus_review={"status": "manual_review", "chains": {
                             "VH": {"status": "manual_review", "metric_status": "scored"},
                             "VL": {"status": "error" if i == 1 else "manual_review", "metric_status": "scored"},
                         }}) for i in (1, 2)]

        def api(route):
            request = route.request
            if request.method != "GET":
                writes.append(request.url)
            path = urlsplit(request.url).path
            if path == "/api/patents":
                payload = {"patents": [dict(document_id=identity, filename=identity+".pdf", status="completed",
                    stage="completed", created_at="2026-11-02T00:00:00Z", record_count=2, page_count=0,
                    pdf_form={"kind": "text"}, patent_text={"status": "missing"},
                    artifacts={key:f"api/patents/{identity}/{key}.json" for key in ("review", "records", "pages")})
                    for identity in ("DOC-A", "DOC-B")]}
            elif path == "/api/dataset/records":
                payload = {"records": [{**r, "_patent_id": "DOC-A", "_dataset_id": "SDA"} for r in records("DOC-A")],
                           "folders": [{"dataset_id": "SDA", "name": "SDA"}]}
            elif path == "/api/reviews/queue":
                payload = {"tasks": [], "processing_queue": None}
            elif path.endswith("/review.json"):
                identity = path.split("/")[-2]
                payload = dict(records=records(identity), pages={"page_count": 0, "pages": []},
                    contexts=[], candidates=[], classifications=[], ocr=[], runtime={}, template={}, validations=[])
            else:
                payload = {"reviews": []}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        try:
            with sync_playwright() as p:
                if not Path(p.chromium.executable_path).exists():
                    self.skipTest("Chromium is not installed")
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1500, "height": 1000})
                    page.add_init_script("sessionStorage.setItem('sda.studio.reviewerName','测试用户');"
                                         "localStorage.setItem('sda.studio.onboarding.v1','dismissed')")
                    page.route("**/api/**", api)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/studio/?document_id=DOC-A")
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    keyword = page.locator('#record-rows button[data-record-id="R1"][data-field="assay_keywords"]')
                    row = page.locator('#record-rows tr[data-record-id="R1"]')
                    failed_op = row.get_by_text("Ophiuchus_Ab", exact=True)
                    pending_op = page.locator('#record-rows tr[data-record-id="R2"]').get_by_text("Ophiuchus_Ab", exact=True)
                    expect(failed_op).to_have_class("review-check-badge review-check-error")
                    expect(failed_op).to_have_css("color", "rgb(180, 60, 88)")
                    expect(pending_op).to_have_class("review-check-badge review-check-manual_review")
                    expect(keyword).to_have_attribute("aria-expanded", "false")
                    self.assertLessEqual(keyword.locator('.tag-disclosure-content').bounding_box()['height'], 24)
                    self.assertLessEqual(row.bounding_box()['height'], 43)
                    columns = page.locator('[data-resizable-table="review"] col')
                    self.assertEqual(columns.nth(3).evaluate("el=>el.style.width"), "340px")
                    self.assertEqual(columns.nth(8).evaluate("el=>el.style.width"), "240px")
                    keyword.click()
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    self.assertGreater(row.bounding_box()['height'], 43)
                    page.locator('#dataset-title').click()
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    page.locator('#record-rows button[data-record-id="R2"][data-field="vh"]').click()
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    target = page.locator('#record-rows tr[data-record-id="R1"] [data-database-column="target"] [data-tag-toggle]')
                    target.focus()
                    target.press("Enter")
                    expect(target).to_have_attribute("aria-expanded", "true")
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    page.locator('#record-search').fill('no matches')
                    page.locator('#record-search').fill('')
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    expect(target).to_have_attribute("aria-expanded", "true")
                    page.locator('#language-toggle').click()
                    expect(keyword).to_have_attribute("aria-expanded", "true")
                    keyword.click()
                    expect(keyword).to_have_attribute("aria-expanded", "false")
                    expect(target).to_have_attribute("aria-expanded", "true")
                    # Identical record IDs in another document do not inherit expansion.
                    page.locator('[data-app-page="intake"]').click()
                    page.locator('#patent-rows tr[data-patent-id="DOC-B"] [data-action="review"]').click()
                    expect(target).to_have_attribute("aria-expanded", "false")
                    # Dataset expansion stays in place and is independent of review expansion.
                    page.locator('[data-app-page="dataset"]').click()
                    page.locator('.dataset-folder-card').click()
                    saved = page.locator('#dataset-overview-rows button[data-record-id="R1"][data-field="assay_keywords"]')
                    saved.click()
                    expect(saved).to_have_attribute("aria-expanded", "true")
                    expect(page.locator('#dataset-page')).to_be_visible()
                    page.locator('#dataset-page-title').click()
                    expect(saved).to_have_attribute("aria-expanded", "true")
                    saved.click()
                    expect(saved).to_have_attribute("aria-expanded", "false")
                    # Migrate old defaults and preserve a genuinely customized sequence width.
                    page.evaluate("""() => {
                        const widths = [...document.querySelectorAll('[data-resizable-table="review"] col')].map(c=>parseFloat(c.style.width));
                        widths[3]=367; widths[4]=245; widths[7]=300; widths[8]=480;
                        localStorage.setItem('sda.studio.tableColumns.v1.review', JSON.stringify(widths));
                    }""")
                    page.reload()
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    self.assertEqual(columns.nth(3).evaluate("el=>el.style.width"), "367px")
                    self.assertEqual(columns.nth(4).evaluate("el=>el.style.width"), "340px")
                    self.assertEqual(columns.nth(7).evaluate("el=>el.style.width"), "180px")
                    self.assertEqual(columns.nth(8).evaluate("el=>el.style.width"), "240px")
                    self.assertEqual(errors, [])
                    self.assertEqual(writes, [])
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
