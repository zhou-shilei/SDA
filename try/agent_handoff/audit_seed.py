#!/usr/bin/env python3
"""Compare a real Template seed with its parsed table evidence and historical seed."""
import argparse
from collections import Counter
import json
from pathlib import Path

from build_snapshot import write_json


def audit(target, snapshot):
    target, snapshot = Path(target).resolve(), Path(snapshot).resolve()
    read = lambda path: json.loads(path.read_text())
    bundle = read(snapshot / 'snapshot.json')
    old_reader = Path(bundle['reader_dir'])
    old_seed = read(old_reader / 'seed.json')
    old_measurements = read(old_reader.parent / 'shared_seed/assay_measurements.json')
    seed = read(target / 'dataagent2/seed.json')
    sources = read(target / 'dataagent2/sources.json')
    measurements = read(target / 'shared_seed/assay_measurements.json')
    template = read(target / 'shared_seed/document_template.json')
    classes = read(target / 'shared_seed/table_classifications.json')
    quality = read(target / 'dataset1.quality.json')
    report = read(target / 'dataset1.report.json')
    raw_keys = {(m['value_ref'], m['metric']) for m in measurements}
    old_keys = {(m['value_ref'], m['metric']) for m in old_measurements}
    seed_keys = {(sources[str(o['s'])]['source_ref'], o['metric']) for o in seed['observations']}
    recovery = target / 'shared_seed/document_plan_recovery.json'
    result = {
        'document_id': bundle['document_id'],
        'scope': 'Real v6 TemplateAgent before Reader; history is a comparison, not ground truth',
        'historical_seed_counts': {k: len(v) for k, v in old_seed.items()},
        'current_seed_counts': {k: len(v) for k, v in seed.items()},
        'historical_parsed_measurements': len(old_measurements),
        'current_parsed_measurements': len(measurements),
        'current_seed_observations': len(seed['observations']),
        'parsed_measurements_missing_from_seed': [list(k) for k in sorted(raw_keys - seed_keys)],
        'newly_parsed_measurement_cells': [list(k) for k in sorted(raw_keys - old_keys)],
        'historical_measurement_cells_not_parsed_now': [list(k) for k in sorted(old_keys - raw_keys)],
        'measurements_by_table': dict(Counter(m['source_table'] for m in measurements)),
        'measurements_by_metric': dict(Counter(m['metric'] for m in measurements)),
        'seed_observations_by_table': dict(Counter(sources[str(o['s'])]['table_id'] for o in seed['observations'])),
        'template_status': template['status'],
        'table_decisions': report['table_decisions'],
        'pending_regions': [{'table_id': c['table_id'], 'region_id': r['region_id'],
                            'reason': r.get('validation_error')}
                           for c in classes for r in c['regions'] if r.get('execution_status') != 'ready'],
        'table_issues': [{'table_id': c['table_id'], 'decision': c['decision'], 'issues': c['issues']}
                         for c in classes if c['issues'] or c['decision'] == 'needs_review'],
        'warnings_by_code': dict(Counter(w['code'] for w in template.get('warnings', []))),
        'model_tasks': report['model_tasks'],
        'recovery_rejections': read(recovery).get('rejected', []) if recovery.is_file() else [],
        'literal_errors': quality['literal_errors'],
        'assembled_summary': report['assembled_summary'],
    }
    write_json(target / 'dataset1.audit.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in (
        'newly_parsed_measurement_cells', 'recovery_rejections', 'table_issues')}, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('--snapshot', type=Path, required=True)
    args = parser.parse_args()
    audit(args.target, args.snapshot)
