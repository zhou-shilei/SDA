"""Verify rendered tables, exact pairing, source focus, original image and escaped text."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check(folder):
    folder = Path(folder).resolve()
    snap = json.loads((folder / 'preview_snapshot.json').read_text())
    errors, remote = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1680, 'height': 1180})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: remote.append(r.url) if r.url.startswith(('http:', 'https:')) else None)
        page.goto((folder / 'index.html').as_uri() + '?label_view=history#stage/classification', wait_until='load')
        expect(page.locator('#stage-preview-content')).to_be_visible()
        assert page.locator('#render-table-select option').count() == len(snap['cases'])
        for i, case in enumerate(snap['cases']):
            page.locator('#render-table-select').select_option(str(i))
            result = snap['classification']['value'][case['classification_index']]
            table = next(t for t in snap['requests'][case['request_key']]['value']['payload']['tables'] if t['table_id'] == case['table_id'])
            actual = page.locator('.render-cell').all_text_contents()
            assert actual == [c['text'] if c['text'] else '（空）' for c in table['cells']], case['table_id']
            assert case['table_id'] in page.locator('.render-input-card h3').inner_text() or table.get('caption')
            assert case['table_id'] in page.locator('.render-output-card').inner_text()
            assert str(result['page']) in page.locator('.render-output-card').inner_text()
        page.locator('#render-table-select').select_option('0')
        page.locator('.render-cell').first.click()
        assert 'p0196-r0001:R0C0' in page.locator('.render-cell-detail').inner_text()
        assert '[73.78080900226058,83.89126873831458,222.8223647106828,141.3599853515624]' in page.locator('.render-cell-detail').inner_text()
        page.locator('.render-issue .render-ref').first.click()
        assert page.locator('.cell-selected').count() == 1
        assert 'R1C2' in page.locator('.render-cell-detail').inner_text()
        page.locator('.render-original > summary').click()
        expect(page.locator('.render-original img')).to_be_visible()
        assert page.locator('.render-original img').evaluate('(img)=>img.complete&&img.naturalWidth>0')
        page.locator('.render-original > summary').click()
        page.screenshot(path=str(folder / 'preview-classification-render.png'), full_page=True)

        # The specific mismatch the user reported must now be the same 262-page table on both sides.
        i = next(i for i, c in enumerate(snap['cases']) if c['table_id'] == 'p0262-r0001')
        page.locator('#render-table-select').select_option(str(i))
        expect(page.locator('.render-output-card')).to_contain_text('p0262-r0001')
        assert '58 行 × 4 列' in page.locator('#stage-preview-content').inner_text()
        page.get_by_role('tab', name='本轮 JSON 与提示词').click()
        assert 'p0262-r0001' in page.locator('#stage-input-example select').inner_text()
        assert 'p0262-r0001' in page.locator('#stage-output-example select').inner_text()
        assert page.locator('#stage-output-example option').count() == 2  # Normalized result / original attempt.
        page.reload(wait_until='load')
        expect(page.locator('.render-output-card')).to_contain_text('p0262-r0001')
        page.screenshot(path=str(folder / 'preview-classification-page262.png'), full_page=True)

        # Hostile text stays text; the preview must not interpret OCR as HTML.
        page.evaluate('''() => {
          const p=HandoffPreview.data;
          const c=p.cases.find(c=>c.table_id==='p0262-r0001');
          p.requests[c.request_key].value.payload.tables[0].cells[0].text='<img src="https://invalid.example/test" onerror="window.injected=true">';
        }''')
        page.locator('#render-table-select').select_option('0')
        page.locator('#render-table-select').select_option(str(i))
        assert '<img src=' in page.locator('.render-cell').first.inner_text()
        assert page.locator('.render-grid-scroll img').count() == 0
        assert not page.evaluate('Boolean(window.injected)')
        page.set_viewport_size({'width': 430, 'height': 930})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not errors, errors
        assert not remote, remote
        browser.close()
    out = {'rendered_tables': len(snap['cases']), 'exact_cell_texts': True, 'paired_input_output': True,
           'page262_regression': True, 'full_bbox': True, 'evidence_cell_focus': True,
           'original_crop': True, 'normalized_and_raw_separate': True,
           'selection_survives_reload': True, 'ocr_text_escaped': True,
           'mobile_no_body_overflow': True, 'errors': errors, 'remote_requests': remote}
    (folder / 'preview-browser-check.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    check(parser.parse_args().snapshot)
