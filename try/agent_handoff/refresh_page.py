#!/usr/bin/env python3
"""Refresh presentation/current definitions without recapturing frozen run data."""
import argparse
import json
from pathlib import Path

from build_snapshot import ROOT, render_page, write_json
from flow_snapshot import capture_current_contract


def refresh(folder):
    folder = Path(folder).resolve()
    def read(name):
        path = folder / name
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    bundle = read('snapshot.json')
    if bundle is None:
        raise ValueError('缺少 snapshot.json，无法刷新静态页')
    current = capture_current_contract(ROOT)
    prepared = read('current_label_inputs.json')
    if prepared:
        current['label_inputs'] = prepared
    html = render_page(bundle, read('flow_snapshot.json'), read('preview_snapshot.json'), current)
    write_json(folder / 'current_contract.json', current)
    pending = folder / 'index.html.tmp'
    pending.write_text(html, encoding='utf-8')
    pending.replace(folder / 'index.html')
    return folder / 'index.html'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    print(refresh(parser.parse_args().snapshot))
