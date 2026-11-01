"""Optional browser check with already-installed Playwright/Chromium; no server."""
import argparse
import hashlib
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def check(folder):
    folder = Path(folder).resolve()
    bundle = json.loads((folder / 'snapshot.json').read_text())
    verified = 0
    for item in [*bundle['artifacts'].values(),
                 *(item for c in bundle['calls'] for item in [c['request'], c['response'], *c['attempts']] if item)]:
        assert hashlib.sha256((folder / item['path']).read_bytes()).hexdigest() == item['sha256']
        verified += 1
    errors, remote = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1680, 'height': 1100})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: remote.append(r.url) if r.url.startswith(('http:', 'https:')) else None)
        page.goto((folder / 'index.html').as_uri(), wait_until='load')
        page.locator('#calls-nav').click()
        page.locator('#calls-view').wait_for(state='visible')
        assert page.locator('.call-item').count() == len(bundle['calls'])
        assert page.locator('.panel').count() == 3
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(folder / 'preview-desktop.png'), full_page=True)

        # Lazily expand and paginate an actual sources array.
        middle = page.locator('.panel').nth(1)
        middle.get_by_role('button', name='展开两层', exact=True).click()
        assert middle.locator('.json-children').count() > 2
        more = middle.get_by_role('button', name='继续展开', exact=False)
        if more.count():
            more.first.click()
        middle.get_by_role('button', name='折叠', exact=True).click()
        assert middle.locator('details[open]').count() == 0

        # Search must find a value AND reveal the collapsed node on click.
        page.locator('#json-search').fill('relation_index')
        page.locator('#search-form button').click()
        assert page.locator('.search-hit').count() >= 1
        page.locator('.search-hit').first.click()
        assert middle.locator('.highlight').count() == 1

        # Switching to the full request should expose its schema and settings.
        middle.locator('select').select_option('1')
        assert middle.locator('.json-key').filter(has_text='instructions').count() >= 1
        with page.expect_download() as captured:
            middle.get_by_role('button', name='下载', exact=True).click()
        saved = folder / 'download-check.json'
        captured.value.save_as(saved)
        assert json.loads(saved.read_text()) == bundle['calls'][0]['request']['value']
        saved.unlink()
        middle.locator('select').select_option('0')

        page.get_by_role('tab', name='字段比较').click()
        page.locator('#diff-form button').click()
        assert page.locator('#diff-summary').inner_text()
        page.locator('#left-path').fill('/this_path_does_not_exist')
        page.locator('#diff-form button').click()
        assert '路径不存在' in page.locator('#diff-summary').inner_text()
        page.get_by_role('tab', name='转换源码').click()
        page.locator('.source-card').filter(has_text='reader_payload').locator('summary').click()
        assert page.locator('.source-code:visible').count() == 1
        assert 'def reader_payload' in page.locator('.source-code:visible').inner_text()

        # The default question handoff should exactly match the received question.
        page.locator('#stage-filter').select_option('answer')
        assert page.locator('.call-item').count() == sum(c['stage'] == 'answer' for c in bundle['calls'])
        page.locator('.call-item').first.click()
        page.get_by_role('tab', name='字段比较').click()
        page.locator('#diff-form button').click()
        assert '完全一致' in page.locator('#diff-summary').inner_text()
        page.screenshot(path=str(folder / 'preview-question-diff.png'), full_page=True)
        page.get_by_role('tab', name='交接 JSON').click()
        page.locator('#call-filter').fill('no-such-call-000')
        assert page.locator('.call-item').count() == 0
        page.locator('#call-filter').fill('')

        missing = next((c for c in bundle['calls'] if c['response'] is None), None)
        if missing:
            page.locator('#stage-filter').select_option(missing['stage'])
            page.locator(f'.call-item[data-key="{missing["key"]}"]').click()
            assert '尚无已保存返回' in page.locator('#call-status').inner_text()
            assert page.locator('.panel').nth(2).get_by_text('本次快照没有保存这份 JSON。').is_visible()

        # Deep link survives reload; source filters are deliberately view-local.
        chosen = page.locator('#call-title').inner_text()
        page.reload(wait_until='load')
        assert page.locator('#call-title').inner_text() == chosen
        page.set_viewport_size({'width': 430, 'height': 930})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(folder / 'preview-mobile.png'), full_page=True)
        assert not errors, errors
        assert not remote, remote
        browser.close()
    result = {'calls': len(bundle['calls']), 'verified_file_hashes': verified,
              'file_url_offline': True, 'lazy_tree_search_download': True,
              'question_handoff_equal': True, 'source_view': True, 'missing_response': bool(missing),
              'deep_link': True, 'mobile_no_overflow': True, 'browser_errors': errors, 'remote_requests': remote}
    (folder / 'browser-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    check(parser.parse_args().snapshot)
