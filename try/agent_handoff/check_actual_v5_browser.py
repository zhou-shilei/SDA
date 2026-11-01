"""Verify real v5 Label pairs, the complete Template input, and bounded answers."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


def check(folder):
    folder = Path(folder).resolve()
    preview = json.loads((folder / 'preview_snapshot.json').read_text())
    bundle = json.loads((folder / 'snapshot.json').read_text())
    flow = json.loads((folder / 'flow_snapshot.json').read_text())
    assert flow['samples']['template_request']['sampled'] is False
    request = flow['samples']['template_request']
    assert request['value'] == json.loads(Path(request['original_path']).read_text())
    for key, request in preview['requests'].items():
        assert request['value']['task'] == 'table-classification-v5'
        assert hashlib.sha256(Path(request['original_path']).read_bytes()).hexdigest() == request['sha256']
    errors, remote = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1680, 'height': 1180})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: remote.append(r.url) if r.url.startswith(('http:', 'https:')) else None)
        page.goto((folder / 'index.html').as_uri()+'#stage/classification')
        expect(page.locator('#classification-view-select')).to_have_value('actual')
        expect(page.locator('#run-version-message')).to_contain_text('本次真实试跑')
        for i, entry in enumerate(preview['cases']):
            assert entry['request_key'], entry
            page.locator('#render-table-select').select_option(str(i))
            actual = page.evaluate('HandoffPreview.current().request.value.payload')
            expected = preview['requests'][entry['request_key']]['value']['payload']
            assert actual == expected
            table = next(t for t in expected['tables'] if t['table_id']==entry['table_id'])
            assert len(table['rows']) <= 1 and 'cells' not in table
            cells = table['headers']+[c for row in table['rows'] for c in row]
            cells.sort(key=lambda c: tuple(map(int,re.search(r':R(\d+)C(\d+)$',c[0]).groups())))
            assert page.locator('.render-cell').all_text_contents() == [t if t else '（空）' for _,t in cells]
            expect(page.locator('.render-output-card')).to_contain_text(entry['table_id'])
            assert page.evaluate('HandoffPreview.current().result.table_id')==entry['table_id']
        page.locator('#render-table-select').select_option('0')
        expect(page.locator('.render-output-card')).to_contain_text('序列定义表')
        page.get_by_role('tab',name='本轮 JSON 与提示词').click()
        expect(page.locator('#stage-input-example select')).to_contain_text('本次 v5 实际请求')
        assert page.locator('#stage-output-example option').count() == 2
        page.locator('#stage-output-example select').select_option('1')
        expect(page.locator('#stage-output-example .example-scope')).to_contain_text('原始 answer.json')
        page.locator('#stage-back').click()
        page.locator('[data-stage="template"]').click()
        page.get_by_role('tab',name='本轮 JSON 与提示词').click()
        expect(page.locator('#stage-data-note')).to_contain_text('完整 payload')
        page.locator('#stage-back').click()
        page.locator('[data-stage="classification"]').click()
        page.set_viewport_size({'width':430,'height':930})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.set_viewport_size({'width':1680,'height':1180})
        page.screenshot(path=str(folder/'preview-actual-label.png'),full_page=True)
        assert not errors, errors
        assert not remote, remote
        browser.close()
    answers = [c for c in bundle['calls'] if c['stage']=='answer']
    assert len(answers) <= bundle['run_context']['max_questions']
    result={'real_label_pairs':len(preview['cases']), 'default_actual_v5':True,
        'complete_template_request':True,'raw_and_normalized_separate':True,
        'reader_calls':sum(c['stage']=='reader' for c in bundle['calls']),
        'answer_calls':len(answers),'errors':errors,'remote_requests':remote}
    (folder/'actual-v5-browser-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot',type=Path)
    check(parser.parse_args().snapshot)
