"""Exercise the map → contract → real data → call details navigation offline."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check(folder):
    folder = Path(folder).resolve()
    errors, remote = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1680, 'height': 1180})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: remote.append(r.url) if r.url.startswith(('https:', 'http:')) else None)
        page.goto((folder / 'index.html').as_uri(), wait_until='load')
        expect(page.locator('#overview-view')).to_be_visible()
        expect(page.locator('#calls-view')).not_to_be_visible()
        assert page.locator('.flow-node').count() == 18
        assert page.locator('.flow-node[data-stage="classification"]').count() == 0
        expect(page.locator('.flow-node[data-stage="table_input"]')).to_contain_text('表格输入打包')
        assert page.locator('.flow-node[data-stage="final"]').get_by_text('尚未生成', exact=False).count() == 1
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(folder / 'preview-flow.png'), full_page=True)
        stage_ids = page.locator('.flow-node').evaluate_all('(nodes) => nodes.map(n => n.dataset.stage)')
        for stage in stage_ids:
            page.locator(f'.flow-node[data-stage="{stage}"]').click()
            expect(page.locator('#stage-dialog')).to_be_visible()
            expect(page.locator('#overview-view')).to_be_visible()
            assert page.locator('#stage-dialog').evaluate('(d) => d.open')
            expect(page.locator('#stage-view')).to_be_visible()
            expect(page.locator('#stage-name')).not_to_be_empty()
            assert page.locator('.contract-card').count() == 2
            assert page.locator('.contract-card tbody tr').count() >= 3
            page.get_by_role('tab', name='本轮 JSON 与提示词').click()
            expect(page.locator('#stage-data-content')).to_be_visible()
            assert page.locator('.stage-example select').count() == 2
            if stage in ('template', 'reader', 'answer', 'consolidation'):
                expect(page.locator('#stage-schema-box')).to_be_visible()
                page.locator('#stage-schema-box > summary').click()
                assert page.locator('#stage-schema-tree .json-key').count() >= 2
            if stage in ('final', 'export'):
                assert '没有保存' in page.locator('#stage-output-example').inner_text()
            page.get_by_role('tab', name='Python 源码', exact=True).click()
            assert page.locator('#stage-source-content .source-card').count() >= 1
            if stage == 'question':
                assert '不调用轻量模型' in page.locator('#stage-purpose').inner_text()
                expect(page.locator('#stage-schema-box')).not_to_be_visible()
            page.locator('#stage-back').click()
            expect(page.locator('#stage-dialog')).not_to_be_visible()
            expect(page.locator('#overview-view')).to_be_visible()

        # Dismissal keeps the diagram and restores keyboard focus to its trigger.
        page.locator('.flow-node[data-stage="consolidation"]').click()
        expect(page.locator('#stage-dialog')).to_be_visible()
        expect(page.locator('#stage-name')).to_have_text('ActionAgent')
        page.keyboard.press('Escape')
        expect(page.locator('#stage-dialog')).not_to_be_visible()
        expect(page.locator('.flow-node[data-stage="consolidation"]')).to_be_focused()
        page.locator('.flow-node[data-stage="consolidation"]').click()
        expect(page.locator('#stage-dialog')).to_be_visible()
        page.mouse.click(2, 2)
        expect(page.locator('#stage-dialog')).not_to_be_visible()

        page.locator('.flow-node[data-stage="reader"]').click()
        expect(page.locator('#stage-name')).to_have_text('ReaderAgent')
        page.screenshot(path=str(folder / 'preview-reader-contract.png'), full_page=True)
        page.locator('#stage-data-action').click()
        expect(page.locator('#stage-data-content')).to_be_visible()
        page.screenshot(path=str(folder / 'preview-reader-data.png'), full_page=True)
        page.locator('#stage-call-action').click()
        expect(page.locator('#calls-view')).to_be_visible()
        assert page.locator('.call-item').count() == 63
        assert 'c1' in page.locator('#call-title').inner_text()
        page.locator('#overview-nav').click()
        expect(page.locator('#overview-view')).to_be_visible()
        page.locator('.chunk-card').nth(1).click()
        expect(page.locator('#calls-view')).to_be_visible()
        assert 'c2' in page.locator('#call-title').inner_text()

        # Browser back restores the actual map rather than staying in raw JSON.
        page.go_back()
        expect(page.locator('#overview-view')).to_be_visible()
        page.locator('.flow-node[data-stage="template"]').click()
        expect(page.locator('#stage-name')).to_have_text('TemplateAgent')
        page.reload(wait_until='load')
        expect(page.locator('#stage-name')).to_have_text('TemplateAgent')
        page.locator('#stage-back').click()
        expect(page.locator('#overview-view')).to_be_visible()
        page.locator('#reference-box summary').click()
        expect(page.locator('#reference-figure')).to_be_visible()
        assert page.locator('#reference-figure').evaluate('(img) => img.complete && img.naturalWidth > 0')
        page.locator('#reference-box summary').click()
        page.set_viewport_size({'width': 430, 'height': 930})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(folder / 'preview-flow-mobile.png'), full_page=True)
        page.locator('.flow-node[data-stage="consolidation"]').click()
        expect(page.locator('#stage-dialog')).to_be_visible()
        assert page.locator('#stage-dialog').evaluate('(d) => d.scrollWidth <= d.clientWidth')
        page.screenshot(path=str(folder / 'preview-action-modal-mobile.png'))
        page.keyboard.press('Escape')
        assert not errors, errors
        assert not remote, remote
        browser.close()
    result = {'clickable_stages': len(stage_ids), 'default_overview': True,
              'input_output_definitions': True, 'stage_samples_and_schema': True,
              'stage_to_real_calls': True, 'original_v6_figure': True,
              'source_navigation': True, 'browser_back_and_reload': True,
              'mobile_no_body_overflow': True, 'errors': errors, 'remote_requests': remote}
    (folder / 'flow-browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    check(parser.parse_args().snapshot)
