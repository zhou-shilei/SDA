"""Independent model-result semantics, input grounding and workflow placement."""
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eval.anarci import domain_details, review_sequences
from eval.esm2 import classify_chain, review_thresholds
from eval.pipeline import aggregate_anarci, aggregate_esm2, evaluate_sequences, merge_evaluations
from eval.runtime import EvaluationConfig, run_worker


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = EvaluationConfig(Path(sys.executable), Path(sys.executable), self.root)
        (self.root / "config.json").write_text("{}")

    def test_imgt_regions_preserve_insertion_residues_and_skip_numbering_gaps(self):
        domain = ([((26, " "), "A"), ((27, " "), "C"), ((28, " "), "-"),
                   ((111, " "), "D"), ((111, "A"), "E"), ((118, " "), "F")], 0, 4)
        result = domain_details(domain, {"chain_type": "H", "bitscore": 120}, "ACDEF")
        self.assertEqual(result["regions"]["FR1"], "A")
        self.assertEqual(result["regions"]["CDR1"], "C")
        self.assertEqual(result["regions"]["CDR3"], "DE")
        self.assertEqual(result["regions"]["FR4"], "F")
        self.assertEqual(result["coverage"], 1)
        self.assertEqual(result["sequence_end"], 5)

    def test_anarci_types_declared_chains_independently_and_batches_duplicate_sequences(self):
        calls = []
        def backend(items, **kwargs):
            calls.append(items)
            numbering = [[([((i + 1, " "), residue) for i, residue in enumerate(seq)], 0, len(seq) - 1)] for _, seq in items]
            return numbering, [[{"chain_type": "H"}] for _ in items], []
        module = types.ModuleType("anarci")
        module.anarci = backend
        inputs = [{"id": "a", "chain": "VH", "sequence": "ACDEF"}, {"id": "b", "chain": "VL", "sequence": "ACDEF"}]
        with patch.dict(sys.modules, {"anarci": module}):
            results = review_sequences(inputs)
        self.assertEqual(len(calls[0]), 1)
        self.assertEqual([r["review"]["status"] for r in results], ["matched", "conflict"])
        self.assertEqual(results[0]["sequence_sha256"], hashlib.sha256(b"ACDEF").hexdigest())

    def test_esm2_has_scores_without_invented_default_pass_thresholds(self):
        with patch.dict(os.environ, {}, clear=True):
            thresholds = review_thresholds()
        self.assertEqual(thresholds, {"VH": None, "VL": None})
        with patch.dict(os.environ, {"DATASETAGENT_ESM2_SITE_SCREEN_ENABLED": "0"}, clear=True):
            score = classify_chain("VH", "ACDEF", {"mean_pll": -1, "min_log_probability": -2, "bottom_decile_mean": -2}, thresholds)
        self.assertEqual(score["status"], "manual_review")
        self.assertEqual(score["metric_status"], "scored")
        self.assertAlmostEqual(score["pseudo_perplexity"], math.e)
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                classify_chain("VH", "ACDEF", {"mean_pll": bad, "min_log_probability": -2, "bottom_decile_mean": -2}, thresholds)

    def test_configured_thresholds_and_invalid_inputs_are_distinct(self):
        thresholds = {"VH": {"pass": -1, "fail": -3}}
        for value, expected in ((-0.5, "passed"), (-2, "manual_review"), (-4, "error")):
            score = classify_chain("VH", "ACDEF", {"mean_pll": value, "min_log_probability": -5, "bottom_decile_mean": -4}, thresholds)
            self.assertEqual(score["status"], expected)
        self.assertEqual(classify_chain("VH", "AC_DEF", None, thresholds)["status"], "error")
        with patch.dict(os.environ, {"DATASETAGENT_ESM2_VH_PASS_PLL": "-1"}, clear=True):
            with self.assertRaises(ValueError):
                review_thresholds()

    def test_worker_hash_mismatch_and_runtime_error_never_become_passes(self):
        items = [{"id": "a", "chain": "VH", "sequence": "ACDEF"}]
        bad = [{"id": "a", "sequence_sha256": "wrong", "review": {"status": "matched"}}]
        with patch("eval.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(bad), "")) as run:
            result, meta = run_worker("anarci", items, self.config, self.root)
        self.assertEqual(result, {})
        self.assertEqual(meta["status"], "unavailable")
        self.assertIn("-m", run.call_args.args[0])
        self.assertIn("eval.anarci", run.call_args.args[0])
        with patch("eval.runtime.subprocess.run", side_effect=subprocess.TimeoutExpired("anarci", 1)):
            result, meta = run_worker("anarci", items, self.config, self.root)
        self.assertEqual(result, {})
        self.assertEqual(meta["status"], "unavailable")

    def test_pipeline_deduplicates_within_batch_and_keeps_original_sequences(self):
        items = [{"construct_key": name, "vh": "ACDEF", "vl": "LMNPQ"} for name in ("a", "b")]
        original = copy.deepcopy(items)
        calls = []
        def worker(tool, payload, config, root):
            calls.append((tool, payload))
            reviews = {}
            for item in payload:
                reviews[item["id"]] = ({"status": "matched", "chain_type": "H" if item["chain"] == "VH" else "L", "domains": [], "message": "typed"}
                    if tool == "anarci" else {"status": "manual_review", "metric_status": "scored", "mean_pll": -1.2, "message": "scored"})
            return reviews, {"status": "completed"}
        with patch("eval.pipeline.run_worker", side_effect=worker):
            report = evaluate_sequences(items, self.root, config=self.config)
        self.assertEqual([len(payload) for _, payload in calls], [2, 2])
        self.assertEqual(items, original)
        self.assertEqual(report["summary"]["chain_count"], 4)
        self.assertEqual(report["summary"]["esm2_scored_chain_count"], 4)
        self.assertEqual(report["summary"]["anarci"], {"passed": 2})
        self.assertTrue((self.root / "sequence_evaluation.csv").is_file())

    def test_unavailable_workers_leave_both_tools_pending(self):
        with patch("eval.pipeline.run_worker", return_value=({}, {"status": "unavailable", "reason": "missing runtime"})):
            report = evaluate_sequences([{"construct_key": "a", "vh": "ACDEF", "vl": "LMNPQ"}], self.root)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["summary"]["anarci"], {"manual_review": 1})
        self.assertEqual(report["summary"]["esm2_scored_chain_count"], 0)

    def test_empty_input_has_no_failed_evaluations(self):
        report = evaluate_sequences([], self.root, config=self.config)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"]["chain_count"], 0)

    def test_final_results_cannot_be_attached_to_changed_sequence(self):
        report = {"results": [{"construct_key": "a", "sequence_sha256": {"VH": "new", "VL": "old"}}]}
        validation = {"construct_key": "a", "vh_check": {"sequence_sha256": "old"}, "vl_check": {"sequence_sha256": "old"}}
        with self.assertRaisesRegex(ValueError, "different VH/VL"):
            merge_evaluations([validation], report)

    def test_one_outlier_turns_chain_and_pair_red_even_when_mean_looks_normal(self):
        seq = "ACDEFGHIKL"
        probabilities = [-0.01] * 9 + [-10.0]
        metrics = {"mean_pll": sum(probabilities) / 10, "min_log_probability": -10,
            "bottom_decile_mean": -10, "position_scores": [{"position": i + 1, "residue": residue,
                "log_probability": probabilities[i]} for i, residue in enumerate(seq)]}
        with patch.dict(os.environ, {}, clear=True):
            score = classify_chain("VL", seq, metrics, {"VL": {"pass": -1.5, "fail": -3}})
        self.assertEqual(score["status"], "error")
        self.assertEqual(score["flagged_position_count"], 1)
        site = score["flagged_positions"][0]
        self.assertEqual((site["position"], site["residue"]), (10, "L"))
        self.assertAlmostEqual(site["pseudo_perplexity"], math.exp(10))
        self.assertAlmostEqual(site["probability"], math.exp(-10))
        self.assertEqual(aggregate_esm2({"VH": {"status": "passed", "message": "normal"}, "VL": score})["status"], "error")

    def test_complete_site_profile_reports_more_than_ten_outliers(self):
        seq = "A" * 12
        metrics = {"mean_pll": -8, "min_log_probability": -8, "bottom_decile_mean": -8,
            "position_scores": [{"position": i + 1, "residue": "A", "log_probability": -8} for i in range(12)]}
        with patch.dict(os.environ, {}, clear=True):
            score = classify_chain("VH", seq, metrics, {"VH": None})
        self.assertEqual(len(score["flagged_positions"]), 12)
        self.assertEqual(len(score["lowest_positions"]), 10)
        bad = copy.deepcopy(metrics)
        bad["position_scores"][0]["residue"] = "C"
        with self.assertRaisesRegex(ValueError, "residue"):
            classify_chain("VH", seq, bad, {"VH": None})

    def test_site_threshold_is_configurable_and_has_inclusive_boundary(self):
        metrics = {"mean_pll": -1, "min_log_probability": -math.log(1000), "bottom_decile_mean": -6.9}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(classify_chain("VH", "ACDEF", metrics, {"VH": None})["status"], "error")
        with patch.dict(os.environ, {"DATASETAGENT_ESM2_VH_SITE_MAX_PPL": "10000"}, clear=True):
            self.assertEqual(classify_chain("VH", "ACDEF", metrics, {"VH": None})["status"], "passed")
        with patch.dict(os.environ, {"DATASETAGENT_ESM2_SITE_MAX_PPL": "nan"}, clear=True):
            with self.assertRaises(ValueError):
                classify_chain("VH", "ACDEF", metrics, {"VH": None})

    def test_anarci_actual_numbering_failures_are_red_but_runtime_absence_is_pending(self):
        matched = {"status": "matched", "message": "numbered"}
        for status in ("conflict", "invalid", "unclassified", "partial", "multiple_domains"):
            self.assertEqual(aggregate_anarci({"VH": matched, "VL": {"status": status, "message": status}})["status"], "error")
        self.assertEqual(aggregate_anarci({"VH": matched, "VL": {"status": "unavailable", "message": "runtime missing"}})["status"], "manual_review")

    def test_graph_runs_evaluation_after_repairs_before_quality(self):
        from dataagent.agent.graph import build_graph, sequence_evaluation_node
        edges = {(e.source, e.target) for e in build_graph().get_graph().edges}
        self.assertIn(("question_answer", "sequence_evaluation"), edges)
        self.assertIn(("sequence_evaluation", "quality"), edges)
        self.assertNotIn(("repair_evidence", "sequence_evaluation"), edges)
        self.assertEqual(sequence_evaluation_node({"config": {"document_id": "D", "input_dir": ".", "output_dir": ".", "evaluation_enabled": False}}), {})


if __name__ == "__main__":
    unittest.main()
