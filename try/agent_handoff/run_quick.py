#!/usr/bin/env python3
"""Run fresh integrated v6 Template/Reader calls on saved OCR with bounded answers."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

from build_snapshot import ROOT, now, write_json


def save_dataset1(data, state, target):
    """Use the production seed loader/ledger; never add Reader model facts."""
    from dataagent2.ledger import Ledger
    from dataagent2.quality import audit_dataset

    reader = Path(target) / 'dataagent2'
    identity = data['document_id']
    write_json(reader / 'seed.json', data['seed'])
    write_json(reader / 'sources.json', data['sources'])
    dataset = Ledger(data).assemble()
    write_json(target / 'dataset1.json', dataset)
    quality = audit_dataset(dataset, data['sources'])
    write_json(target / 'dataset1.quality.json', quality)
    template = state['document_template']
    report = {'document_id': identity, 'scope': 'TemplateAgent output before any Reader or Answer calls',
        'seed_counts': {k: len(v) for k, v in data['seed'].items()},
        'assembled_summary': dataset['summary'], 'template_status': template['status'],
        'table_decisions': dict(Counter(t['decision'] for t in state['table_classifications'])),
        'table_count': len(state['table_classifications']),
        'warnings': template.get('warnings', []),
        'input_warnings': data.get('input_warnings', []),
        'unresolved_seed_entities': [e for e in data['seed']['entities'] if e.get('identity_status') == 'unresolved'],
        'literal_errors': quality['literal_errors'],
        'model_tasks': [c['task'] for c in state['llm_runs']],
        'excluded_table_ids': data.get('excluded_table_ids', [])}
    write_json(target / 'dataset1.report.json', report)
    return report


def execute(snapshot, output, *, effort='medium', max_questions=2, seed_only=False):
    sys.path.insert(0, str(ROOT / 'src'))
    from dataagent.agent.graph import build_seed_graph, initial_state
    from dataagent.agent.schemas import AgentConfig
    from dataagent.records import export_measurements
    from dataagent2.inputs import load_input
    from dataagent2.pipeline import run
    from dataagent2.provider import Provider
    from dataagent2.studio_pipeline import collect_usage, export_result

    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('试跑目录必须是新目录，避免覆盖历史运行')
    bundle = json.loads((snapshot / 'snapshot.json').read_text())
    old_reader = Path(bundle['reader_dir'])
    identity = bundle['document_id']
    old_input = old_reader.parent / 'shared_seed/sda_input.json'
    original = old_input.read_bytes()
    target = output / 'document/results' / identity
    seed, reader = target / 'shared_seed', target / 'dataagent2'
    seed.mkdir(parents=True)
    shutil.copyfile(old_input, seed / 'sda_input.json')
    # Reuse evidence files for snapshot navigation, without running PDF/OCR again.
    (output / 'document/structured').symlink_to(old_reader.parents[2] / 'structured', target_is_directory=True)
    context = {'started_at': now(), 'status': 'running', 'document_id': identity,
        'model': 'gpt-6.1-sol', 'effort': effort, 'max_questions': max_questions,
        'seed_only': seed_only,
        'ocr_reused': True, 'source_input': str(old_input),
        'source_sha256': hashlib.sha256(original).hexdigest(),
        'scope': ('复用已保存 OCR 和原图样式复核；实际运行 v6 TemplateAgent，生成 Reader 之前的 Dataset1。'
                  if seed_only else '复用已保存 OCR 和原图样式复核；实际运行 v6 TemplateAgent、全文 Reader 和有限问答。')}
    write_json(target / 'quick_run.json', context)
    started = time.monotonic()
    try:
        config = AgentConfig(document_id=identity, input_dir=str(old_reader.parents[2] / 'structured/layout'),
            output_dir=str(seed), sda_input_file=str(seed / 'sda_input.json'),
            semantic_provider='codex', llm_model=context['model'], llm_effort=effort,
            llm_workers=0, llm_timeout=600, selection_mode='all', run_ocr=False,
            run_sequence_page_ocr=False, copy_images=False, sequence_workers=1,
            question_answer_enabled=False, evaluation_enabled=False)
        state = build_seed_graph().invoke(initial_state(config), {'recursion_limit': 100})
        write_json(seed / 'agent_state.json', state)
        write_json(seed / 'document_template.json', state['document_template'])
        measurements = export_measurements(identity, state['ocr_results'], state['table_classifications'], state['document_template'], [])
        write_json(seed / 'assay_measurements.json', measurements['measurements'])
        data = load_input(seed)
        if seed_only:
            report = save_dataset1(data, state, target)
            context.update(status='seed_ready', report=report)
        else:
            provider = Provider(reader, model=context['model'], effort=effort, timeout=600, request_interval=0)
            report = run(data, provider, reader, workers=0, max_questions=max_questions)
            final = json.loads((reader / 'dataset.json').read_text())
            export_result(data, final, seed, target)
            context.update(status='complete' if report['benchmark_complete'] else 'partial', report=report)
        write_json(target / 'usage.json', collect_usage(seed, reader, identity))
    except Exception as exc:
        context.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        context.update(finished_at=now(), wall_seconds=round(time.monotonic()-started, 2))
        write_json(target / 'quick_run.json', context)
    print(json.dumps({'reader_dir': str(reader), 'status': context['status'],
        'wall_seconds': context['wall_seconds']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--effort', default='medium')
    parser.add_argument('--max-questions', type=int, default=2)
    parser.add_argument('--seed-only', action='store_true', help='Only run TemplateAgent and save the initial Dataset1; no Reader/Answer')
    args = parser.parse_args()
    execute(args.snapshot, args.output, effort=args.effort, max_questions=args.max_questions, seed_only=args.seed_only)
