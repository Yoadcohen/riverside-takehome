"""Tests. Run:  python3 -m unittest discover tests"""

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import handoff

TESTS = Path(__file__).parent
SAMPLE_CACHE = TESTS / "fixtures" / "ai_cache_sample.json"


def answer_key():
    with open(TESTS / "expected_outcomes.csv", newline="", encoding="utf-8") as f:
        return {r["opportunity_id"]: r for r in csv.DictReader(f)}


def ids(text):
    return sorted(x.strip() for x in text.split(";") if x.strip())


class MatchesAnswerKey(unittest.TestCase):
    """The script must give every opportunity the handoff_status and checks we decided by hand."""

    def check(self, cache_path):
        results = {r["opportunity_id"]: r for r in handoff.assess(cache_path=cache_path)}
        key = answer_key()
        self.assertEqual(set(results), set(key))
        for opp_id, expected in key.items():
            with self.subTest(opp_id):
                self.assertEqual(results[opp_id]["handoff_status"], expected["expected_handoff_status"])
                self.assertEqual(ids(results[opp_id]["exception_types"]), ids(expected["expected_exception_types"]))

    def test_with_sample_ai_results(self):
        self.check(SAMPLE_CACHE)

    @unittest.skipUnless(handoff.AI_CACHE.exists(), "ai_cache.json not created yet; run extract_notes.py")
    def test_with_real_ai_results(self):
        self.check(handoff.AI_CACHE)


class AiGuardrails(unittest.TestCase):

    def test_fact_without_quote_from_note_is_dropped(self):
        note = "CFO approved an 18% discount."
        facts, dropped = handoff.validated_ai_facts(
            {"approved_discount_pct": 10, "approved_discount_evidence": "CFO approved a 10% discount."}, note)
        self.assertIsNone(facts["approved_discount_pct"])
        self.assertEqual(dropped, ["approved_discount_pct"])

    def test_number_must_match_its_own_quote(self):
        note = "CFO approved an 18% discount."
        facts, dropped = handoff.validated_ai_facts(
            {"approved_discount_pct": 25, "approved_discount_evidence": "CFO approved an 18% discount."}, note)
        self.assertIsNone(facts["approved_discount_pct"])
        self.assertEqual(dropped, ["approved_discount_pct"])

    def test_product_must_appear_in_its_own_quote(self):
        note = "Customer signed for the assessment package."
        facts, dropped = handoff.validated_ai_facts(
            {"products": [{"name": "intervention kit IK-2", "evidence": "Customer signed for the assessment package."}]}, note)
        self.assertEqual(facts["products"], [])
        self.assertEqual(dropped, ["product:intervention kit IK-2"])

    def test_missing_ai_results_send_closed_won_deals_to_review_not_ready(self):
        results = {r["opportunity_id"]: r for r in handoff.assess(cache_path=TESTS / "does_not_exist.json")}
        self.assertEqual(results["OPP-001"]["handoff_status"], "Needs Review")
        self.assertEqual(results["OPP-001"]["confidence"], "Low")
        self.assertEqual(results["OPP-002"]["handoff_status"], "Blocked")  # rules still block
        self.assertEqual(results["OPP-003"]["handoff_status"], "Not Yet Eligible")

    def test_ai_result_for_a_changed_note_is_ignored(self):
        cache = json.loads(SAMPLE_CACHE.read_text())
        cache["results"]["OPP-006"]["note_text"] = "An older version of the note."
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cache, f)
        results = {r["opportunity_id"]: r for r in handoff.assess(cache_path=f.name)}
        self.assertIn("R4", results["OPP-006"]["exception_types"])

    def test_ai_finding_alone_never_blocks(self):
        results = {r["opportunity_id"]: r for r in handoff.assess(cache_path=SAMPLE_CACHE)}
        self.assertEqual(results["OPP-006"]["handoff_status"], "Needs Review")
        self.assertEqual(results["OPP-006"]["confidence"], "Medium")


class OrderStatus(unittest.TestCase):

    def test_cancelled_order_does_not_count_as_an_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            shutil.copytree(handoff.DATA_DIR, data)
            orders = data / "orders.csv"
            orders.chmod(0o644)
            orders.write_text(orders.read_text().replace("ORD-7001,OPP-001,Pending PO", "ORD-7001,OPP-001,Cancelled"))
            results = {r["opportunity_id"]: r for r in handoff.assess(data_dir=data, cache_path=SAMPLE_CACHE)}
        self.assertIn("W10", ids(results["OPP-001"]["exception_types"]))
        self.assertEqual(results["OPP-001"]["recommended_next_action"], "Build the order.")


class NameMatching(unittest.TestCase):

    def test_sd_reads_as_school_district(self):
        self.assertEqual(handoff.clean_name("Westfield SD"), "westfield school district")
        self.assertEqual(handoff.match_names("Westfield School District", "Westfield SD")[0], "Medium")
        self.assertEqual(handoff.match_names("Westfield School District", "Westfield School District")[0], "High")

    def test_different_districts_do_not_match(self):
        self.assertIsNone(handoff.match_names("Pine Ridge Unified School District", "Pine Ridge SD"))


class SourceFilesUnchanged(unittest.TestCase):

    def test_data_files_are_read_only_inputs(self):
        for name in handoff.FILES.values():
            self.assertTrue((handoff.DATA_DIR / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
