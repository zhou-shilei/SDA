#!/usr/bin/env python3
"""Compare a fresh table-only Template call with a saved table+text call."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(HERE)]

from dataagent.llm_runtime import CodexProvider
from dataagent.llm_table_agents import LLMTemplateAgent
from dataagent.records import export_measurements
from dataagent2.inputs import load_input
from run_quick import save_dataset1


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def run(original, output, *, title_manifest=None):
    if output.exists():
        raise ValueError('Use a new output directory; previous experiments are never overwritten')
    if os.environ.get('DATASETAGENT_REVIEW_GUIDANCE'):
        raise ValueError('Human guidance would confound this table/text ablation')
    output.mkdir(parents=True)
    request_paths = [p for p in (original / 'shared_seed/requests').glob('*/input.json')
                     if read(p)['task'] == 'document-template-v6']
    if len(request_paths) != 1:
        raise ValueError('Expected one original v6 request')
    request_path = request_paths[0]
    answer_paths = list(request_path.parent.glob('attempt-*/answer.json'))
    if len(answer_paths) != 1:
        raise ValueError('Select model retry explicitly before comparing')
    request, answer = read(request_path), read(answer_paths[0])
    original_state = read(original / 'shared_seed/agent_state.json')
    prepared = read(original / 'shared_seed/sda_input.json')
    title_data = read(title_manifest) if title_manifest else None
    titles = {item['table_id']: item['text'] for item in title_data['titles']} if title_data else {}
    files = ['src/dataagent/llm_table_agents.py', 'src/dataagent/llm_io.py',
             'src/dataagent/records.py', 'src/dataagent2/inputs.py',
             'src/dataagent2/ledger.py', 'src/dataagent/prompt/document_template.txt']
    code_hashes = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files}
    for f in files:
        dest = output / 'code_snapshot' / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / f).read_bytes())
    experiment = {'started_at': now(), 'status': 'running',
        'original_run': str(original), 'model': request['model'], 'effort': request['effort'],
        'with_text_policy': 'Reuse the single original real model answer, revalidate with the same current code used for table-only.',
        'without_text_policy': 'One fresh Template call, identical tables, prompt, schema, vocabulary and model settings; no body/method/sequence/context passages.',
        'reader_or_answer_calls': 0, 'new_pdf_ocr_calls': 0,
        'reference_policy': 'User selected previous full-PDF model extraction as reference, explicitly not human ground truth.',
        'reference': str(ROOT / 'try/direct_codex_pdf/49-Sructure/20261029T211804Z/dataset.json'),
        'source_request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest(),
        'source_answer_sha256': hashlib.sha256(answer_paths[0].read_bytes()).hexdigest(),
        'code_sha256': code_hashes, 'arms': {}}
    if title_manifest:
        experiment.update(title_policy=title_data['policy'], title_manifest=str(title_manifest),
            title_manifest_sha256=hashlib.sha256(title_manifest.read_bytes()).hexdigest(),
            title_evidence=title_data['titles'],
            without_text_policy='One fresh table+caption call; the fourteen visually checked printed captions are the only additional document evidence. No prose or inherited continuation titles.')
    write(output / 'experiment.json', experiment)

    def save_arm(name, provider, *, with_text):
        arm = output / name
        seed = arm / 'shared_seed'
        seed.mkdir(parents=True)
        state = copy.deepcopy(original_state)
        current_prepared = copy.deepcopy(prepared)
        if name == 'with_titles':
            for table in state['ocr_results']:
                identity = table.get('source_id') or table.get('table_id')
                table['caption'] = titles.get(identity, table.get('caption', ''))
            for table in current_prepared.get('tables', []):
                identity = table.get('source_id') or table.get('table_id')
                table['caption'] = titles.get(identity, table.get('caption', ''))
        methods = state['assay_method_pages'] if with_text else []
        contexts = prepared['table_contexts'] if with_text else []
        sequences = state['sequence_reference_passages'] if with_text else []
        if not with_text:
            current_prepared['document_blocks'] = []
            current_prepared['table_contexts'] = []
            state['assay_method_pages'] = []
            state['sequence_reference_passages'] = []
        write(seed / 'sda_input.json', current_prepared)
        provider.audit_path = seed / 'llm_calls.jsonl'
        captured = {}

        class GuardedProvider:
            audit_path = provider.audit_path

            def generate(self, task, instructions, payload, contract, **kwargs):
                if (task, instructions, contract.model_json_schema()) != (
                    request['task'], request['instructions'], request['schema']):
                    raise ValueError('Task/prompt/schema differs from the original call')
                expected_tables = copy.deepcopy(request['payload']['tables'])
                if name == 'with_titles':
                    for table in expected_tables:
                        table['caption'] = titles.get(table['table_id'], table['caption'])
                if payload['tables'] != expected_tables:
                    raise ValueError('Table inputs differ between arms')
                for key in ('sequence_catalog', 'required_entity_names', 'assay_keyword_context', 'repair_findings'):
                    if key == 'sequence_catalog' and name == 'with_titles':
                        continue  # Derived only from the same tables and newly supplied captions.
                    if payload[key] != request['payload'][key]:
                        raise ValueError('Non-text input changed: ' + key)
                if with_text and payload != request['payload']:
                    raise ValueError('With-text replay evidence differs from original')
                if not with_text and payload['passages']:
                    raise ValueError('Table-only arm contains leaked prose')
                captured.update({'payload_chars': len(json.dumps(payload, ensure_ascii=False)),
                    'table_count': len(payload['tables']), 'passage_count': len(payload['passages']),
                    'omitted_passages': len(payload['omitted_passage_refs']),
                    'sequence_catalog_count': len(payload['sequence_catalog']),
                    'tables_without_captions_sha256': digest([{k: v for k, v in t.items() if k != 'caption'} for t in payload['tables']]),
                    'tables_sha256': digest(payload['tables']), 'payload_sha256': digest(payload)})
                write(arm / 'controlled_input.json', {'task': task, 'instructions': instructions,
                      'payload': payload, 'schema': contract.model_json_schema()})
                if with_text:
                    result, recovered = CodexProvider._validate_result(answer, {
                        'contract': contract, 'validate': kwargs['validate'], 'recover': kwargs['recover']})
                    meta = {**original_state['llm_runs'][0], 'response_source': 'original_saved_answer_revalidated',
                        'model_calls_this_arm': 0, 'recovered': recovered}
                    write(seed / 'validated_response.json', result)
                    return result, meta
                return provider.generate(task, instructions, payload, contract,
                    validate=kwargs['validate'], recover=kwargs['recover'], validation_attempts=1)

        started = time.monotonic()
        config = state['config']
        template, validation = LLMTemplateAgent(GuardedProvider()).run(prepared['document_id'],
            state['ocr_results'], methods, contexts, sequences,
            antigen_target=config.get('antigen_target'), keyword_library_dir=config.get('keyword_library_dir'))
        state.update(document_template=template, template_validation=validation,
                     table_classifications=template['table_classifications'])
        state['llm_runs'] = [template['agent']]
        state['config'] = {**config, 'output_dir': str(seed)}
        measurements = export_measurements(prepared['document_id'], state['ocr_results'],
            state['table_classifications'], template, [])
        for filename, value in [('agent_state', state), ('document_template', template),
                                ('template_validation', validation),
                                ('table_classifications', state['table_classifications']),
                                ('assay_measurements', measurements['measurements'])]:
            write(seed / (filename + '.json'), value)
        write(arm / 'measurement_export.json', measurements)
        report = save_dataset1(load_input(seed), state, arm)
        info = {**captured, 'status': 'complete', 'wall_seconds_this_processing': round(time.monotonic() - started, 2),
            'seed_counts': report['seed_counts'], 'summary': report['assembled_summary'],
            'llm': template['agent'], 'unresolved_count': len(template['document_plan']['unresolved'])}
        write(arm / 'arm.json', info)
        experiment['arms'][name] = info
        write(output / 'experiment.json', experiment)
        print(name, json.dumps(info['seed_counts']), flush=True)

    try:
        fresh = CodexProvider(output / 'cache', model=request['model'], effort=request['effort'], timeout=900)
        if title_manifest:
            save_arm('with_titles', fresh, with_text=False)
        else:
            replay = CodexProvider(output / 'cache', model=request['model'], effort=request['effort'])
            save_arm('with_text', replay, with_text=True)
            save_arm('tables_only', fresh, with_text=False)
        experiment['status'] = 'complete'
    except Exception as exc:
        experiment.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        experiment['finished_at'] = now()
        experiment['code_changed_during_run'] = [f for f in files
            if hashlib.sha256((ROOT / f).read_bytes()).hexdigest() != code_hashes[f]]
        write(output / 'experiment.json', experiment)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--titles', type=Path)
    args = parser.parse_args()
    run(args.original.resolve(), args.output.resolve(), title_manifest=args.titles.resolve() if args.titles else None)
