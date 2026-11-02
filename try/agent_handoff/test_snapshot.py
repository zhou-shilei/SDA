import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from build_snapshot import build, write_json


class SnapshotTest(unittest.TestCase):
    def test_frozen_bytes_missing_response_and_safe_embedding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, out = root / 'run/dataagent2', root / 'snapshot'
            write_json(run / 'manifest.json', {'document_id': 'fixture'})
            write_json(run / 'seed.json', {'entities': [], 'links': []})
            question = {'id': 'q1', 'targets': [], 'kind': 'identity_or_mapping'}
            write_json(run / 'questions.initial.json', [question])
            hostile = '</script><script>globalThis.injected=true</script>@@APP@@'
            request = {'stage': 'reader', 'instructions': hostile, 'payload': {'chunk': 'c1', 'sources': [], 'links': None}, 'schema': {}, 'model': 'test', 'effort': 'test'}
            write_json(run / 'requests/reader/input.json', request)
            write_json(run / 'requests/reader/validated.json', {'mentions': []})
            write_json(run / 'requests/answer/input.json', {'stage': 'answer', 'payload': {'question': question}})
            result = build(run, out, root)
            self.assertEqual(result['calls'], 2)
            self.assertEqual(result['responses'], 1)
            bundle = json.loads((out / 'snapshot.json').read_text())
            reader, answer = bundle['calls']
            original = (run / 'requests/reader/input.json').read_bytes()
            self.assertEqual((out / reader['request']['path']).read_bytes(), original)
            self.assertEqual(reader['request']['sha256'], hashlib.sha256(original).hexdigest())
            self.assertFalse((out / reader['request']['path']).is_symlink())
            self.assertEqual(json.loads((out / reader['payload_path']).read_text()), request['payload'])
            self.assertIsNone(answer['response'])
            self.assertEqual(answer['upstream_pointer'], '/0')
            self.assertFalse((out / 'records/answer/output.json').exists())
            page = (out / 'index.html').read_text()
            self.assertNotIn(hostile, page)
            embedded = page.split('<script id="snapshot-data" type="application/json">', 1)[1].split('</script>', 1)[0]
            self.assertEqual(json.loads(embedded)['calls'][0]['request']['value']['instructions'], hostile)
            write_json(run / 'requests/reader/input.json', {'changed': True})
            self.assertEqual((out / reader['request']['path']).read_bytes(), original)
            with self.assertRaisesRegex(ValueError, '快照目录已存在'):
                build(run, out, root)

    def test_invalid_source_does_not_leave_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, out = root / 'run', root / 'out'
            write_json(run / 'manifest.json', {'document_id': 'test'})
            write_json(run / 'seed.json', {})
            path = run / 'requests/broken/input.json'
            path.parent.mkdir(parents=True)
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                build(run, out, root)
            self.assertFalse(out.exists())
            self.assertFalse(list(root.glob('.handoff-*')))

    def test_second_action_keeps_its_own_input_output_and_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, out = root / 'run/dataagent2', root / 'snapshot'
            write_json(run / 'manifest.json', {'document_id': 'fixture', 'followup_revision': 1})
            write_json(run / 'seed.json', {})
            write_json(run / 'action_agent.json', {'status': 'validated', 'provider': {'key': 'first'}})
            write_json(run / 'answer_action.json', {'status': 'validated', 'provider': {'key': 'second'}})
            write_json(run / 'dataset2.business.json', {'entities': [{'id': 'before'}]})
            write_json(run / 'dataset3.business.json', {'entities': [{'id': 'after'}]})
            write_json(run / 'answer_action/action_agent.input.json', {'initial_dataset': {'stage': 2}})
            write_json(run / 'answer_action/action_agent.dataset.json', {'entities': [{'id': 'after'}]})
            for key, stage in [('first', 'action'), ('second', 'answer_action')]:
                write_json(run / f'requests/{key}/input.json', {'stage': stage, 'payload': {'stage': stage}})
                write_json(run / f'requests/{key}/validated.json', {'stage': stage, 'dataset': {}})
            build(run, out, root)
            bundle = json.loads((out / 'snapshot.json').read_text())
            first, second = bundle['calls']
            self.assertEqual([first['stage'], second['stage']], ['action', 'answer_action'])
            self.assertTrue(first['current'])
            self.assertTrue(second['current'])
            self.assertEqual(second['upstream'], 'answer_action_input')
            self.assertEqual(second['report'], bundle['artifacts']['answer_action_status']['value'])
            self.assertEqual(second['response']['value']['stage'], 'answer_action')
            self.assertEqual((out / second['request']['path']).read_bytes(),
                             (run / 'requests/second/input.json').read_bytes())
            self.assertNotEqual(bundle['artifacts']['dataset2_business']['value'],
                                bundle['artifacts']['dataset3_business']['value'])


if __name__ == '__main__':
    unittest.main()
