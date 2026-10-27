"""Offline regression tests for grounding, reuse, isolation and token accounting."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dataagent2.contracts import empty_response
from dataagent2.inputs import load_input, make_chunks
from dataagent2.ledger import Ledger, ground_sequence
from dataagent2.pipeline import reader_payload, question_payload, question_tasks, run
from dataagent2.provider import Provider, event_error, event_usage, write_json


SEQUENCE = "EVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKGRFTISRDNSKNTLYLQMNSLRAEDTAVYYCARDRGYWGQGTLVTVSS"


def fixture():
    sources = {
        1: {"id": 1, "source_ref": "ocr:p1", "text": "Antibody A VH is SEQ ID 1; antibody B shares this VH.", "page": 1, "kind": "text", "table_id": None, "row": None, "column": None},
        2: {"id": 2, "source_ref": "t:R0C0", "text": "VH SEQ ID 1", "page": 2, "kind": "cell", "table_id": "t", "row": 0, "column": 0},
        3: {"id": 3, "source_ref": "t:R1C0", "text": SEQUENCE, "page": 2, "kind": "cell", "table_id": "t", "row": 1, "column": 0},
    }
    seed = {"entities": [{"id": "i1", "name": "Antibody A", "names": ["Antibody A"], "native_id": "a", "role": "antibody", "refs": [1]},
                         {"id": "i2", "name": "Antibody B", "names": ["Antibody B"], "native_id": "b", "role": "antibody", "refs": [1]}],
            "links": [], "experiments": [], "observations": []}
    return {"document_id": "test", "pdf_sha256": "test", "sources": sources, "seed": seed,
            "signature": "test-input", "native_dir": "unused",
            "baseline": {"shared_stage_tokens": 100, "total_tokens": 1000},
            "reference_for_scoring_only": {"secret": "NEVER_FEED_REFERENCE"}}


def response():
    r = empty_response()
    r["links"] = [{"item": k, "region": "VH", "seq": 1, "name": None, "refs": [1]} for k in ("i1", "i2")]
    r["sequences"] = [{"seq": 1, "name": None, "region": "VH", "parts": [{"s": 3, "start": "", "end": ""}], "refs": [2, 3]}]
    return r


class GroundingTests(unittest.TestCase):
    def test_all_questions_can_run_and_pending_file_is_refreshed(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                return empty_response(), {}

            def usage(self):
                return {"total_tokens": 0}

        with tempfile.TemporaryDirectory() as tmp:
            report = run(fixture(), FakeProvider(), Path(tmp), max_questions=None)
            initial = json.loads((Path(tmp) / "questions.initial.json").read_text())
            remaining = json.loads((Path(tmp) / "questions.json").read_text())
        self.assertEqual(report["questions_attempted"], len(initial))
        self.assertEqual(report["unresolved_question_count"], len(remaining))
        self.assertEqual(len(initial), 2)

    def test_failed_full_chain_on_another_page_is_not_reported_as_only_a_boundary_gap(self):
        data = fixture()
        data["sources"][3].update(sequence_ocr={"status": "failed"}, row=0)
        data["seed"]["links"] = [{"item": "i1", "region": "H_CHAIN", "seq": 1, "name": None, "refs": [1]}]
        ds = Ledger(data).assemble()
        self.assertEqual(ds["entities"][0]["sequences"]["VH"]["failure_kind"], "ocr_failure")

    def test_literal_audit_detects_changed_sources_and_missing_measurement_sequence_join(self):
        from dataagent2.quality import audit_dataset
        data = fixture()
        ledger = Ledger(data)
        r = response()
        r["observations"] = [{"item": "i1", "experiment": None, "metric": "EC50", "value": "1", "unit": None, "s": 1, "refs": [1]}]
        ledger.apply(r, list(data["sources"]), "c1")
        ds = ledger.assemble()
        audit = audit_dataset(ds, data["sources"])
        self.assertFalse(audit["literal_errors"])
        self.assertEqual(audit["measurements_with_complete_variable_pair"], 0)
        data["sources"][3]["text"] = "A" + SEQUENCE[1:]
        self.assertTrue(audit_dataset(ds, data["sources"])["literal_errors"])

    def test_failed_table_ocr_has_an_image_recovery_task_and_visible_reader_metadata(self):
        data = fixture()
        data["sources"][3]["sequence_ocr"] = {"status": "failed", "error": "Only middle line found"}
        data["sources"][3]["row"] = 0
        r = response()
        for link in r["links"]:
            link["refs"] = [2]
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        ds = ledger.assemble()
        slot = ds["entities"][0]["sequences"]["VH"]
        self.assertEqual(slot["failure_kind"], "ocr_failure")
        q = next(q for q in question_tasks(ds, data) if q["seq_ids"] == [1])
        self.assertEqual(q["kind"], "ocr_failure")
        payload = reader_payload(data, {"id": "c1", "core": [1, 2, 3], "context": []})
        self.assertEqual(payload["source_sequence_ocr"][3]["status"], "failed")
        targeted, _ = question_payload(q, ledger, 14000)
        self.assertIn("Only middle line", targeted["source_sequence_ocr"][3]["error"])
        # The same deterministic failure survives when a well-informed Reader
        # declines to propose the audit-only definition in the first place.
        ledger = Ledger(data)
        r["sequences"] = []
        ledger.apply(r, [1, 2, 3], "c1")
        self.assertEqual(ledger.assemble()["entities"][0]["sequences"]["VH"]["failure_kind"], "ocr_failure")

    def test_explicit_and_legacy_constant_regions_never_fill_variable_slots(self):
        for region, name in (("CL", None), ("other", "CSJ117轻链恒定区(CL)")):
            data = fixture()
            r = response()
            for link in r["links"]:
                link.update(region=region, name=name)
            r["sequences"][0].update(region=region, name=name)
            ledger = Ledger(data)
            ledger.apply(r, [1, 2, 3], "c1")
            item = ledger.assemble()["entities"][0]
            self.assertEqual(item["sequences"]["CL"]["value"], SEQUENCE)
            self.assertIsNone(item["sequences"]["VL"]["value"])

    def test_domain_slicing_preserves_characters_across_formatted_fragments(self):
        from dataagent2.augment_domains import slice_fragments
        data = fixture()
        data["sources"][1]["text"] = "AA <br> C D"
        data["sources"][2]["text"] = "E \n F G H"
        _, fragments = ground_sequence([{"s": i, "start": "", "end": ""} for i in (1, 2)], data["sources"])
        sliced = slice_fragments(fragments, data["sources"], 2, 7)
        self.assertEqual("".join(f["sequence"] for f in sliced), "CDEFG")
        self.assertEqual(sliced[0]["quote"], "C D")
        for f in sliced:
            self.assertEqual(data["sources"][f["s"]]["text"][f["start"]:f["end"]], f["quote"])
        data["sources"][1]["text"] += "A"
        with self.assertRaisesRegex(ValueError, "identity changed"):
            slice_fragments(fragments, data["sources"], 2, 7)

    def test_computational_augmentation_is_separate_and_rejects_changed_residues(self):
        import hashlib
        from dataagent2.augment_domains import augment
        data = fixture()
        data["sources"][3]["text"] = SEQUENCE + "ASTK"
        r = response()
        for link in r["links"]:
            link["region"] = "H_CHAIN"
        r["sequences"][0]["region"] = "H_CHAIN"
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write_json(p / "input.json", ledger.assemble())
            write_json(p / "sources.json", data["sources"])
            candidate = {"item": "i1", "chain": "VH", "numbering_matches_literal": True,
                "full_seq_id": 1, "chain_type": "H", "start": 0, "end": len(SEQUENCE),
                "sequence": SEQUENCE, "bitscore": 100, "evalue": 1e-20}
            analysis = {"document": "test", "input_sha256": hashlib.sha256((p / "input.json").read_bytes()).hexdigest(),
                        "candidates": [candidate]}
            write_json(p / "domains.json", analysis)
            augment(p / "input.json", p / "domains.json", p / "sources.json", p / "computed.json")
            actual = json.loads((p / "computed.json").read_text())
            self.assertEqual(actual["summary"]["variable_chains"], 0)
            self.assertEqual(actual["computational_augmentation"]["computed_variable_slots"], 1)
            self.assertEqual(actual["entities"][0]["sequences"]["VH"]["status"], "computed_domain")
            self.assertEqual(actual["entities"][0]["sequences"]["VH"]["seq_id_scope"], "full_chain")
            candidate["sequence"] = "D" + SEQUENCE[1:]
            write_json(p / "domains.json", analysis)
            with self.assertRaisesRegex(ValueError, "changes original"):
                augment(p / "input.json", p / "domains.json", p / "sources.json", p / "computed.json")
            with self.assertRaisesRegex(ValueError, "separate output"):
                augment(p / "input.json", p / "domains.json", p / "sources.json", p / "input.json")

    def test_text_table_measurement_mirrors_merge_without_merging_replicate_cells(self):
        data = fixture()
        for sid, kind in [(4, "cell"), (5, "text")]:
            data["sources"][sid] = {**data["sources"][3], "id": sid, "text": "0.5 nM", "kind": kind}
        data["seed"]["experiments"] = [{"id": "exp1", "table": "t"}]
        r = empty_response()
        r["observations"] = [{"item": "i1", "experiment": "exp1", "metric": metric,
            "value": "0.5", "unit": "nM", "s": sid, "refs": [sid]}
            for sid, metric in [(4, "IC50"), (5, "protein_blocking_elisa_ic50")]]
        ledger = Ledger(data)
        ledger.apply(r, [4, 5], "c1")
        ds = ledger.assemble()
        self.assertEqual(ds["summary"]["observations"], 1)
        self.assertEqual(ds["observations"][0]["corroborating_sources"], [5])
        data["sources"][6] = {**data["sources"][4], "id": 6, "row": 2}
        r["observations"] = [{**r["observations"][0], "s": 6, "refs": [6]}]
        ledger.apply(r, [6], "c2")
        self.assertEqual(ledger.assemble()["summary"]["observations"], 3)

    def test_variable_and_full_chain_ids_do_not_create_a_false_mapping_conflict(self):
        data = fixture()
        data["sources"][4] = {**data["sources"][1], "id": 4,
                              "text": "Antibody A full heavy chain SEQ ID 2 contains VH SEQ ID 1."}
        r = response()
        r["links"].append({"item": "i1", "region": "H_CHAIN", "seq": 2, "name": None, "refs": [4]})
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3, 4], "c1")
        sequence = ledger.assemble()["entities"][0]["sequences"]["VH"]
        self.assertEqual(sequence["status"], "supported")
        self.assertEqual(sequence["seq_id"], 1)

    def test_shared_sequence_is_grounded_once_and_joined_to_two_items(self):
        data = fixture()
        ledger = Ledger(data)
        ledger.apply(response(), [1, 2, 3], "c1")
        ledger.apply(response(), [1, 2, 3], "c1-replayed")
        ds = ledger.assemble()
        self.assertEqual(ds["summary"]["variable_chains"], 2)
        self.assertEqual(ds["summary"]["sequence_definitions"], 1)
        self.assertEqual(ds["entities"][0]["sequences"]["VH"]["value"], SEQUENCE)

    def test_invented_source_and_residue_substitution_are_rejected(self):
        data = fixture()
        ledger = Ledger(data)
        r = response()
        r["sequences"][0]["parts"][0]["s"] = 99
        ledger.apply(r, [1, 2, 3], "c1")
        self.assertEqual(ledger.assemble()["summary"]["variable_chains"], 0)
        self.assertEqual(len(ledger.rejections), 1)
        data["sources"][3]["text"] = SEQUENCE + "O"
        with self.assertRaises(ValueError):
            ground_sequence([{"s": 3, "start": "", "end": ""}], data["sources"])

    def test_ambiguous_anchors_and_duplicate_residues_are_rejected(self):
        data = fixture()
        data["sources"][3]["text"] = "AAAA AAAA"
        with self.assertRaises(ValueError):
            ground_sequence([{"s": 3, "start": "AAA", "end": "AAA"}], data["sources"])
        with self.assertRaises(ValueError):
            ground_sequence([{"s": 3, "start": "", "end": ""}] * 2, data["sources"])

    def test_full_chain_is_not_automatically_a_variable_domain(self):
        data = fixture()
        r = response()
        for l in r["links"]:
            l["region"] = "H_CHAIN"
        r["sequences"][0]["region"] = "H_CHAIN"
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3], "c1")
        ds = ledger.assemble()
        self.assertEqual(ds["summary"]["variable_chains"], 0)
        self.assertEqual(ds["entities"][0]["sequences"]["H_CHAIN"]["value"], SEQUENCE)

    def test_table_conflict_is_visible_and_does_not_silently_merge_sequences(self):
        data = fixture()
        data["sources"][4] = {**data["sources"][3], "id": 4, "text": SEQUENCE.replace("EVQ", "DVQ", 1)}
        r = response()
        r["sequences"].append({**copy.deepcopy(r["sequences"][0]), "parts": [{"s": 4, "start": "", "end": ""}], "refs": [2, 4]})
        ledger = Ledger(data)
        ledger.apply(r, [1, 2, 3, 4], "c1")
        ds = ledger.assemble()
        self.assertEqual(ds["entities"][0]["sequences"]["VH"]["status"], "conflict")
        self.assertGreater(len(ds["conflicts"]), 0)

    def test_measurement_owner_conflict_is_not_exported(self):
        data = fixture()
        data["sources"][4] = {**data["sources"][3], "id": 4, "text": "0.5 nM"}
        r = empty_response()
        r["observations"] = [{"item": i, "experiment": None, "metric": "KD", "value": "0.5", "unit": "nM", "s": 4, "refs": [1, 4]} for i in ("i1", "i2")]
        ledger = Ledger(data)
        ledger.apply(r, [1, 4], "c1")
        self.assertEqual(ledger.assemble()["summary"]["observations"], 0)


class InputTests(unittest.TestCase):
    def test_document_core_sources_read_exactly_once(self):
        data = fixture()
        for i in range(4, 35):
            data["sources"][i] = {**data["sources"][1], "id": i, "text": "background " * 50, "page": i}
        chunks = make_chunks(data, 3000)
        core = [k for c in chunks for k in c["core"]]
        self.assertEqual(len(core), len(set(core)))
        self.assertEqual(set(core), set(data["sources"]))
        self.assertNotIn("NEVER_FEED_REFERENCE", json.dumps(reader_payload(data, chunks[0])))

    def test_original_template_loader_excludes_final_sequence_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write_json(p / "sda_input.json", {"document_id": "test", "document_blocks": [], "tables": []})
            write_json(p / "document_template.json", {"document_id": "test", "entity_template": {"entities": []},
                "sequence_links": [], "sequence_extraction": {"value": "LEAKED_FINAL_ANSWER"}, "assay_template": {}})
            d = load_input(p)
            self.assertNotIn("LEAKED_FINAL_ANSWER", json.dumps(d))


class UsageTests(unittest.TestCase):
    def test_admission_is_shared_across_document_providers(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("dataagent2.provider.NEXT_REQUEST_AT", 0), \
                patch("dataagent2.provider.time.monotonic", return_value=100), \
                patch("dataagent2.provider.time.sleep") as sleep:
            Provider(Path(tmp) / "doc1", request_interval=65).admit()
            Provider(Path(tmp) / "doc2", request_interval=65).admit()
            sleep.assert_called_once_with(65)

    def test_persistent_rate_limit_stops_new_calls_and_preserves_unknown_usage(self):
        from dataagent2.provider import SERVICE_STOP
        events = json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})
        from unittest.mock import MagicMock
        proc = MagicMock(returncode=1)
        proc.communicate.return_value = (events, "irrelevant CLI startup warning")
        SERVICE_STOP.clear()
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch("dataagent2.provider.subprocess.Popen", return_value=proc) as popen, \
                    patch("dataagent2.provider.Provider.admit"), \
                    patch("dataagent2.provider.time.sleep") as sleep:
                provider = Provider(tmp)
                with self.assertRaisesRegex(RuntimeError, "429"):
                    provider.generate("reader", "test", {"chunk": 1})
                self.assertEqual(popen.call_count, 2)
                sleep.assert_called_once_with(45)
                with self.assertRaisesRegex(RuntimeError, "circuit open"):
                    provider.generate("reader", "test", {"chunk": 2})
                self.assertEqual(popen.call_count, 2)
                self.assertEqual(provider.usage()["unknown_requests"], 2)
                self.assertTrue(all("429" in c["error"] for c in provider.calls))
        finally:
            SERVICE_STOP.clear()

    def test_partial_terminal_usage_remains_unknown(self):
        events = '\n'.join(json.dumps({"type": "turn.completed", "usage": u}) for u in [
            {"input_tokens": 100, "output_tokens": 20}, {"output_tokens": 5}])
        self.assertIsNone(event_usage(events)["total_tokens"])

    def test_service_failure_is_extracted_from_events(self):
        events = '\n'.join(json.dumps(e) for e in [
            {"type": "thread.started"},
            {"type": "error", "message": "429 Too Many Requests"},
            {"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}])
        self.assertEqual(event_error(events), "429 Too Many Requests")

    def test_cache_and_reasoning_are_subsets(self):
        u = event_usage(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100,
            "output_tokens": 20, "cached_input_tokens": 80, "reasoning_output_tokens": 15}}))
        self.assertEqual(u["total_tokens"], 120)

    def test_failed_retries_count_but_log_duplicates_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = Provider(tmp)
            call = {"attempt_id": "failed-1", "stage": "reader", "status": "failed", "usage": event_usage("")}
            provider.record(call)
            call = {"attempt_id": "ok-2", "stage": "reader", "status": "ok", "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}
            provider.record(call)
            provider.record(call)
            self.assertEqual(provider.usage()["total_tokens"], 120)
            self.assertEqual(provider.usage()["requests"], 2)
            self.assertEqual(provider.usage()["unknown_requests"], 1)

    def test_resume_uses_saved_response_without_model_call(self):
        from dataagent2.contracts import INSTRUCTIONS, SCHEMA
        from dataagent2.provider import digest
        with tempfile.TemporaryDirectory() as tmp:
            provider = Provider(tmp)
            p = {"document": "test"}
            key = digest({"stage": "reader", "instructions": INSTRUCTIONS, "payload": p,
                "schema": SCHEMA, "model": provider.model, "effort": provider.effort})
            write_json(Path(tmp) / "requests" / key / "validated.json", empty_response())
            with patch("dataagent2.provider.subprocess.Popen", side_effect=AssertionError("must not call model")):
                _, meta = provider.generate("reader", INSTRUCTIONS, p)
            self.assertTrue(meta["cache_hit"])
            self.assertEqual(provider.usage()["requests"], 0)


class PipelineTests(unittest.TestCase):
    def test_ocr_tasks_do_not_consume_text_question_budget_or_claim_resolution(self):
        data = fixture()
        data["sources"][3].update(row=0, sequence_ocr={"status": "failed", "error": "Incomplete crop"})
        data["seed"]["links"] = [{**link, "refs": [2]} for link in response()["links"]]
        stages = []

        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                stages.append(stage)
                if stage == "answer":
                    assert payload["question"]["kind"] != "ocr_failure"
                return empty_response(), {}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": len(stages), "unknown_requests": 0}

        with tempfile.TemporaryDirectory() as tmp:
            report = run(data, FakeProvider(), Path(tmp), max_questions=1)
            answers = json.loads((Path(tmp) / "answers.json").read_text())
        self.assertEqual(stages, ["reader", "answer"])
        self.assertEqual(report["questions_attempted"], 1)
        self.assertEqual(report["questions_requiring_image_ocr"], 1)
        self.assertTrue(any(a["status"] == "requires_image_ocr" for a in answers))
        self.assertFalse(next(a for a in answers if a["status"] == "applied")["resolved"])

    def test_partial_ledger_is_persisted_in_order_without_double_applying_rejections(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                r = response() if payload["chunk"] == "c1" else empty_response()
                if payload["chunk"] == "c1":
                    r["issues"].append({"kind": "other", "items": [], "seq": None, "refs": [99]})
                return r, {}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": 1, "unknown_requests": 0}

        data = fixture()
        for sid in (4, 5):
            data["sources"][sid] = {**data["sources"][1], "id": sid, "page": sid, "text": "x" * 2800}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("dataagent2.pipeline.as_completed", side_effect=lambda pending: iter(pending)), \
                patch("dataagent2.pipeline.write_json", wraps=write_json) as writes:
            report = run(data, FakeProvider(), Path(tmp), chunk_chars=3000, workers=1, max_questions=0)
            snapshots = [call.args[1] for call in writes.call_args_list if Path(call.args[0]).name == "dataset.partial.json"]
            self.assertEqual([s["reader_coverage"]["completed_chunks"] for s in snapshots], [1, 2, 3])
            self.assertEqual(report["final"]["variable_chains"], 2)
            self.assertEqual(report["final"]["rejected_facts"], 1)

    def test_supervisor_preserves_round_history_and_prioritizes_unread_document(self):
        from dataagent2.supervise import supervise
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write_json(p / "supervisor.json", {"documents": ["read", "unread"], "history": [{"round": 1}]})
            write_json(p / "read" / "report.json", {"benchmark_complete": False,
                "coverage": {"completed_chunks": 1, "total_chunks": 1}})

            def child(command, **kwargs):
                self.assertEqual(command[command.index("--documents") + 1:command.index("--output")], ["unread", "read"])
                for doc in ["read", "unread"]:
                    write_json(p / doc / "report.json", {"document_id": doc, "benchmark_complete": True})
                return SimpleNamespace(returncode=0)

            with patch("dataagent2.supervise.subprocess.run", side_effect=child):
                self.assertEqual(supervise(p, ["read", "unread"], rounds=3, cooldown=0, initial_delay=0), 0)
            state = json.loads((p / "supervisor.json").read_text())
            self.assertEqual(state["round"], 2)
            self.assertEqual(len(state["history"]), 2)

    def test_failed_answer_does_not_claim_complete_benchmark_or_savings(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, *args):
                if stage == "answer":
                    raise RuntimeError("429 Too Many Requests")
                return response(), {}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": 1, "unknown_requests": 0}

        with tempfile.TemporaryDirectory() as tmp:
            report = run(fixture(), FakeProvider(), Path(tmp), max_questions=1)
        self.assertTrue(report["reader_complete"])
        self.assertFalse(report["benchmark_complete"])
        self.assertEqual(report["question_api_failures"], 1)
        self.assertIsNone(report["comparable_model_total_tokens"])

    def test_question_retrieval_includes_known_domain_evidence_within_budget(self):
        from dataagent2.pipeline import question_payload
        from dataagent2.provider import compact
        from dataagent2.inputs import source_row
        data = fixture()
        for sid in range(4, 20):
            data["sources"][sid] = {**data["sources"][1], "id": sid, "text": "Antibody A background " * 100}
        ledger = Ledger(data)
        ledger.apply(response(), [1, 2, 3], "c1")
        question = {"id": "q1", "targets": [{"item": "i1", "chain": "VH"}],
                    "seq_ids": [2], "kind": "boundary_or_definition"}
        payload, admitted = question_payload(question, ledger, 1000)
        self.assertIn(3, admitted)
        self.assertEqual(payload["known_variable_definitions"][0][:2], [1, "VH"])
        self.assertLessEqual(sum(len(compact(source_row(ledger.sources[s]))) for s in admitted), 1000)

    def test_supervisor_resumes_rate_limits_and_stops_on_success(self):
        from dataagent2.supervise import supervise
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempts = []

            def child(command, **kwargs):
                attempts.append(command)
                complete = len(attempts) == 2
                kwargs["stdout"].write("finished" if complete else "reader: service rate limited")
                write_json(root / "test" / "report.json", {"document_id": "test", "benchmark_complete": complete,
                    "coverage": {"completed_chunks": int(complete)}, "new_model_usage": {"total_tokens": 120}})
                return SimpleNamespace(returncode=0 if complete else 1)

            with patch("dataagent2.supervise.subprocess.run", side_effect=child):
                result = supervise(root, ["test"], rounds=3, cooldown=0, initial_delay=0)
            self.assertEqual(result, 0)
            self.assertEqual(len(attempts), 2)
            state = json.loads((root / "supervisor.json").read_text())
            self.assertGreater(state["history"][1]["request_interval_seconds"],
                               state["history"][0]["request_interval_seconds"])
            self.assertEqual(json.loads((root / "supervisor.json").read_text())["phase"], "finished")

    def test_supervisor_does_not_retry_non_rate_failures(self):
        from dataagent2.supervise import supervise
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, \
                patch("dataagent2.supervise.subprocess.run", return_value=SimpleNamespace(returncode=1)) as child:
            result = supervise(Path(tmp), ["test"], rounds=3, cooldown=0, initial_delay=0)
            self.assertEqual(result, 1)
            self.assertEqual(child.call_count, 1)
            self.assertEqual(json.loads((Path(tmp) / "supervisor.json").read_text())["phase"], "failed")

    def test_cli_exits_unsuccessfully_for_an_incomplete_benchmark(self):
        from dataagent2.__main__ import main
        with tempfile.TemporaryDirectory() as tmp, \
                patch("sys.argv", ["dataagent2", "--documents", "test", "--output", tmp]), \
                patch("dataagent2.__main__.discover", return_value=("native", "reference")), \
                patch("dataagent2.__main__.load_input", return_value=fixture()), \
                patch("dataagent2.__main__.run", return_value={"benchmark_complete": False,
                    "coverage": {"completed_chunks": 0, "total_chunks": 1}}):
            with self.assertRaises(SystemExit) as exc:
                main()
            self.assertEqual(exc.exception.code, 1)
            self.assertIn("test", json.loads((Path(tmp) / "comparison.json").read_text())["errors"])

    def test_smoke_run_does_not_claim_full_document_savings(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, *args):
                return empty_response(), {}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": 1, "unknown_requests": 0}

        data = fixture()
        for i in range(4, 20):
            data["sources"][i] = {**data["sources"][1], "id": i, "text": "background " * 100, "page": i}
        with tempfile.TemporaryDirectory() as tmp:
            report = run(data, FakeProvider(), Path(tmp), chunk_chars=3000, max_chunks=1)
        self.assertFalse(report["benchmark_complete"])
        self.assertIsNone(report["comparable_model_total_tokens"])
        self.assertIsNone(report["known_token_reduction_fraction"])

    def test_reference_is_not_sent_and_actual_cost_is_separate_from_reused_cost(self):
        class FakeProvider:
            model, effort = "test", "test"

            def generate(self, stage, instructions, payload):
                assert "NEVER_FEED_REFERENCE" not in json.dumps(payload)
                return response(), {"cache_hit": False}

            def usage(self):
                return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                        "requests": 1, "unknown_requests": 0}

        with tempfile.TemporaryDirectory() as tmp:
            report = run(fixture(), FakeProvider(), Path(tmp), max_questions=0)
            self.assertTrue(report["benchmark_complete"])
            self.assertEqual(report["new_model_usage"]["total_tokens"], 120)
            self.assertEqual(report["comparable_model_total_tokens"], 220)
            self.assertEqual(report["first_pass"]["variable_chains"], 2)

    def test_shared_gap_questions_are_deduplicated(self):
        from dataagent2.pipeline import question_tasks
        ledger = Ledger(fixture())
        r = response()
        r["sequences"] = []
        ledger.apply(r, [1, 2, 3], "c1")
        questions = question_tasks(ledger.assemble(), fixture())
        shared = [q for q in questions if q["seq_ids"] == [1]]
        self.assertEqual(len(shared), 1)
        self.assertEqual(len(shared[0]["targets"]), 2)


if __name__ == "__main__":
    unittest.main()
