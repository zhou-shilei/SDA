"""Semantic input preserves row identity while enforcing the Label sampling boundary."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_io import build_evidence
from dataagent.llm_table_agents import (
    LLMTableClassificationAgent, LLMTemplateAgent, PROMPT_VERSION,
    build_document_input, validate_classifications,
)
from dataagent.table_payload import pack_semantic_table, semantic_cell_refs
from dataagent.table_structure import verified_header_links


def table(identity="T", values=None, **extra):
    values = values if values is not None else [
        ["clone", "hTSLP EC $ _{50} $ (nM)", "cynoTSLP EC $ _{50} $ (nM)"],
        ["", "", ""], ["AB-1", "0.2651", "0.4464"], ["AB-2", "", "0.4019"],
    ]
    return {"source_id": identity, "page": 205, "rows": [
        [{"text": text, "source_ref": f"{identity}:R{r}C{c}"} for c, text in enumerate(row)]
        for r, row in enumerate(values)], **extra}


def assay_decision(identity="T", header="T:R0C1", start=2, end=3):
    return {"table_id": identity, "primary_label": "assay_ec50", "labels": ["assay_ec50"],
        "confidence": .9, "entity_axis": "rows", "measurement_axis": "columns",
        "decision": "extract", "detected_endpoints": ["EC50"],
        "evidence_refs": [header, f"{identity}:R{start}C0"], "explanation": "Visible EC50 column",
        "issues": [], "regions": [{"kind": "assay", "row_start": start, "row_end": end,
            "header_rows": [0] if header.startswith(identity+":") else [],
            "entity_column": 0, "condition_columns": [], "entity_groups": [],
            "metrics": [{"endpoint": "EC50", "value_column": 1, "value_row": None,
                "header_refs": [header], "unit": "nM", "unit_refs": [header]}],
            "excluded_columns": [], "sequence_id_column": None, "sequence_column": None,
            "chain_column": None, "name_column": None, "sequence_scope": "unknown"}]}


class TablePayloadTests(unittest.TestCase):
    def test_template_roundtrip_all_rows_empty_cells_and_literal_text(self):
        original = table()
        original["rows"][-1][-1]["text"] = "<script>data</script>" + "A" * 600
        before = copy.deepcopy(original)
        packed = pack_semantic_table(original)
        recovered = {}
        for ref, text in packed["headers"]:
            recovered[ref] = text
        for row in packed["rows"]:
            recovered.update(row)
        expected = {f"T:R{r}C{c}": cell["text"] for r, row in enumerate(original["rows"]) for c, cell in enumerate(row)}
        self.assertEqual(recovered, expected)
        self.assertEqual(packed["rows"][-1][1], ["T:R3C1", ""])
        self.assertEqual(packed["omitted_data_row_count"], 0)
        self.assertEqual(original, before)

    def test_label_only_headers_and_first_nonempty_data_row(self):
        packed = pack_semantic_table(table(), sample_data_row=True)
        self.assertEqual(packed["header_row_indices"], [0])
        self.assertEqual(packed["row_indices"], [2])
        self.assertEqual(packed["data_row_bounds"], [2, 3])
        self.assertEqual(packed["row_count"], 4)
        self.assertNotIn("AB-2", json.dumps(packed))
        self.assertNotIn("T:R3C0", semantic_cell_refs(packed))
        self.assertNotIn("cells", packed)
        self.assertNotIn("image_ref", packed)

    def test_declared_multilevel_headers_internal_headers_and_spans(self):
        source = table(values=[["Human", "Cyno"], ["EC50 (nM)", "EC50 (nM)"],
            ["1", "2"], ["EC50 (nM)", "EC50 (nM)"], ["3", "4"]], header_rows=[0, 1, 3])
        source["rows"][0][0].update(colspan=2, header_span={"row": 0, "column": 0, "row_end": 0, "column_end": 1})
        source["rows"][2][0]["numeric_text"] = "1"
        packed = pack_semantic_table(source, sample_data_row=True)
        self.assertEqual(packed["header_selection"], "declared")
        self.assertEqual(packed["header_row_indices"], [0, 1, 3])
        self.assertEqual(packed["row_indices"], [2])
        self.assertEqual(packed["cell_views"]["T:R0C0"]["colspan"], 2)
        self.assertEqual(packed["cell_views"]["T:R2C0"]["numeric_text"], "1")

    def test_header_heuristic_does_not_consume_named_species_data_rows(self):
        source = table(values=[["clone", "species"], ["AB-1", "human"], ["AB-2", "mouse"]])
        packed = pack_semantic_table(source, sample_data_row=True)
        self.assertEqual(packed["header_row_indices"], [0])
        self.assertEqual(packed["row_indices"], [1])
        self.assertNotIn("AB-2", json.dumps(packed))

    def test_score_columns_are_recognized_as_local_headers(self):
        source = table(values=[["clone", "AC-SINS Score", "PSR Score"],
                               ["AB-1", "7", "4"], ["AB-2", "4", "4"]])
        packed = pack_semantic_table(source, sample_data_row=True)
        self.assertEqual(packed["header_row_indices"], [0])
        self.assertEqual(packed["header_status"], "local")
        self.assertEqual(packed["row_indices"], [1])

    def test_heuristic_multilevel_header_and_headerless_sequence_continuation(self):
        source = table(values=[["clone", "Human", "Cyno"], ["", "EC50 (nM)", "EC50 (nM)"], ["AB-1", "1", "2"]])
        packed = pack_semantic_table(source, sample_data_row=True)
        self.assertEqual(packed["header_row_indices"], [0, 1])
        self.assertEqual(packed["row_indices"], [2])
        source = table(values=[["AB-1", "1", "ACDEFGHIKLMNPQRSTVWY"*3], ["AB-2", "2", "ACDEFGHIKLMNPQRSTVWY"*3]])
        packed = pack_semantic_table(source, sample_data_row=True)
        self.assertEqual(packed["headers"], [])
        self.assertEqual(packed["row_indices"], [0])

    def test_empty_and_header_only_tables_do_not_fabricate_a_sample(self):
        for source in [table(values=[]), table(values=[["clone", "EC50 (nM)"]])]:
            packed = pack_semantic_table(source, sample_data_row=True)
            self.assertEqual(packed["rows"], [])
            self.assertIsNone(packed["sample_row_index"])

    def test_label_rejects_hidden_cell_and_unprovided_image_citations(self):
        source = table()
        all_evidence = build_evidence([source])
        visible = semantic_cell_refs(pack_semantic_table(source, sample_data_row=True)) | {"T"}
        evidence = {ref: all_evidence[ref] for ref in visible}
        decision = assay_decision()
        validate_classifications({"tables": [decision]}, [source], evidence)
        for hidden in ["T:R3C0", "T:image"]:
            candidate = {**decision, "evidence_refs": [hidden]}
            with self.assertRaisesRegex(ValueError, "Unknown evidence"):
                validate_classifications({"tables": [candidate]}, [source], evidence)
        candidate = copy.deepcopy(decision)
        candidate["regions"][0]["header_rows"] = [3]
        with self.assertRaisesRegex(ValueError, "header row was not supplied"):
            validate_classifications({"tables": [candidate]}, [source], evidence)

    def test_template_full_rows_are_scoped_evidence_without_extra_prose_fields(self):
        source = table(table_classification=assay_decision())
        payload, evidence = build_document_input("DOC", [source], [], [])
        packed = payload["tables"][0]
        self.assertEqual(packed["input_scope"], "all_rows")
        self.assertIn("T:R3C0", semantic_cell_refs(packed))
        self.assertIn("T:R3C0", evidence)
        self.assertNotIn("required_entity_names", payload)
        self.assertNotIn("passages", payload)
        self.assertNotIn("T:image", evidence)

    def test_template_budget_omits_whole_table_and_its_cell_refs(self):
        source = table(values=[["name", "sequence"], ["AB-1", "A"*250000]])
        with patch("dataagent.sequence_resolver.parse_sequence_definitions", return_value={}):
            payload, evidence = build_document_input("DOC", [source], [], [])
        self.assertEqual(payload["tables"], [])
        self.assertEqual(payload["omitted_table_ids"], ["T"])
        self.assertNotIn("T:R1C0", evidence)

    def test_provider_gets_one_row_no_image_and_no_full_table_rule_hint(self):
        captured = []
        class Provider:
            audit_path = None
            def generate(self, task, instructions, payload, contract, **kwargs):
                captured.append((task, payload, kwargs))
                response = contract.model_validate({"tables": [assay_decision()]}).model_dump()
                kwargs["validate"](response)
                return response, {"model": "fixture"}
        results = LLMTableClassificationAgent(Provider(), image_roots=[]).run([table()])
        task, payload, options = captured[0]
        self.assertEqual(task, f"table-classification-v{PROMPT_VERSION}")
        self.assertEqual(options["images"], [])
        self.assertEqual(payload["tables"][0]["row_indices"], [2])
        self.assertNotIn("AB-2", json.dumps(payload))
        self.assertNotIn("rule_hint", payload["tables"][0])
        self.assertEqual(results[0]["regions"][0]["row_end"], 3)
        self.assertEqual(results[0]["classifier"]["input_scope"], "headers_and_one_data_row")

    def test_verified_cross_page_headers_remain_available_in_support_sample(self):
        parent = table("H", values=[["clone", "EC50 (nM)"]], page=204,
            width=100, vertical_lines=[0, 50, 100])
        child = table("T", values=[["AB-1", "1"], ["AB-2", "2"]], page=205,
            width=100, vertical_lines=[0, 50, 100])
        contexts = [{"table_id": "H", "bbox": [0, 0, 100, 40]},
            {"table_id": "T", "bbox": [0, 0, 100, 40], "continuation_of": "H"}]
        captured = []
        class Provider:
            audit_path = None
            def generate(self, task, instructions, payload, contract, **kwargs):
                captured.append(payload)
                response = contract.model_validate({"tables": [assay_decision(header="H:R0C1", start=0, end=1)]}).model_dump()
                kwargs["validate"](response)
                return response, {}
        results = LLMTableClassificationAgent(Provider(), image_roots=[]).run(
            [parent, child], contexts, target_table_ids={"T"})
        self.assertEqual(captured[0]["tables"][0]["headers"], [])
        self.assertIn("H:R0C1", semantic_cell_refs(captured[0]["support_tables"][0]))
        self.assertEqual(results[0]["allowed_header_table_ids"], ["H"])

    def test_verified_sequence_continuation_carries_literal_inherited_header(self):
        parent = table("P", [
            ["Name", "SEQ ID", "Amino Acid Sequence"],
            ["HCDR1", "HCDR1", "HCDR1"],
            ["Reference", "NO:31", "GFTFRTYG"],
            ["AB-1", "NO:32", "VWYDGSYT"],
        ], page=197)
        child = table("C", [
            ["AB-12", "NO:44", "VWYDGSNT"],
            ["AB-13", "NO:45", "ISYDGSTK"],
            ["AB-14", "NO:46", "IWYDGSTK"],
        ], page=198)
        contexts = [
            {"table_id": "P", "page": 197, "bbox": [70, 510, 520, 750], "logical_table_id": "table-3"},
            {"table_id": "C", "page": 198, "bbox": [71, 65, 521, 655], "logical_table_id": "table-3",
             "continuation_of": "P", "continuation_confidence": 0.65},
        ]

        self.assertEqual(verified_header_links([parent, child], contexts), {"C": ["P"]})
        payload, evidence = build_document_input("DOC", [parent, child], [], contexts)
        packed = next(item for item in payload["tables"] if item["table_id"] == "C")
        self.assertEqual(packed["header_status"], "inherited")
        self.assertEqual(packed["headers"], [])
        self.assertEqual(packed["header_row_indices"], [])
        self.assertEqual(packed["inherited_headers"]["source_table_id"], "P")
        self.assertEqual(packed["inherited_headers"]["rows"][0]["cells"], [
            ["P:R0C0", "Name"], ["P:R0C1", "SEQ ID"], ["P:R0C2", "Amino Acid Sequence"],
        ])
        self.assertIn("P:R0C2", semantic_cell_refs(packed))
        self.assertIn("P:R0C2", evidence)

    def test_sequence_header_is_not_inherited_when_parent_columns_disagree(self):
        parent = table("P", [
            ["Reference", "HCDR1", "HCDR2"],
            ["AB-1", "GFTFRTYG", "VWYDGSYT"],
        ], page=229)
        child = table("C", [
            ["Reference", "FTITRDNSKNTLNLQMNSLRAEDTAVYYCARAPQWELVHEAFDIWGQGTMVTVSS (SEQ", "ID NO: 3)"],
            ["AB-1", "FTITRDNSKNTLNLQMNSLRAEDTAVYYCSRSPQWEDIFEAMDIWGQGTMVTVSS (SEQ", "ID NO: 4)"],
            ["AB-2", "FTITRDNSKNTLNLQMNSLRAEDTAVYYCARSPQWEEIFEAMDIWGQGTMVTVSS (SEQ", "ID NO: 5)"],
        ], page=230)
        contexts = [
            {"table_id": "P", "page": 229, "bbox": [119, 143, 423, 740], "logical_table_id": "P"},
            {"table_id": "C", "page": 230, "bbox": [131, 131, 419, 739], "logical_table_id": "P",
             "continuation_of": "P", "continuation_confidence": 0.65},
        ]
        self.assertEqual(verified_header_links([parent, child], contexts), {})

    def test_template_agent_logs_and_validates_new_table_format(self):
        captured = []
        class Provider:
            audit_path = None
            def generate(self, task, instructions, payload, contract, **kwargs):
                captured.append(payload)
                response = {"document_id": "DOC", "entities": [{"entity_id": "ab1", "canonical_name": "AB-1",
                    "raw_names": ["AB-1"], "role": "antibody", "parent_entity_id": None, "variant": None,
                    "evidence_refs": ["T:R1C0"], "alias_evidence_refs": []}],
                    "sequence_links": [], "assay_profiles": [], "unresolved": [],
                    "tables": [{"table_id": "T", "labels": ["assay_ec50"], "confidence": .9,
                        "entity_axis": "rows", "decision": "needs_review", "evidence_refs": ["T:R0C1"],
                        "explanation": "Fixture requires structure review", "regions": [], "issues": []}]}
                kwargs["validate"](response)
                return response, {}
        source = table(values=[["clone", "EC50 (nM)"], ["AB-1", "0.1"]])
        result, validation = LLMTemplateAgent(Provider()).run("DOC", [source])
        self.assertEqual(captured[0]["tables"][0]["rows"][0][0], ["T:R1C0", "AB-1"])
        self.assertEqual(validation["entity_count"], 1)
        self.assertEqual(result["entity_template"]["entities"][0]["canonical_name"], "AB-1")

    def test_coarse_flag_never_runs_a_full_table_prepass(self):
        calls = []
        class Provider:
            audit_path = None
            def generate(self, task, instructions, payload, contract, **kwargs):
                calls.append(task)
                decisions = []
                for item in payload["tables"]:
                    self_test.assertEqual(len(item["rows"]), 1)
                    self_test.assertNotIn("AB-2", json.dumps(item))
                    decisions.append(assay_decision(item["table_id"], header=item["table_id"]+":R0C1"))
                self_test.assertEqual(kwargs["images"], [])
                response = contract.model_validate({"tables": decisions}).model_dump()
                kwargs["validate"](response)
                return response, {}
        self_test = self
        results = LLMTableClassificationAgent(Provider(), image_roots=[], coarse_triage=True).run(
            [table(f"T{i}") for i in range(9)])
        self.assertEqual(len(results), 9)
        self.assertEqual(calls, [f"table-classification-v{PROMPT_VERSION}"]*3)


if __name__ == "__main__":
    unittest.main()
