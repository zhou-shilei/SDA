"""Exercise the tour in Chromium without OCR, workers or workspace mutations."""
import functools
import importlib.util
import json
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class OnboardingBrowserTest(unittest.TestCase):
    def test_tour_identity_navigation_persistence_and_mobile(self):
        from playwright.sync_api import expect, sync_playwright

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        root = Path(__file__).resolve().parents[1]
        handler = functools.partial(QuietHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        requests = []
        has_results = True
        patent = dict(document_id="DOC", filename="DOC.pdf", status="completed", stage="completed", record_count=1,
                      created_at="2026-11-02T00:00:00Z",
                      page_count=0, pdf_form={"kind": "text"}, patent_text={"status": "missing"},
                      artifacts={key: f"api/patents/DOC/{key}.json" for key in ("review", "records", "pages")})
        bundle = dict(pages={"page_count": 0, "pages": []},
                      records=[dict(document_id="DOC", record_id="R1", antibody_id="Ab1", vh="EVQL", vl="DIQL",
                                    ic50=1, ic50_unit="nM", link_status="linked", source={}, evidence=[])],
                      contexts=[], candidates=[], classifications=[], ocr=[], runtime={}, template={}, validations=[])

        def api(route):
            request = route.request
            requests.append((request.method, urlsplit(request.url).path))
            path = urlsplit(request.url).path
            payload = {
                "/api/patents": {"patents": [patent] if has_results else []},
                "/api/dataset/records": {"records": [], "folders": [{"dataset_id": "SDA", "name": "SDA"}]},
                "/api/reviews/human": {"reviews": []},
                "/api/reviews/queue": {"tasks": [], "processing_queue": None},
                "/api/patents/DOC/review.json": bundle,
            }.get(path, {})
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        try:
            with sync_playwright() as playwright:
                if not Path(playwright.chromium.executable_path).exists():
                    self.skipTest("Chromium is not installed")
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.route("**/api/**", api)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/studio/?view=intake")
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    expect(page.locator("#reviewer-dialog")).to_be_visible()
                    expect(page.locator("#onboarding-dialog")).not_to_be_visible()
                    page.locator("#reviewer-name-input").fill("测试用户")
                    page.locator("#save-reviewer-name").click()
                    tour = page.locator("#onboarding-dialog")
                    expect(tour).to_be_visible()
                    expect(page.locator("#onboarding-step")).to_contain_text("1 / 6")
                    expect(page.locator("#dataset-page")).to_be_visible()
                    expect(page.locator("#onboarding-title")).to_have_text("第一步：新建靶点文件夹")
                    expect(page.locator("#onboarding-description")).to_contain_text("TSLP")
                    self.assert_highlight_target(page, "#dataset-create-button")
                    # Modal focus must stay in the tour, including when tabbing backwards.
                    for _ in range(8):
                        page.keyboard.press("Shift+Tab")
                        self.assertTrue(page.evaluate("document.activeElement.closest('#onboarding-dialog') !== null"))
                    page.keyboard.press("ArrowRight")
                    expect(page.locator("#onboarding-title")).to_have_text("第二步：导入 PDF 并加入队列")
                    page.locator("#onboarding-back").click()
                    expect(page.locator("#onboarding-step")).to_contain_text("1 / 6")
                    expected_steps = [
                        ("intake", "#patent-import-form"),
                        ("intake", '#patent-rows button[data-action="review"]'),
                        ("review", '#record-rows button[data-field="vh"]'),
                        ("review", "#edit-field"),
                        ("review", "#accept-field"),
                    ]
                    for number, (app_page, target) in enumerate(expected_steps, 2):
                        page.locator("#onboarding-next").click()
                        expect(page.locator("#onboarding-step")).to_contain_text(f"{number} / 6")
                        expect(page.locator(f"#{app_page}-page")).to_be_visible()
                        expect(page.locator(".onboarding-highlight")).to_be_visible()
                        self.assert_highlight_target(page, target)
                        self.assert_card_inside_viewport(page)
                    page.locator("#onboarding-next").click()
                    expect(tour).not_to_be_visible()
                    expect(page.locator("#intake-page")).to_be_visible()
                    self.assertEqual(page.evaluate("localStorage.getItem('sda.studio.onboarding.v1')"), "completed")
                    page.reload()
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    expect(tour).not_to_be_visible()
                    # Manual replay, escape, and locale-aware copy.
                    page.locator("#onboarding-start").click()
                    expect(tour).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(tour).not_to_be_visible()
                    expect(page.locator("#onboarding-start")).to_be_focused()
                    page.locator("#language-toggle").click()
                    page.locator("#onboarding-start").click()
                    expect(page.locator("#onboarding-title")).to_have_text("Step 1: Create a target folder")
                    page.locator("#onboarding-skip").click()
                    # The app has a wide table canvas; the tour must still fit a phone viewport.
                    # With no parsed documents, review and sequence steps highlight their containers.
                    has_results = False
                    page.reload()
                    expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.locator("#onboarding-start").click()
                    for number in range(1, 7):
                        expect(page.locator("#onboarding-step")).to_contain_text(f"{number} / 6")
                        expect(page.locator(".onboarding-highlight")).to_be_visible()
                        self.assert_card_inside_viewport(page)
                        page.locator("#onboarding-next").click()
                    expect(tour).not_to_be_visible()
                    self.assertEqual(errors, [])
                    self.assertFalse([request for request in requests if request[0] != "GET"], requests)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def assert_highlight_target(self, page, selector):
        page.wait_for_function("""selector => {
            const target = document.querySelector(selector).getBoundingClientRect();
            const highlight = document.querySelector('.onboarding-highlight').getBoundingClientRect();
            return Math.abs(highlight.left - Math.max(6, target.left - 7)) <= 1
                && Math.abs(highlight.top - Math.max(6, target.top - 7)) <= 1
                && Math.abs(highlight.right - Math.min(innerWidth - 6, target.right + 7)) <= 1
                && Math.abs(highlight.bottom - Math.min(innerHeight - 6, target.bottom + 7)) <= 1;
        }""", arg=selector)

    def assert_card_inside_viewport(self, page):
        page.wait_for_function("""() => {
            const r = document.querySelector('.onboarding-card').getBoundingClientRect();
            return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
        }""")
