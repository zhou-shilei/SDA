"""Check the default current Label input and explicit historical view offline."""
import argparse
import json
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


def check(folder):
    folder = Path(folder).resolve()
    prepared = json.loads((folder / 'current_label_inputs.json').read_text())
    errors, remote = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1680, 'height': 1180})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: remote.append(r.url) if r.url.startswith(('http:', 'https:')) else None)
        page.goto((folder / 'index.html').as_uri() + '#stage/classification')
        expect(page.locator('#classification-view-select')).to_have_value('current')
        for i in range(page.locator('#render-table-select option').count()):
            page.locator('#render-table-select').select_option(str(i))
            current = page.evaluate('HandoffPreview.current().request.value.payload')
            table = current['tables'][0]
            assert current == prepared['tables'][table['table_id']]['payload']
            assert len(table['rows']) <= 1
            assert table['input_scope'] == 'headers_and_one_data_row'
            assert not {'cells', 'bbox', 'confidence', 'image_ref'} & table.keys()
            cells = table['headers'] + [cell for row in table['rows'] for cell in row]
            cells.sort(key=lambda cell: tuple(map(int, re.search(r':R(\d+)C(\d+)$', cell[0]).groups())))
            assert page.locator('.render-cell').all_text_contents() == [t if t else '（空）' for _, t in cells]
            expect(page.locator('.render-output-card')).to_contain_text('没有新版分类结果')
            assert page.evaluate("HandoffPreview.choices('output')[0].missing")
        page.locator('#render-table-select').select_option('0')
        page.get_by_role('tab', name='本轮 JSON 与提示词').click()
        expect(page.locator('#stage-input-example select')).to_contain_text('当前 v5 待发送输入')
        page.locator('#stage-prompt-box summary').click()
        expect(page.locator('#stage-prompt')).to_contain_text('AT MOST ONE')
        expect(page.locator('#stage-output-example')).to_contain_text('本次快照没有保存这份 JSON')
        page.locator('#classification-view-select').select_option('history')
        expect(page.locator('#stage-data-content')).to_be_visible()  # Keep the active tab when changing versions.
        assert 'cells' in page.evaluate('HandoffPreview.current().request.value.payload.tables[0]')
        expect(page.locator('#stage-input-example select')).to_contain_text('历史实际请求')
        assert not page.evaluate('HandoffPreview.current().prepared || false')
        page.locator('#classification-view-select').select_option('current')
        page.reload()
        expect(page.locator('#classification-view-select')).to_have_value('current')
        expect(page.locator('.render-version-note')).to_contain_text('未执行模型结构复核')
        page.set_viewport_size({'width': 430, 'height': 930})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.set_viewport_size({'width': 1680, 'height': 1180})
        page.screenshot(path=str(folder / 'preview-current-label.png'), full_page=True)
        assert not errors, errors
        assert not remote, remote
        browser.close()
    result = {'current_tables': len(prepared['tables']), 'default_v5_input': True,
        'no_historical_output_reuse': True, 'current_prompt': True, 'explicit_history_switch': True,
        'mobile_no_overflow': True, 'errors': errors, 'remote_requests': remote}
    (folder / 'current-label-browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    check(parser.parse_args().snapshot)
