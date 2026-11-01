"""Capture exact matched table-classification pairs for a read-only visual preview."""
import base64
import hashlib
import json
from pathlib import Path

from build_snapshot import now, write_json


def record(path):
    raw = path.read_bytes()
    return {'original_path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'captured_at': now(), 'value': json.loads(raw)}


def capture(reader_dir):
    reader_dir = Path(reader_dir).resolve()
    shared = reader_dir.parent / 'shared_seed'
    result = {'version': 1, 'captured_at': now(), 'requests': {}, 'cases': [], 'images': {}, 'ocr': None}
    progress = shared / 'classification_progress.json'
    if not progress.is_file():
        return result
    result['classification'] = record(progress)
    by_hash = {}
    for path in sorted((shared / 'requests').glob('*/input.json')):
        request = json.loads(path.read_bytes())
        if str(request.get('task', '')).startswith('table-classification-'):
            by_hash[path.parent.name.split('-')[0]] = (path, request)
    for index, row in enumerate(result['classification']['value']):
        meta = row.get('classifier', {})
        entry = {'table_id': row['table_id'], 'classification_index': index, 'request_key': None,
                 'missing_reason': '没有找到与该分类结果 input_hash 匹配的原始请求'}
        matched = by_hash.get(meta.get('input_hash'))
        if matched:
            path, envelope = matched
            tables = envelope.get('payload', {}).get('tables', [])
            if any(t.get('table_id') == row['table_id'] for t in tables):
                key = path.parent.name
                if key not in result['requests']:
                    result['requests'][key] = record(path)
                entry.update(request_key=key, missing_reason=None)
                # Actual model attempt stays distinct from the program-normalized result.
                audit = meta.get('audit_dir')
                if audit:
                    answer = Path(audit).resolve() / 'answer.json'
                    if path.parent.resolve() in answer.parents and answer.is_file():
                        entry['raw_response'] = record(answer)
        result['cases'].append(entry)
    # Embed one original image for the default pair; other cases retain full
    # structured cells and explicitly say when an image was not embedded.
    first = next((c for c in result['cases'] if c['request_key']), None)
    ocr_file = reader_dir.parents[2] / 'structured/table_ocr/tables.jsonl'
    if first and ocr_file.is_file():
        with ocr_file.open('rb') as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if row.get('table_id') != first['table_id']:
                    continue
                result['ocr'] = {'original_path': str(ocr_file), 'sha256': hashlib.sha256(raw).hexdigest(),
                                 'hash_scope': '该表 JSONL 行的原始字节', 'captured_at': now(), 'value': row}
                image = Path(row.get('image_path', ''))
                if image.is_file():
                    content = image.read_bytes()
                    digest = hashlib.sha256(content).hexdigest()
                    if digest == row.get('image_sha256'):
                        mime = 'image/jpeg' if image.suffix.lower() in ('.jpg', '.jpeg') else 'image/png'
                        result['images'][first['table_id']] = {
                            'original_path': str(image), 'sha256': digest,
                            'data_url': f'data:{mime};base64,' + base64.b64encode(content).decode('ascii')}
                break
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    dest = args.snapshot / 'preview_snapshot.json'
    if dest.exists():
        parser.exit(1, '渲染快照已存在，不覆盖已保存的数据。\n')
    bundle = json.loads((args.snapshot / 'snapshot.json').read_text())
    value = capture(Path(bundle['reader_dir']))
    write_json(dest, value)
    print(json.dumps({'path': str(dest), 'tables': len(value['cases']),
                      'matched': sum(c['request_key'] is not None for c in value['cases'])}, ensure_ascii=False))
