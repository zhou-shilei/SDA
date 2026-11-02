"""Optional browser regression; all data and uploads stay in a temporary workspace."""
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.server import StudioServer
from studio.sda_data import Workspace, now


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is optional')
class TeamQueueBrowserTest(unittest.TestCase):
    def test_collaboration_reload_drafts_and_batch_upload(self):
        import pymupdf
        from playwright.sync_api import sync_playwright, expect
        expect.set_options(timeout=10000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'test.pdf'
            with pymupdf.open() as document:
                for i in range(3):
                    document.new_page().insert_text((72,72), f'Test page {i+1}')
                document.save(pdf)
            workspace = Workspace(root)
            originals = ['DOC-AAA', 'DOC-BBB']
            for i, identity in enumerate(originals):
                workspace.update_job(identity, filename=f'{identity}.pdf', status='completed', stage='completed', created_at=f'2026-11-0{i+1}T00:00:00Z')

            def patents():
                result = []
                for identity, job in workspace.jobs().items():
                    base = f'api/patents/{identity}/'
                    result.append({**job, 'document_id': identity, 'record_count': 1 if identity in originals else 0,
                                   'page_count': 3, 'builtin': False, 'collection_review_available': False, 'pdf_form': {'kind':'text'},
                                   'patent_text': {'status':'missing'}, 'extraction_engine': 'dataagent2',
                                   'artifacts': {key:base+key+'.json' for key in ('review','records','pages')}})
                return result

            def bundle(identity):
                if identity not in workspace.jobs(): raise KeyError(identity)
                return {'document_id':identity, 'pdf':pdf,
                        'pages':{'page_count':3,'pages':[dict(page=i,width=595,height=842,pixel_width=595,pixel_height=842,image=f'pages/{i:04d}.png') for i in range(1,4)]},
                        'records':[dict(document_id=identity,record_id='R1',antibody_id=identity,vh='EVQL',vl='DIQL',ic50=1,ic50_unit='nM',link_status='linked',source={},evidence=[])],
                        'contexts':[],'candidates':[],'classifications':[],'ocr':[],'runtime':{},'template':{},'validations':[]}

            def enqueue(identity, **kwargs):
                workspace.update_job(identity, status='queued', stage='queued', run_id=f'upload-{identity}',
                                     queued_at=now(), progress=0, extraction_engine=kwargs.get('engine'))
                server.queue.submit(identity, config={})
                return next(p for p in patents() if p['document_id']==identity)

            with patch.object(workspace, 'patents', side_effect=patents), patch.object(workspace, 'bundle', side_effect=bundle), \
                 patch.object(workspace, 'page_bundle', side_effect=bundle), patch.object(workspace, 'locations', return_value={}), \
                 patch.object(workspace, 'is_builtin', return_value=False):
                server = StudioServer(('127.0.0.1',0), workspace)
                worker = threading.Thread(target=server.serve_forever,daemon=True)
                worker.start()
                try:
                    with patch.object(server,'enqueue',side_effect=enqueue), sync_playwright() as playwright:
                        if not Path(playwright.chromium.executable_path).exists(): self.skipTest('Chromium is not installed')
                        browser = playwright.chromium.launch(headless=True,args=['--no-sandbox'])
                        try:
                            alice = browser.new_context(viewport={"width": 1440, "height": 1000})
                            bob = browser.new_context(viewport={"width": 1440, "height": 1000})
                            a, b = alice.new_page(), bob.new_page()
                            a.set_default_timeout(7000)
                            b.set_default_timeout(7000)
                            errors = []
                            a.on("pageerror", lambda e: errors.append(str(e)))
                            b.on("pageerror", lambda e: errors.append(str(e)))
                            url = f"http://127.0.0.1:{server.server_port}/studio/"

                            def login(page, name):
                                page.goto(url)
                                page.locator("#reviewer-name-input").fill(name)
                                page.locator("#save-reviewer-name").click()
                                expect(page.locator("#app")).to_have_attribute("aria-busy", "false")
                                page.locator("#onboarding-skip").click()
                                page.locator('[data-app-page="queue"]').click()
                                expect(page.locator("#queue-rows tr")).to_have_count(2)

                            login(a, "张三")
                            login(b, "李四")
                            expect(b.locator("#queue-page .queue-import-card > .page-card-header h2")).to_have_text("PDF 解析任务")
                            expect(b.locator("#queue-processing-host [data-processing-queue]")).to_have_count(1)
                            expect(b.locator("#queue-content .queue-card h2")).to_have_text("人工标注任务")
                            a.locator('[data-queue-action="next"]').click()
                            try:
                                expect(a.locator("#review-page")).to_be_visible()
                            except AssertionError:
                                print("QUEUE ERROR:", a.locator(".queue-error").all_text_contents(), "PAGE ERRORS:", errors)
                                raise
                            a.locator("#edit-field").click()
                            expect(a.locator("#edit-dialog")).to_be_visible()
                            a.locator("#corrected-value").fill("7.5")
                            a.locator('#correction-reasons label:has(input[value="numeric"])').click()
                            a.locator("#save-correction").click()
                            expect(a.locator("#edit-dialog")).not_to_be_visible()
                            a.locator("#page-next").click()
                            expect(a.locator("#page-indicator")).to_have_text("2 / 3")
                            a.locator('[data-app-page="queue"]').click()
                            expect(a.locator(".queue-tools [role=status]")).to_have_text("队列每 5 秒更新")
                            # Reload and resume the same assignment; the authoritative value survives.
                            a.reload()
                            expect(a.locator("#app")).to_have_attribute("aria-busy", "false")
                            a.locator('[data-app-page="queue"]').click()
                            a.locator('[data-queue-action="resume"]').click()
                            expect(a.locator("#review-page")).to_be_visible()
                            expect(a.locator("#page-indicator")).to_have_text("2 / 3")
                            a.locator("#edit-field").click()
                            expect(a.locator("#corrected-value")).to_have_value("7.5")
                            a.locator("#close-edit-dialog").click()
                            # Bob receives another document, with the same record ID but no leaked correction.
                            b.locator('[data-queue-action="next"]').click()
                            expect(b.locator("#review-page")).to_be_visible()
                            b.locator("#edit-field").click()
                            expect(b.locator("#corrected-value")).to_have_value("1")
                            b.locator("#close-edit-dialog").click()
                            b.locator('[data-app-page="queue"]').click()
                            b.locator('[data-queue-action="release"]').click()
                            expect(b.locator('[data-queue-action="release"]')).to_have_count(0)
                            a.locator('[data-app-page="queue"]').click()
                            a.locator('[data-queue-action="submit"]').click()
                            expect(a.locator('[data-queue-action="submit"]')).to_have_count(0)
                            b.reload()
                            expect(b.locator("#app")).to_have_attribute("aria-busy", "false")
                            b.locator('[data-queue-action="claim"][data-id="DOC-AAA"]').click()
                            expect(b.locator("#review-page")).to_be_visible()
                            b.locator("#edit-field").click()
                            expect(b.locator("#corrected-value")).to_have_value("7.5")
                            b.locator("#close-edit-dialog").click()
                            b.locator('[data-app-page="queue"]').click()
                            b.locator('[data-queue-action="approve"]').click()
                            expect(b.locator("#queue-rows")).to_contain_text("已完成")
                            # Multiple PDFs are uploaded without starting any real OCR/model process.
                            expect(b.locator("#queue-import-host #pdf-drop-zone strong")).to_have_text("提交 PDF")
                            expect(b.locator("#queue-import-host #start-agent-button")).to_be_disabled()
                            b.locator("#patent-pdf-input").set_input_files([
                                dict(name="DOC-CCC.pdf", mimeType="application/pdf", buffer=pdf.read_bytes()),
                                dict(name="DOC-DDD.pdf", mimeType="application/pdf", buffer=pdf.read_bytes()),
                            ])
                            expect(b.locator("#selected-pdf-name")).to_contain_text("2 个 PDF")
                            expect(b.locator("#patent-engine-select")).to_have_count(0)
                            expect(b.locator('[data-patent-strategy], [data-action="compare"]')).to_have_count(0)
                            expect(b.locator('#patent-rows')).not_to_contain_text('当前运行')
                            # Polling and page switches retain selected files.
                            b.locator("#queue-filter").select_option("mine")
                            b.wait_for_timeout(5200)
                            expect(b.locator("#queue-import-host #selected-pdf-name")).to_contain_text("2 个 PDF")
                            b.locator('[data-app-page="intake"]').click()
                            expect(b.locator("#intake-import-host #selected-pdf-name")).to_contain_text("2 个 PDF")
                            b.locator('[data-app-page="queue"]').click()
                            expect(b.locator("#queue-import-host #start-agent-button")).to_have_text("▶ 开始解析")
                            b.locator("#start-agent-button").click()
                            expect(b.locator("#selected-pdf-name")).to_have_text("点击选择或拖入 PDF")
                            b.locator("#queue-filter").select_option("all")
                            expect(b.locator("#queue-rows tr")).to_have_count(4)
                            expect(b.locator("#queue-rows")).to_contain_text("等待处理")
                            for identity in ("DOC-CCC", "DOC-DDD"):
                                self.assertEqual(workspace.jobs()[identity]["extraction_engine"], "dataagent2")
                                self.assertEqual(workspace.jobs()[identity]["submitted_by"], "李四")
                            # Everyone sees the actual parser FIFO, separately
                            # from the historical order of annotation tasks.
                            selector = '#queue-page [data-processing-document]'
                            a.locator('[data-app-page="queue"]').click()
                            for page in (a, b):
                                expect(page.locator(selector)).to_have_count(2)
                                self.assertEqual(page.locator(selector).evaluate_all('(rows) => rows.map(row => row.dataset.processingDocument)'),
                                                 ['DOC-CCC', 'DOC-DDD'])
                                expect(page.locator(selector).first).to_contain_text('李四')
                            processing = server.queue.claim()
                            workspace.update_job('DOC-CCC', progress=42, stage='ocr')
                            for page in (a, b):
                                expect(page.locator(selector).first).to_contain_text('正在解析')
                                expect(page.locator(selector).first).to_contain_text('42%')
                                expect(page.locator(selector).nth(1)).to_contain_text('前方 1 个任务')
                            a.locator('[data-app-page="intake"]').click()
                            expect(a.locator('#intake-processing-queue [data-processing-document]')).to_have_count(2)
                            workspace.update_job('DOC-CCC', status='failed', error='simulated failure')
                            server.queue.finish(processing)
                            for page, section in ((a, '#intake-processing-queue'), (b, '#queue-page')):
                                expect(page.locator(f'{section} [data-processing-document]')).to_have_count(1)
                                expect(page.locator(f'{section} [data-processing-document]')).to_have_attribute('data-processing-document', 'DOC-DDD')
                            b.screenshot(path='/tmp/sda-shared-processing-queue.png', full_page=True)
                            b.locator("#language-toggle").click()
                            expect(b.locator("#queue-page .queue-card h2")).to_have_text("Annotation tasks")
                            expect(b.locator("#queue-page .queue-import-card > .page-card-header h2")).to_have_text("PDF processing tasks")
                            expect(b.locator("#queue-page [data-processing-queue] h2")).to_have_text("Shared processing queue")
                            expect(b.locator("#queue-import-host #pdf-drop-zone strong")).to_have_text("Submit PDFs")
                            expect(b.locator("#queue-import-host #start-agent-button")).to_have_text("▶ Start parsing")
                            self.assertEqual(errors, [])
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    server.server_close()

if __name__ == '__main__':
    unittest.main()
