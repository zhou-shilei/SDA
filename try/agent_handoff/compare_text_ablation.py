#!/usr/bin/env python3
"""Source-scoped comparison; the full-PDF model result is NOT human truth."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

TABLES = {'p0205-r0001': 'Table 7', 'p0206-r0002': 'Table 8',
          'p0206-r0001': 'Table 9', 'p0207-r0002': 'Table 10',
          'p0207-r0001': 'Table 11', 'p0208-r0001': 'Table 12',
          'p0208-r0002': 'Table 13'}


def read(path):
    return json.loads(path.read_text())


def entity_name(value):
    name = str(value).strip().casefold()
    return 'reference antibody' if name == 'reference' else name


def metric_name(value):
    # Preserve the crucial KD (equilibrium) / kd (off-rate) distinction.
    return {'ka': 'kon', 'kd': 'koff'}.get(value, value)


def number(value):
    value = str(value).replace(' ', '').replace('−', '-').replace('×10^', 'e')
    try:
        return str(Decimal(value).normalize())
    except InvalidOperation:
        return None


def value_key(value):
    value = re.sub(r'\s+', ' ', str(value)).strip()
    ci = re.fullmatch(r'([+-]?[\d.eE]+)\s*\(\s*([+-]?[\d.eE]+)\s+to\s+([+-]?[\d.eE]+)\s*\)', value)
    if ci:
        values = tuple(number(v) for v in ci.groups())
        return ('ci', *values) if all(v is not None for v in values) else None
    sign = re.match(r'^(<=|>=|<|>|≤|≥|~|≈)?\s*(.*)$', value)
    parsed = number(sign[2])
    return ('scalar', sign[1] or '=', parsed) if parsed is not None else None


def unit_name(value):
    text = str(value or '').replace('\\mu', 'u').replace('µ', 'u').replace('μ', 'u')
    return re.sub(r'\s+', '', text)


def species_name(value):
    text = str(value or '').casefold()
    if 'cyno' in text:
        return 'cynomolgus'
    if 'human' in text:
        return 'human'
    return ''


def donor_name(value):
    match = re.search(r'\d+', str(value or ''))
    return match[0] if match else ''


def method_family(value):
    text = str(value or '').casefold()
    if 'kinexa' in text or 'kinetic exclusion' in text:
        return 'kinexa'
    if 'primary_cell_cytokine' in text or 'mdc' in text or 'ccl17' in text:
        return 'primary_mdc_ccl17'
    if 'cell_reporter' in text or 'baf3' in re.sub(r'[^a-z0-9]', '', text) or 'luciferase' in text:
        return 'cell_reporter'
    if 'elisa' in text:
        return 'blocking_elisa' if ('block' in text or 'receptor' in text) else 'binding_elisa'
    return ''


def base_key(row):
    return (row['table'], row['entity'], row['metric'], tuple(row['value']), row['unit'])


def key(row, extra=()):
    return (*base_key(row), *(row.get(k, '') for k in extra))


def matches(reference, candidate, extra=()):
    expected, actual = Counter(key(r, extra) for r in reference), Counter(key(r, extra) for r in candidate)
    matched = sum((expected & actual).values())
    return {'matched': matched, 'reference': len(reference), 'candidate': len(candidate),
            'reference_coverage': matched / len(reference) if reference else None,
            'candidate_agreement': matched / len(candidate) if candidate else None,
            'missing_count': sum((expected - actual).values()),
            'unmatched_candidate_count': sum((actual - expected).values())}


def reference_rows(document):
    experiments = {e['id']: e for e in document['experiments']}
    rows, excluded = [], []
    for m in document['measurements']:
        table = next((e['table'] for e in m.get('evidence', []) if e.get('table') in TABLES.values()), None)
        if table is None:
            continue
        parsed = value_key(m['value'])
        if parsed is None:
            excluded.append({'id': m['id'], 'value': m['value'], 'status': m.get('status')})
            continue
        exp = experiments[m['experiment_id']]
        condition = m.get('condition') or {}
        donor = condition.get('donor') if isinstance(condition, dict) else None
        rows.append({'id': m['id'], 'table': table, 'entity': entity_name(m['entity_id']),
            'metric': metric_name(m['metric']), 'value': parsed, 'raw_value': str(m['value']),
            'unit': unit_name(m['unit']), 'species': species_name(exp.get('species')),
            'method': method_family(exp.get('method')), 'donor': donor_name(donor),
            'experiment': m['experiment_id'], 'source': m.get('evidence')})
    return rows, excluded


def candidate_rows(document, sources):
    entities = {e['id']: e for e in document['entities']}
    experiments = {e['id']: e for e in document['experiments']}
    rows = []
    for m in document['observations']:
        table = TABLES.get(m.get('table'))
        if not table:
            continue
        exp = experiments.get(m.get('experiment'), {})
        parsed = value_key(m['value'])
        if parsed is None:
            continue
        source = sources[str(m['s'])]
        rows.append({'id': m['id'], 'table': table, 'entity': entity_name(entities[m['item']]['name']),
            'metric': metric_name(m['metric']), 'value': parsed, 'raw_value': m['value'],
            'unit': unit_name(m.get('unit')), 'species': species_name(exp.get('species')),
            'method': method_family(exp.get('method')), 'donor': donor_name(m.get('donor')),
            'experiment': m.get('experiment'), 'source': source['source_ref']})
    return rows


def compare(output):
    experiment = read(output / 'experiment.json')
    reference = read(Path(experiment['reference']))
    original_state = read(Path(experiment['original_run']) / 'shared_seed/agent_state.json')
    prepared = read(Path(experiment['original_run']) / 'shared_seed/sda_input.json')
    # The OCR input hashes the normalized processing PDF; the model reference
    # hashes the original PDF. Verify the recorded relationship, not equality
    # of these two different file encodings.
    jobs = read(ROOT / '.studio/jobs.json')
    structured = Path(jobs[prepared['document_id']]['structured_dir'])
    layout = read(structured / 'layout' / (prepared['document_id'] + '_manifest.json'))
    reference_report = read(Path(experiment['reference']).parent / 'experiment_report.json')
    original_pdf = Path(layout['source_pdf'])
    original_hash = hashlib.sha256(original_pdf.read_bytes()).hexdigest()
    if not (prepared['pdf_sha256'] == layout['processing_pdf_sha256']
            and original_hash == layout['source_pdf_sha256'] == reference_report['审计']['source_pdf_sha256']):
        raise ValueError('The table OCR and selected reference do not trace to the same source PDF')
    refrows, excluded = reference_rows(reference)
    core_entities = {f'ab-{n}' for n in range(1, 15)} | {'reference antibody'}
    core_links = {(entity_name(l['entity_id']), l['region_type'], int(l['sequence_id'].split('-')[-1]))
                  for l in reference['sequence_links'] if l['region_type'] in ('VH', 'VL')
                  and entity_name(l['entity_id']) in core_entities}
    refdefs = {s['seq_id_no']: s['sequence'] for s in reference['sequences']}
    expected_pairs = {name for name in core_entities if all(any(
        n == name and ch == chain and seq in refdefs for n, ch, seq in core_links) for chain in ('VH', 'VL'))}
    result = {'reference_label': 'Previous full-PDF MODEL extraction; NOT human ground truth',
        'methodology': {'controlled_tables': list(TABLES), 'numeric_reference_results': len(refrows),
            'excluded_common_table_non_numeric_results': excluded,
            'matching': 'Multiset exact agreement on table, literal entity (Reference alias only), endpoint, normalized numeric value including CI and unit. No unit rescaling or suffix merging.',
            'semantic_matching': 'Add species, broad assay-method family and explicit donor fields. Missing fields do not count as correct; retained header refs are not silently converted to donor labels.',
            'sequence_scope': 'Direct VH/VL links for unsuffixed AB-1..AB-14 and Reference Antibody only; CDR, full-chain and provisional inherited variant links are excluded from this metric.',
            'limits': ['Single fresh table-only call versus one reused with-text call; stochastic variability and model-service timing are not estimated.',
                       'Agreement with a model reference is not factual accuracy; disagreements need original-source adjudication.',
                       'Reader/Answer and new PDF/OCR were not run. Follow-up question counts are host-generated gaps, not measured future PDF reads.']},
        'source_pdf_provenance': {'same_original_pdf_verified': True,
            'original_pdf_sha256': original_hash, 'processing_pdf_sha256': prepared['pdf_sha256'],
            'manifest': str(structured / 'layout' / (prepared['document_id'] + '_manifest.json')),
            'reference_dataset_sha256': hashlib.sha256(Path(experiment['reference']).read_bytes()).hexdigest()},
        'reference': {'entities_all_scopes': len(reference['entities']), 'measurements_all_scopes': len(reference['measurements']),
            'sequences_all_scopes': len(reference['sequences']), 'experiments_all_scopes': len(reference['experiments']),
            'common_numeric_measurements': len(refrows), 'by_table': dict(Counter(r['table'] for r in refrows)),
            'core_variable_links': len(core_links), 'core_variable_pairs_with_reference_definitions': len(expected_pairs)},
        'arms': {}, 'experiment': experiment}
    from dataagent2.inputs import load_input
    from dataagent2.pipeline import question_tasks
    for name in experiment['arms']:
        arm = output / name
        document, sources = read(arm / 'dataset1.json'), read(arm / 'dataagent2/sources.json')
        template = read(arm / 'shared_seed/document_template.json')
        candidate = candidate_rows(document, sources)
        actual_links = {(entity_name(e['canonical_name']), l['chain'], l['seq_id'])
            for l in template['sequence_links'] for e in template['entity_template']['entities']
            if e['entity_id'] == l['entity_id'] and l['scope'] == 'variable' and entity_name(e['canonical_name']) in core_entities}
        covered = core_links & actual_links
        by_table = {t: matches([r for r in refrows if r['table'] == t], [r for r in candidate if r['table'] == t])
                    for t in TABLES.values()}
        core_candidate_pairs = sum(all(e['sequences'][chain]['status'] == 'supported' for chain in ('VH', 'VL'))
            for e in document['entities'] if entity_name(e['name']) in core_entities)
        source_pool = read(arm / 'controlled_input.json')['payload']
        pages = {p['page'] for p in [*source_pool['tables'], *source_pool['passages']] if p.get('page')}
        raw_path = next((arm / 'shared_seed/requests').glob('*/attempt-*/answer.json'), None)
        if name == 'with_text':
            old = Path(experiment['original_run'])
            raw_path = next((old / 'shared_seed/requests').glob('*/attempt-*/answer.json'))
        raw = read(raw_path) if raw_path else {}
        raw_links = {(entity_name(e['canonical_name']), l['chain'], l['seq_id'])
            for l in raw.get('sequence_links', []) for e in raw.get('entities', [])
            if e['entity_id'] == l['entity_id'] and l['scope'] == 'variable' and entity_name(e['canonical_name']) in core_entities}
        base = matches(refrows, candidate)
        missing = Counter(base_key(r) for r in refrows) - Counter(base_key(r) for r in candidate)
        extra = Counter(base_key(r) for r in candidate) - Counter(base_key(r) for r in refrows)
        armresult = {'total_entities': len(document['entities']), 'total_observations': len(document['observations']),
            'total_experiments': len(document['experiments']), 'total_links': len(document['links']),
            'sequence_definitions': len(document['sequences']), 'complete_variable_pairs_all': document['summary']['complete_pairs'],
            'measurement_base_agreement': base, 'with_species': matches(refrows, candidate, ('species',)),
            'with_method_family': matches(refrows, candidate, ('method',)),
            'with_species_method_and_donor': matches(refrows, candidate, ('species', 'method', 'donor')),
            'donor_table_explicit': matches([r for r in refrows if r['table'] == 'Table 11'],
                                          [r for r in candidate if r['table'] == 'Table 11'], ('donor',)),
            'by_table': by_table,
            'core_variable_links': {'reference': len(core_links), 'raw_model_agreement': len(core_links & raw_links),
                'retained_agreement': len(covered), 'retained_candidates': len(actual_links),
                'missing': [list(x) for x in sorted(core_links - actual_links)]},
            'core_variable_pairs': {'reference': len(expected_pairs), 'candidate': core_candidate_pairs},
            'input_pages_represented': len(pages), 'passages_supplied': len(source_pool['passages']),
            'host_followup_questions': len(question_tasks(document, load_input(arm / 'shared_seed'))),
            'input_chars': experiment['arms'][name]['payload_chars'],
            'llm_usage': experiment['arms'][name]['llm'].get('usage', {}),
            'original_llm_elapsed_seconds': experiment['arms'][name]['llm'].get('total_elapsed_seconds'),
            'missing_reference_rows': [r for r in refrows if base_key(r) in missing],
            'unmatched_candidate_rows': [r for r in candidate if base_key(r) in extra],
            'all_normalized_rows': candidate}
        armresult['explicit_missing_metadata_on_common_rows'] = {
            field: sum(not row.get(field) for row in candidate) for field in ('method', 'species', 'donor')}
        result['arms'][name] = armresult
    result['reference_rows'] = refrows
    path = output / 'comparison.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({name: {k: v for k, v in data.items() if k not in (
        'missing_reference_rows', 'unmatched_candidate_rows', 'all_normalized_rows', 'by_table')}
        for name, data in result['arms'].items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    compare(parser.parse_args().output.resolve())
