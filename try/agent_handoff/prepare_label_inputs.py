#!/usr/bin/env python3
"""Prepare current Label payload previews from saved OCR; never call a provider."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from build_snapshot import ROOT, now, write_json


def prepare(folder):
    sys.path.insert(0, str(ROOT / 'src'))
    from dataagent.llm_io import ClassificationBatch, build_evidence, clipped_text, table_id
    from dataagent.table_structure import semantic_table, verified_header_links
    from dataagent.table_payload import pack_semantic_table

    folder = Path(folder).resolve()
    bundle = json.loads((folder / 'snapshot.json').read_text(encoding='utf-8'))
    source = Path(bundle['reader_dir']).parent / 'shared_seed/sda_input.json'
    raw = source.read_bytes()
    data = json.loads(raw)
    tables = [semantic_table(t) for t in data.get('tables', [])]
    contexts = data.get('table_contexts', [])
    evidence = build_evidence(tables, contexts)
    links = verified_header_links(tables, contexts)
    by_id = {table_id(t): t for t in tables}

    def pack(table):
        identity = table_id(table)
        item = pack_semantic_table(table, sample_data_row=True)
        item['allowed_header_table_ids'] = links.get(identity, [])
        item['context'] = [
            {'ref': ref, 'text': clipped_text(s['text'], 2400), 'page': s.get('page')}
            for ref, s in evidence.items() if s.get('table_id') == identity and s.get('kind') == 'context']
        return item

    result = {'captured_at': now(), 'status': 'prepared_not_sent',
        'notice': '基于已保存 OCR，按当前语义整理和表头/单行打包函数生成；未执行模型结构复核、分类调用或产生新版分类结果。',
        'source': {'path': str(source), 'sha256': hashlib.sha256(raw).hexdigest()},
        'builder_sources': {}, 'tables': {}}
    for relative in ['src/dataagent/table_payload.py', 'src/dataagent/table_structure.py', 'src/dataagent/llm_table_agents.py']:
        result['builder_sources'][relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    for identity, table in by_id.items():
        payload = {'tables': [pack(table)],
            'support_tables': [pack(by_id[s]) for s in sorted(set(links.get(identity, [])) - {identity})],
            'repair_findings': []}
        result['tables'][identity] = {'task': 'table-classification-v5', 'payload': payload,
            'instructions': (ROOT / 'src/dataagent/prompt/table_classification.txt').read_text(encoding='utf-8'),
            'schema': ClassificationBatch.model_json_schema()}
    write_json(folder / 'current_label_inputs.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    result = prepare(parser.parse_args().snapshot)
    print(f"已准备 {len(result['tables'])} 张表的新 Label 输入预览；模型调用 0 次。")
