import copy
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.pop("GEMINI_API_KEY", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import ai  # noqa: E402
import configure  # noqa: E402
import engine  # noqa: E402
import interpret  # noqa: E402
import validate  # noqa: E402

PACKS = {m: json.loads((ROOT / "data" / f"market_{m}.json").read_text(encoding="utf-8")) for m in ("us", "in")}
DOC = {("us", 1): "us_plan_v1.txt", ("us", 2): "us_plan_v2.txt", ("in", 1): "in_policy_v1.txt", ("in", 2): "in_policy_v2.txt"}


def build(market, version=1, scope="linked", reject=()):
    text = (ROOT / "data" / "docs" / DOC[(market, version)]).read_text(encoding="utf-8")
    res = interpret.run(PACKS[market], text)
    for r in res["rules"]:
        r["status"] = "rejected" if r["category"] in reject else "approved"
        if r.get("needs_decision"):
            r["decision"] = scope
    cfg = configure.run(PACKS[market], res["rules"], res["parsed"])
    return res, cfg


def claim(market, cid):
    return next(c for c in PACKS[market]["claims"] if c["id"] == cid)


def line(res, lid):
    return next(x for x in res["lines"] if x["id"] == lid)


def reasons(ln):
    return [r["reason"] for r in ln["reductions"]]


class UsEngineTests(unittest.TestCase):
    def setUp(self):
        self.res, self.cfg = build("us")
        self.model, self.pack = self.cfg["model"], PACKS["us"]

    def adj(self, cid, member):
        return engine.adjudicate_us(self.model, claim("us", cid), dict(self.pack["members"][member]), self.pack["tariff"])

    def test_knee_claim_applies_contract_rate_deductible_and_coinsurance(self):
        r = self.adj("CLM-US-1002", "M-US-2")
        knee = line(r, "L1")
        self.assertEqual((knee["allowed"], knee["member"], knee["plan"]), (18500, 4100, 14400))
        self.assertEqual(reasons(knee), ["FEE_SCHEDULE", "DEDUCTIBLE", "COINSURANCE"])
        self.assertEqual(knee["reductions"][1]["ref"]["clause"], "1.1")
        self.assertEqual(line(r, "L2")["member"], 30)
        self.assertEqual(r["totals"]["plan"], 14505)

    def test_clean_visits_pay_with_copays_and_cite_the_copay_clause(self):
        r = self.adj("CLM-US-1001", "M-US-1")
        v, pt = line(r, "L1"), line(r, "L2")
        self.assertEqual((v["allowed"], v["member"], v["plan"]), (135, 30, 105))
        self.assertEqual((pt["allowed"], pt["member"], pt["plan"]), (65, 40, 25))
        self.assertEqual(next(x for x in v["reductions"] if x["reason"] == "COPAY")["ref"]["clause"], "3.1")

    def test_missing_authorization_denies_and_cites_the_authorization_clause(self):
        r = self.adj("CLM-US-1003", "M-US-3")
        mri = line(r, "L1")
        self.assertEqual((mri["status"], reasons(mri)), ("denied", ["NO_AUTH"]))
        self.assertEqual(mri["reductions"][0]["ref"]["clause"], "4.2")

    def test_duplicate_and_excluded_lines_are_denied_and_overpriced_line_is_cut_to_contract(self):
        r = self.adj("CLM-US-1004", "M-US-4")
        self.assertEqual(reasons(line(r, "L2")), ["DUPLICATE"])
        self.assertEqual((line(r, "L3")["status"], reasons(line(r, "L3"))), ("denied", ["NOT_COVERED"]))
        self.assertEqual(line(r, "L4")["allowed"], 65)

    def test_out_of_pocket_cap_limits_the_member_share(self):
        c = {"lines": [{"id": "L1", "code": "X", "service_key": "generic", "qty": 1, "billed": 5000, "date": "d", "allowed_override": 5000}], "authorizations": []}
        r = engine.adjudicate_us(self.model, c, {"deductible_met": 1500, "oop_met": 5900}, {})
        self.assertEqual((r["lines"][0]["member"], r["lines"][0]["plan"]), (100, 4900))
        self.assertIn("OOP_CAP", reasons(r["lines"][0]))


class IndiaEngineTests(unittest.TestCase):
    def adj(self, cid, member, scope="linked"):
        _, cfg = build("in", scope=scope)
        return engine.adjudicate_in(cfg["model"], claim("in", cid), PACKS["in"]["members"][member], PACKS["in"]["tariff"])

    def test_knee_claim_applies_room_cap_proportionate_deduction_and_copay(self):
        r = self.adj("CLM-IN-2001", "M-IN-1", "linked")
        self.assertEqual(r["room_ratio"], 0.556)
        self.assertEqual(line(r, "L1")["allowed"], 35000)
        self.assertAlmostEqual(line(r, "L2")["allowed"], 33333.33, places=1)
        self.assertEqual((line(r, "L6")["status"], reasons(line(r, "L6"))), ("denied", ["NON_PAYABLE"]))
        self.assertIn("TARIFF", reasons(line(r, "L5")))
        self.assertAlmostEqual(r["totals"]["plan"], 161000.0, delta=0.05)

    def test_icu_decision_changes_the_payment_only_when_there_are_icu_charges(self):
        for decision, plan in (("linked", 133000.0), ("icu_exempt", 142600.0)):  # hand-checked: ratio 5/9, ICU Rs. 24,000, copay 10%
            self.assertAlmostEqual(self.adj("CLM-IN-2005", "M-IN-2", decision)["totals"]["plan"], plan, delta=0.05, msg=decision)
        reduced, exempt = self.adj("CLM-IN-2005", "M-IN-2", "linked"), self.adj("CLM-IN-2005", "M-IN-2", "icu_exempt")
        self.assertIn("PROPORTIONATE", reasons(line(reduced, "L2")))
        self.assertNotIn("PROPORTIONATE", reasons(line(exempt, "L2")))
        self.assertEqual(reasons(line(exempt, "L4")), ["COPAY_PCT"])  # the stent is never reduced
        self.assertAlmostEqual(self.adj("CLM-IN-2001", "M-IN-1", "icu_exempt")["totals"]["plan"], 161000.0, delta=0.05)  # no ICU charge: the decision changes nothing

    def test_protected_items_are_never_reduced_even_if_the_item_master_says_room_linked(self):
        _, cfg = build("in")
        self.assertEqual(cfg["model"]["protected_categories"], ["consumable", "diagnostic", "implant", "pharmacy"])
        cl = claim("in", "CLM-IN-2005")
        next(x for x in cl["lines"] if x["id"] == "L4")["room_linked"] = True  # a mis-set flag on the stent
        member = PACKS["in"]["members"]["M-IN-2"]
        safe = engine.adjudicate_in(cfg["model"], cl, member, PACKS["in"]["tariff"])
        self.assertNotIn("PROPORTIONATE", reasons(line(safe, "L4")))
        unsafe_model = dict(cfg["model"], protected_categories=[])
        unsafe = engine.adjudicate_in(unsafe_model, cl, member, PACKS["in"]["tariff"])
        self.assertIn("PROPORTIONATE", reasons(line(unsafe, "L4")))
        self.assertLess(unsafe["totals"]["plan"], safe["totals"]["plan"])

    def test_every_reduction_cites_a_clause_where_a_rule_caused_it(self):
        r = self.adj("CLM-IN-2001", "M-IN-1")
        for ln in r["lines"]:
            for red in ln["reductions"]:
                if red["reason"] in ("ROOM_CAP", "PROPORTIONATE", "COPAY_PCT", "NON_PAYABLE"):
                    self.assertTrue(red["ref"] and red["ref"]["clause"], red["reason"])

    def test_waiting_period_denies_the_whole_claim(self):
        r = self.adj("CLM-IN-2003", "M-IN-3")
        self.assertTrue(r["waiting_denied"])
        self.assertTrue(all(ln["status"] == "denied" and reasons(ln) == ["WAITING_PERIOD"] for ln in r["lines"]))
        self.assertEqual(r["lines"][0]["reductions"][0]["ref"]["clause"], "4.1")

    def test_cataract_within_sub_limit_is_paid_net_of_copay(self):
        r = self.adj("CLM-IN-2002", "M-IN-2")
        self.assertAlmostEqual(r["totals"]["plan"], 40500.0, delta=0.05)
        self.assertEqual(r["totals"]["denied_lines"], 0)

    def test_duplicate_non_medical_and_overpriced_chemo(self):
        r = self.adj("CLM-IN-2004", "M-IN-4")
        self.assertEqual(reasons(line(r, "L3")), ["DUPLICATE"])
        self.assertEqual(reasons(line(r, "L4")), ["NON_PAYABLE"])
        self.assertEqual(line(r, "L1")["allowed"], 45000)
        self.assertAlmostEqual(r["totals"]["plan"], 45000.0, delta=0.05)


class ConfigureAgentTests(unittest.TestCase):
    def test_us_payload_fields_trace_to_rules_and_round_trip_to_the_same_model(self):
        res, cfg = build("us")
        f = {x["field"]: x for x in cfg["fields"]}
        self.assertEqual((f["cost_share.deductible"]["value"], f["cost_share.deductible"]["clause"]), (1500, "1.1"))
        self.assertTrue(all(x["source_rule"] for x in cfg["fields"]))
        back = configure.model_from_fields(cfg["fields"], cfg["model"])
        for k in ("cost_share", "copays", "prior_auth", "exclusions", "deductible_exempt"):
            self.assertEqual(back[k], cfg["model"][k], k)
        self.assertIn("field,value,unit,source_rule,clause", configure.payload_csv(cfg["fields"]))

    def test_india_round_trip_keeps_the_reviewer_scope_decision(self):
        _, cfg = build("in", scope="icu_exempt")
        self.assertEqual(cfg["model"]["proportionate_scope"], "icu_exempt")
        back = configure.model_from_fields(cfg["fields"], cfg["model"])
        self.assertEqual((back["proportionate_scope"], back["sub_limits"], back["waiting_period_days"]),
                         ("icu_exempt", cfg["model"]["sub_limits"], cfg["model"]["waiting_period_days"]))
        self.assertEqual(back["protected_categories"], cfg["model"]["protected_categories"])

    def test_unapproved_rules_stay_out_of_the_model(self):
        _, cfg = build("us", reject=("oop_max",))
        self.assertNotIn("oop_max", cfg["model"]["cost_share"])

    def test_us_mapping_flags_one_to_many_and_missing(self):
        _, cfg = build("us")
        st = {m["service"]: m["status"] for m in cfg["mappings"]}
        self.assertEqual(st["total_knee_replacement"], "mapped")
        self.assertEqual((st["advanced_imaging"], st["office_visit"], st["physical_therapy"]), ("one_to_many",) * 3)
        self.assertEqual(st["preventive"], "missing")
        self.assertNotIn("cosmetic", st)  # a category, not a codeable service

    def test_india_mapping_excludes_superseded_codes_by_effective_date(self):
        _, cfg = build("in")
        by = {m["service"]: m for m in cfg["mappings"]}
        cat = by["cataract"]
        self.assertEqual(cat["status"], "mapped")
        self.assertIn("PKG-OPHTH-CAT", [c["code"] for c in cat["codes"]])
        self.assertEqual([c["code"] for c in cat["excluded"]], ["PKG-OPHTH-CAT-OLD"])
        self.assertEqual(by["joint_replacement"]["status"], "one_to_many")
        self.assertEqual(by["non_medical"]["status"], "mapped")

    def test_ai_may_propose_a_missing_mapping_but_it_stays_unverified(self):
        ai.available, saved = (lambda: True), ai.generate_json
        ai.generate_json = lambda s, u: {"candidates": [{"system": "CPT", "code": "99381"}]}
        try:
            _, cfg = build("us")
        finally:
            ai.available, ai.generate_json = (lambda: False), saved
        prev = next(m for m in cfg["mappings"] if m["service"] == "preventive")
        self.assertEqual((prev["status"], prev["proposed_by_ai"]), ("review", True))
        self.assertIn("NOT verified", prev["note"])


class ValidationAgentTests(unittest.TestCase):
    def run_val(self, market, scope="linked", defect=False):
        res, cfg = build(market, scope=scope)
        fields, note = (validate.inject_defect(cfg["fields"], market) if defect else (cfg["fields"], None))
        return validate.run(PACKS[market], cfg["model"], fields, cfg["mappings"], res["rules"], set(), injected=note), cfg

    def test_a_correct_configuration_passes_every_scenario_in_both_markets(self):
        for market in ("us", "in"):
            v, _ = self.run_val(market)
            failed = [r for r in v["results"] if not r["pass"]]
            self.assertEqual(failed, [], market)
            self.assertGreater(len(v["results"]), 15)
            self.assertTrue(v["package"]["ready"])
            self.assertGreaterEqual(v["package"]["coverage_pct"], 80)

    def test_both_scope_decisions_validate_cleanly(self):
        for scope in ("linked", "icu_exempt"):
            v, _ = self.run_val("in", scope)
            self.assertEqual(v["package"]["tests"]["failed"], 0, scope)

    def test_an_injected_us_keying_error_is_caught_and_blocks_approval(self):
        v, _ = self.run_val("us", defect=True)
        self.assertGreater(v["package"]["tests"]["failed"], 0)
        self.assertFalse(v["package"]["ready"])
        self.assertTrue(any(e["type"] == "defect" and e["severity"] == "block" for e in v["package"]["open_exceptions"]))
        self.assertIn("coinsurance", v["injected"])

    def test_an_injected_india_keying_error_is_caught(self):
        v, _ = self.run_val("in", defect=True)
        self.assertGreater(v["package"]["tests"]["failed"], 0)
        self.assertFalse(v["package"]["ready"])

    def test_mapping_issues_are_review_items_not_blockers_until_confirmed(self):
        v, cfg = self.run_val("us")
        mp = [e for e in v["package"]["open_exceptions"] if e["type"] == "mapping"]
        self.assertTrue(mp and all(e["severity"] == "review" for e in mp))
        res, cfg = build("us")
        v2 = validate.run(PACKS["us"], cfg["model"], cfg["fields"], cfg["mappings"], res["rules"], {m["service"] for m in cfg["mappings"]})
        self.assertEqual([e for e in v2["package"]["open_exceptions"] if e["type"] == "mapping"], [])

    def test_expected_values_come_from_the_approved_model_not_the_loaded_one(self):
        res, cfg = build("us")
        tampered, _ = validate.inject_defect(cfg["fields"], "us")
        v = validate.run(PACKS["us"], cfg["model"], tampered, cfg["mappings"], res["rules"], set())
        bad = next(r for r in v["results"] if not r["pass"])
        self.assertNotEqual(bad["expected"]["plan"], bad["actual"]["plan"])

    def test_log_has_the_expected_steps(self):
        v, _ = self.run_val("us")
        self.assertEqual([s["step"] for s in v["log"]], ["Load the configuration under test", "Generate test scenarios from the rules",
                                                       "Run every scenario and compare", "Measure rule coverage", "Assemble the approval package"])


if __name__ == "__main__":
    unittest.main()
