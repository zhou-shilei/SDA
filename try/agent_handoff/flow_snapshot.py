"""Supplement the frozen calls with sampled upstream artifacts and source contracts."""
import ast
import hashlib
import json
import shutil
from pathlib import Path

from build_snapshot import now, source_ref, write_json


def sample(value):
    """Keep exact scalar values, at most one member of each array (explicit sample)."""
    if isinstance(value, list):
        return [sample(value[0])] if value else []
    if isinstance(value, dict):
        return {k: sample(v) for k, v in value.items()}
    return value


def capture_current_contract(root):
    """Capture current definitions separately; never relabel historical calls."""
    root = Path(root)
    locations = {
        'table_payload': ('src/dataagent/table_payload.py', 'pack_semantic_table'),
        'table_plans': ('src/dataagent/llm_table_agents.py', 'template_plan_context'),
        'template_input': ('src/dataagent/llm_table_agents.py', 'build_document_input'),
        'template_schema': ('src/dataagent/llm_io.py', 'TemplatePlan'),
        'template': ('src/dataagent/llm_table_agents.py', 'LLMTemplateAgent.run'),
        'load': ('src/dataagent2/inputs.py', 'load_input'),
        'reader': ('src/dataagent2/pipeline.py', 'reader_payload'),
        'reader_initial_dataset': ('src/dataagent2/pipeline.py', 'initial_dataset'),
        'chunks': ('src/dataagent2/inputs.py', 'make_chunks'),
        'question': ('src/dataagent2/followup.py', 'question_plan'),
        'answer': ('src/dataagent2/followup.py', 'answer_payload'),
        'answer_validate': ('src/dataagent2/followup.py', 'validate_answer'),
        'answer_action_payload': ('src/dataagent2/followup.py', 'build_answer_action_payload'),
        'followup': ('src/dataagent2/followup.py', 'run_followup'),
        'reader_request': ('src/dataagent2/pipeline.py', 'reader_request'),
        'source_encode': ('src/dataagent2/source_transport.py', 'encode_payload'),
        'source_decode': ('src/dataagent2/source_transport.py', 'decode_payload'),
        'action_payload': ('src/dataagent2/action_agent.py', 'build_action_payload'),
        'action_validate': ('src/dataagent2/action_agent.py', 'validate_action_result'),
        'action_run': ('src/dataagent2/action_agent.py', 'run_action_agent'),
        'cell_geometry': ('src/document/cell_geometry.py', 'locate_grid'),
        'coordinate_transform': ('src/document/cell_geometry.py', 'transform_box'),
        'sequence_fragments': ('src/dataagent2/ledger.py', 'ground_sequence'),
        'run': ('src/dataagent2/pipeline.py', 'run'),
    }
    result = {'version': 8, 'captured_at': now(), 'sources': {
        key: source_ref(root, path, name) for key, (path, name) in locations.items()}, 'prompts': {}}
    for key, filename in [('template', 'document_template.txt')]:
        path = root / 'src/dataagent/prompt' / filename
        if path.is_file():
            raw = path.read_bytes()
            result['prompts'][key] = {'path': str(path.relative_to(root)),
                'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8')}
    path = root / 'src/dataagent2/contracts.py'
    if not path.is_file():
        return result
    raw = path.read_bytes()
    for statement in ast.parse(raw.decode('utf-8')).body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == 'INSTRUCTIONS' for target in statement.targets):
            result['prompts']['reader'] = {'path': str(path.relative_to(root)),
                'sha256': hashlib.sha256(raw).hexdigest(), 'text': ast.literal_eval(statement.value)}
            break
    path = root / 'src/dataagent2/action_agent.py'
    if not path.is_file():
        return result
    # This module only uses the project's standard-library business contracts.
    import sys
    sys.path.insert(0, str(root / 'src'))
    from dataagent2.action_agent import ACTION_INSTRUCTIONS, SCHEMA_ACTION
    result['prompts']['consolidation'] = {'path': str(path.relative_to(root)),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'text': ACTION_INSTRUCTIONS}
    result['action_schema'] = SCHEMA_ACTION
    from dataagent2.followup import ANSWER_INSTRUCTIONS, ANSWER_ACTION_INSTRUCTIONS, ANSWER_SCHEMA
    path = root / 'src/dataagent2/followup.py'
    for key, instructions in [('answer', ANSWER_INSTRUCTIONS), ('action', ANSWER_ACTION_INSTRUCTIONS)]:
        result['prompts'][key] = {'path': str(path.relative_to(root)),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'text': instructions}
    result['answer_schema'] = ANSWER_SCHEMA
    return result


def capture(reader_dir, root, output=None):
    reader_dir, root = Path(reader_dir), Path(root)
    structured = reader_dir.parents[2] / 'structured'
    shared = reader_dir.parent / 'shared_seed'
    result = {'captured_at': now(), 'samples': {}, 'sources': {}, 'images': {}, 'pages': [], 'tables': []}

    def take(key, path, label, *, jsonl=False, sampled=True):
        if not path.is_file():
            return
        if jsonl:
            with path.open('rb') as stream:
                raw = next((line for line in stream if line.strip()), b'null')
            scope = 'JSONL 首条记录；记录内部数组各保留首项'
        else:
            raw = path.read_bytes()
            scope = 'JSON 结构采样；每个数组各保留首项' if sampled else '完整 JSON'
        value = json.loads(raw)
        result['samples'][key] = {
            'label': label, 'original_path': str(path), 'captured_at': now(),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'hash_scope': '首条非空 JSONL 行的原始字节' if jsonl else '完整源文件字节',
            'scope': scope, 'sampled': sampled or jsonl,
            'counts': {k: len(v) for k, v in value.items() if isinstance(v, list)} if isinstance(value, dict) else {'rows': len(value)} if isinstance(value, list) else {},
            'value': sample(value) if sampled else value,
        }
        if output:
            target = Path(output) / 'upstream' / (key + ('.jsonl' if jsonl else '.json'))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            result['samples'][key]['path'] = str(target.relative_to(output))
            result['samples'][key]['full_file_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            result['samples'][key]['bytes'] = path.stat().st_size

    take('layout', structured / 'layout' / f'{reader_dir.parent.name}_manifest.json', '版面与裁图清单', sampled=False)
    take('page_ocr', structured / 'page_ocr/pages.jsonl', '正文 OCR · 第一页完整记录', jsonl=True, sampled=False)
    take('table_ocr', structured / 'table_ocr/tables.jsonl', '表格 OCR · 第一张表完整记录', jsonl=True, sampled=False)
    take('document', structured / 'document.json', '统一结构化文档')
    take('semantic', shared / 'sda_input.json', '语义阶段输入')
    take('classification', shared / 'classification_progress.json', '表格分类 · 首条结果')
    take('facts', reader_dir / 'facts.first_pass.json', '首轮合并事实')
    # Final results may not exist in this running document. Never substitute partial data.
    take('final', reader_dir / 'dataset.json', '最终 Reader 数据集')
    take('export', reader_dir.parent / 'dataset.json', '网页导出数据集')
    if output:
        for kind, path in [('pages', structured / 'page_ocr/pages.jsonl'), ('tables', structured / 'table_ocr/tables.jsonl')]:
            if not path.is_file():
                continue
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                identity = row.get('table_id') if kind == 'tables' else f"page-{row.get('page_number', 0):04d}"
                target = Path(output) / 'upstream' / kind / f'{identity}.json'
                write_json(target, row)
                result[kind].append({'id': identity, 'page': row.get('page', row.get('page_number')),
                    'path': str(target.relative_to(output)), 'count': len(row.get('cells', row.get('lines', [])))})
        layout = result['samples'].get('layout', {}).get('value', {})
        for image in layout.get('images', []):
            source = structured / 'layout' / image.get('image', '')
            if not source.is_file():
                continue
            target = Path(output) / 'images' / (image['id'] + source.suffix)
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            result['images'][image['id']] = {'path': str(target.relative_to(output)), 'page': image['page'], 'sha256': image['sha256']}
        pdf = Path(layout.get('source_pdf', ''))
        if pdf.is_file():
            target = Path(output) / 'upstream/source.pdf'
            shutil.copyfile(pdf, target)
            result['pdf'] = {'path': str(target.relative_to(output)), 'sha256': layout.get('source_pdf_sha256')}
    for path in sorted((shared / 'requests').glob('*/input.json')):
        value = json.loads(path.read_bytes())
        task = value.get('task', '')
        key = 'template_request' if task.startswith('document-template-') else 'classification_request' if task.startswith('table-classification-') else None
        if key and key not in result['samples']:
            take(key, path, 'Template 请求' if key == 'template_request' else '表格分类请求', sampled=False)
            # Schema and instructions are contracts, preserve them in full.
            result['samples'][key]['schema'] = value.get('schema')
            result['samples'][key]['instructions'] = value.get('instructions')
    locations = {
        'prepare_pdf': ('src/document/pdf2doc.py', 'prepare_pdf'),
        'layout': ('src/document/pdf2doc.py', 'crop_table_regions'),
        'assemble': ('src/document/pdf2doc.py', 'assemble_document'),
        'document_run': ('src/document/run_document_pdf.py', 'run_pdf'),
        'semantic': ('src/dataagent/sda_inputs.py', 'prepare_sda_inputs'),
        'table_plans': ('src/dataagent/llm_table_agents.py', 'template_plan_context'),
        'template_input': ('src/dataagent/llm_table_agents.py', 'build_document_input'),
        'template_schema': ('src/dataagent/llm_io.py', 'TemplatePlan'),
        'chunks': ('src/dataagent2/inputs.py', 'make_chunks'),
        'global_index': ('src/dataagent2/relation_index.py', 'build_index'),
        'assemble_ledger': ('src/dataagent2/ledger.py', 'Ledger.assemble'),
        'export': ('src/dataagent2/studio_pipeline.py', 'export_result'),
        'audit': ('src/dataagent2/quality.py', 'audit_dataset'),
    }
    for key, (path, name) in locations.items():
        result['sources'][key] = source_ref(root, path, name)
    figure = root / 'docs/figures/dataagent2_pipeline_v6.png'
    if figure.is_file():
        import base64
        result['reference_image'] = 'data:image/png;base64,' + base64.b64encode(figure.read_bytes()).decode('ascii')
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    bundle = json.loads((args.snapshot / 'snapshot.json').read_text())
    dest = args.snapshot / 'flow_snapshot.json'
    if dest.exists():
        parser.exit(1, '流程补充快照已存在；不覆盖采样数据。\n')
    write_json(dest, capture(Path(bundle['reader_dir']), Path(__file__).resolve().parents[2]))
    print(dest)
