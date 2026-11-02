"""Optional real-browser coverage of tags, facets and shared group reviews."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from studio.comparison_groups import assign_comparison_candidates
from studio.sda_data import Workspace
from studio.server import StudioServer


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class ComparisonBrowserTest(unittest.TestCase):
    def test_tags_filters_group_review_reload_and_publication(self):
        from playwright.sync_api import sync_playwright, expect
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            workspace.create_folder("IL-13")
            rows = [dict(document_id="DOC", record_id=f"r{i}", entity_id=f"a{i}", antibody_id=f"Ab{i}",
                         experiment_id="exp1" if i < 3 else "exp2", assay_endpoint="IC50", ic50=str(i),
                         ic50_unit="nM", vh="EVQL", vl="DIQL", h_chain="EVQLFULL" if i == 1 else None,
                         antigen_target="hIL-13 activity", assay_system="HEK-293T.STAT6-GFP cells",
                         assay_method="reporter_gene", assay_condition="24h", link_status="linked",
                         assay_keywords=[dict(category="readout", key="GFP fluorescence")], evidence=[], source={})
                    for i in range(1, 5)]
            assign_comparison_candidates(rows)
            patents = [dict(document_id="DOC", filename="DOC.pdf", status="completed", stage="completed",
                            record_count=4, page_count=0, created_at="2026-11-02", builtin=False,
                            artifacts={key: f"api/patents/DOC/{key}.json" for key in ("review", "records", "pages")})]
            bundle = dict(records=rows, pages=dict(page_count=0, pages=[]), pdf=None, contexts=[], candidates=[],
                          classifications=[], ocr=[], runtime={}, template={}, validations=[])
            with patch.object(workspace, "patents", return_value=patents), \
                 patch.object(workspace, "bundle", side_effect=lambda _: copy.deepcopy(bundle)), \
                 patch.object(workspace, "locations", return_value={}):
                server = StudioServer(("127.0.0.1", 0), workspace)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with sync_playwright() as playwright:
                        if not Path(playwright.chromium.executable_path).exists():
                            self.skipTest("Chromium is not installed")
                        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                        try:
                            page = browser.new_page(viewport=dict(width=1500, height=1000))
                            page.set_default_timeout(7000)
                            errors = []
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            page.goto(f"http://127.0.0.1:{server.server_port}/studio/?document_id=DOC")
                            page.locator("#reviewer-name-input").fill("张三")
                            page.locator("#save-reviewer-name").click()
                            expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                            page.locator("#onboarding-skip").click()
                            expect(page.locator("#record-rows tr")).to_have_count(4)
                            expect(page.locator('#record-rows [data-database-column="method"] .keyword-method')).to_have_count(4)
                            expect(page.locator('#record-rows [data-sequence-availability="vh"]')).to_have_count(4)
                            expect(page.locator('#record-rows [data-sequence-availability="full_heavy"]')).to_have_count(1)
                            menu = page.locator('#review-page [data-database-filter="sequence"]')
                            menu.locator("summary").click()
                            menu.locator('[data-filter-value="full_heavy"]').check()
                            expect(page.locator("#record-rows tr")).to_have_count(1)
                            menu.locator('[data-filter-value="all"]').check()
                            expect(page.locator("#record-rows tr")).to_have_count(4)
                            menu.locator("summary").click()
                            group_menu = page.locator('#review-page [data-database-filter="comparison_group"]')
                            group_menu.locator("summary").click()
                            group_menu.locator('[data-filter-value="DOC:G1"]').check()
                            expect(page.locator("#record-rows tr")).to_have_count(2)
                            group_menu.locator('[data-filter-value="DOC:G2"]').check()
                            expect(page.locator("#record-rows tr")).to_have_count(4)
                            group_menu.locator('[data-filter-value="all"]').check()
                            group_menu.locator("summary").click()
                            page.locator('[data-app-page="queue"]').click()
                            page.locator('[data-queue-action="next"]').click()
                            expect(page.locator("#review-page")).to_be_visible()
                            page.locator('[data-comparison-record="r1"]').click()
                            expect(page.locator(".comparison-dialog")).to_contain_text("候选待复核")
                            expect(page.locator(".comparison-members tbody tr")).to_have_count(2)
                            page.locator('[data-comparison-decision="confirmed"]').click()
                            expect(page.locator(".comparison-dialog")).to_contain_text("已确认可比")
                            expect(page.locator(".comparison-reviewer")).to_contain_text("张三")
                            page.locator('[data-close-comparison]').click()
                            expect(page.locator('#record-rows .keyword-confirmed')).to_have_count(2)
                            page.reload()
                            expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                            expect(page.locator('#record-rows .keyword-confirmed')).to_have_count(2)
                            page.locator('[data-app-page="queue"]').click()
                            page.locator('[data-queue-action="resume"]').click()
                            expect(page.locator("#review-page")).to_be_visible()
                            page.locator('#select-all-records').check()
                            page.locator('#review-dataset-select').select_option(label="IL-13")
                            page.locator('#review-publish-button').click()
                            expect(page.locator('#review-publish-button')).to_be_enabled()
                            page.locator('[data-app-page="dataset"]').click()
                            page.locator('.dataset-folder-card', has_text="IL-13").click()
                            expect(page.locator('#dataset-overview-rows tr')).to_have_count(4)
                            expect(page.locator('#dataset-overview-rows .keyword-confirmed')).to_have_count(2)
                            page.locator('#dataset-overview-rows [data-comparison-record="r1"]').click()
                            expect(page.locator(".comparison-dialog")).to_contain_text("已确认可比")
                            expect(page.locator('[data-comparison-decision]')).to_have_count(0)
                            page.locator('[data-close-comparison]').click()
                            page.locator('#language-toggle').click()
                            expect(page.locator('#dataset-overview-rows .keyword-confirmed').first).to_have_text('Confirmed comparable')
                            self.assertEqual(errors, [])
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
