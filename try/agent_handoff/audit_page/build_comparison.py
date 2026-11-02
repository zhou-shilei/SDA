#!/usr/bin/env python3
"""Package an offline three-way comparison without publishing any patent data."""
import argparse
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent


def build(experiment, destination):
    comparison = json.loads((experiment / 'comparison.json').read_text())
    if comparison['experiment']['status'] != 'complete':
        raise ValueError('Do not present unfinished trials as completed results')
    if comparison['experiment']['code_changed_during_run']:
        raise ValueError('Source code changed during the controlled experiment')
    if comparison['reference']['common_numeric_measurements'] != 221:
        raise ValueError('Reference scope changed; review explanatory copy')
    audit = comparison['experiment']['arms']
    if audit['with_text']['tables_sha256'] != audit['tables_only']['tables_sha256']:
        raise ValueError('Table inputs differ between arms')
    packed = json.dumps(comparison, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    copies = {'tables-only-dataset1.json': experiment / 'tables_only/dataset1.json',
              'with-text-dataset1.json': experiment / 'with_text/dataset1.json',
              'full-pdf-model-reference.json': Path(comparison['experiment']['reference']),
              'comparison.json': experiment / 'comparison.json'}
    offline_datasets = {filename: json.loads(source.read_text()) for filename, source in copies.items()
                        if filename != 'comparison.json'}
    downloads = json.dumps(offline_datasets, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    pieces = {'STYLE': (HERE / 'style.css').read_text(), 'APP': (HERE / 'comparison.js').read_text(),
              'DATA': packed, 'DOWNLOADS': downloads}
    html = re.sub(r'@@(STYLE|APP|DATA|DOWNLOADS)@@', lambda m: pieces[m[1]], (HERE / 'comparison.html').read_text())
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'text-comparison.html').write_text(html)
    (destination / 'comparison-data').mkdir(exist_ok=True)
    for filename, source in copies.items():
        shutil.copyfile(source, destination / 'comparison-data' / filename)
    index = destination / 'index.html'
    source = index.read_text()
    entry = ('<aside id="text-comparison-entry" style="margin:0 0 18px;padding:16px 20px;'
             'border:1px solid #c6d7c0;border-left:4px solid #236b53;border-radius:5px;background:#edf2e6">'
             '<a href="text-comparison.html" style="font-weight:650;font-size:15px;text-decoration:none">'
             '新增真实对照：仅表格 vs 表格＋正文 vs 完整 PDF 模型参考 →</a>'
             '<p style="font-size:12px;color:#67756b;margin-top:5px">'
             '同样的 28 张表、共同的 221 个数值结果；切换比较口径，查看缺项和输入成本。参考结果非人工标准。</p></aside>')
    if 'id="text-comparison-entry"' in source:
        source = re.sub(r'<aside id="text-comparison-entry".*?</aside>', lambda _: entry, source, count=1, flags=re.S)
    else:
        marker = '    <aside id="template-audit-entry"'
        if marker not in source:
            marker = '    <section id="overview-view">'
        if marker not in source:
            raise ValueError('Snapshot navigation marker missing')
        source = source.replace(marker, '    ' + entry + '\n' + marker, 1)
    index.write_text(source)
    print(destination / 'text-comparison.html')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    parser.add_argument('--destination', type=Path, default=HERE.parent / 'snapshots/49-Sructure')
    args = parser.parse_args()
    build(args.experiment.resolve(), args.destination.resolve())
