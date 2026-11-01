#!/usr/bin/env python3
"""Revalidate a saved real Template response and reassemble Dataset1 without a model call."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from build_snapshot import ROOT, now, write_json
from run_quick import save_dataset1


def revalidate(original, output):
    sys.path.insert(0, str(ROOT / 'src'))
    from dataagent.llm_runtime import CodexProvider
    from dataagent.llm_table_agents import LLMTemplateAgent
    from dataagent.records import export_measurements
    from dataagent2.inputs import load_input

    original, output = Path(original).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Use a fresh revalidation directory')
    requests = [(p, json.loads(p.read_text())) for p in (original / 'shared_seed/requests').glob('*/input.json')]
    requests = [(p, r) for p, r in requests if r['task'] == 'document-template-v6']
    if len(requests) != 1:
        raise ValueError('Expected exactly one recorded document-template-v6 request')
    request_path, request = requests[0]
    answers = list(request_path.parent.glob('attempt-*/answer.json'))
    if len(answers) != 1:
        raise ValueError('Expected one actual answer; select retries explicitly before replay')
    answer_path = answers[0]
    answer = json.loads(answer_path.read_text())
    state = json.loads((original / 'shared_seed/agent_state.json').read_text())
    prepared = json.loads((original / 'shared_seed/sda_input.json').read_text())
    shutil.copytree(original / 'shared_seed', output / 'shared_seed', ignore=shutil.ignore_patterns('requests'))

    class SavedResponse:
        audit_path = output / 'shared_seed/revalidation.jsonl'

        def generate(self, task, instructions, payload, contract, **kwargs):
            # New validators may inspect only the evidence actually sent in
            # the live request. Schema, prompt and evidence must be identical.
            if (task, instructions, payload, contract.model_json_schema()) != (
                request['task'], request['instructions'], request['payload'], request['schema']
            ):
                raise ValueError('Rebuilt request differs from the real request; refusing to replay')
            response, recovered = CodexProvider._validate_result(answer, {
                'contract': contract, 'validate': kwargs['validate'], 'recover': kwargs['recover']})
            return response, {**state['llm_runs'][0], 'response_source': 'saved_raw_response_revalidated',
                              'model_calls_this_revalidation': 0, 'recovered': recovered}

    config = state['config']
    template, validation = LLMTemplateAgent(SavedResponse()).run(state['document_template']['document_id'],
        state['ocr_results'], state['assay_method_pages'], prepared['table_contexts'], state['sequence_reference_passages'],
        antigen_target=config.get('antigen_target'), keyword_library_dir=config.get('keyword_library_dir'))
    state.update(document_template=template, template_validation=validation, table_classifications=template['table_classifications'])
    state['config'] = {**config, 'output_dir': str(output / 'shared_seed')}
    measurements = export_measurements(template['document_id'], state['ocr_results'], state['table_classifications'], template, [])
    for name, value in [('agent_state', state), ('document_template', template), ('template_validation', validation),
                        ('table_classifications', state['table_classifications']), ('assay_measurements', measurements['measurements'])]:
        write_json(output / 'shared_seed' / (name + '.json'), value)
    report = save_dataset1(load_input(output / 'shared_seed'), state, output)
    provenance = {'created_at': now(), 'source_run': str(original), 'model_calls_made': 0,
        'request_path': str(request_path), 'answer_path': str(answer_path),
        'request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest(),
        'answer_sha256': hashlib.sha256(answer_path.read_bytes()).hexdigest(),
        'scope': 'Same real raw response, identical prompt/schema/evidence; current validators and seed adapter only.',
        'code_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
            'src/dataagent/llm_table_agents.py', 'src/dataagent2/inputs.py', 'src/dataagent2/relation_index.py')}}
    write_json(output / 'revalidation.json', provenance)
    print(json.dumps({'output': str(output), 'seed_counts': report['seed_counts'],
                      'assembled_summary': report['assembled_summary']}, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    revalidate(args.original, args.output)
