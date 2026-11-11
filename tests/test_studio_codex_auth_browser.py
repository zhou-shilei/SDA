"""Exercise login-file selection, paste, retry and real queue binding using fake credentials."""

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from studio.server import StudioServer
from studio.sda_data import Workspace


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class CodexAuthBrowserTests(unittest.TestCase):
    def test_login_upload_paste_isolation_retry_and_mobile(self):
        from playwright.sync_api import expect, sync_playwright
        import pymupdf

        root = Path(__file__).resolve().parents[1]
        screenshots = root / ".studio/previews/codex-auth-20261103"
        screenshots.mkdir(parents=True, exist_ok=True)
        fake = {"tokens": {"access_token": "fake-browser-access", "refresh_token": "fake-browser-refresh",
                           "id_token": "fake-browser-id", "account_id": "fake-browser-account"}}
        encoded = json.dumps(fake).encode()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            server = StudioServer(("127.0.0.1", 0), workspace)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        page.add_init_script("localStorage.setItem('sda.studio.onboarding.v1','dismissed')")
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}/studio/?view=intake")
                        expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                        dialog = page.locator("#reviewer-dialog")
                        expect(dialog).to_be_visible()
                        page.locator("#reviewer-name-input").fill("测试用户")
                        page.screenshot(path=str(screenshots / "login-desktop.png"))
                        page.set_viewport_size({"width": 390, "height": 844})
                        bounds = dialog.bounding_box()
                        self.assertGreaterEqual(bounds["x"], 0)
                        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
                        self.assertLessEqual(bounds["y"] + bounds["height"], 844)
                        page.screenshot(path=str(screenshots / "login-mobile.png"))
                        page.set_viewport_size({"width": 1440, "height": 1000})
                        file_input = page.locator("#reviewer-auth-file")
                        file_input.set_input_files({"name": "auth.json", "mimeType": "application/json", "buffer": b'{"tokens":{}}'})
                        expect(page.locator("#reviewer-auth-error")).to_be_visible()
                        expect(page.locator("#save-reviewer-name")).to_be_disabled()
                        page.locator("#reviewer-auth-clear").click()
                        expect(page.locator("#save-reviewer-name")).to_be_enabled()
                        file_input.set_input_files({"name": "auth.json", "mimeType": "application/json", "buffer": b'x' * 65537})
                        expect(page.locator("#reviewer-auth-error")).to_contain_text("64 KB")
                        page.locator("#reviewer-auth-clear").click()
                        # Native file selection followed by a transient server failure.
                        file_input.set_input_files({"name": "auth.json", "mimeType": "application/json", "buffer": encoded})
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("已选择 auth.json")
                        page.route("**/api/codex/auth", lambda route: route.fulfill(
                            status=503, content_type="application/json", body='{"error":"temporary"}'))
                        page.locator("#save-reviewer-name").click()
                        expect(page.locator("#reviewer-auth-error")).to_contain_text("保存失败")
                        expect(page.locator("#save-reviewer-name")).to_be_enabled()
                        expect(dialog).to_be_visible()
                        self.assertIsNone(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')"))
                        page.unroute("**/api/codex/auth")
                        page.locator("#save-reviewer-name").click()
                        expect(dialog).not_to_be_visible()
                        profile = json.loads(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')"))
                        self.assertEqual(profile["name"], "测试用户")
                        self.assertEqual(set(profile), {"name", "id"})
                        for data in (page.content(), page.evaluate("JSON.stringify(localStorage)"), page.evaluate("JSON.stringify(sessionStorage)")):
                            self.assertNotIn("fake-browser-access", data)
                        # A real PDF is queued on the temporary server, whose worker is never started.
                        with pymupdf.open() as pdf:
                            pdf.new_page()
                            pdf_bytes = pdf.tobytes()
                        page.locator("#patent-pdf-input").set_input_files({"name": "AUTH-DEMO.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes})
                        page.locator("#start-agent-button").click()
                        expect(page.locator('#patent-rows [data-patent-id="AUTH-DEMO"]')).not_to_have_count(0)
                        self.assertEqual(workspace.jobs()["AUTH-DEMO"]["_codex_auth_id"], profile["id"])
                        page.locator("#reviewer-identity-button").click()
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("已保存")
                        page.locator("#reviewer-auth-clear").click()
                        page.locator("#cancel-reviewer-name").click()
                        self.assertEqual(json.loads(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')")), profile)
                        page.locator("#reviewer-identity-button").click()
                        page.locator("#reviewer-name-input").fill("另一用户")
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("默认账号")
                        page.locator("#save-reviewer-name").click()
                        expect(dialog).not_to_be_visible()
                        self.assertIsNone(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')"))
                        self.assertEqual(workspace.jobs()["AUTH-DEMO"]["_codex_auth_id"], profile["id"])
                        # Paste JSON and drop a file without putting their contents in a visible input.
                        page.locator("#reviewer-identity-button").click()
                        page.locator("#reviewer-auth-dropzone").evaluate("""(el, text) => {
                            const data = new DataTransfer(); data.setData('text/plain', text);
                            el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true}));
                        }""", encoded.decode())
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("已选择")
                        page.locator("#reviewer-auth-clear").click()
                        page.locator("#reviewer-auth-dropzone").evaluate("""(el, text) => {
                            const data = new DataTransfer(); data.items.add(new File([text], 'auth.json', {type:'application/json'}));
                            el.dispatchEvent(new DragEvent('drop', {dataTransfer: data, bubbles: true, cancelable: true}));
                        }""", encoded.decode())
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("已选择")
                        page.locator("#save-reviewer-name").click()
                        expect(dialog).not_to_be_visible()
                        second = json.loads(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')"))
                        self.assertNotEqual(second["id"], profile["id"])
                        page.reload()
                        expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                        page.locator("#language-toggle").click()
                        page.locator("#reviewer-identity-button").click()
                        expect(page.locator("#reviewer-auth-status")).to_contain_text("auth.json saved")
                        page.screenshot(path=str(screenshots / "login-en.png"))
                        page.locator("#reviewer-auth-clear").click()
                        page.locator("#save-reviewer-name").click()
                        expect(dialog).not_to_be_visible()
                        self.assertIsNone(page.evaluate("sessionStorage.getItem('sda.studio.codexCredential')"))
                        self.assertEqual(errors, [])
                        (screenshots / "report.json").write_text(json.dumps({"page_errors": errors,
                            "model_calls": 0, "checks": ["file", "invalid", "size", "paste", "drop", "save retry",
                            "task binding", "name isolation", "cancel", "clear", "reload", "zh/en", "mobile"]}, indent=2))
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
