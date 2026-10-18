import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402

auth.ITERATIONS = 1000
import app as appmod  # noqa: E402
import casework as cw  # noqa: E402
import claims  # noqa: E402
import service as s  # noqa: E402

BEN = {"name": "Meera Rao", "role": "benefits_analyst"}
CLM = {"name": "Arjun Mehta", "role": "claims_analyst"}


def publish(market="in", scope="linked", sample=None):
    """Run the three benefit agents on a sample and publish: the claims tile only reads published rules."""
    r = s.create_run(market, BEN, sample_id=sample or f"{market}_v1")["id"]
    s.run_interpret(r, BEN)
    for x in s.get_run(r)["rules"]:
        if x.get("needs_decision"):
            s.set_decision(r, x["id"], scope, BEN)
        s.set_rule_status(r, x["id"], "approved", BEN)
    s.run_configure(r, BEN)
    for m in s.get_run(r)["configure"]["mappings"]:
        if m["status"] != "mapped":
            s.confirm_mapping(r, m["service"], BEN)
    s.run_validate(r, BEN)
    return s.approve_run(r, BEN)


def work(claim_id, market="in", explain=True):
    cid = cw.create_case(market, CLM, claim_id=claim_id)["id"]
    cw.run_intake(cid, CLM)
    cw.run_adjudicate(cid, CLM)
    if explain:
        cw.run_explain(cid, CLM)
    return cid


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        s.reset()

    def test_adjudication_needs_a_published_rule_set(self):
        cid = cw.create_case("in", CLM, claim_id="CLM-IN-2001")["id"]
        cw.run_intake(cid, CLM)
        with self.assertRaisesRegex(ValueError, "No rule set is published"):
            cw.run_adjudicate(cid, CLM)

    def test_nothing_runs_until_asked(self):
        c = cw.create_case("in", CLM, claim_id="CLM-IN-2001")
        self.assertEqual((c["stage"], c["intake"], c["adjudication"]), ("loaded", None, None))
        with self.assertRaisesRegex(ValueError, "Intake Agent first"):
            cw.run_adjudicate(c["id"], CLM)

    def test_intake_reads_every_line_and_verifies_it_in_the_bill(self):
        cid = cw.create_case("in", CLM, claim_id="CLM-IN-2001")["id"]
        i = cw.run_intake(cid, CLM)["intake"]
        self.assertEqual((i["summary"]["lines"], i["summary"]["verified"], i["extractor"]), (7, 7, "fallback"))
        self.assertEqual(i["summary"]["checks"]["fail"], 0)
        self.assertAlmostEqual(i["summary"]["billed"], 261000)

    def test_intake_catches_an_unsigned_discharge_summary(self):
        cid = cw.create_case("in", CLM, claim_id="CLM-IN-2004")["id"]
        checks = {c["id"]: c for c in cw.run_intake(cid, CLM)["intake"]["checks"]}
        self.assertEqual(checks["signature"]["status"], "fail")

    def test_intake_flags_a_bill_whose_total_does_not_add_up(self):
        cl = next(c for c in s.pack("in")["claims"] if c["id"] == "CLM-IN-2002")
        bill = claims.documents(s.pack("in"), cl)["bill"].replace("Total billed: 45,000", "Total billed: 49,000")
        cid = cw.create_case("in", CLM, text=bill, member_id="M-IN-2")["id"]
        checks = {c["id"]: c for c in cw.run_intake(cid, CLM)["intake"]["checks"]}
        self.assertEqual(checks["total"]["status"], "fail")

    def test_pasted_bill_for_the_wrong_patient_fails_the_member_check(self):
        cl = next(c for c in s.pack("in")["claims"] if c["id"] == "CLM-IN-2002")
        bill = claims.documents(s.pack("in"), cl)["bill"]
        cid = cw.create_case("in", CLM, text=bill, member_id="M-IN-2")["id"]
        self.assertEqual(cw.run_intake(cid, CLM)["intake"]["summary"]["checks"]["fail"], 0)
        with self.assertRaises(ValueError):
            cw.create_case("in", CLM, text=bill, member_id="NOPE")

    def test_hand_checked_amounts_and_the_icu_decision(self):
        publish("in", "linked")
        a = cw.get_case(work("CLM-IN-2001"))["adjudication"]
        self.assertEqual((a["totals"]["plan"], a["totals"]["member"]), (161000.0, 100000.0))
        self.assertAlmostEqual(next(b for b in a["by_reason"] if b["reason"] == "PROPORTIONATE")["amount"], 43111.11, places=2)
        self.assertEqual(cw.get_case(work("CLM-IN-2005"))["adjudication"]["totals"]["plan"], 133000.0)  # ICU treated as room-linked
        s.reset()
        publish("in", "icu_exempt")
        self.assertEqual(cw.get_case(work("CLM-IN-2001"))["adjudication"]["totals"]["plan"], 161000.0)  # no ICU on this claim: unchanged
        self.assertEqual(cw.get_case(work("CLM-IN-2005"))["adjudication"]["totals"]["plan"], 142600.0)  # the reviewer's decision is worth Rs. 9,600 here

    def test_every_reduction_cites_a_clause_when_a_rule_caused_it(self):
        publish("in")
        a = cw.get_case(work("CLM-IN-2001"))["adjudication"]
        by = {b["reason"]: b for b in a["by_reason"]}
        self.assertEqual(by["ROOM_CAP"]["ref"]["clause"], "2.1")
        self.assertEqual(by["COPAY_PCT"]["ref"]["clause"], "5.1")
        self.assertIn("proportion", by["PROPORTIONATE"]["quote"])
        self.assertIsNone(by["TARIFF"]["ref"])  # a contract rule, not a policy clause

    def test_outcomes_and_routing(self):
        publish("in")
        out = {cl: cw.get_case(work(cl))["adjudication"] for cl in ("CLM-IN-2002", "CLM-IN-2003", "CLM-IN-2004")}
        self.assertEqual(out["CLM-IN-2002"]["outcome"], "approve")
        self.assertIn("fast-track", out["CLM-IN-2002"]["route"])
        self.assertEqual(out["CLM-IN-2003"]["outcome"], "reject")
        self.assertIn("insurer's medical team", out["CLM-IN-2003"]["route"])
        self.assertEqual(out["CLM-IN-2004"]["outcome"], "query")  # unsigned discharge summary blocks a decision
        self.assertTrue(any("1.78x" in f["text"] for f in out["CLM-IN-2004"]["flags"]))

    def test_waiting_period_denial_leaves_the_full_bill_with_the_patient(self):
        publish("in")
        a = cw.get_case(work("CLM-IN-2003"))["adjudication"]
        self.assertEqual((a["totals"]["plan"], a["totals"]["member"]), (0.0, 80500.0))

    def test_a_query_outcome_cannot_be_approved(self):
        publish("in")
        cid = work("CLM-IN-2004")
        with self.assertRaisesRegex(ValueError, "Query the hospital"):
            cw.decide(cid, "approve", "", CLM)
        self.assertEqual(cw.decide(cid, "query", "Need a signed summary", CLM)["decision"]["action"], "query")

    def test_decision_needs_the_explanation_first(self):
        publish("in")
        cid = work("CLM-IN-2002", explain=False)
        with self.assertRaisesRegex(ValueError, "Explanation Agent first"):
            cw.decide(cid, "approve", "", CLM)

    def test_new_published_version_makes_old_adjudication_stale_and_reprocess_shows_the_change(self):
        publish("in", "linked")
        cid = work("CLM-IN-2005")
        publish("in", "icu_exempt", sample="in_v1")  # a second version with a different ICU decision
        self.assertTrue(cw.get_case(cid)["stale"])
        with self.assertRaisesRegex(ValueError, "changed"):
            cw.run_explain(cid, CLM)
        a = cw.run_adjudicate(cid, CLM)["adjudication"]
        self.assertEqual((a["rule_version"], a["reprocessed_from"]["version"], a["reprocessed_from"]["plan"], a["totals"]["plan"]), (2, 1, 133000.0, 142600.0))

    def test_template_letter_passes_its_own_verification(self):
        publish("in")
        e = cw.get_case(work("CLM-IN-2001"))["explain"]
        self.assertEqual((e["source"], e["verification"]["amounts_ok"], e["verification"]["clauses_ok"]), ("template", True, True))
        self.assertIn("Rs. 2,61,000", e["letter"])
        self.assertIn("Policy clause 2.1", e["letter"])

    def test_verifier_rejects_an_invented_amount_or_clause(self):
        publish("in")
        f = cw.get_case(work("CLM-IN-2001"))["explain"]["facts"]
        self.assertEqual(claims.verify_letter("Payable Rs. 1,61,000 under clause 2.1.", f)["amounts_ok"], True)
        bad = claims.verify_letter("Payable Rs. 1,75,000 under clause 9.9.", f)
        self.assertEqual((bad["amounts_ok"], bad["clauses_ok"], bad["unknown_amounts"], bad["unknown_clauses"]), (False, False, [175000.0], ["9.9"]))

    def test_an_ai_letter_with_an_invented_number_is_replaced_by_the_template(self):
        publish("in")
        cid = work("CLM-IN-2001", explain=False)
        with mock.patch.object(claims.ai, "available", return_value=True), mock.patch.object(claims.ai, "generate_text", return_value="You will receive Rs. 9,99,999 (clause 2.1)."):
            e = cw.run_explain(cid, CLM)["explain"]
        self.assertEqual(e["source"], "template")
        self.assertIn("rejected", e["note"])
        self.assertNotIn("9,99,999", e["letter"])

    def test_an_ai_letter_built_only_from_the_facts_is_accepted(self):
        publish("in")
        cid = work("CLM-IN-2001", explain=False)
        good = "Dear Ravi Kumar, your claim pays Rs. 1,61,000. Room rent was capped by clause 2.1. DRAFT for reviewer approval."
        with mock.patch.object(claims.ai, "available", return_value=True), mock.patch.object(claims.ai, "generate_text", return_value=good):
            e = cw.run_explain(cid, CLM)["explain"]
        self.assertEqual((e["source"], e["letter"]), ("gemini", good))

    def test_a_gemini_bill_that_does_not_match_the_text_is_flagged_by_the_evidence_check(self):
        publish("in")
        cl = next(c for c in s.pack("in")["claims"] if c["id"] == "CLM-IN-2002")
        bill = claims.documents(s.pack("in"), cl)["bill"]
        forged = claims.parse_bill(bill)
        forged["lines"][1]["billed"] = 99999.0  # the model "read" an amount that is not in the document
        cid = cw.create_case("in", CLM, text=bill, member_id="M-IN-2")["id"]
        with mock.patch.object(claims.ai, "available", return_value=True), mock.patch.object(claims.ai, "generate_json", return_value=None), \
                mock.patch.object(claims, "gemini_bill", return_value=forged):
            i = cw.run_intake(cid, CLM)["intake"]
        self.assertEqual(i["extractor"], "gemini")
        self.assertFalse(i["lines"][1]["evidence"]["amount_found"])
        self.assertEqual(next(c for c in i["checks"] if c["id"] == "evidence")["status"], "fail")

    def test_us_market_also_works(self):
        publish("us")
        a = cw.get_case(work("CLM-US-1004", "us"))["adjudication"]
        self.assertEqual(a["totals"]["denied_lines"], 2)
        self.assertEqual(next(b for b in a["by_reason"] if b["reason"] == "NOT_COVERED")["amount"], 3200.0)


class AppealTests(unittest.TestCase):
    def setUp(self):
        s.reset()

    def run_appeal(self, claim_id, market="in", letter=None, lens="contest"):
        cid = work(claim_id, market)
        cw.open_appeal(cid, CLM, letter, lens)
        cw.run_classify(cid, CLM)
        cw.run_recheck(cid, CLM)
        return cid, cw.run_draft(cid, CLM)

    def test_an_appeal_needs_an_explained_claim(self):
        publish("in")
        cid = work("CLM-IN-2001", explain=False)
        with self.assertRaisesRegex(ValueError, "explain the claim"):
            cw.open_appeal(cid, CLM)

    def test_agents_must_run_in_order(self):
        publish("in")
        cid = work("CLM-IN-2001")
        cw.open_appeal(cid, CLM)
        with self.assertRaisesRegex(ValueError, "Classification Agent first"):
            cw.run_recheck(cid, CLM)
        cw.run_classify(cid, CLM)
        with self.assertRaisesRegex(ValueError, "Policy Recheck Agent first"):
            cw.run_draft(cid, CLM)

    def test_a_ratio_applied_to_an_implant_is_a_strong_ground_that_rests_on_the_policy_text(self):
        for scope in ("linked", "icu_exempt"):  # the ground does not depend on the reviewer's ICU decision
            s.reset()
            publish("in", scope)
            cid, d = self.run_appeal("CLM-IN-2001")
            rc = cw.get_appeal(cid)["recheck"]
            self.assertEqual((rc["merit"], rc["letter_payable"], rc["rules_payable"], rc["pay_diff"]), ("grounds", 125000.0, 161000.0, 36000.0), scope)
        g = rc["grounds"][0]
        self.assertEqual((g["strength"], g["clause"], g["page"]), ("strong", "2.2", 2))
        self.assertIn("implants", g["text"])
        self.assertIn("not reduced", g["text"])
        self.assertNotIn("circular", json.dumps(rc).lower())  # nothing here leans on a regulator claim
        self.assertEqual((d["draft"]["draft_type"], d["draft"]["verification"]["amounts_ok"], d["draft"]["verification"]["clauses_ok"]), ("appeal", True, True))
        self.assertIn("Rs. 36,000", d["draft"]["letter"])
        self.assertTrue(any("clause 2.2" in c for c in rc["checklist"]))

    def test_the_audit_lens_drafts_a_correction_note_before_issue_not_an_appeal(self):
        publish("in", "linked")
        cid, d = self.run_appeal("CLM-IN-2001", lens="audit")
        rc, dr = cw.get_appeal(cid)["recheck"], d["draft"]
        self.assertEqual((rc["merit"], rc["draft_type"], dr["draft_type"]), ("grounds", "correction", "correction"))
        self.assertIn("Pre-issue audit", dr["letter"])
        self.assertIn("Correction needed before issue", dr["letter"])
        self.assertNotIn("Grievance", dr["letter"])
        self.assertTrue(dr["verification"]["amounts_ok"] and dr["verification"]["clauses_ok"])
        self.assertTrue(all("enclose" not in c.lower() for c in rc["checklist"]))

    def test_audit_lens_clears_a_correct_decision_to_issue_and_drafts_nothing(self):
        publish("in")
        cid, d = self.run_appeal("CLM-IN-2002", lens="audit")
        rc = cw.get_appeal(cid)["recheck"]
        self.assertEqual((rc["merit"], rc["draft_type"], d["draft"]["letter"]), ("none", "none", None))

    def test_the_lens_is_validated_and_recorded(self):
        publish("in")
        cid = work("CLM-IN-2002")
        with self.assertRaisesRegex(ValueError, "audit or contest"):
            cw.open_appeal(cid, CLM, None, "bogus")
        self.assertEqual(cw.open_appeal(cid, CLM, None, "contest")["lens"], "contest")

    def test_a_clean_claim_gets_no_appeal_at_all(self):
        publish("in")
        cid, d = self.run_appeal("CLM-IN-2002")
        rc = cw.get_appeal(cid)["recheck"]
        self.assertEqual((rc["merit"], rc["draft_type"], d["draft"]["letter"], d["draft"]["draft_type"]), ("none", "none", None, "none"))
        with self.assertRaisesRegex(ValueError, "no letter"):
            cw.approve_appeal(cid, CLM)

    def test_wrong_reason_cited_but_amount_right_gets_a_clarification_not_an_appeal(self):
        publish("in")
        cid, d = self.run_appeal("CLM-IN-2003")
        rc = cw.get_appeal(cid)["recheck"]
        self.assertEqual((rc["merit"], rc["draft_type"], rc["pay_diff"]), ("clarify", "clarification", 0.0))
        self.assertEqual(rc["verdicts"][0]["verdict"], "other_reason")
        self.assertIn("state the correct clause", rc["grounds"][0]["text"])
        self.assertIn("exact policy clause", d["draft"]["letter"])
        self.assertIn("(clause 4.2)", rc["grounds"][0]["text"])
        self.assertTrue(d["draft"]["verification"]["clauses_ok"])

    def test_a_repeated_charge_is_a_possible_ground_that_needs_evidence(self):
        publish("in")
        cid, d = self.run_appeal("CLM-IN-2004")
        rc = cw.get_appeal(cid)["recheck"]
        self.assertEqual([g["title"] for g in rc["grounds"]], ["The repeated charge may be a genuine second service"])
        self.assertTrue(any("two times" in c or "twice" in c for c in rc["checklist"]))

    def test_classification_verifies_amounts_and_names_the_prevention(self):
        publish("in")
        cid = work("CLM-IN-2004")
        cw.open_appeal(cid, CLM)
        c = cw.run_classify(cid, CLM)["classify"]
        self.assertEqual((c["extractor"], c["summary"]["unknown"], c["summary"]["verified"], c["summary"]["deductions"]), ("fallback", 0, 4, 4))
        self.assertTrue(any("repeated charges" in e["edit"].lower() for e in c["edits"]))

    def test_a_pasted_letter_with_an_amount_it_does_not_contain_is_caught(self):
        publish("in")
        cid = work("CLM-IN-2002")
        cw.open_appeal(cid, CLM)
        a = cw._appeal(cid)
        with mock.patch.object(denials_mod().ai, "available", return_value=True), mock.patch.object(denials_mod().ai, "generate_json", return_value={
                "billed": 45000, "payable": 40500, "deductions": [{"code": "IN-COPAY", "label": "Co-payment applied", "amount": 7777, "clause": "5.1"}]}):
            c = cw.run_classify(cid, CLM)["classify"]
        self.assertEqual(c["extractor"], "gemini")
        self.assertFalse(c["items"][0]["amount_found"])
        self.assertEqual(c["summary"]["verified"], 0)
        self.assertEqual(a["stage"], "classified")

    def test_changed_rules_make_the_recheck_refuse_until_the_claim_is_adjudicated_again(self):
        publish("in", "linked")
        cid = work("CLM-IN-2001")
        cw.open_appeal(cid, CLM)
        cw.run_classify(cid, CLM)
        publish("in", "icu_exempt")
        with self.assertRaisesRegex(ValueError, "rules changed"):
            cw.run_recheck(cid, CLM)

    def test_us_appeal_uses_the_insurer_settings_that_differ(self):
        publish("us")
        cid, d = self.run_appeal("CLM-US-1002", "us")
        rc = cw.get_appeal(cid)["recheck"]
        self.assertGreater(rc["rules_payable"], rc["letter_payable"])
        self.assertEqual(rc["merit"], "grounds")


def denials_mod():
    import denials
    return denials


class ApiTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()

    def client(self, user):
        c = TestClient(appmod.app)
        self.assertEqual(c.post("/api/auth/login", json={"username": user, "password": auth.DEMO_PASSWORD}).status_code, 200)
        return c

    def test_only_a_claims_analyst_can_work_claims_everyone_can_read(self):
        publish("in")
        arjun, meera, kavita = self.client("arjun"), self.client("meera"), self.client("kavita")
        self.assertEqual(meera.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2001"}).status_code, 403)
        self.assertEqual(kavita.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2001"}).status_code, 403)
        cid = arjun.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2001"}).json()["id"]
        for name in ("intake", "adjudicate", "explain"):
            self.assertEqual(arjun.post(f"/api/claims/cases/{cid}/{name}").status_code, 200, name)
        self.assertEqual(meera.post(f"/api/claims/cases/{cid}/decide", json={"action": "approve", "note": ""}).status_code, 403)
        self.assertEqual(kavita.get(f"/api/claims/cases/{cid}").status_code, 200)
        self.assertEqual(kavita.get("/api/claims/cases?market=in").status_code, 200)
        self.assertEqual(arjun.post(f"/api/claims/cases/{cid}/decide", json={"action": "approve", "note": "ok"}).json()["decision"]["by"], "Arjun Mehta")

    def test_the_decision_is_recorded_under_the_signed_in_user_not_a_supplied_name(self):
        publish("in")
        arjun = self.client("arjun")
        cid = arjun.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2002"}).json()["id"]
        for name in ("intake", "adjudicate", "explain"):
            arjun.post(f"/api/claims/cases/{cid}/{name}")
        r = arjun.post(f"/api/claims/cases/{cid}/decide", json={"action": "approve", "note": "x", "by": "Somebody Else"})
        self.assertEqual(r.json()["decision"]["by"], "Arjun Mehta")

    def test_appeal_endpoints_flow_and_are_role_gated(self):
        publish("in", "icu_exempt")
        arjun, meera = self.client("arjun"), self.client("meera")
        cid = arjun.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2001"}).json()["id"]
        for name in ("intake", "adjudicate", "explain"):
            arjun.post(f"/api/claims/cases/{cid}/{name}")
        self.assertEqual(meera.post("/api/denials/appeals", json={"case_id": cid}).status_code, 403)
        self.assertEqual(arjun.post("/api/denials/appeals", json={"case_id": cid}).status_code, 200)
        for name in ("classify", "recheck", "draft", "approve"):
            self.assertEqual(arjun.post(f"/api/denials/appeals/{cid}/{name}").status_code, 200, name)
        got = meera.get(f"/api/denials/appeals/{cid}").json()
        self.assertEqual((got["stage"], got["approval"]["by"], got["recheck"]["merit"]), ("approved", "Arjun Mehta", "grounds"))
        self.assertEqual(meera.get("/api/denials/appeals?market=in").json()[0]["merit"], "grounds")

    def test_errors_are_plain_messages_and_unauthenticated_calls_are_refused(self):
        self.assertEqual(TestClient(appmod.app).get("/api/claims/cases?market=in").status_code, 401)
        arjun = self.client("arjun")
        self.assertEqual(arjun.post("/api/claims/cases/999/intake").status_code, 404)
        r = arjun.post("/api/claims/cases", json={"market": "in"})
        self.assertEqual((r.status_code, "Choose a sample claim" in r.json()["detail"]), (422, True))
        self.assertEqual(arjun.post("/api/claims/cases", json={"market": "xx", "claim_id": "a"}).status_code, 422)

    def test_scanned_bills_need_gemini_and_say_so(self):
        arjun = self.client("arjun")
        r = arjun.post("/api/claims/cases", json={"market": "in", "filename": "bill.pdf", "mime": "application/pdf", "file_b64": "JVBERi0=", "member_id": "M-IN-1"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("Gemini key", r.json()["detail"])

    def test_meta_and_kpis_expose_what_the_pages_need(self):
        publish("in")
        arjun = self.client("arjun")
        self.assertEqual(arjun.get("/api/meta").json()["in"]["reasons"]["ROOM_CAP"], "Room rent above policy limit")
        cid = arjun.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2002"}).json()["id"]
        arjun.post(f"/api/claims/cases/{cid}/intake")
        arjun.post(f"/api/claims/cases/{cid}/adjudicate")
        k = arjun.get("/api/kpis?market=in").json()
        self.assertEqual((k["claims"]["cases"], k["claims"]["adjudicated"], k["claims"]["plan"]), (1, 1, 40500.0))


if __name__ == "__main__":
    unittest.main()
