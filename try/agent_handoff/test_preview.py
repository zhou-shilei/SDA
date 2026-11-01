import json
from pathlib import Path
import tempfile
import unittest

from build_snapshot import write_json
from preview_snapshot import capture


class PreviewPairTest(unittest.TestCase):
    def test_pairs_by_request_hash_and_table_id_and_keeps_all_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = Path(folder) / 'document/results/patent/dataagent2'
            shared = reader.parent / 'shared_seed'
            cells = [{'ref': f't:R{i}C0', 'row': i, 'column': 0, 'text': f'row {i}',
                      'bbox': [1.0, 2.0, 3.0, 4.0]} for i in range(3)]
            write_json(shared / 'requests/good-request/input.json', {
                'task': 'table-classification-v4', 'payload': {'tables': [
                    {'table_id': 't', 'row_count': 3, 'column_count': 1, 'cells': cells}]}})
            write_json(shared / 'requests/wrong-request/input.json', {
                'task': 'table-classification-v4', 'payload': {'tables': [{'table_id': 'different-table'}]}})
            write_json(shared / 'classification_progress.json', [
                {'table_id': 't', 'classifier': {'input_hash': 'good'}},
                {'table_id': 't', 'classifier': {'input_hash': 'wrong'}},
                {'table_id': 't', 'classifier': {'input_hash': 'missing'}},
            ])
            out = capture(reader)
            self.assertEqual(len(out['cases']), 3)
            self.assertEqual(out['cases'][0]['request_key'], 'good-request')
            self.assertIsNone(out['cases'][1]['request_key'])
            self.assertIsNone(out['cases'][2]['request_key'])
            payload = out['requests']['good-request']['value']['payload']
            self.assertEqual(payload['tables'][0]['cells'], cells)
            self.assertEqual(len(payload['tables'][0]['cells'][0]['bbox']), 4)
            self.assertEqual(json.loads((shared / 'classification_progress.json').read_text()), out['classification']['value'])


if __name__ == '__main__':
    unittest.main()
