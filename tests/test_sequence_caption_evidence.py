"""Caption/row evidence must establish ownership, chain and domain independently."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dataagent.sequence_caption_evidence import caption_sequence_relation, complete_caption_refs


class CaptionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.entity = {'raw_names': ['AB-1']}
        self.link = {'seq_id': 4, 'chain': 'VH', 'scope': 'variable'}
        self.sources = [
            {'kind': 'table', 'table_id': 'T', 'text': 'Table 1. VH Amino Acid Sequences'},
            {'kind': 'cell', 'table_id': 'T', 'row_index': 3, 'col_index': 0, 'text': 'AB-1'},
            {'kind': 'cell', 'table_id': 'T', 'row_index': 3, 'col_index': 1, 'text': 'NO:4'},
        ]

    def test_same_table_title_and_named_row_support_the_mapping(self):
        self.assertTrue(caption_sequence_relation(self.link, self.entity, self.sources))

    def test_missing_or_other_table_caption_is_not_support(self):
        self.assertFalse(caption_sequence_relation(self.link, self.entity, self.sources[1:]))
        sources = copy.deepcopy(self.sources)
        sources[0]['table_id'] = 'OTHER'
        self.assertFalse(caption_sequence_relation(self.link, self.entity, sources))

    def test_other_row_or_wrong_number_cannot_claim_the_sequence(self):
        sources = copy.deepcopy(self.sources)
        sources[1]['row_index'] = 2
        self.assertFalse(caption_sequence_relation(self.link, self.entity, sources))
        sources = copy.deepcopy(self.sources)
        sources[2]['text'] = 'NO:40'
        self.assertFalse(caption_sequence_relation(self.link, self.entity, sources))

    def test_chain_swap_scope_swap_cdr_and_ambiguous_titles_are_rejected(self):
        for title in ['Table 2. VL Amino Acid Sequences', 'Heavy Chain Amino Acid Sequences',
                      'CDR Amino Acid Sequences', 'VH and VL Amino Acid Sequences']:
            with self.subTest(title=title):
                sources = copy.deepcopy(self.sources)
                sources[0]['text'] = title
                self.assertFalse(caption_sequence_relation(self.link, self.entity, sources))

    def test_full_heavy_chain_remains_full_chain(self):
        sources = copy.deepcopy(self.sources)
        sources[0]['text'] = 'Table 4. Heavy Chain Amino Acid Sequences'
        self.assertTrue(caption_sequence_relation({**self.link, 'scope': 'full_chain'}, self.entity, sources))

    def test_bare_number_requires_its_identifier_header(self):
        sources = copy.deepcopy(self.sources)
        sources[2]['text'] = '4'
        self.assertFalse(caption_sequence_relation(self.link, self.entity, sources))
        sources.append({'kind': 'cell', 'table_id': 'T', 'row_index': 0, 'col_index': 1, 'text': 'SEQ ID'})
        self.assertTrue(caption_sequence_relation(self.link, self.entity, sources))

    def test_explicit_group_is_ownership_not_identity_or_variant_inheritance(self):
        sources = copy.deepcopy(self.sources)
        sources[1]['text'] = 'AB-1, 3, 8, 9'
        self.assertTrue(caption_sequence_relation(self.link, {'raw_names': ['AB-3']}, sources))
        for name in ['AB-13', 'AB-3a', 'CD-3']:
            self.assertFalse(caption_sequence_relation(self.link, {'raw_names': [name]}, sources))

    def test_missing_caption_citation_can_only_be_completed_from_a_valid_same_table(self):
        evidence = {'T': self.sources[0], 'T:R3C0': self.sources[1], 'T:R3C1': self.sources[2]}
        link = {**self.link, 'evidence_refs': ['T:R3C0', 'T:R3C1']}
        completed = complete_caption_refs(link, self.entity, evidence)
        self.assertEqual(completed['evidence_refs'], ['T:R3C0', 'T:R3C1', 'T'])
        self.assertEqual(link['evidence_refs'], ['T:R3C0', 'T:R3C1'])
        for invalid in [{**link, 'chain': 'VL'}, {**link, 'scope': 'full_chain'}]:
            self.assertEqual(complete_caption_refs(invalid, self.entity, evidence), invalid)

    def test_document_recovery_preserves_title_mapping_and_adds_its_source(self):
        from dataagent.llm_table_agents import recover_document_plan
        evidence = {'T': self.sources[0], 'T:R3C0': self.sources[1], 'T:R3C1': self.sources[2]}
        entity = {**self.entity, 'entity_id': 'ab1', 'canonical_name': 'AB-1', 'role': 'antibody',
                  'parent_entity_id': None, 'variant': None, 'evidence_refs': ['T:R3C0'], 'alias_evidence_refs': []}
        link = {**self.link, 'entity_id': 'ab1', 'evidence_refs': ['T:R3C0', 'T:R3C1']}
        plan = {'document_id': 'DOC', 'entities': [entity], 'sequence_links': [link],
                'assay_profiles': [], 'unresolved': []}
        safe, rejected = recover_document_plan(plan, 'DOC', evidence, {'T'})
        self.assertFalse(rejected)
        self.assertEqual(safe['sequence_links'][0]['evidence_refs'], ['T:R3C0', 'T:R3C1', 'T'])

    def test_seed_passes_caption_evidence_to_reader_without_renumbering_cells(self):
        from dataagent2.inputs import load_input, make_chunks
        from dataagent2.pipeline import reader_payload
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            caption = 'Table 1. VH Amino Acid Sequences'
            prepared = {'document_id': 'DOC', 'document_blocks': [], 'tables': [{
                'source_id': 'T', 'page': 1, 'caption': caption,
                'rows': [[{'text': 'AB-1', 'source_ref': 'T:R0C0'}, {'text': 'NO:4', 'source_ref': 'T:R0C1'}]]}]}
            template = {'document_id': 'DOC', 'agent': {'table_planning': 'integrated'},
                'entity_template': {'entities': [{'entity_id': 'ab1', 'canonical_name': 'AB-1',
                    'raw_names': ['AB-1'], 'evidence_refs': ['T:R0C0']}]},
                'sequence_links': [{'entity_id': 'ab1', 'chain': 'VH', 'scope': 'variable', 'seq_id': 4,
                    'evidence_refs': ['T:R0C0', 'T:R0C1', 'T']}]}
            for name, value in [('sda_input', prepared), ('document_template', template)]:
                (folder / (name + '.json')).write_text(json.dumps(value))
            data = load_input(folder)
            self.assertEqual(data['sources'][1]['source_ref'], 'T:R0C0')
            self.assertEqual(data['sources'][2]['source_ref'], 'T:R0C1')
            self.assertEqual(data['sources'][3]['text'], caption)
            self.assertIn(3, data['seed']['links'][0]['refs'])
            self.assertTrue(any(row[-1] == caption for chunk in make_chunks(data)
                                for row in reader_payload(data, chunk)['sources']))


if __name__ == '__main__':
    unittest.main()
