"""Integrated planning keeps context/uncertainty and validates executable regions."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_io import TemplatePlan
from dataagent.llm_runtime import CodexProvider
from dataagent.llm_table_agents import LLMTemplateAgent, TEMPLATE_PROMPT_VERSION
from dataagent.table_classification import route_ocr_results
from dataagent2.inputs import load_input, make_chunks
from test_table_payload import assay_decision, table


def compact_decision(decision):
    return {k: v for k, v in decision.items()
            if k not in {"primary_label", "measurement_axis", "detected_endpoints"}}


def simple_decision(identity, label, decision):
    return {"table_id": identity, "labels": [label], "confidence": .9,
            "entity_axis": "unknown", "decision": decision,
            "evidence_refs": [identity + ":R0C0"], "explanation": "Literal fixture evidence",
            "regions": [], "issues": []}


def plan(tables, entities=None, profiles=None):
    return {"document_id": "DOC", "tables": tables, "entities": entities or [],
            "sequence_links": [], "assay_profiles": profiles or [], "unresolved": []}


def entity(name, ref):
    return {"entity_id": name, "canonical_name": name, "raw_names": [name],
            "role": "antibody", "parent_entity_id": None, "variant": None,
            "evidence_refs": [ref], "alias_evidence_refs": []}


class Provider:
    audit_path = None

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.recovered = False

    def generate(self, task, instructions, payload, contract, **kwargs):
        if task != f"document-template-v{TEMPLATE_PROMPT_VERSION}" or contract is not TemplatePlan:
            raise AssertionError("Only the integrated template request is allowed")
        self.calls.append(payload)
        response, self.recovered = CodexProvider._validate_result(copy.deepcopy(self.response),
            {"contract": contract, "validate": kwargs["validate"], "recover": kwargs["recover"]})
        return response, {"model": "fixture", "status": "recovered" if self.recovered else "validated"}


class IntegratedTemplateTests(unittest.TestCase):
    def test_one_call_keeps_sequence_and_uncertain_tables_but_excludes_noise(self):
        tables = [table("S", [["Name", "SEQ ID", "Sequence"], ["AB-1", "1", "ACDEFGHIKLMNPQRSTVWY"]]),
                  table("U", [["damaged header"], ["unreadable continuation"]]),
                  table("N", [["invoice"], ["shipping fees"]])]
        tables[0]["table_classification"] = {"labels": ["non_target_table"], "retain_for_pipeline": False}
        provider = Provider(plan([simple_decision("S", "sequence_definition", "context_only"),
                                  simple_decision("U", "ambiguous", "needs_review"),
                                  simple_decision("N", "non_target_table", "exclude")]))
        template, validation = LLMTemplateAgent(provider).run("DOC", tables)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual([t["table_id"] for t in provider.calls[0]["tables"]], ["S", "U", "N"])
        self.assertTrue(all(t["input_scope"] == "all_rows" and "classification" not in t
                            for t in provider.calls[0]["tables"]))
        routed = route_ocr_results(tables, template["table_classifications"])
        self.assertEqual([t["source_id"] for t in routed], ["S", "U"])
        self.assertEqual(validation["status"], "needs_review")
        self.assertEqual(template["agent"]["table_planning"], "integrated")
        self.assertFalse(provider.recovered)

    def test_missing_or_duplicate_table_decision_is_retained_for_review(self):
        for decisions in [[], [simple_decision("T", "non_target_table", "exclude")] * 2]:
            with self.subTest(decisions=decisions):
                provider = Provider(plan(decisions))
                template, validation = LLMTemplateAgent(provider).run("DOC", [table()])
                decision = template["table_classifications"][0]
                self.assertEqual(decision["decision"], "needs_review")
                self.assertTrue(decision["retain_for_pipeline"])
                self.assertTrue(provider.recovered)
                self.assertEqual(validation["status"], "needs_review")

    def test_target_label_cannot_be_excluded(self):
        provider = Provider(plan([simple_decision("T", "sequence_definition", "exclude")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [table()])
        self.assertEqual(template["table_classifications"][0]["decision"], "needs_review")
        self.assertTrue(provider.recovered)

    def test_alias_definition_in_body_text_is_unavailable_to_template(self):
        source = table(values=[["Name", "SEQ ID"], ["Reference", "3"]])
        alias = entity("Reference Antibody", "T:R1C0")
        alias.update(raw_names=["Reference", "Reference Antibody"],
                     evidence_refs=["T:R1C0", "method:p13"], alias_evidence_refs=["method:p13"])
        for text in ['In the drawings, "Reference" refers to the Reference Antibody.',
                     'Reference refers to binding measured using the Reference Antibody.']:
            with self.subTest(text=text):
                provider = Provider(plan([simple_decision("T", "sequence_mapping", "context_only")], [alias]))
                template, _ = LLMTemplateAgent(provider).run("DOC", [source], method_pages=[{"page": 13, "text": text}])
                self.assertEqual(template["entity_template"]["entities"], [])
                self.assertTrue(provider.recovered)

    def test_excluded_table_cannot_supply_entities_to_the_seed(self):
        provider = Provider(plan([simple_decision("N", "non_target_table", "exclude")],
                                 [entity("invoice", "N:R0C0")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [table("N", [["invoice"]])])
        self.assertTrue(provider.recovered)
        self.assertEqual(template["entity_template"]["entities"], [])
        self.assertEqual(template["table_classifications"][0]["decision"], "exclude")

    def test_invalid_region_does_not_drop_independently_valid_table(self):
        good = compact_decision(assay_decision())
        bad = compact_decision(assay_decision("B", header="B:R0C1"))
        bad["regions"][0]["row_end"] = 99
        provider = Provider(plan([good, bad], [entity("AB-1", "T:R2C0"), entity("AB-2", "T:R3C0")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [table(), table("B")])
        good_result, bad_result = template["table_classifications"]
        self.assertEqual(good_result["regions"][0]["execution_status"], "ready")
        self.assertEqual(good_result["regions"][0]["region_id"], "T:r2-3:0")
        self.assertEqual(bad_result["decision"], "needs_review")
        self.assertTrue(bad_result["retain_for_pipeline"])

    def test_required_names_come_from_new_regions_and_missing_names_are_reported(self):
        provider = Provider(plan([compact_decision(assay_decision())], [entity("AB-1", "T:R2C0")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [table()])
        missing = [f for f in template["document_plan"]["unresolved"] if f["code"] == "document_plan_missing_entity"]
        self.assertEqual(missing[0]["evidence_refs"], ["T:R3C0"])
        self.assertTrue(provider.recovered)

    def test_profiles_use_regions_created_in_the_same_response(self):
        profile = {"table_id": "T", "region_id": "T:r2-3:0", "protocol_id": "p1",
                   "label": "EC50", "method": None, "system": None, "species": [],
                   "endpoints": ["EC50"], "summary_zh": "", "summary_en": "",
                   "evidence_refs": ["T:R0C1"]}
        provider = Provider(plan([compact_decision(assay_decision())],
            [entity("AB-1", "T:R2C0"), entity("AB-2", "T:R3C0")], [profile]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [table()])
        self.assertFalse(provider.recovered)
        self.assertEqual(template["assay_template"]["region_profiles"][0]["region_id"], "T:r2-3:0")
        self.assertEqual(template["table_classifications"][0]["detected_endpoints"], ["EC50"])

    def test_budget_omission_is_not_a_negative_classification(self):
        source = table(values=[["Name", "Sequence"], ["AB-1", "A" * 250000]])
        provider = Provider(plan([]))
        with patch("dataagent.sequence_resolver.parse_sequence_definitions", return_value={}):
            template, validation = LLMTemplateAgent(provider).run("DOC", [source])
        self.assertEqual(provider.calls[0]["omitted_table_ids"], ["T"])
        self.assertEqual(template["table_classifications"][0]["classifier"]["mode"], "pending")
        self.assertTrue(template["table_classifications"][0]["retain_for_pipeline"])
        self.assertEqual(validation["status"], "needs_review")

    def test_verified_cross_page_headers_are_available_in_the_same_call(self):
        parent = table("H", [["Clone", "EC50 (nM)"]], page=204,
                       width=100, vertical_lines=[0, 50, 100])
        child = table("T", [["AB-1", "1"], ["AB-2", "2"]], page=205,
                      width=100, vertical_lines=[0, 50, 100])
        contexts = [{"table_id": "H", "bbox": [0, 0, 100, 40]},
                    {"table_id": "T", "bbox": [0, 0, 100, 40], "continuation_of": "H"}]
        provider = Provider(plan([simple_decision("H", "assay_ec50", "context_only"),
            compact_decision(assay_decision(header="H:R0C1", start=0, end=1))],
            [entity("AB-1", "T:R0C0"), entity("AB-2", "T:R1C0")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [parent, child], table_contexts=contexts)
        self.assertFalse(provider.recovered)
        self.assertEqual(provider.calls[0]["tables"][1]["allowed_header_table_ids"], ["H"])
        self.assertEqual(provider.calls[0]["tables"][1]["header_status"], "inherited")
        child_plan = next(item for item in template["table_classifications"] if item["table_id"] == "T")
        self.assertEqual(child_plan["header_status"], "inherited")
        self.assertEqual(child_plan["header_inheritance"]["source_table_id"], "H")
        self.assertEqual(child_plan["header_inheritance"]["source_refs"], ["H:R0C0", "H:R0C1"])
        region = template["table_classifications"][1]["regions"][0]
        self.assertEqual(region["metrics"][0]["header_refs"], ["H:R0C1"])
        self.assertEqual(region["execution_status"], "ready")

    def test_mixed_table_keeps_separate_region_ids_and_header_evidence(self):
        source = table(values=[["Clone", "EC50 (nM)"], ["AB-1", ".1"],
                               ["Clone", "EC50 (nM)"], ["AB-2", ".2"]])
        decision = compact_decision(assay_decision(start=1, end=1))
        second = copy.deepcopy(decision["regions"][0])
        second.update(row_start=3, row_end=3, header_rows=[2])
        second["metrics"][0].update(header_refs=["T:R2C1"], unit_refs=["T:R2C1"])
        decision["regions"].append(second)
        provider = Provider(plan([decision], [entity("AB-1", "T:R1C0"), entity("AB-2", "T:R3C0")]))
        template, _ = LLMTemplateAgent(provider).run("DOC", [source])
        self.assertFalse(provider.recovered)
        regions = template["table_classifications"][0]["regions"]
        self.assertEqual([r["region_id"] for r in regions], ["T:r1-1:0", "T:r3-3:1"])
        self.assertTrue(all(r["execution_status"] == "ready" for r in regions))

    def test_reader_input_applies_only_explicit_integrated_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = [table("S"), table("U"), table("N")]
            prepared = {"document_id": "DOC", "tables": tables, "document_blocks": []}
            template = {"document_id": "DOC", "agent": {"table_planning": "integrated"},
                        "table_classifications": [{"table_id": identity, "decision": decision}
                            for identity, decision in [("S", "context_only"), ("U", "needs_review"), ("N", "exclude")]]}
            (root / "sda_input.json").write_text(json.dumps(prepared))
            (root / "document_template.json").write_text(json.dumps(template))
            data = load_input(root)
            self.assertEqual({s["table_id"] for s in data["sources"].values()}, {"S", "U"})
            self.assertEqual(data["excluded_table_ids"], ["N"])
            self.assertTrue(make_chunks(data))
            self.assertEqual(json.loads((root / "sda_input.json").read_text()), prepared)
            # Archived runs remain reproducible with their original source IDs.
            template["agent"] = {}
            (root / "document_template.json").write_text(json.dumps(template))
            self.assertIn("N", {s["table_id"] for s in load_input(root)["sources"].values()})

    def test_seed_keeps_unresolved_controls_without_merging_their_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = {"document_id": "DOC", "tables": [table(t, [["Clone", "IC50 (nM)"], ["Reference", ".1"]])
                for t in ("A", "B")], "document_blocks": []}
            template = {"document_id": "DOC", "agent": {"table_planning": "integrated"}, "entity_template": {"entities": []}}
            measurements = [{"antibody_id": "Reference", "entity_ref": t + ":R1C0", "metric": "IC50",
                "value": .1, "raw_text": ".1", "unit": "nM", "value_ref": t + ":R1C1",
                "source_table": t, "header_refs": [t + ":R0C1"]} for t in ("A", "B")]
            for name, value in [("sda_input", prepared), ("document_template", template), ("assay_measurements", measurements)]:
                (root / (name + ".json")).write_text(json.dumps(value))
            data = load_input(root)
            entities, observations = data["seed"]["entities"], data["seed"]["observations"]
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(entities), 2)
            self.assertNotEqual(observations[0]["item"], observations[1]["item"])
            self.assertTrue(all(e["name"] == "Reference" and e["identity_status"] == "unresolved" for e in entities))
            self.assertEqual(data["seed"]["links"], [])
            self.assertEqual(len(data["input_warnings"]), 2)
            from dataagent2.relation_index import build_index
            self.assertTrue(all(i["identity_status"] == "unresolved" for i in build_index(data)["items"]))

    def test_seed_binds_measurements_by_declared_region_and_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = {"document_id": "DOC", "tables": [table(values=[["Clone", "KD (nM)", "koff (1/s)"], ["AB-1", ".1", ".2"]])],
                        "document_blocks": []}
            profiles = [{"table_id": "T", "region_id": "T:r1-1:0", "protocol_id": name, "label": name,
                "method": None, "system": None, "species": [], "endpoints": [metric],
                "evidence_refs": ["T:R0C1"]} for name, metric in (("affinity", "KD"), ("rate", "koff"))]
            template = {"document_id": "DOC", "agent": {"table_planning": "integrated"},
                        "entity_template": {"entities": [entity("AB-1", "T:R1C0")]},
                        "assay_template": {"region_profiles": profiles}}
            measurements = [{"antibody_id": "AB-1", "entity_ref": "T:R1C0", "metric": metric, "value": value,
                "raw_text": str(value), "value_ref": f"T:R1C{col}", "source_table": "T", "region_id": "T:r1-1:0",
                "header_refs": ["T:R0C1"]} for col, metric, value in [(1, "KD", .1), (2, "koff", .2)]]
            for name, value in [("sda_input", prepared), ("document_template", template), ("assay_measurements", measurements)]:
                (root / (name + ".json")).write_text(json.dumps(value))
            data = load_input(root)
            self.assertEqual([o["experiment"] for o in data["seed"]["observations"]], ["affinity", "rate"])
            from dataagent2.ledger import Ledger
            self.assertEqual(Ledger(data).assemble()["summary"]["missing_experiment_observations"], 0)
            # A conflicting binding must not select the first protocol.
            profiles.append({**profiles[0], "protocol_id": "conflict"})
            (root / "document_template.json").write_text(json.dumps(template))
            self.assertIsNone(load_input(root)["seed"]["observations"][0]["experiment"])


if __name__ == "__main__":
    unittest.main()
