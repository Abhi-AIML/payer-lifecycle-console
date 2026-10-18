"""System connections for each module, with a real "Test connection" and a real push.

The demo ships a sandbox that plays the downstream systems (a benefit engine, a claims core, a grievance outbox). They are part of this app,
require a bearer token, and keep what they receive, so testing and pushing do real HTTP calls and the receipts are real.
A connection shows "Connected" only after a test succeeded. Nothing here claims a connection that has not been tested.

Safety: the server only calls its own host or loopback, never an arbitrary address, so this cannot be used to probe other machines.
Calls to its own public address are routed over loopback (see _route), so on Cloud Run they never leave the instance.
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

import ai
import service as s

# From Secret Manager on Cloud Run; otherwise random per process. Never a fixed, published value.
SANDBOX_TOKEN = os.environ.get("SANDBOX_TOKEN") or secrets.token_urlsafe(24)
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
SYSTEMS = {"benefit-engine": "benefit_engine", "claims-core": "claims_core", "grievance-outbox": "outbox"}  # sandbox URL segment -> connection id
WORK_ROLE = {"benefits": "benefits_analyst", "claims": "claims_analyst", "denials": "claims_analyst"}
DEFS = {
    "benefits": [
        {"id": "gemini", "label": "Gemini", "type": "ai", "desc": "Reads policy documents and PDFs and proposes rules. Every quote and value is then verified in code."},
        {"id": "benefit_engine", "label": "Benefit engine", "type": "rest", "path": "/sandbox/benefit-engine", "push": "Send the configuration to the benefit engine",
         "desc": "The system that receives the published configuration payload. A sandbox endpoint provided by this demo."},
        {"id": "crosswalk", "label": "Code crosswalk", "type": "internal", "desc": "Effective-dated service-to-code mappings used by the Configuration & Coding Agent."}],
    "claims": [
        {"id": "rules", "label": "Published rule set", "type": "internal", "desc": "The approved rules the Adjudication Agent applies. Published on the Benefit module."},
        {"id": "claims_core", "label": "Claims core", "type": "rest", "path": "/sandbox/claims-core", "push": "Post the decision to the claims core",
         "desc": "The system of record that receives each decided claim. A sandbox endpoint provided by this demo."},
        {"id": "gemini", "label": "Gemini", "type": "ai", "desc": "Reads scanned bills and writes the settlement letter. Every amount and clause in the letter is verified against the calculation."}],
    "denials": [
        {"id": "rules", "label": "Published rule set", "type": "internal", "desc": "The approved rules the Policy Recheck Agent recomputes against."},
        {"id": "outbox", "label": "Grievance outbox", "type": "rest", "path": "/sandbox/grievance-outbox", "push": "Queue the approved letter or note in the outbox",
         "desc": "Where an approved appeal or clarification letter is queued for dispatch. A sandbox endpoint provided by this demo."},
        {"id": "gemini", "label": "Gemini", "type": "ai", "desc": "Reads settlement letters in other formats and drafts the appeal. Every amount and clause is verified against the recheck facts."}]}


def _store() -> dict:
    return s.STATE.setdefault("connections", {})


def _find(uc: str, name: str) -> dict:
    d = next((x for x in DEFS.get(uc, []) if x["id"] == name), None)
    if not d:
        raise LookupError("Connection not found.")
    return d


def _conf(uc: str, name: str) -> dict:
    return _store().setdefault(f"{uc}/{name}", {"base_url": None, "token": SANDBOX_TOKEN, "environment": "Sandbox", "timeout_ms": 5000,
                                                "last_tested": None, "last_ok": None, "last_message": None, "last_ms": None})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def effective_url(d: dict, c: dict, origin: str) -> str:
    return (c["base_url"] or origin.rstrip("/") + d["path"]).rstrip("/")


def _guard(url: str, origin: str) -> None:
    """Only the app's own host (or loopback) may be called."""
    host, ok = urlparse(url), {urlparse(origin).hostname} | LOOPBACK
    if host.scheme not in ("http", "https") or host.hostname not in ok:
        raise ValueError("This demo only connects to its own sandbox or to localhost. Use the default address, or another localhost address.")


def _route(url: str) -> str:
    """Rewrites a guarded URL to the address actually called.

    "localhost" becomes 127.0.0.1: on Windows "localhost" tries IPv6 first and waits; the server listens on IPv4.
    The app's own public address becomes loopback on $PORT (set by Cloud Run): the sandbox is this app, so the call
    stays on the same instance and its in-memory receipts, with no public HTTPS round trip.
    """
    u = urlparse(url)
    if u.hostname == "localhost":
        return u._replace(netloc=u.netloc.replace("localhost", "127.0.0.1", 1)).geturl()
    if u.hostname not in LOOPBACK and os.environ.get("PORT"):  # _guard allows only loopback or our own host
        return u._replace(scheme="http", netloc=f"127.0.0.1:{os.environ['PORT']}").geturl()
    return url


def _http(method: str, url: str, token: str, timeout_ms: int, body=None):
    url = _route(url)
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as r:
            return json.load(r), round((time.perf_counter() - t0) * 1000)
    except urllib.error.HTTPError as e:
        raise ValueError("The system refused the credentials (401)." if e.code == 401 else f"The system answered with an error ({e.code}).") from None
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Could not reach the system ({type(e).__name__}).") from None


def view(uc: str, market: str, origin: str) -> dict:
    out = []
    for d in DEFS[uc]:
        c = _conf(uc, d["id"])
        if d["type"] == "rest":
            params = [("Base URL", effective_url(d, c, origin), "base_url"), ("Auth type", "Bearer token", None), ("Token", "••••••••••••", "token"),
                      ("Environment", c["environment"], "environment"), ("Timeout (ms)", str(c["timeout_ms"]), "timeout_ms")]
        elif d["type"] == "ai":
            live = ai.available()
            params = [("Provider", "Gemini API", None), ("Model", ai.model() if live else "not set", None), ("API key", "Configured (hidden)" if live else "Not set: add GEMINI_API_KEY to .env", None), ("Timeout", "60 s", None)]
        else:
            params = [("Source", "Market pack" if d["id"] == "crosswalk" else "Published rule set", None)]
        out.append({"id": d["id"], "label": d["label"], "type": d["type"], "desc": d["desc"], "push": d.get("push"), "editable": d["type"] == "rest",
                    "params": [{"label": a, "value": b, "key": k} for a, b, k in params], **{k: c[k] for k in ("last_tested", "last_ok", "last_message", "last_ms")},
                    "received": len([x for x in s.STATE.setdefault("sandbox", []) if x["system"] == d["id"]])})
    return {"uc": uc, "market": market, "connected": sum(1 for x in out if x["last_ok"]), "total": len(out), "connections": out}


def update(uc: str, name: str, body: dict, user: dict, market: str, origin: str) -> dict:
    d = _find(uc, name)
    if d["type"] != "rest":
        raise ValueError("This connection has no editable settings.")
    c = _conf(uc, name)
    if body.get("base_url") is not None:
        u = str(body["base_url"]).strip()
        if u:
            if len(u) > 300 or not u.startswith(("http://", "https://")):
                raise ValueError("Enter an http or https address.")
            _guard(u, origin)
        c["base_url"] = u or None
    if body.get("token"):
        if len(str(body["token"])) > 200:
            raise ValueError("The token is too long.")
        c["token"] = str(body["token"])
    if body.get("environment") is not None:
        c["environment"] = str(body["environment"]).strip()[:30] or "Sandbox"
    if body.get("timeout_ms") is not None:
        t = int(body["timeout_ms"])
        if not 500 <= t <= 30000:
            raise ValueError("Timeout must be between 500 and 30000 ms.")
        c["timeout_ms"] = t
    c.update(last_ok=None, last_message="Settings changed. Test the connection again.", last_tested=None, last_ms=None)
    s.audit(user, "connection_updated", f"{uc}/{name}")
    return view(uc, market, origin)


def test(uc: str, name: str, user: dict, market: str, origin: str) -> dict:
    d, c = _find(uc, name), _conf(uc, name)
    t0 = time.perf_counter()
    if d["type"] == "ai":
        if not ai.available():
            ok, msg = False, "No GEMINI_API_KEY is configured, so the agents use the labelled rule-based reader."
        else:
            out = ai.generate_text("Reply with the single word: ok", "ping")
            ok, msg = bool(out), (f"Gemini answered ({ai.model()})." if out else "Gemini did not answer: " + (ai.LAST_ERROR["message"] or "unknown error"))
    elif d["type"] == "internal":
        if d["id"] == "crosswalk":
            p = s.pack(market)
            ok, msg = True, f"{len(p['crosswalk'])} services and {sum(len(v['codes']) for v in p['crosswalk'].values())} codes loaded."
        else:
            pub = s.STATE["published"][market]
            ok, msg = bool(pub), (f"Rule set version {pub['version']} is published with {len(pub['rules'])} rules." if pub else "No rule set is published yet. Publish one on the Benefit module.")
    else:
        url = effective_url(d, c, origin)
        _guard(url, origin)
        try:
            res, ms = _http("GET", url + "/health", c["token"], c["timeout_ms"])
            ok, msg = bool(res.get("ok")), f"{res.get('system', name)} answered in {ms} ms."
        except ValueError as e:
            ok, msg = False, str(e)
    c.update(last_tested=_now(), last_ok=ok, last_message=msg, last_ms=round((time.perf_counter() - t0) * 1000))
    s.audit(user, "connection_tested", f"{uc}/{name}: {'ok' if ok else 'failed'}", market)
    return view(uc, market, origin)


# ---------------------------------------------------------------- pushing real payloads to the sandbox systems
def _payload(uc: str, ref_id: int, market: str) -> tuple[str, dict]:
    import casework as cw
    if uc == "benefits":
        run = s._get(ref_id)
        if run["stage"] != "approved" or not run["configure"]:
            raise ValueError("Publish the rule set first. Only a published configuration is sent.")
        pub = s.STATE["published"][run["market"]]
        return "benefit_engine", {"kind": "benefit_configuration", "market": run["market"], "version": pub["version"], "title": run["title"],
                                  "fields": run["configure"]["fields"], "model": {k: v for k, v in run["configure"]["model"].items() if k != "refs"}}
    if uc == "claims":
        c = cw._case(ref_id)
        if not c["decision"]:
            raise ValueError("Record the reviewer decision first.")
        a = c["adjudication"]
        return "claims_core", {"kind": "claim_decision", "claim_id": c["intake"]["claim"]["id"], "market": c["market"], "action": c["decision"]["action"], "decided_by": c["decision"]["by"],
                               "billed": a["totals"]["billed"], "plan_pays": a["totals"]["plan"], "patient_pays": a["totals"]["member"], "rule_version": a["rule_version"],
                               "deductions": [{"code": b["code"], "amount": b["amount"], "clause": (b["ref"] or {}).get("clause")} for b in a["by_reason"]]}
    ap = cw._appeal(ref_id)
    if not ap["approval"] or not ap["draft"] or not ap["draft"]["letter"]:
        raise ValueError("Approve the letter first.")
    return "outbox", {"kind": "correction_note" if ap.get("lens") == "audit" else "grievance_letter", "claim_id": cw._case(ref_id)["intake"]["claim"]["id"], "market": ap["market"], "draft_type": ap["draft"]["draft_type"],
                      "approved_by": ap["approval"]["by"], "letter": ap["draft"]["letter"]}


def push(uc: str, ref_id: int, user: dict, market: str, origin: str) -> dict:
    name, payload = _payload(uc, ref_id, market)
    d, c = _find(uc, name), _conf(uc, name)
    url = effective_url(d, c, origin)
    _guard(url, origin)
    res, ms = _http("POST", url + "/receive", c["token"], c["timeout_ms"], payload)
    s.audit(user, "pushed_to_system", f"{uc}/{name}: receipt {res.get('receipt')}", market)
    return {"receipt": res.get("receipt"), "system": d["label"], "ms": ms, "kind": payload["kind"]}


def receipts(uc: str, name: str) -> list[dict]:
    _find(uc, name)
    return [x for x in s.STATE.setdefault("sandbox", []) if x["system"] == name][::-1][:20]


# ---------------------------------------------------------------- the sandbox systems themselves
def sandbox_auth(header: str | None) -> bool:
    return bool(header) and secrets.compare_digest(header.encode(), f"Bearer {SANDBOX_TOKEN}".encode())


def sandbox_receive(system: str, body: dict) -> dict:
    items = s.STATE.setdefault("sandbox", [])
    r = {"system": system, "receipt": f"SBX-{len(items) + 1:04d}", "received_at": _now(), "kind": body.get("kind"), "ref": body.get("claim_id") or body.get("title") or "", "bytes": len(json.dumps(body))}
    items.append(r)
    return {"receipt": r["receipt"], "received_at": r["received_at"]}
