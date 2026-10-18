import base64
import os
import sys
import unittest
from pathlib import Path

os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402

auth.ITERATIONS = 1000  # cheap hashing for tests only
import app as appmod  # noqa: E402
import service as s  # noqa: E402

PW = auth.DEMO_PASSWORD


def client(user):
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": user, "password": PW})
    assert r.status_code == 200, r.text
    return c


class FlowTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()
        self.m = client("meera")

    def start(self, market="us", sample=None):
        sample = sample or f"{market}_v1"
        r = self.m.post("/api/benefits/runs", json={"market": market, "sample_id": sample})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def approve_all(self, rid, scope="linked"):
        run = self.m.get(f"/api/benefits/runs/{rid}").json()
        for r in run["rules"]:
            if r.get("needs_decision"):
                self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/rules/{r['id']}/decision", json={"value": scope}).status_code, 200)
            self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/rules/{r['id']}/status", json={"status": "approved"}).status_code, 200, r["label"])

    def confirm_all(self, rid, cfg):
        for mp in cfg["configure"]["mappings"]:
            if mp["status"] != "mapped":
                self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/mappings/{mp['service']}/confirm").status_code, 200)

    def test_nothing_runs_until_the_person_clicks_run_agent(self):
        rid = self.start()
        run = self.m.get(f"/api/benefits/runs/{rid}").json()
        self.assertEqual((run["stage"], run["rules"], run["interpret"]), ("document_loaded", [], None))
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/configure").status_code, 422)
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/validate", json={}).status_code, 422)

    def test_full_us_flow_publishes_a_rule_set_with_human_gates_between_agents(self):
        rid = self.start("us")
        r = self.m.post(f"/api/benefits/runs/{rid}/interpret").json()
        self.assertEqual((r["stage"], r["interpret"]["summary"]["rules"], r["interpret"]["extractor"]), ("interpreted", 10, "rules-fallback"))
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/configure").status_code, 422)  # rules not yet reviewed
        self.approve_all(rid)
        cfg = self.m.post(f"/api/benefits/runs/{rid}/configure").json()
        self.assertEqual(cfg["stage"], "configured")
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/approve").status_code, 422)  # not validated yet
        v = self.m.post(f"/api/benefits/runs/{rid}/validate", json={}).json()
        self.assertTrue(v["validate"]["package"]["ready"])
        blocked = self.m.post(f"/api/benefits/runs/{rid}/approve")
        self.assertEqual(blocked.status_code, 422)  # mappings still need a coding reviewer
        self.assertIn("confirm these mappings", blocked.json()["detail"])
        self.confirm_all(rid, cfg)
        done = self.m.post(f"/api/benefits/runs/{rid}/approve").json()
        self.assertEqual((done["stage"], done["approval"]["version"]), ("approved", 1))
        pub = self.m.get("/api/benefits/published?market=us").json()
        self.assertEqual((pub["version"], pub["rule_count"], pub["published_by"]), (1, 10, "Meera Rao"))
        self.assertIsNone(self.m.get("/api/benefits/published?market=in").json())

    def test_india_needs_the_scope_decision_before_the_rule_can_be_approved(self):
        rid = self.start("in")
        run = self.m.post(f"/api/benefits/runs/{rid}/interpret").json()
        prop = next(r for r in run["rules"] if r.get("needs_decision"))
        blocked = self.m.post(f"/api/benefits/runs/{rid}/rules/{prop['id']}/status", json={"status": "approved"})
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("scope", blocked.json()["detail"].lower())
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/rules/{prop['id']}/decision", json={"value": "nonsense"}).status_code, 422)
        self.approve_all(rid, "icu_exempt")
        cfg = self.m.post(f"/api/benefits/runs/{rid}/configure").json()
        self.assertEqual(cfg["configure"]["model"]["proportionate_scope"], "icu_exempt")

    def test_a_simulated_keying_error_is_caught_and_cannot_be_published(self):
        rid = self.start("us")
        self.m.post(f"/api/benefits/runs/{rid}/interpret")
        self.approve_all(rid)
        cfg = self.m.post(f"/api/benefits/runs/{rid}/configure").json()
        self.confirm_all(rid, cfg)
        v = self.m.post(f"/api/benefits/runs/{rid}/validate", json={"inject_defect": True}).json()
        self.assertGreater(v["validate"]["package"]["tests"]["failed"], 0)
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/approve").status_code, 422)
        clean = self.m.post(f"/api/benefits/runs/{rid}/validate", json={}).json()
        self.assertEqual(clean["validate"]["package"]["tests"]["failed"], 0)
        self.assertEqual(self.m.post(f"/api/benefits/runs/{rid}/approve").status_code, 200)

    def test_changing_a_rule_after_configuration_sends_the_run_back_to_review(self):
        rid = self.start("us")
        self.m.post(f"/api/benefits/runs/{rid}/interpret")
        self.approve_all(rid)
        self.m.post(f"/api/benefits/runs/{rid}/configure")
        ded = next(r for r in self.m.get(f"/api/benefits/runs/{rid}").json()["rules"] if r["category"] == "deductible")
        run = self.m.post(f"/api/benefits/runs/{rid}/rules/{ded['id']}/edit", json={"value": 1800}).json()
        self.assertEqual((run["stage"], run["configure"]), ("interpreted", None))
        edited = next(r for r in run["rules"] if r["id"] == ded["id"])
        self.assertEqual((edited["value"], edited["status"], edited["edited"]), (1800, "proposed", True))
        self.assertTrue(any("edited by a reviewer" in f["text"] for f in edited["flags"]))

    def test_amendment_run_shows_what_changed_against_the_published_version(self):
        rid = self.start("us")
        self.m.post(f"/api/benefits/runs/{rid}/interpret")
        self.approve_all(rid)
        cfg = self.m.post(f"/api/benefits/runs/{rid}/configure").json()
        self.confirm_all(rid, cfg)
        self.m.post(f"/api/benefits/runs/{rid}/validate", json={})
        self.m.post(f"/api/benefits/runs/{rid}/approve")
        rid2 = self.start("us", "us_v2")
        run2 = self.m.post(f"/api/benefits/runs/{rid2}/interpret").json()
        self.assertEqual({d["label"]: (d["old"], d["new"]) for d in run2["diff"]}, {"Annual deductible": (1500, 1000), "Coinsurance (member pays)": (20, 10)})

    def test_pasted_text_works_and_bad_input_is_refused_clearly(self):
        r = self.m.post("/api/benefits/runs", json={"market": "us", "text": "[Page 1]\n1.1 The annual deductible is $2,000 per member.\n"})
        self.assertEqual(r.status_code, 200)
        run = self.m.post(f"/api/benefits/runs/{r.json()['id']}/interpret").json()
        self.assertEqual(run["rules"][0]["value"], 2000)
        self.assertEqual(self.m.post("/api/benefits/runs", json={"market": "us"}).status_code, 422)
        self.assertEqual(self.m.post("/api/benefits/runs", json={"market": "fr", "sample_id": "us_v1"}).status_code, 422)

    def test_pdf_upload_without_a_gemini_key_is_refused_with_a_clear_reason(self):
        r = self.m.post("/api/benefits/runs", json={"market": "us", "filename": "plan.pdf", "mime": "application/pdf", "file_b64": base64.b64encode(b"%PDF-1.4 fake").decode()})
        self.assertEqual(r.status_code, 422)
        self.assertIn("Gemini key", r.json()["detail"])

    def test_text_file_upload_works(self):
        txt = (appmod.ROOT / "data" / "docs" / "us_plan_v1.txt").read_bytes()
        r = self.m.post("/api/benefits/runs", json={"market": "us", "filename": "plan.txt", "mime": "text/plain", "file_b64": base64.b64encode(txt).decode()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.m.post(f"/api/benefits/runs/{r.json()['id']}/interpret").json()["interpret"]["summary"]["rules"], 10)


class AccessTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()

    def test_unauthenticated_requests_are_refused_but_pages_load(self):
        c = TestClient(appmod.app)
        for path in ("/api/meta", "/api/benefits/samples?market=us", "/api/benefits/published?market=us", "/api/auth/me"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.get("/").status_code, 200)

    def test_only_the_benefits_analyst_can_run_agents_and_publish(self):
        rid = client("meera").post("/api/benefits/runs", json={"market": "us", "sample_id": "us_v1"}).json()["id"]
        for who in ("arjun", "kavita"):
            c = client(who)
            self.assertEqual(c.get(f"/api/benefits/runs/{rid}").status_code, 200, who)  # may read
            self.assertEqual(c.post("/api/benefits/runs", json={"market": "us", "sample_id": "us_v1"}).status_code, 403, who)
            self.assertEqual(c.post(f"/api/benefits/runs/{rid}/interpret").status_code, 403, who)
            self.assertEqual(c.post(f"/api/benefits/runs/{rid}/approve").status_code, 403, who)
            self.assertEqual(c.post(f"/api/benefits/runs/{rid}/rules/R1/status", json={"status": "approved"}).status_code, 403, who)

    def test_the_server_decides_who_acted(self):
        c = client("meera")
        rid = c.post("/api/benefits/runs", json={"market": "us", "sample_id": "us_v1", "created_by": "Mallory", "actor": "Mallory"}).json()["id"]
        self.assertEqual(s.get_run(rid)["created_by"], "Meera Rao")
        self.assertEqual(s.STATE["audit"][-1]["actor"], "Meera Rao")

    def test_lockout_and_logout_work(self):
        c = TestClient(appmod.app)
        for _ in range(auth.MAX_FAILS):
            self.assertEqual(c.post("/api/auth/login", json={"username": "meera", "password": "x"}).status_code, 401)
        self.assertEqual(c.post("/api/auth/login", json={"username": "meera", "password": PW}).status_code, 429)
        auth.FAILS["meera"]["locked_until"] = 0
        c = client("meera")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        c.post("/api/auth/logout")
        self.assertEqual(c.get("/api/auth/me").status_code, 401)

    def test_cookie_is_httponly_and_has_its_own_name(self):
        r = TestClient(appmod.app).post("/api/auth/login", json={"username": "meera", "password": PW})
        cookie = r.headers["set-cookie"].lower()
        self.assertTrue(cookie.startswith("payer_session="))
        self.assertIn("httponly", cookie)

    def test_cross_site_post_is_blocked(self):
        c = client("meera")
        r = c.post("/api/benefits/runs", json={"market": "us", "sample_id": "us_v1"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
