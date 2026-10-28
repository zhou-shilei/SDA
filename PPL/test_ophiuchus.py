#!/usr/bin/env python3
"""Checks that protect partner conditioning, labels and residue alignment."""
import sys
sys.dont_write_bytecode = True
import unittest
from types import SimpleNamespace
import benchmark as b
from ophiuchus import targets, encode


class Inputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = b.load_lines(b.ROOT / 'results/pilot_20261027/sequences.jsonl')
        cls.byid = {x['id']: x for x in cls.items}

    def test_partner_cache_uses_context(self):
        single = targets(self.items, 'single'); paired = targets(self.items, 'paired')
        for x in self.items:
            if x['kind'] != 'valid_sequence_swap':
                continue
            donor = x['edits'][0]['donor_parent_id']
            self.assertEqual(single[x['id']]['cache_key'], single[donor]['cache_key'])
            self.assertNotEqual(paired[x['id']]['cache_key'], paired[donor]['cache_key'])

    def test_label_control_keeps_input_unchanged(self):
        for mode in ['single', 'paired']:
            mapping = targets(self.items, mode)
            for x in self.items:
                if x['kind'] == 'wrong_chain_label':
                    self.assertEqual(mapping[x['id']], mapping[x['parent_id']])

    def test_partner_is_clean_original_in_same_split(self):
        mapping = targets(self.items, 'paired')
        clean = {(x['pair_id'], x['chain']): x for x in self.items if x['kind'] == 'clean'}
        for x in self.items:
            if x['id'] not in mapping:
                self.assertEqual(x['kind'], 'invalid_O')
                continue
            parent = self.byid[x['parent_id']]
            partner = clean[(parent['pair_id'], 'VL' if parent['chain'] == 'VH' else 'VH')]
            self.assertEqual(mapping[x['id']]['partner_sequence'], partner['sequence'])
            self.assertEqual(partner['split'], x['split'])

    def test_no_input_mutation(self):
        before = (b.ROOT / 'results/pilot_20261027/sequences.jsonl').read_bytes()
        self.assertEqual(b.digest(before.decode()), '9bb6158581fb586be92acc9d7c3dfe049521b9da785987dd3caeccdd2b4d4d05')

    def test_layout_target_columns_and_length_guard(self):
        # Small alphabet independent of the checkpoint, to audit exact token slots.
        letters = {aa: i + 4 for i, aa in enumerate(b.AA)}
        alphabet = SimpleNamespace(encode=lambda s: [letters[a] for a in s],
                                   get_tok=lambda i: next(a for a, j in letters.items() if i == j))
        model = SimpleNamespace(cls_idx=0, eos_idx=2, padding_idx=1, alphabet=alphabet)
        group = [{'sequence': 'ACD', 'partner_sequence': 'EF', 'target_chain': 'VH', 'mode': 'paired'},
                 {'sequence': 'EF', 'partner_sequence': 'ACD', 'target_chain': 'VL', 'mode': 'paired'}]
        ids, chains, columns = encode(model, group, 'cpu')
        self.assertEqual(tuple(ids.shape), (2, 278))
        self.assertEqual(ids[0].tolist(), ids[1].tolist())
        self.assertEqual(columns, [[1, 2, 3], [151, 152]])
        self.assertEqual(chains[0].tolist(), [0] * 150 + [1] * 128)
        self.assertEqual(ids[0, columns[0]].tolist(), [letters[a] for a in 'ACD'])
        self.assertEqual(ids[1, columns[1]].tolist(), [letters[a] for a in 'EF'])
        with self.assertRaisesRegex(ValueError, 'truncate'):
            encode(model, [{**group[0], 'sequence': 'A' * 149}], 'cpu')


if __name__ == '__main__':
    unittest.main()
