import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schema_pipeline import build_document_dataset
from template_agent import TemplateAgent, export_dataset, validate_template
from template_sequence_agent import SequenceEvidence, SequenceCompletion, document_from_sources


def action(kind, **kwargs):
    return {"action": kind, "plan": "Resolve source-backed sequence associations", "queries": [],
            "source_ids": [], "definitions": [], "bindings": [], **kwargs}


class ScriptedLLM:
    def __init__(self, actions, reject=None):
        self.actions = iter(actions)
        self.calls = []
        self.reject = reject or (lambda p: False)
        self.output_schema = None

    async def chat(self, *, system, user):
        request = json.loads(user)
        payload = request["input"]
        self.calls.append((system, payload))
        if isinstance(payload, list):
            return json.dumps({"decisions": [{"proposal_id": p["proposal_id"], "supported": not self.reject(p),
                "reason": "Source support checked in fixture"} for p in payload]})
        fn = next(self.actions)
        return json.dumps(fn(payload) if callable(fn) else fn)


class TemplateSequenceAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        lines = [
            "BMK1 is the positive control Dupilumab.",
            "Dupilumab heavy chain (Dup-HC) sequence:",
            "EVQLVESGGGLEQPGGSLRLS",
            "CAGSGFTFRDYAMT (SEQ ID NO: 38)",
            "Dupilumab light chain (Dup-LC) sequence:",
            "DIVMTQSPLSLPVTPGEPASIS (SEQ ID NO: 39)",
            "MTM is an IgG-scFv bispecific antibody.",
            "Mutated m8 heavy variable domain (Mm8Hv) sequence:",
            "QVQLVQSGAEVKKPGASVKVS (SEQ ID NO: 44)",
            "MTM scFv on the heavy chain consists of Mm8Hv (SEQ ID NO: 44).",
        ]
        self.document = {"document_id": "DOC", "paragraphs": [
            {"id": f"l{i + 1}", "text": text, "pdf_page": 1} for i, text in enumerate(lines)]}
        self.template = TemplateAgent().run("DOC", [{"document_id": "DOC", "table_id": "T",
            "sequence_records": [{"name": "BMK1", "identity_only": True},
                                 {"name": "MTM", "identity_only": True, "construct_type": "bispecific antibody"}],
            "assay_records": [{"name": "BMK1", "metric": "IC50", "value": 201, "unit": "pM"}]}])
        self.bmk, self.mtm = [e["entity_id"] for e in self.template["entities"]]
        self.catalog = {"document_id": "DOC", "source_type": "same_document_sequence_listing",
            "records": [{"seq_id": 38, "sequence": "EVQLVESGGGLEQPGGSLRLSCAGSGFTFRDYAMT", "molecule_type": "AA"},
                        {"seq_id": 39, "sequence": "DIVMTQSPLSLPVTPGEPASIS", "molecule_type": "AA"},
                        {"seq_id": 44, "sequence": "QVQLVQSGAEVKKPGASVKVS", "molecule_type": "AA"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def definition_id(self, number, occurrence=0):
        defs = SequenceEvidence("DOC", self.document).inventory()
        return [d["definition_id"] for d in defs if d["seq_id"] == number][occurrence]

    def proposal(self, number=38, name="Dup-HC", region="H_CHAIN", refs=None):
        return {"definition_id": self.definition_id(number), "name": name, "region": region,
                "disposition": "definition", "evidence_refs": refs or ["l2", "l3", "l4"]}

    def binding(self, number=38, name="Dup-HC", region="H_CHAIN", role="molecule", entity=None, refs=None):
        return {"definition_id": self.definition_id(number), "entity_id": entity or self.bmk,
                "name": name, "region": region, "role": role, "parent_chain": None, "order": None,
                "evidence_refs": refs or ["l1", "l2", "l3", "l4"]}

    def run_agent(self, llm, *, catalog=True, max_calls=8, document=None, template=None):
        return asyncio.run(TemplateAgent().complete(template or self.template, document or self.document,
            llm, self.out, sequence_catalog=self.catalog if catalog else None, max_calls=max_calls))

    def test_planner_search_read_verify_and_full_chain_alias_binding(self):
        llm = ScriptedLLM([action("search", queries=["BMK1"]), action("read", source_ids=["l2"]),
            action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")])
        before = copy.deepcopy(self.template)
        result = self.run_agent(llm)
        entity = result["template"]["entities"][0]
        self.assertEqual(entity["H_CHAIN"]["sequence"]["value"], self.catalog["records"][0]["sequence"])
        self.assertIsNone(entity["VH"]["sequence"]["value"])
        self.assertEqual(entity["sequence_components"][0]["seq_id"], 38)
        self.assertEqual(len(export_dataset(result["template"])), 1)
        self.assertEqual(export_dataset(result["template"])[0]["VALUE"], 201)
        self.assertEqual(self.template, before)
        self.assertEqual([s["action"] for s in result["report"]["steps"]], ["search", "read", "submit", "finish"])
        self.assertEqual(result["report"]["calls"], 5)
        self.assertIsNone(llm.output_schema)
        self.assertTrue((self.out / "sequence_inventory.json").is_file())
        validate_template(result["template"])
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.validate(result["template"], TemplateAgent().schema)

    def test_full_chain_cannot_be_put_in_vh_even_if_verifier_would_accept(self):
        llm = ScriptedLLM([action("submit", definitions=[self.proposal(region="VH")],
                                 bindings=[self.binding(region="VH")]), action("finish")])
        result = self.run_agent(llm)
        self.assertEqual(result["report"]["accepted"], [])
        self.assertIsNone(result["template"]["entities"][0]["VH"]["sequence"]["value"])
        self.assertTrue(result["report"]["rejected"])

    def test_unknown_citations_and_foreign_documents_are_rejected(self):
        llm = ScriptedLLM([action("submit", definitions=[self.proposal(refs=["foreign", "l4"])]), action("finish")])
        result = self.run_agent(llm)
        self.assertIn("exposed", result["report"]["rejected"][0]["reason"])
        with self.assertRaises(ValueError):
            self.run_agent(llm, document={"document_id": "OTHER", "paragraphs": []})
        catalog = copy.deepcopy(self.catalog)
        catalog["document_id"] = "OTHER"
        with self.assertRaises(ValueError):
            asyncio.run(TemplateAgent().complete(self.template, self.document, llm, self.out, sequence_catalog=catalog))

    def test_verifier_rejection_does_not_modify_chain(self):
        llm = ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")],
                          reject=lambda p: p["kind"] == "binding")
        result = self.run_agent(llm)
        self.assertIsNone(result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"])
        self.assertNotIn("sequence_components", result["template"]["entities"][0])
        self.assertEqual(result["template"]["sequence_definitions"][0]["status"], "definition")

    def test_bispecific_domain_is_grouped_and_does_not_fill_overall_vh(self):
        definition = self.proposal(44, "Mm8Hv", "VH", ["l8", "l9"])
        binding = self.binding(44, "Mm8Hv", "VH", "scFv", self.mtm, ["l7", "l8", "l9", "l10"])
        binding["parent_chain"] = "H_CHAIN"
        llm = ScriptedLLM([action("submit", definitions=[definition], bindings=[binding]), action("finish")])
        result = self.run_agent(llm)
        entity = result["template"]["entities"][1]
        self.assertEqual(entity["sequence_components"][0]["role"], "scFv")
        self.assertEqual(entity["sequence_components"][0]["parent_chain"], "H_CHAIN")
        self.assertEqual(entity["sequence_components"][0]["sequence"], self.catalog["records"][2]["sequence"])
        self.assertIsNone(entity["VH"]["sequence"]["value"])
        binding["role"] = "molecule"
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[definition], bindings=[binding]), action("finish")]))
        self.assertTrue(any("bispecific" in r["reason"] or "mutated" in r["reason"] for r in result["report"]["rejected"]))

    def test_no_catalog_reads_complete_local_span_without_copying_other_sequences(self):
        # Only heading/terminal refs are cited; intermediate residues are copied
        # programmatically from the bounded original definition, not the model.
        d = self.proposal(refs=["l2", "l4"])
        b = self.binding(refs=["l1", "l2", "l4", "l5", "l6"])
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[d], bindings=[b]), action("finish")]), catalog=False)
        value = result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"]
        self.assertEqual(value, self.catalog["records"][0]["sequence"])

    def test_budget_exhaustion_preserves_all_pending_definitions_without_unverified_writes(self):
        llm = ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()])])
        result = self.run_agent(llm, max_calls=1)
        self.assertEqual(result["report"]["stop_reason"], "verification_budget_exhausted")
        self.assertEqual(result["report"]["calls"], 1)
        self.assertEqual(result["report"]["accepted"], [])
        self.assertEqual(result["report"]["coverage"]["total_occurrences"], 4)
        self.assertEqual(len(result["report"]["coverage"]["pending_definition_ids"]), 4)
        audit = self.run_agent(ScriptedLLM([]), max_calls=0)
        self.assertEqual(audit["report"]["calls"], 0)

    def test_split_terminal_id_and_cross_page_definition_are_inventoried(self):
        document = {"document_id": "DOC", "paragraphs": [
            {"id": "a", "text": "Dupilumab heavy chain (Dup-HC) sequence:", "pdf_page": 1},
            {"id": "b", "text": "EVQLVESGGGLEQPGGSLRLS", "pdf_page": 1},
            {"id": "c", "text": "CAGSGFTFRDYAMT (SEQ ID NO:", "pdf_page": 2},
            {"id": "d", "text": "38)", "pdf_page": 2}]}
        inventory = SequenceEvidence("DOC", document).inventory()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["seq_id"], 38)
        self.assertEqual(inventory[0]["source_refs"], ["c", "d"])

    def test_no_progress_and_malformed_outputs_stop_with_audit(self):
        llm = ScriptedLLM([action("search", queries=["absent"])] * 6)
        result = self.run_agent(llm)
        self.assertEqual(result["report"]["stop_reason"], "no_progress")
        self.assertEqual(result["report"]["calls"], 3)
        llm = ScriptedLLM([{"action": "arbitrary"}] * 6)
        result = self.run_agent(llm)
        self.assertEqual(result["report"]["stop_reason"], "repeated_errors")
        self.assertEqual(len(result["report"]["errors"]), 3)

    def test_existing_conflicting_id_is_protected_and_duplicate_binding_is_idempotent(self):
        llm = ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding(), self.binding()]), action("finish")])
        result = self.run_agent(llm)
        self.assertEqual(len(result["template"]["entities"][0]["sequence_components"]), 1)
        template = copy.deepcopy(self.template)
        slot = template["entities"][0]["H_CHAIN"]["seq_id"]
        slot.update(value=None, status="conflict")
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")]), template=template)
        self.assertEqual(result["template"]["entities"][0]["H_CHAIN"]["seq_id"]["status"], "conflict")

    def test_binding_cannot_bypass_definition_classification(self):
        result = self.run_agent(ScriptedLLM([action("submit", bindings=[self.binding()]), action("finish")]))
        self.assertFalse(result["report"]["accepted"])
        self.assertTrue(any("Classify" in r["reason"] for r in result["report"]["rejected"]))

    def test_references_can_be_disposed_without_inventing_a_name(self):
        definition = {"definition_id": self.definition_id(44, 1), "name": "", "region": "other",
                      "disposition": "reference", "evidence_refs": ["l10"]}
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[definition]), action("finish")]))
        self.assertEqual(result["template"]["sequence_definitions"][-1]["status"], "reference")

    def test_changed_source_cannot_reuse_completed_mapping(self):
        result = self.run_agent(ScriptedLLM([action("search", queries=["BMK1"]), action("finish")]))
        document = copy.deepcopy(self.document)
        document["paragraphs"][1]["text"] = "Different construct heavy chain sequence:"
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.run_agent(ScriptedLLM([]), document=document, template=result["template"])

    def test_explicit_unnumbered_sequence_is_preserved_and_can_fill_a_chain(self):
        document = {"document_id": "DOC", "paragraphs": [
            {"id": "a", "text": "BMK1 heavy chain sequence:"},
            {"id": "b", "text": "EVQLVESGGGLEQPGGSLRLS"},
            {"id": "c", "text": "CAGSGFTFRDYAMT"},
            {"id": "d", "text": "The purified antibody was tested."}]}
        inventory = SequenceEvidence("DOC", document).inventory()
        self.assertEqual(len(inventory), 1)
        self.assertIsNone(inventory[0]["seq_id"])
        definition = {"definition_id": inventory[0]["definition_id"], "name": "BMK1", "region": "H_CHAIN",
                      "disposition": "definition", "evidence_refs": ["a", "b", "c"]}
        binding = {"definition_id": inventory[0]["definition_id"], "name": "BMK1", "region": "H_CHAIN",
                   "entity_id": self.bmk, "role": "molecule", "parent_chain": None, "order": None,
                   "evidence_refs": ["a", "b", "c"]}
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[definition], bindings=[binding]), action("finish")]),
                                document=document, catalog=False)
        entity = result["template"]["entities"][0]
        self.assertEqual(entity["H_CHAIN"]["sequence"]["value"], self.catalog["records"][0]["sequence"])
        self.assertIsNone(entity["H_CHAIN"]["seq_id"]["value"])
        self.assertEqual(result["report"]["coverage"]["unnumbered_definitions"], 1)

    def test_three_line_marker_and_no_catalog_cross_page_sequence(self):
        document = {"document_id": "DOC", "paragraphs": [
            {"id": "a", "text": "BMK1 heavy chain sequence:", "pdf_page": 1},
            {"id": "b", "text": "EVQLVESGGGLEQPGGSLRLS", "pdf_page": 1},
            {"id": "c", "text": "WO 2025/108394", "pdf_page": 2},
            {"id": "d", "text": "PCT/CN2024/133628", "pdf_page": 2},
            {"id": "e", "text": "CAGSGFTFRDYAMT (SEQ", "pdf_page": 2},
            {"id": "f", "text": "ID NO:", "pdf_page": 2},
            {"id": "g", "text": "38)", "pdf_page": 2}]}
        inventory = SequenceEvidence("DOC", document).inventory()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["source_refs"], ["e", "f", "g"])
        d = {"definition_id": inventory[0]["definition_id"], "name": "BMK1", "region": "H_CHAIN",
             "disposition": "definition", "evidence_refs": ["a", "e", "f", "g"]}
        b = {"definition_id": d["definition_id"], "name": "BMK1", "region": "H_CHAIN", "role": "molecule",
             "entity_id": self.bmk, "parent_chain": None, "order": None, "evidence_refs": ["a", "e", "f", "g"]}
        llm = ScriptedLLM([action("read", source_ids=["a"]), action("submit", definitions=[d], bindings=[b]), action("finish")])
        result = self.run_agent(llm, catalog=False, document=document)
        self.assertEqual(result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"], self.catalog["records"][0]["sequence"])

    def test_unread_evidence_and_early_finish_cannot_bypass_retrieval(self):
        document = copy.deepcopy(self.document)
        document["paragraphs"] += [{"id": f"padding-{i}", "text": "Unrelated paragraph"} for i in range(25)]
        document["paragraphs"].append({"id": "unread", "text": "BMK1 is Dupilumab"})
        b = self.binding(refs=["unread", "l2", "l4"])
        llm = ScriptedLLM([action("finish"), action("submit", definitions=[self.proposal()], bindings=[b]), action("finish")])
        result = self.run_agent(llm, document=document)
        self.assertTrue(any("exposed" in r["reason"] for r in result["report"]["rejected"]))
        self.assertTrue(any("actual JSON" in r["error"] for r in result["report"]["errors"]))

    def test_mutated_domain_cannot_be_inherited_by_parent(self):
        template = copy.deepcopy(self.template)
        template["entities"][1]["name"] = "m8"
        template["entities"][1]["construct_type"] = "antibody"
        d = self.proposal(44, "Mm8Hv", "VH", ["l8", "l9"])
        b = self.binding(44, "Mm8Hv", "VH", "molecule", self.mtm, ["l8", "l9"])
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[d], bindings=[b]), action("finish")]), template=template)
        self.assertTrue(any("mutated" in r["reason"] for r in result["report"]["rejected"]))
        self.assertIsNone(result["template"]["entities"][1]["VH"]["sequence"]["value"])

    def test_keyword_queries_and_context_budget_on_a_large_inventory(self):
        index = SequenceEvidence("DOC", self.document)
        found = index.search(["BMK1 Dupilumab sequence 38"], 4000)
        self.assertGreater(found["matches"], 0)
        self.assertTrue(any("Dup-HC" in r["text"] for r in found["rows"]))
        document = copy.deepcopy(self.document)
        document["paragraphs"] += [{"id": f"extra-{i}", "text": f"Other reference SEQ ID NO: {100 + i}"} for i in range(100)]
        agent = SequenceCompletion(self.template, document, ScriptedLLM([]), self.out,
            catalog=self.catalog, max_calls=4, max_context_chars=4000, timing=None)
        state = agent.state()
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False)), 4000)
        self.assertGreater(state["inventory_omitted"], 0)
        self.assertEqual(state["inventory_total"], 104)

    def test_uppercase_heading_is_not_copied_into_residues(self):
        document = {"document_id": "DOC", "paragraphs": [
            {"id": "a", "text": "MTM-HC"},
            {"id": "b", "text": "EVQLVESGGGLEQPGGSLRLS (SEQ ID NO: 57)"}]}
        definition_id = SequenceEvidence("DOC", document).inventory()[0]["definition_id"]
        d = {"definition_id": definition_id, "name": "MTM-HC", "region": "H_CHAIN",
             "disposition": "definition", "evidence_refs": ["a", "b"]}
        b = {"definition_id": definition_id, "name": "MTM-HC", "region": "H_CHAIN", "role": "molecule",
             "entity_id": self.mtm, "parent_chain": None, "order": None, "evidence_refs": ["a", "b"]}
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[d], bindings=[b]), action("finish")]),
                                document=document, catalog=False)
        self.assertEqual(result["template"]["entities"][1]["H_CHAIN"]["sequence"]["value"], "EVQLVESGGGLEQPGGSLRLS")

    def test_nonprotein_listing_cannot_be_attached_as_an_antibody_chain(self):
        self.catalog["records"][0]["molecule_type"] = "DNA"
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")]))
        self.assertTrue(any("Non-protein" in r["reason"] for r in result["report"]["rejected"]))
        self.assertIsNone(result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"])

    def test_verifier_unknown_decisions_fail_closed(self):
        class BrokenVerifier(ScriptedLLM):
            async def chat(inner, *, system, user):
                if isinstance(json.loads(user)["input"], list):
                    return json.dumps({"decisions": [{"proposal_id": "unknown", "supported": True, "reason": "unsupported ID"}]})
                return await super().chat(system=system, user=user)
        llm = BrokenVerifier([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")])
        result = self.run_agent(llm)
        self.assertFalse(result["report"]["accepted"])
        self.assertTrue(any("unknown proposal" in e["error"] for e in result["report"]["errors"]))
        self.assertIsNone(result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"])

    def test_molecule_chain_cannot_be_its_own_parent(self):
        b = self.binding()
        b["parent_chain"] = "H_CHAIN"
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[b]), action("finish")]))
        self.assertTrue(any("own child" in r["reason"] for r in result["report"]["rejected"]))
        self.assertIsNone(result["template"]["entities"][0]["H_CHAIN"]["sequence"]["value"])

    def test_explicit_component_mapping_table_can_reference_a_sequence_without_reprinting_it(self):
        document = {"document_id": "DOC", "paragraphs": [{"id": "cell", "kind": "cell", "table_id": "T9", "row": 2,
            "column": 4, "text": "MTM scFv heavy variable domain Mm8Hv (SEQ ID NO: 44)"}]}
        definition_id = SequenceEvidence("DOC", document).inventory()[0]["definition_id"]
        d = {"definition_id": definition_id, "name": "", "region": "other",
             "disposition": "reference", "evidence_refs": ["cell"]}
        b = {"definition_id": definition_id, "name": "Mm8Hv", "region": "VH", "role": "scFv",
             "entity_id": self.mtm, "parent_chain": None, "order": None, "evidence_refs": ["cell"]}
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[d], bindings=[b]), action("finish")]), document=document)
        component = result["template"]["entities"][1]["sequence_components"][0]
        self.assertEqual(component["seq_id"], 44)
        self.assertEqual(component["sequence"], self.catalog["records"][2]["sequence"])
        self.assertIsNone(result["template"]["entities"][1]["VH"]["sequence"]["value"])

    def test_alias_with_only_one_complete_chain_remains_a_priority(self):
        template = copy.deepcopy(self.template)
        template["entities"][0]["aliases"] = ["Dupilumab"]
        template["entities"][0]["H_CHAIN"]["sequence"].update(status="present", value="EVQLVESGGGLEQPGGSLRLS")
        agent = SequenceCompletion(template, self.document, ScriptedLLM([]), self.out,
            catalog=self.catalog, max_calls=0, max_context_chars=24000, timing=None)
        self.assertEqual(agent.state()["current_task"]["name"], "BMK1")

    def test_legacy_definition_ids_migrate_without_losing_or_duplicating_components(self):
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")]))
        completed = result["template"]
        mapping = {}
        for i, definition in enumerate(completed["sequence_definitions"]):
            old = definition["definition_id"]
            definition["definition_id"] = f"legacy-{i}"
            mapping[old] = definition["definition_id"]
        for entity in completed["entities"]:
            for component in entity.get("sequence_components", []):
                component["definition_id"] = mapping[component["definition_id"]]
        result = self.run_agent(ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")]),
                                template=completed)
        components = result["template"]["entities"][0]["sequence_components"]
        self.assertEqual(len(components), 1)
        self.assertTrue(components[0]["definition_id"].startswith("seq-38@"))
        self.assertFalse(result["report"]["accepted"])
        validate_template(result["template"])

    def test_dataset_path_exports_inventory_components_and_agent_report(self):
        source = {r["id"]: {**r, "kind": "text"} for r in self.document["paragraphs"]}
        (self.out / "document_sources.json").write_text(json.dumps(source))
        data_dir = self.out / "data"
        data_dir.mkdir()
        (data_dir / "sequences.json").write_text(json.dumps(self.catalog))
        llm = ScriptedLLM([action("submit", definitions=[self.proposal()], bindings=[self.binding()]), action("finish")])
        result = asyncio.run(build_document_dataset("DOC", [], data_dir, self.out, llm=llm,
            initial_template=self.template, complete_text=True,
            sequence_agent=True, template_agent_max_calls=4))
        self.assertIn("sequence_audit", result["dataset"])
        self.assertEqual(result["dataset"]["entities"][0]["sequence_components"][0]["seq_id"], 38)
        self.assertEqual(result["dataset"]["entities"][0]["H_CHAIN"]["sequence"], self.catalog["records"][0]["sequence"])
        self.assertEqual(result["report"]["template_agent"]["calls"], 3)


if __name__ == "__main__":
    unittest.main()
