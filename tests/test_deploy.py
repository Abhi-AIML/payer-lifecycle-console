"""Deployment and hardening: health check, security headers, agent rate limit, Gemini key handling, sandbox routing, structured logs."""
import json
import logging
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
import ai  # noqa: E402
import app as appmod  # noqa: E402
import connectors as cn  # noqa: E402
import logs  # noqa: E402
import service as s  # noqa: E402


class HardeningTests(unittest.TestCase):
    def setUp(self):
        s.reset()
        appmod.build_users()

    def login(self, user="meera"):
        c = TestClient(appmod.app)
        self.assertEqual(c.post("/api/auth/login", json={"username": user, "password": auth.DEMO_PASSWORD}).status_code, 200)
        return c

    def test_health_check_needs_no_sign_in_and_reveals_nothing(self):
        r = TestClient(appmod.app).get("/healthz")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))

    def test_pages_get_a_content_security_policy_and_api_responses_are_not_cached(self):
        c = TestClient(appmod.app)
        page = c.get("/")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        api = c.get("/api/auth/me")
        self.assertNotIn("content-security-policy", api.headers)
        self.assertEqual(api.headers["cache-control"], "no-store")

    def test_hsts_and_secure_cookie_only_over_https(self):
        r = TestClient(appmod.app).post("/api/auth/login", json={"username": "meera", "password": auth.DEMO_PASSWORD})
        self.assertNotIn("strict-transport-security", r.headers)
        self.assertNotIn("secure", r.headers["set-cookie"].lower())
        r = TestClient(appmod.app, base_url="https://testserver").post("/api/auth/login", json={"username": "meera", "password": auth.DEMO_PASSWORD})
        self.assertIn("max-age", r.headers["strict-transport-security"])
        self.assertIn("secure", r.headers["set-cookie"].lower())

    def test_agent_runs_are_rate_limited_per_account(self):
        meera = self.login()
        rid = meera.post("/api/benefits/runs", json={"market": "us", "sample_id": "us_v1"}).json()["id"]
        with mock.patch.object(appmod.AGENT_LIMIT, "limit", 2):
            appmod.AGENT_LIMIT.reset()
            self.assertEqual(meera.post(f"/api/benefits/runs/{rid}/interpret").status_code, 200)
            self.assertEqual(meera.post(f"/api/benefits/runs/{rid}/interpret").status_code, 200)
            r = meera.post(f"/api/benefits/runs/{rid}/interpret")
            self.assertEqual(r.status_code, 429)
            self.assertGreater(int(r.headers["retry-after"]), 0)
            self.assertEqual(meera.get(f"/api/benefits/runs/{rid}").status_code, 200)  # reads are never limited
        self.assertEqual(s.STATE["audit"][-1]["action"], "rate_limited")

    def test_sign_in_page_gets_the_demo_password_only_when_allowed(self):
        c = TestClient(appmod.app)
        self.assertEqual(c.get("/api/auth/demo").json(), {"password": auth.DEMO_PASSWORD})
        with mock.patch.object(appmod.config, "SHOW_DEMO_PASSWORD", False):
            self.assertEqual(c.get("/api/auth/demo").json(), {"password": None})


class GeminiKeyTests(unittest.TestCase):
    def test_key_travels_in_a_header_never_in_the_url(self):
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return Resp()

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret-key", "GEMINI_MODEL": "gemini-test"}), \
             mock.patch.object(ai.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(ai.generate_text("sys", "hi"), "ok")
        self.assertNotIn("secret-key", seen[0].full_url)
        self.assertEqual(seen[0].get_header("X-goog-api-key"), "secret-key")


class SandboxRoutingTests(unittest.TestCase):
    def test_own_public_address_is_called_over_loopback_on_cloud_run(self):
        with mock.patch.dict(os.environ, {"PORT": "8080"}):
            self.assertEqual(cn._route("https://console-abc.a.run.app/sandbox/claims-core/health"), "http://127.0.0.1:8080/sandbox/claims-core/health")
            self.assertEqual(cn._route("http://127.0.0.1:9000/x"), "http://127.0.0.1:9000/x")
        self.assertEqual(cn._route("http://localhost:8110/x"), "http://127.0.0.1:8110/x")

    def test_sandbox_token_is_never_a_published_constant(self):
        self.assertNotEqual(cn.SANDBOX_TOKEN, "sandbox-demo-token")
        self.assertFalse(cn.sandbox_auth("Bearer wrong"))
        self.assertTrue(cn.sandbox_auth(f"Bearer {cn.SANDBOX_TOKEN}"))


class StructuredLogTests(unittest.TestCase):
    def test_audit_line_is_json_with_severity_and_a_filterable_label(self):
        record = logging.LogRecord("payer.audit", logging.INFO, __file__, 1, "login", None, None)
        record.fields = {"logging.googleapis.com/labels": {"kind": "audit"}}
        line = json.loads(logs.JsonFormatter().format(record))
        self.assertEqual((line["severity"], line["message"], line["logging.googleapis.com/labels"]["kind"]), ("INFO", "login", "audit"))


if __name__ == "__main__":
    unittest.main()
