import json
import os
import sys
import unittest
from pathlib import Path

os.environ.pop("GEMINI_API_KEY", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import ai  # noqa: E402
import interpret  # noqa: E402

PACKS = {m: json.loads((ROOT / "data" / f"market_{m}.json").read_text(encoding="utf-8")) for m in ("us", "in")}
DOCS = {n: (ROOT / "data" / "docs" / n).read_text(encoding="utf-8") for n in ("us_plan_v1.txt", "us_plan_v2.txt", "in_policy_v1.txt", "in_policy_v2.txt")}


def run(market, doc, **kw):
    return interpret.run(PACKS[market], DOCS[doc], **kw)


def by_cat(res, cat, service=None):
    return [r for r in res["rules"] if r["category"] == cat and (service is None or r.get("service") == service)]


class FallbackExtractionTests(unittest.TestCase):
    def test_us_plan_yields_ten_rules_with_correct_values_and_citations(self):
        res = run("us", "us_plan_v1.txt")
        self.assertEqual((res["extractor"], res["summary"]["rules"]), ("rules-fallback", 10))
        self.assertEqual(by_cat(res, "deductible")[0]["value"], 1500)
        self.assertEqual((by_cat(res, "coinsurance_member_pct")[0]["value"], by_cat(res, "oop_max")[0]["value"]), (20, 6000))
        self.assertEqual(by_cat(res, "copay", "physical_therapy")[0]["value"], 40)
        d = by_cat(res, "deductible")[0]
        self.assertEqual((d["page"], d["clause"]), (1, "1.1"))
        self.assertEqual({r["service"] for r in by_cat(res, "prior_auth")}, {"total_knee_replacement", "advanced_imaging"})
        self.assertEqual({r["service"] for r in by_cat(res, "exclusion")}, {"cosmetic", "experimental"})
        self.assertEqual(res["summary"]["missing_required"], [])

    def test_india_policy_yields_eleven_rules_and_flags_the_proportionate_scope(self):
        res = run("in", "in_policy_v1.txt")
        self.assertEqual(res["summary"]["rules"], 11)
        self.assertEqual(by_cat(res, "sum_insured")[0]["value"], 500000)
        self.assertEqual(by_cat(res, "room_rent_cap")[0]["value"], 5000)
        self.assertEqual(by_cat(res, "waiting_period", "joint_replacement")[0]["value"], 730)
        self.assertEqual(by_cat(res, "sub_limit", "total_knee_replacement")[0]["per"], "admission")
        prop = by_cat(res, "proportionate_scope")[0]
        self.assertTrue(prop["needs_decision"] and prop["decision"] is None)
        self.assertIn("surgeon", [i.lower() for i in prop["items"]])
        self.assertEqual(prop["protected"], ["implants", "medicines", "consumables", "diagnostic tests"])
        self.assertIn("ICU", prop["ambiguity"]["title"])
        self.assertNotIn("May 2024", prop["ambiguity"]["message"].replace("(29 May 2024)", ""))  # no claim that a circular fixes the scope
        self.assertEqual(res["summary"]["needs_decision"], 1)

    def test_every_extracted_rule_passes_its_own_evidence_check(self):
        for market, doc in (("us", "us_plan_v1.txt"), ("in", "in_policy_v1.txt")):
            res = run(market, doc)
            self.assertEqual(res["summary"]["flagged"], 0, market)
            self.assertTrue(all(r["evidence"]["quote_found"] and r["evidence"]["value_in_quote"] for r in res["rules"]))

    def test_log_records_each_step_in_order(self):
        steps = [s["step"] for s in run("us", "us_plan_v1.txt")["log"]]
        self.assertEqual(steps[0], "Read and classify the document")
        self.assertIn("Verify evidence for every rule", steps)
        self.assertEqual(steps[-1], "Check required rules")


class EvidenceVerificationTests(unittest.TestCase):
    def test_a_quote_that_is_not_in_the_document_is_blocked(self):
        rule = {"category": "deductible", "service": None, "value": 9999, "raw_value": 9999, "quote": "The annual deductible is $9,999 per member."}
        ev = interpret.verify_rule(rule, DOCS["us_plan_v1.txt"])
        self.assertFalse(ev["quote_found"])

    def test_a_value_that_is_not_in_its_quote_is_blocked(self):
        rule = {"category": "deductible", "service": None, "value": 2500, "raw_value": 2500,
                "quote": "The annual deductible is $1,500 per member and applies to inpatient and outpatient services."}
        ev = interpret.verify_rule(rule, DOCS["us_plan_v1.txt"])
        self.assertTrue(ev["quote_found"])
        self.assertFalse(ev["value_in_quote"])

    def test_whitespace_and_case_do_not_defeat_a_genuine_quote(self):
        rule = {"category": "oop_max", "service": None, "value": 6000, "raw_value": 6000, "quote": "the  OUT-OF-POCKET maximum is $6,000   per member per year."}
        self.assertTrue(all(interpret.verify_rule(rule, DOCS["us_plan_v1.txt"]).values()))


class FakeGeminiTests(unittest.TestCase):
    """The Gemini path, with the network call replaced. Proves hallucinations are caught, not trusted."""

    def setUp(self):
        self._avail, self._gen = ai.available, ai.generate_json
        ai.available = lambda: True
        ai.model = lambda: "fake-model"

    def tearDown(self):
        ai.available, ai.generate_json = self._avail, self._gen

    def test_good_rules_pass_and_a_hallucinated_rule_is_flagged_not_dropped(self):
        ai.generate_json = lambda system, user: {"rules": [
            {"category": "deductible", "service": None, "value": 1500, "raw_value": 1500, "unit": "currency", "clause": "1.1", "page": 1,
             "quote": "The annual deductible is $1,500 per member and applies to inpatient and outpatient services."},
            {"category": "oop_max", "service": None, "value": 8000, "raw_value": 8000, "unit": "currency", "clause": "2.2", "page": 2,
             "quote": "The out-of-pocket maximum is $8,000 per member per year."},
            {"category": "not_a_category", "value": 1, "quote": "x"}]}
        res = run("us", "us_plan_v1.txt")
        self.assertEqual(res["extractor"], "gemini")
        self.assertEqual(res["summary"]["rules"], 2)
        good, bad = res["rules"]
        self.assertEqual(good["flags"], [])
        self.assertTrue(any(f["severity"] == "block" for f in bad["flags"]))
        self.assertEqual(res["summary"]["flagged"], 1)

    def test_unusable_gemini_output_falls_back_and_says_so(self):
        ai.generate_json = lambda system, user: None
        res = run("us", "us_plan_v1.txt")
        self.assertEqual(res["extractor"], "rules-fallback")
        self.assertTrue(any(s["status"] == "warn" and "did not return" in s["step"] for s in res["log"]))


class ConflictAndDiffTests(unittest.TestCase):
    def test_two_clauses_with_different_values_conflict(self):
        text = DOCS["us_plan_v1.txt"] + "\n[Page 6]\n6.1 The annual deductible is $2,000 per member.\n"
        res = interpret.run(PACKS["us"], text)
        flagged = by_cat(res, "deductible")
        self.assertEqual(len(flagged), 2)
        self.assertTrue(all(any("Conflicts" in f["text"] for f in r["flags"]) for r in flagged))

    def test_us_amendment_diff(self):
        d = interpret.diff_rules(run("us", "us_plan_v1.txt")["rules"], run("us", "us_plan_v2.txt")["rules"])
        got = {x["label"]: (x["old"], x["new"], x["old_clause"]) for x in d}
        self.assertEqual(got, {"Annual deductible": (1500, 1000, "1.1"), "Coinsurance (member pays)": (20, 10, "2.1")})

    def test_india_amendment_diff(self):
        d = interpret.diff_rules(run("in", "in_policy_v1.txt")["rules"], run("in", "in_policy_v2.txt")["rules"])
        got = {x["label"]: (x["old"], x["new"]) for x in d}
        self.assertEqual(got, {"Room-rent cap per day": (5000, 7000), "Co-payment": (10, 5)})

    def test_added_and_removed_rules_are_detected(self):
        base = run("us", "us_plan_v1.txt")["rules"]
        d = interpret.diff_rules(base, base[:-1])
        self.assertEqual([x["change"] for x in d], ["removed"])


if __name__ == "__main__":
    unittest.main()
