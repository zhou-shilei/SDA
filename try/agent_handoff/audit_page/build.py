#!/usr/bin/env python3
"""Build an offline explanation from the saved audit and its original evidence."""
import argparse
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
HANDOFF = HERE.parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def build(assessment, output):
    audit = read(assessment)
    run = Path(audit['run']['path'])
    current = run / 'revalidated-v2'
    request_paths = [Path(p) for p in audit['provenance'] if Path(p).name == 'input.json']
    if len(request_paths) != 1:
        raise ValueError('Expected one original request in audit provenance')
    request = read(request_paths[0])
    passages = {p['ref']: p['text'] for p in request['payload']['passages']}
    dataset = read(current / 'dataset1.json')
    sources = read(current / 'dataagent2/sources.json')
    entities = {e['name']: e for e in dataset['entities']}
    checks = audit['inspected_ab1_to_ab14_mapping_subset']['checks']
    examples = []
    for n in range(1, 15):
        name = f'AB-{n}'
        mappings = [c for c in checks if c['entity'] == name]
        if len(mappings) != 2 or len({m['source'] for m in mappings}) != 1:
            raise ValueError(f'Expected exactly one paired-chain source for {name}')
        example = {'name': name, 'source': mappings[0]['source'],
                   'quote': passages[mappings[0]['source']], 'mappings': mappings,
                   'dataset_state': {'name': name, 'sequences': {
                       chain: entities[name]['sequences'][chain] for chain in ('VH', 'VL')}}}
        examples.append(example)
    donor_example = []
    for observation in dataset['observations']:
        if observation['item'] == entities['AB-1']['id'] and observation['table'] == 'p0207-r0001':
            donor_example.append({'value': observation['value'], 'unit': observation['unit'],
                                  'refs': [{'ref': sources[str(ref)]['source_ref'],
                                            'text': sources[str(ref)]['text']} for ref in observation['refs']]})
    if len(donor_example) != 5:
        raise ValueError('Expected five sourced donor measurements')
    # Fixed explanatory counts must match the evidence; changed runs need new copy.
    counts = audit['revalidated_dataset']
    if (counts['entities'], counts['links'], counts['experiments'], counts['observations'],
        len(audit['measurement_coverage']['missing_cells'])) != (45, 18, 16, 197, 24):
        raise ValueError('Audit counts changed; update the explanatory text before rebuilding')
    data = {'audit': audit, 'examples': examples, 'donor_example': donor_example}
    packed = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    pieces = {'STYLE': (HERE / 'style.css').read_text(), 'APP': (HERE / 'app.js').read_text(), 'DATA': packed}
    html = re.sub(r'@@(STYLE|APP|DATA)@@', lambda m: pieces[m[1]], (HERE / 'page.html').read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding='utf-8')
    (output.parent / 'template-audit-data.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    index = output.parent / 'index.html'
    if index.exists():
        entry = ('<aside id="template-audit-entry" style="margin:0 0 22px;padding:16px 20px;'
                 'border:1px solid #c6d7c0;border-left:4px solid #236b53;border-radius:5px;background:#edf2e6">'
                 '<a href="template-audit.html" style="font-weight:650;font-size:15px;text-decoration:none">'
                 '最新 v6 识别质量说明：为什么模型识别到了，Dataset 仍有缺口？ →</a>'
                 '<p style="font-size:12px;color:#67756b;margin-top:5px">'
                 '2026-10-31 固定审计 · 通过 AB-1、24 个漏项和供者条件，逐步看懂实际结果。与下方历史调用快照分开。</p></aside>')
        source = index.read_text(encoding='utf-8')
        if 'id="template-audit-entry"' in source:
            source = re.sub(r'<aside id="template-audit-entry".*?</aside>', lambda _: entry, source, count=1, flags=re.S)
        else:
            marker = '    <section id="overview-view">'
            if marker not in source:
                raise ValueError('Snapshot overview marker not found')
            source = source.replace(marker, '    ' + entry + '\n' + marker, 1)
        index.write_text(source, encoding='utf-8')
    print(output.resolve())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assessment', type=Path, default=HANDOFF / 'template-v6-assessment-20261031.json')
    parser.add_argument('--output', type=Path, default=HANDOFF / 'snapshots/49-Sructure/template-audit.html')
    args = parser.parse_args()
    build(args.assessment, args.output)
