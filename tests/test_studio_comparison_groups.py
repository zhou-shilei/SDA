"""Grouping boundaries and durable comparison decisions without OCR or models."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio.comparison_groups import assign_comparison_candidates, apply_comparison_reviews
from studio.sda_data import Workspace
from studio.review_queue import ReviewQueue


class ComparisonGroupTests(unittest.TestCase):
    def rows(self, **changes):
        return [dict(document_id="DOC", record_id=f"r{i}", entity_id=f"ab{i}", antibody_id=f"Ab{i}",
                     experiment_id="exp1", assay_endpoint="IC50", ic50=str(i), ic50_unit="nM",
                     assay_condition="24 h", antigen_target="IL-13", **changes) for i in (1, 2)]

    def test_candidates_split_on_every_recorded_dimension(self):
        for field, value in {"document_id": "OTHER", "experiment_id": "exp2", "assay_endpoint": "KD",
                             "ic50_unit": "pM", "antigen_target": "IL-4", "antigen_species": "mouse",
                             "assay_condition": "48 h", "donor_id": "D2", "batch_id": "B2",
                             "assay_keywords": [{"category": "readout", "key": "GFP"}]}.items():
            with self.subTest(field=field):
                rows = self.rows()
                rows[1][field] = value
                assign_comparison_candidates(rows)
                self.assertTrue(all(not r.get("comparison_group_id") for r in rows))

    def test_same_endpoint_or_table_and_replicates_do_not_create_groups(self):
        rows = self.rows()
        for row in rows:
            row.pop("experiment_id")
            row["table_id"] = "t1"
        assign_comparison_candidates(rows)
        self.assertTrue(all(not r.get("comparison_group_id") for r in rows))
        rows = self.rows()
        rows[1]["entity_id"] = rows[0]["entity_id"]
        self.assertNotIn("comparison_group_id", assign_comparison_candidates(rows)[0])

    def test_order_independent_membership_sensitive_signature_and_source_groups(self):
        rows = self.rows()
        reversed_rows = copy.deepcopy(rows[::-1])
        assign_comparison_candidates(rows)
        assign_comparison_candidates(reversed_rows)
        self.assertEqual(rows[0]["comparison_signature"], reversed_rows[0]["comparison_signature"])
        self.assertEqual(rows[0]["comparison_status"], "candidate")
        fresh = self.rows()
        fresh[1]["ic50"] = "9"
        assign_comparison_candidates(fresh)
        self.assertNotEqual(rows[0]["comparison_signature"], fresh[0]["comparison_signature"])
        explicit = dict(document_id="DOC", comparison_group_id="G1", comparison_status="tentative")
        assign_comparison_candidates([explicit, *self.rows()])
        self.assertEqual(explicit["comparison_status"], "tentative")

    def test_review_signature_and_published_snapshot(self):
        rows = assign_comparison_candidates(self.rows())
        reviews = {f"DOC::{r['record_id']}::comparison_group": dict(document_id="DOC", record_id=r["record_id"],
                   status="confirmed", comparison_signature=r["comparison_signature"], reviewer_name="张三",
                   updated_at="2026-11-02T10:00:00Z") for r in rows}
        self.assertTrue(all(r["comparison_status"] == "confirmed" for r in apply_comparison_reviews(rows, reviews)))
        self.assertTrue(all(r["comparison_status"] == "candidate" for r in rows))
        stale = copy.deepcopy(reviews)
        for review in stale.values():
            review["comparison_signature"] = "old"
        self.assertTrue(all(r["comparison_status"] == "candidate" for r in apply_comparison_reviews(rows, stale)))
        revised = copy.deepcopy(reviews)
        for row in rows:
            revised[f"DOC::{row['record_id']}::comparison_group"]["comparison_values"] = {
                k: row.get(k) for k in ("vh", "vl", "ic50", "ic50_unit")}
        corrections = {"DOC::r1::ic50": {"value": "9"}}
        self.assertTrue(all(r["comparison_status"] == "candidate" for r in apply_comparison_reviews(rows, revised, corrections)))
        revised["DOC::r1::comparison_group"]["comparison_values"]["ic50"] = "9"
        snapshot = apply_comparison_reviews(rows, revised, corrections)
        self.assertEqual(snapshot[0]["ic50"], "9")
        self.assertTrue(all(r["comparison_status"] == "confirmed" for r in snapshot))
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            queue = ReviewQueue(workspace.root)
            patents = [dict(document_id="DOC", status="completed", created_at="2026-11-02")]
            task = queue.act(patents, dict(operation="claim", document_id="DOC", reviewer_name="张三"))
            queue.act(patents, dict(operation="save", document_id="DOC", reviewer_name="张三", token=task["token"],
                                   version=task["version"], draft=dict(reviews=reviews)))
            folder = workspace.create_folder("IL-13")
            with patch.object(workspace, "bundle", return_value=dict(records=rows)), patch.object(workspace, "patents", return_value=patents):
                workspace.publish("DOC", ["r1", "r2"], folder["dataset_id"])
            snapshot = workspace.published(folder["dataset_id"])
            self.assertTrue(all(r["comparison_status"] == "confirmed" for r in snapshot))
            self.assertEqual(snapshot[0]["comparison_reviewer"], "张三")


if __name__ == "__main__":
    unittest.main()
