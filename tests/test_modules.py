import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402

auth.ITERATIONS = 1000
import app as appmod  # noqa: E402
import connectors as cn  # noqa: E402
import service as s  # noqa: E402
from test_claims import publish  # noqa: E402

SANDBOX = TestClient(appmod.app)


def via_testclient(method, url, token, timeout_ms, body=None):
    """The app calls its own sandbox over HTTP. In tests there is no socket, so route that call through the ASGI client."""
    path = urlparse(url).path
    r = SANDBOX.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise ValueError("The system refused the credentials (401).")
    if r.status_code >= 400:
        raise ValueError(f"The system answered with an error ({r.status_code}).")
    return r.json(), 3


class ModuleTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()
        self._orig = cn._http
        cn._http = via_testclient

    def tearDown(self):
        cn._http = self._orig

    def client(self, user):
        c = TestClient(appmod.app)
        self.assertEqual(c.post("/api/auth/login", json={"username": user, "password": auth.DEMO_PASSWORD}).status_code, 200)
        return c

    # ---- reference tables
    def test_every_reference_table_loads_for_every_role_and_market(self):
        publish("in")
        kav = self.client("kavita")
        names = {"benefits": ["crosswalk", "published", "model"], "claims": ["tariff", "members", "items"], "denials": ["reasons", "causes", "escalation"]}
        for market in ("in", "us"):
            for uc, tables in names.items():
                for t in tables:
                    r = kav.get(f"/api/reference/{uc}/{t}?market={market}")
                    self.assertEqual(r.status_code, 200, f"{uc}/{t}/{market}: {r.text}")
                    self.assertIn("title", r.json())

    def test_reference_tables_show_real_data(self):
        publish("in", "icu_exempt")
        kav = self.client("kavita")
        t = kav.get("/api/reference/claims/tariff?market=in").json()
        self.assertEqual(next(r for r in t["rows"] if r["code"] == "ROOM_DELUXE")["rate"], 9000)
        pr = kav.get("/api/reference/benefits/published?market=in").json()
        self.assertIn("version 1", pr["title"])
        self.assertEqual(next(r for r in pr["rows"] if r["label"].startswith("Room-rent"))["clause"], "2.1")
        self.assertEqual(next(r for r in pr["rows"] if "Proportionate" in r["label"])["value"], "Room-linked charges, ICU not reduced")
        self.assertIn("sum_insured", kav.get("/api/reference/benefits/model?market=in").json()["json"])
        esc = kav.get("/api/reference/denials/escalation?market=in").json()
        self.assertTrue(all(r["verified"] == "Not verified" for r in esc["rows"]))

    def test_unknown_table_and_module_and_anonymous_calls(self):
        kav = self.client("kavita")
        self.assertEqual(kav.get("/api/reference/claims/nope?market=in").status_code, 404)
        self.assertEqual(kav.get("/api/reference/nope/tariff?market=in").status_code, 404)
        self.assertEqual(TestClient(appmod.app).get("/api/reference/claims/tariff?market=in").status_code, 401)

    def test_published_table_is_empty_before_anything_is_published(self):
        t = self.client("kavita").get("/api/reference/benefits/published?market=in").json()
        self.assertEqual(t["rows"], [])
        self.assertIn("Nothing is published", t["note"])

    # ---- connections
    def test_connections_start_untested_and_never_claim_connected(self):
        v = self.client("kavita").get("/api/connections/benefits?market=in").json()
        self.assertEqual((v["connected"], v["total"]), (0, 3))
        self.assertTrue(all(c["last_ok"] is None for c in v["connections"]))
        eng = next(c for c in v["connections"] if c["id"] == "benefit_engine")
        self.assertTrue(any(p["label"] == "Base URL" and p["value"].endswith("/sandbox/benefit-engine") for p in eng["params"]))
        self.assertNotIn("sandbox-demo-token", json.dumps(v))  # the token is masked

    def test_testing_a_connection_makes_a_real_call_and_a_wrong_token_fails(self):
        meera = self.client("meera")
        r = meera.post("/api/connections/benefits/benefit_engine/test?market=in").json()
        eng = next(c for c in r["connections"] if c["id"] == "benefit_engine")
        self.assertEqual((eng["last_ok"], r["connected"]), (True, 1))
        self.assertIn("Benefit engine (sandbox) answered", eng["last_message"])
        meera.post("/api/connections/benefits/benefit_engine?market=in", json={"token": "wrong-token"})
        r = meera.post("/api/connections/benefits/benefit_engine/test?market=in").json()
        eng = next(c for c in r["connections"] if c["id"] == "benefit_engine")
        self.assertEqual((eng["last_ok"], r["connected"]), (False, 0))
        self.assertIn("refused the credentials", eng["last_message"])

    def test_editing_a_connection_resets_its_status_and_validates_input(self):
        meera = self.client("meera")
        meera.post("/api/connections/benefits/benefit_engine/test?market=in")
        r = meera.post("/api/connections/benefits/benefit_engine?market=in", json={"environment": "UAT", "timeout_ms": 8000}).json()
        eng = next(c for c in r["connections"] if c["id"] == "benefit_engine")
        self.assertEqual((eng["last_ok"], eng["last_tested"]), (None, None))
        self.assertTrue(any(p["label"] == "Environment" and p["value"] == "UAT" for p in eng["params"]))
        self.assertEqual(meera.post("/api/connections/benefits/benefit_engine?market=in", json={"timeout_ms": 5}).status_code, 422)
        self.assertEqual(meera.post("/api/connections/benefits/benefit_engine?market=in", json={"base_url": "ftp://x"}).status_code, 422)

    def test_the_server_refuses_to_call_other_hosts(self):
        meera = self.client("meera")
        r = meera.post("/api/connections/benefits/benefit_engine?market=in", json={"base_url": "http://169.254.169.254/latest"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("only connects to its own sandbox", r.json()["detail"])
        r = meera.post("/api/connections/benefits/benefit_engine?market=in", json={"base_url": "https://example.com/api"})
        self.assertEqual(r.status_code, 422)

    def test_gemini_connection_says_plainly_when_there_is_no_key(self):
        r = self.client("meera").post("/api/connections/benefits/gemini/test?market=in").json()
        g = next(c for c in r["connections"] if c["id"] == "gemini")
        self.assertEqual(g["last_ok"], False)
        self.assertIn("No GEMINI_API_KEY", g["last_message"])

    def test_internal_connections_report_real_state(self):
        arjun = self.client("arjun")
        r = arjun.post("/api/connections/claims/rules/test?market=in").json()
        self.assertEqual(next(c for c in r["connections"] if c["id"] == "rules")["last_ok"], False)
        publish("in")
        r = arjun.post("/api/connections/claims/rules/test?market=in").json()
        rules = next(c for c in r["connections"] if c["id"] == "rules")
        self.assertEqual(rules["last_ok"], True)
        self.assertIn("version 1", rules["last_message"])

    def test_roles_gate_editing_testing_and_pushing(self):
        meera, arjun, kav = self.client("meera"), self.client("arjun"), self.client("kavita")
        self.assertEqual(kav.post("/api/connections/benefits/benefit_engine/test?market=in").status_code, 403)
        self.assertEqual(arjun.post("/api/connections/benefits/benefit_engine/test?market=in").status_code, 403)
        self.assertEqual(meera.post("/api/connections/claims/claims_core/test?market=in").status_code, 403)
        self.assertEqual(kav.post("/api/push/claims?market=in", json={"ref_id": 1}).status_code, 403)
        self.assertEqual(kav.get("/api/connections/claims?market=in").status_code, 200)

    # ---- the sandbox systems
    def test_sandbox_requires_the_bearer_token(self):
        c = TestClient(appmod.app)
        self.assertEqual(c.get("/sandbox/benefit-engine/health").status_code, 401)
        self.assertEqual(c.get("/sandbox/benefit-engine/health", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.get("/sandbox/benefit-engine/health", headers={"Authorization": f"Bearer {cn.SANDBOX_TOKEN}"}).status_code, 200)
        self.assertEqual(c.get("/sandbox/nowhere/health", headers={"Authorization": f"Bearer {cn.SANDBOX_TOKEN}"}).status_code, 404)

    # ---- pushing real payloads
    def test_benefit_configuration_is_pushed_only_after_publishing_and_a_receipt_is_kept(self):
        meera = self.client("meera")
        rid = s.create_run("in", {"name": "Meera Rao", "role": "benefits_analyst"}, sample_id="in_v1")["id"]
        self.assertEqual(meera.post("/api/push/benefits?market=in", json={"ref_id": rid}).status_code, 422)
        publish("in")  # a second, published run
        run_id = max(s.STATE["runs"])
        r = meera.post("/api/push/benefits?market=in", json={"ref_id": run_id})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["receipt"].startswith("SBX-"))
        got = meera.get("/api/connections/benefits/benefit_engine/receipts").json()
        self.assertEqual((got[0]["kind"], got[0]["receipt"]), ("benefit_configuration", r.json()["receipt"]))

    def test_decision_and_letter_are_pushed_with_receipts(self):
        import casework as cw
        from test_claims import CLM, work
        publish("in", "linked")
        arjun = self.client("arjun")
        cid = work("CLM-IN-2001")
        self.assertEqual(arjun.post("/api/push/claims?market=in", json={"ref_id": cid}).status_code, 422)  # no decision yet
        cw.decide(cid, "approve", "ok", CLM)
        r = arjun.post("/api/push/claims?market=in", json={"ref_id": cid})
        self.assertEqual(r.status_code, 200, r.text)
        cw.open_appeal(cid, CLM, None, "contest")
        for step in ("classify", "recheck", "draft"):
            self.assertEqual(arjun.post(f"/api/denials/appeals/{cid}/{step}").status_code, 200)
        self.assertEqual(arjun.post("/api/push/denials?market=in", json={"ref_id": cid}).status_code, 422)  # not approved yet
        arjun.post(f"/api/denials/appeals/{cid}/approve")
        r = arjun.post("/api/push/denials?market=in", json={"ref_id": cid})
        self.assertEqual(r.status_code, 200, r.text)
        kinds = {x["kind"] for uc, n in (("claims", "claims_core"), ("denials", "outbox")) for x in arjun.get(f"/api/connections/{uc}/{n}/receipts").json()}
        self.assertEqual(kinds, {"claim_decision", "grievance_letter"})

    def test_kpis_for_the_overview_pages_are_computed(self):
        import casework as cw
        from test_claims import CLM, work
        publish("in", "linked")
        cid = work("CLM-IN-2001")
        cw.open_appeal(cid, CLM); cw.run_classify(cid, CLM); cw.run_recheck(cid, CLM); cw.run_draft(cid, CLM)
        k = self.client("kavita").get("/api/kpis?market=in").json()["claims"]
        self.assertEqual((k["adjudicated"], k["lines_total"], k["lines_verified"], k["appeals_rechecked"], k["deductions_total"], k["deductions_supported"], k["letters_verified"]), (1, 7, 7, 1, 5, 3, 1))


if __name__ == "__main__":
    unittest.main()
