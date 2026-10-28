#!/usr/bin/env python3
"""Verify comparison outputs preserve the completed ESM baseline and denominators."""
import argparse
import csv
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import benchmark as b


def read(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, default=b.ROOT / 'results/pilot_20261027')
    args = p.parse_args(); out = args.out.resolve()
    if not out.is_relative_to(b.ROOT):
        raise ValueError('All outputs must stay in PPL')
    items = b.load_lines(out / 'sequences.jsonl')
    rows = read(out / 'model_comparison_per_sequence.csv')
    baseline = {x['id']: x for x in read(out / 'per_sequence.csv')}
    assert len(rows) == len(items) * 9
    assert len({(r['id'], r['model'], r['rule']) for r in rows}) == len(rows)
    assert set(baseline) == {x['id'] for x in items}
    checked = 0
    for r in rows:
        if r['localized_hit'] == 'True':
            assert r['flag'] == 'True'
        if r['parent_flag'] == 'True':
            assert r['new_flag'] == ''
        assert (r['combined_flag'] == 'True') == (r['anarci_flag'] == 'True' or r['flag'] == 'True')
        if r['scored'] == 'False':
            assert r['kind'] == 'invalid_O' and r['flag'] == ''
        if r['model'] != 'esm2':
            continue
        old = baseline[r['id']]
        assert (r['scored'] == 'True') == (old['esm_scored'] == 'True')
        if r['scored'] == 'False':
            continue
        field = {'site1000': 'esm_site1000_flag', 'max_cal95': 'esm_site_cal95_flag', 'mean_cal95': 'esm_mean_cal95_flag'}[r['rule']]
        assert r['flag'] == old[field], (r['id'], r['rule'], r['flag'], old[field])
        if r['rule'] == 'site1000':
            assert r['localized_hit'] == old['localized_hit']
        checked += 1
    invalid_summaries = [r for r in read(out / 'model_comparison_summary.csv') if r['kind'] == 'invalid_O']
    assert invalid_summaries and all(int(r['flag_n']) == 0 and int(r['combined_flag_count']) == int(r['n']) for r in invalid_summaries)
    manifest = json.loads((out / 'model_comparison_manifest.json').read_text())
    assert manifest['sequence_manifest_sha256'] == b.digest((out / 'sequences.jsonl').read_text())
    for name in ['model_comparison_regions.png', 'model_comparison_regions.pdf', 'model_comparison_examples.json',
                 'model_comparison_joint_workflows.csv', 'model_comparison_pair_bootstrap.csv']:
        assert (out / name).stat().st_size > 100
    assert (b.ROOT / 'REPORT_ophiuchus_zh.md').stat().st_size > 100
    result = {'rows': len(rows), 'samples': len(items), 'models': 3, 'rules': 3,
              'esm_baseline_decisions_preserved': checked, 'unchanged_input_manifest': True,
              'localization_implies_alarm': True, 'new_alarm_excludes_flagged_parents': True,
              'invalid_characters_not_in_model_denominators': True, 'report_and_figures_present': True}
    b.dump(out / 'model_comparison_verification.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
