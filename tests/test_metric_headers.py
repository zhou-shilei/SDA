import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.llm_io import TableDecision, TableRegion, metric_endpoints
from dataagent.metric_recovery import recover_metric_plans
from dataagent.records import endpoints_in, export_measurements
import test_dataagent_migration as fixtures


class MetricHeadersTests(unittest.TestCase):
    def test_endpoint_typography_preserves_units_and_does_not_invent_digits(self):
        for text, expected in (("hTSLP EC $ _{50} $ (nM)", {"EC50"}),
                               ("$ IC_{50} $ (nM)", {"IC50"}),
                               ("IC<sub>90</sub>", {"IC90"}),
                               (r"\mathrm{EC}_{50}", {"EC50"}),
                               ("IC₅₀", {"IC50"}), ("K_D (pM)", {"KD"}),
                               ("k_a (1/Ms)", {"kon"}), ("k_d (1/s)", {"koff"}),
                               ("EC (95% confidence intervals)", set()), ("IC500", set())):
            with self.subTest(text=text):
                self.assertEqual(metric_endpoints(text), expected)
                self.assertEqual(endpoints_in(text), expected)

    def test_saved_plans_are_recovered_only_after_full_source_validation(self):
        case = fixtures.DataagentMigrationTests()
        case.setUp()
        self.addCleanup(case.temporary.cleanup)
        case.test_semantic_graph_with_scripted_provider_checks_real_contracts()
        state = json.loads((case.root / "semantic/agent_state.json").read_text())
        original = state["table_classifications"][0]
        proposal = {k: original[k] for k in TableDecision.model_fields}
        proposal["regions"] = [{k: r[k] for k in TableRegion.model_fields} for r in original["regions"]]
        original.update(decision="needs_review", regions=[], retain_for_pipeline=False)
        state["ocr_results"][0]["rows"][0][1]["text"] = "IC $ _{50} $ (nM)"
        requests = case.root / "saved-requests"
        requests.mkdir()
        (requests / "answer.json").write_text(json.dumps({"tables": [proposal]}))
        recovered, audit = recover_metric_plans(state, requests)
        self.assertEqual(len(audit), 1)
        self.assertEqual(state["table_classifications"][0]["decision"], "needs_review")
        self.assertEqual(recovered["table_classifications"][0]["decision"], "extract")
        values = export_measurements("DOC", recovered["ocr_results"], recovered["table_classifications"])
        self.assertEqual([m["value"] for m in values["measurements"]], [0.5, 1.2])
        for header in ("EC (nM)", "IC50 (pM)"):
            changed = copy.deepcopy(state)
            changed["ocr_results"][0]["rows"][0][1]["text"] = header
            self.assertEqual(recover_metric_plans(changed, requests)[1], [])
