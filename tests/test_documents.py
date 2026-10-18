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
import docpdf  # noqa: E402
import interpret  # noqa: E402
import service as s  # noqa: E402
from docparse import parse_document  # noqa: E402
from test_claims import CLM, publish, work  # noqa: E402

PACK = s.PACKS["in"]
POLICY = (Path(__file__).resolve().parents[1] / "data" / "docs" / "in_policy_v1.txt").read_text(encoding="utf-8")
PNG = b"\x89PNG\r\n\x1a\n"


class DocumentTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()

    def client(self, user):
        c = TestClient(appmod.app)
        self.assertEqual(c.post("/api/auth/login", json={"username": user, "password": auth.DEMO_PASSWORD}).status_code, 200)
        return c

    def test_sample_claims_and_members_endpoint_lists_every_sample(self):
        r = self.client("kavita").get("/api/claims/samples?market=in")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([c["id"] for c in r.json()["claims"]], ["CLM-IN-2001", "CLM-IN-2002", "CLM-IN-2003", "CLM-IN-2004", "CLM-IN-2005"])
        self.assertEqual(len(r.json()["members"]), 4)
        self.assertEqual(len(self.client("kavita").get("/api/claims/samples?market=us").json()["claims"]), 4)

    # ---- policy documents as PDFs
    def test_the_policy_becomes_a_real_pdf_whose_pages_match_the_page_markers(self):
        pdf, layout = docpdf.policy_pdf(POLICY)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(docpdf.page_count(pdf), 6)
        self.assertEqual({k: v["page"] for k, v in layout.items()}, {"1.1": 1, "2.1": 2, "2.2": 2, "3.1": 3, "3.2": 3, "4.1": 4, "4.2": 4, "5.1": 5, "5.2": 5, "5.3": 5, "6.1": 6})
        self.assertIn("Implants, medicines, consumables", " ".join(docpdf.pymupdf.open("pdf", pdf)[1].get_text().split()))  # the PDF carries the words of clause 2.2 on page 2

    def test_run_endpoints_serve_the_pdf_and_highlighted_pages(self):
        meera = self.client("meera")
        rid = meera.post("/api/benefits/runs", json={"market": "in", "sample_id": "in_v1"}).json()["id"]
        r = meera.get(f"/api/benefits/runs/{rid}/document.pdf")
        self.assertEqual((r.status_code, r.headers["content-type"], r.content[:4]), (200, "application/pdf", b"%PDF"))
        plain = meera.get(f"/api/benefits/runs/{rid}/page/2.png")
        marked = meera.get(f"/api/benefits/runs/{rid}/page/2.png?clause=2.2")
        self.assertEqual((plain.status_code, plain.content[:8], marked.content[:8]), (200, PNG, PNG))
        self.assertNotEqual(plain.content, marked.content)  # the cited clause is highlighted
        self.assertEqual(meera.get(f"/api/benefits/runs/{rid}/page/2.png?clause=9.9").content, plain.content)  # an unknown clause highlights nothing
        self.assertEqual(meera.get(f"/api/benefits/runs/{rid}/page/99.png").status_code, 404)
        self.assertEqual(meera.get("/api/benefits/runs/999/document.pdf").status_code, 404)

    def test_documents_need_a_signed_in_user_but_any_role_can_read_them(self):
        meera = self.client("meera")
        rid = meera.post("/api/benefits/runs", json={"market": "in", "sample_id": "in_v1"}).json()["id"]
        self.assertEqual(TestClient(appmod.app).get(f"/api/benefits/runs/{rid}/document.pdf").status_code, 401)
        self.assertEqual(self.client("kavita").get(f"/api/benefits/runs/{rid}/document.pdf").status_code, 200)

    def test_an_uploaded_pdf_is_kept_as_uploaded(self):
        original = docpdf.text_pdf("UPLOADED POLICY\n[Page 1]\n1.1 The sum insured is Rs. 5,00,000 per policy year.", "Uploaded")
        with mock.patch.object(s.ai, "available", return_value=True), mock.patch.object(s.ai, "transcribe", return_value="UPLOADED POLICY\n[Page 1]\n1.1 The sum insured is Rs. 5,00,000 per policy year."):
            import base64
            run = s.create_run("in", {"name": "Meera Rao", "role": "benefits_analyst"}, filename="p.pdf", mime="application/pdf", file_b64=base64.b64encode(original).decode())
        self.assertEqual(s.doc_pdf(run["id"]), original)
        self.assertNotIn("_pdf", run)  # bytes never leak into the JSON view

    # ---- claim documents as PDFs
    def test_the_three_supporting_documents_are_pdfs_with_page_counts(self):
        arjun = self.client("arjun")
        case = arjun.post("/api/claims/cases", json={"market": "in", "claim_id": "CLM-IN-2001"}).json()
        self.assertEqual(set(case["doc_pages"]), {"bill", "discharge", "preauth"})
        for kind in ("bill", "discharge", "preauth"):
            r = arjun.get(f"/api/claims/cases/{case['id']}/doc/{kind}.pdf")
            self.assertEqual((r.status_code, r.content[:4]), (200, b"%PDF"), kind)
            self.assertEqual(arjun.get(f"/api/claims/cases/{case['id']}/doc/{kind}/page/1.png").content[:8], PNG)
        self.assertIn("Ravi Kumar", docpdf.pymupdf.open("pdf", arjun.get(f"/api/claims/cases/{case['id']}/doc/bill.pdf").content)[0].get_text())
        self.assertEqual(arjun.get(f"/api/claims/cases/{case['id']}/doc/nonsense.pdf").status_code, 404)

    def test_a_claim_without_a_discharge_summary_offers_only_the_documents_it_has(self):
        arjun = self.client("arjun")
        case = arjun.post("/api/claims/cases", json={"market": "us", "claim_id": "CLM-US-1001"}).json()
        self.assertEqual(set(case["doc_pages"]), {"bill"})
        self.assertEqual(arjun.get(f"/api/claims/cases/{case['id']}/doc/discharge.pdf").status_code, 404)

    def test_a_long_bill_continues_on_a_second_page(self):
        rows = "\n".join(f"{i} | C{i} | Item number {i} | 1 | 2026-10-01 | {i * 100}" for i in range(1, 60))
        pdf = docpdf.text_pdf("HOSPITAL: FINAL BILL (SYNTHETIC)\nPatient: Test\nITEMS (item no | code | description | qty | date | amount)\n" + rows + "\nTotal billed: 1", "Long bill")
        self.assertGreaterEqual(docpdf.page_count(pdf), 2)

    # ---- page and clause provenance
    def test_every_rule_is_checked_to_sit_in_the_clause_and_page_it_cites(self):
        res = interpret.run(PACK, POLICY)
        self.assertTrue(all(r["evidence"]["page_clause"] for r in res["rules"]))

    def test_a_rule_that_cites_the_wrong_page_or_clause_is_flagged_and_cannot_be_approved(self):
        parsed = parse_document(POLICY)
        for field, wrong in (("page", 5), ("clause", "9.9")):
            rules = interpret.fallback_extract(parsed)
            rules[1][field] = wrong  # a model "read" the right quote but the wrong location
            out = interpret._finish(rules, parsed, POLICY, PACK, "gemini")
            r = out[1]
            self.assertTrue(r["evidence"]["quote_found"] and not r["evidence"]["page_clause"], field)
            self.assertTrue(any(f["severity"] == "block" and "not in clause" in f["text"] for f in r["flags"]), field)
            self.assertIn("page and clause", s._can_approve(r))
            self.assertTrue(all(o["evidence"]["page_clause"] for o in out if o is not r))

    # ---- the patient letter template
    def test_the_template_has_a_default_and_lists_its_placeholders(self):
        v = self.client("kavita").get("/api/claims/letter-template?market=in").json()
        self.assertTrue(v["is_default"])
        self.assertIn("{deductions}", v["template"])
        self.assertEqual({p["name"] for p in v["placeholders"] if p["required"]}, {"deductions", "plan_pays", "patient_pays"})

    def test_a_saved_template_shapes_the_letter_and_the_letter_is_still_verified(self):
        publish("in")
        arjun = self.client("arjun")
        tpl = "Hello {patient},\n\nAbout claim {claim_id}: {outcome_line}\n\nThe insurer pays {plan_pays}. You pay {patient_pays}.\n\nWhy:\n{deductions}\n\nWarm regards, Claims Desk"
        self.assertEqual(arjun.post("/api/claims/letter-template?market=in", json={"template": tpl}).status_code, 200)
        e = cw.get_case(work("CLM-IN-2001"))["explain"]
        self.assertTrue(e["letter"].startswith("Hello Ravi Kumar,"))
        self.assertIn("Warm regards, Claims Desk", e["letter"])
        self.assertIn("Rs. 1,61,000", e["letter"])
        self.assertEqual((e["verification"]["amounts_ok"], e["verification"]["clauses_ok"]), (True, True))
        self.assertFalse(arjun.get("/api/claims/letter-template?market=in").json()["is_default"])
        self.assertTrue(arjun.get("/api/claims/letter-template?market=us").json()["is_default"])  # per market
        self.assertTrue(arjun.post("/api/claims/letter-template?market=in", json={"template": ""}).json()["is_default"])  # reset

    def test_templates_with_unknown_or_missing_placeholders_are_refused(self):
        arjun = self.client("arjun")
        bad = {"Hello {patient} {nonsense} {deductions} {plan_pays} {patient_pays} and more words here to be long enough": "Unknown placeholder",
               "Hello {patient}. This template is long enough but has no deductions or amounts in it at all.": "must include", "too short": "between 40 and 3000"}
        for text, why in bad.items():
            r = arjun.post("/api/claims/letter-template?market=in", json={"template": text})
            self.assertEqual(r.status_code, 422, text)
            self.assertIn(why, r.json()["detail"])

    def test_a_template_cannot_smuggle_in_a_number_the_verifier_would_reject(self):
        publish("in")
        arjun = self.client("arjun")
        tpl = "Dear {patient}, you will receive Rs. 9,99,999. {outcome_line} {plan_pays} {patient_pays}\n{deductions}\nThank you for your patience with us."
        self.assertEqual(arjun.post("/api/claims/letter-template?market=in", json={"template": tpl}).status_code, 200)
        e = cw.get_case(work("CLM-IN-2001"))["explain"]
        self.assertFalse(e["verification"]["amounts_ok"])  # the reviewer sees that the letter carries a number that is not in the calculation
        self.assertEqual(e["verification"]["unknown_amounts"], [999999.0])

    def test_only_a_claims_analyst_can_change_the_template_and_anyone_can_preview(self):
        meera, kavita, arjun = self.client("meera"), self.client("kavita"), self.client("arjun")
        tpl = claims.DEFAULT_LETTER_TEMPLATE
        self.assertEqual(meera.post("/api/claims/letter-template?market=in", json={"template": tpl}).status_code, 403)
        self.assertEqual(kavita.post("/api/claims/letter-template?market=in", json={"template": tpl}).status_code, 403)
        p = kavita.post("/api/claims/letter-template/preview?market=in", json={"template": tpl}).json()["preview"]
        self.assertTrue(p.startswith("PREVIEW WITH SAMPLE VALUES"))
        self.assertIn("Sample Patient", p)
        self.assertEqual(arjun.post("/api/claims/letter-template/preview?market=in", json={"template": "short"}).status_code, 422)

    def test_the_template_is_given_to_gemini_as_the_house_style_and_the_result_is_still_checked(self):
        publish("in")
        cid = work("CLM-IN-2001", explain=False)
        cw.set_letter_template("in", "Dear {patient}, {outcome_line}\n{deductions}\nPays {plan_pays}, you pay {patient_pays}. Kind regards, the Desk.", CLM)
        seen = {}

        def fake(system, user):
            seen["user"] = user
            return "Dear Ravi Kumar, the plan pays Rs. 1,61,000 under clause 2.1. Kind regards, the Desk."
        with mock.patch.object(claims.ai, "available", return_value=True), mock.patch.object(claims.ai, "generate_text", side_effect=fake):
            e = cw.run_explain(cid, CLM)["explain"]
        self.assertIn("HOUSE TEMPLATE", seen["user"])
        self.assertIn("Kind regards, the Desk.", seen["user"])
        self.assertEqual(e["source"], "gemini")


if __name__ == "__main__":
    unittest.main()
