"""Payer Lifecycle Console API and pages. In-memory only: state and sessions reset when the server restarts.

Run locally:  python -m uvicorn app:app --port 8110     (from this backend folder)
On Cloud Run: see the Dockerfile and deploy/.

Access is enforced here, on the server, for every endpoint. The acting user always comes from the session.
  benefits_analyst: runs the three agents, approves rules, publishes the rule set
  claims_analyst:   reads published rules, works claims and denials
  manager:          reads everything
"""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ai
import auth
import casework as cw
import config
import connectors as cn
import logs
import reference as ref
import service as s
from ratelimit import SlidingWindow

logs.setup()
ROOT = Path(__file__).resolve().parents[1]
COOKIE = "payer_session"  # distinct from other apps on localhost: cookies are shared across ports
app = FastAPI(title="Payer Lifecycle Console", docs_url="/docs" if config.API_DOCS else None,
              redoc_url=None, openapi_url="/openapi.json" if config.API_DOCS else None)
AGENT_LIMIT = SlidingWindow(config.AGENT_RUNS_PER_WINDOW, config.AGENT_WINDOW_SECONDS)


def build_users() -> None:
    """Creates the demo accounts and clears per-account abuse state (login lockouts, agent rate limits)."""
    pw = auth.DEMO_PASSWORD
    auth.reset_users()
    AGENT_LIMIT.reset()
    auth.add_user("meera", "Meera Rao", "benefits_analyst", pw, title="Benefits analyst")
    auth.add_user("arjun", "Arjun Mehta", "claims_analyst", pw, title="Claims analyst")
    auth.add_user("kavita", "Kavita Nair", "manager", pw, title="Operations manager")


build_users()

# Pages use inline scripts and styles, so those stay allowed; everything else is limited to this origin and Google Fonts.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")


def _https(request: Request) -> bool:
    # Cloud Run terminates TLS in front of the container; uvicorn trusts its X-Forwarded-Proto (see Dockerfile).
    return request.url.scheme == "https" or config.ON_CLOUD_RUN


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request blocked."}, 403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if _https(request):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if resp.headers.get("content-type", "").startswith("text/html"):  # not on PDFs: a CSP there can block the browser's PDF viewer
        resp.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Unauthenticated liveness check for Cloud Run and the uptime check. Reveals nothing about state."""
    return {"ok": True}


def current_user(request: Request) -> dict:
    user = auth.session_user(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def require(*roles: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            s.audit(user, "forbidden", "role cannot use this endpoint")
            raise HTTPException(403, "Your role cannot do this.")
        return user
    return dep


def count_agent_run(user: dict) -> None:
    """Counts one agent run against the account's limit, or refuses it with 429."""
    if wait := AGENT_LIMIT.hit(user["username"]):
        s.audit(user, "rate_limited", f"agent limit reached, retry in {wait} s")
        raise HTTPException(429, f"Too many agent runs. Try again in {wait} seconds.", headers={"Retry-After": str(wait)})


def throttled(role_dep):
    """The role check, then the per-account agent limit. Used on every endpoint that can call Gemini."""
    def dep(user: dict = Depends(role_dep)) -> dict:
        count_agent_run(user)
        return user
    return dep


EDIT, READ, ANY = require("benefits_analyst"), require("benefits_analyst", "claims_analyst", "manager"), Depends(current_user)
WORK = require("claims_analyst")  # claims and denials work; the people who publish rules do not also decide claims
EDIT_AI, WORK_AI = throttled(EDIT), throttled(WORK)


@app.exception_handler(LookupError)
async def _404(_: Request, exc: LookupError):
    return JSONResponse({"detail": str(exc)}, 404)


@app.exception_handler(ValueError)
async def _422(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, 422)


class Login(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class NewRun(BaseModel):
    market: str
    sample_id: str | None = None
    text: str | None = Field(None, max_length=200_000)
    filename: str | None = Field(None, max_length=200)
    file_b64: str | None = Field(None, max_length=9_000_000)
    mime: str | None = Field(None, max_length=100)


class Status(BaseModel):
    status: str


class Decision(BaseModel):
    value: str = Field(max_length=40)


class Edit(BaseModel):
    value: float | int


class Validate(BaseModel):
    inject_defect: bool = False


class NewCase(BaseModel):
    market: str
    claim_id: str | None = Field(None, max_length=40)
    text: str | None = Field(None, max_length=100_000)
    filename: str | None = Field(None, max_length=200)
    file_b64: str | None = Field(None, max_length=9_000_000)
    mime: str | None = Field(None, max_length=100)
    member_id: str | None = Field(None, max_length=40)


class CaseDecision(BaseModel):
    action: str = Field(max_length=20)
    note: str = Field("", max_length=500)


class NewAppeal(BaseModel):
    case_id: int
    lens: str = Field("audit", max_length=10)
    letter_text: str | None = Field(None, max_length=50_000)


# ---------------------------------------------------------------- auth
@app.post("/api/auth/login")
def login(body: Login, request: Request):
    try:
        user = auth.authenticate(body.username, body.password)
    except auth.LockedOut as exc:
        s.audit(None, "login_locked", body.username[:40])
        return JSONResponse({"detail": str(exc)}, 429, headers={"Retry-After": str(exc.seconds)})
    if not user:
        s.audit(None, "login_failed", body.username[:40])
        return JSONResponse({"detail": "Incorrect username or password."}, 401)
    resp = JSONResponse({"user": auth.public(user)})
    resp.set_cookie(COOKIE, auth.create_session(user["username"]), httponly=True, secure=_https(request), samesite="lax",
                    max_age=auth.ABSOLUTE_TTL, path="/")
    s.audit(user, "login")
    return resp


@app.get("/api/auth/demo")
def demo_accounts():
    """What the sign-in page may show. The password is withheld when it was handed to judges privately."""
    return {"password": auth.DEMO_PASSWORD if config.SHOW_DEMO_PASSWORD else None}


@app.post("/api/auth/logout")
def logout(request: Request):
    user = auth.session_user(request.cookies.get(COOKIE))
    auth.destroy_session(request.cookies.get(COOKIE))
    if user:
        s.audit(user, "logout")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(user: dict = ANY):
    return auth.public(user)


# ---------------------------------------------------------------- meta
@app.get("/api/meta")
def meta(user: dict = ANY):
    return {m: {"label": p["label"], "short": p["short"], "terms": p["terms"], "currency": p["currency"], "plan": p["plan"], "clock_note": p["clock_note"],
                "licensing_note": p.get("licensing_note", ""), "reasons": {k: v["label"] for k, v in p["reasons"].items()}} for m, p in s.PACKS.items()}


@app.get("/api/ai/status")
def ai_status(user: dict = ANY):
    return ai.status()


@app.post("/api/reset")
def reset(user: dict = Depends(require("benefits_analyst", "manager"))):
    s.reset()
    s.audit(user, "demo_reset")
    return {"ok": True}


# ---------------------------------------------------------------- benefit plan interpretation and coding
@app.get("/api/benefits/samples")
def samples(market: str, user: dict = Depends(READ)):
    return s.samples(market)


@app.get("/api/benefits/runs")
def runs(market: str, user: dict = Depends(READ)):
    return s.list_runs(market)


@app.post("/api/benefits/runs")
def new_run(body: NewRun, user: dict = Depends(EDIT_AI)):
    return s.create_run(body.market, user, sample_id=body.sample_id, text=body.text, filename=body.filename, file_b64=body.file_b64, mime=body.mime)


@app.get("/api/benefits/runs/{run_id}")
def get_run(run_id: int, user: dict = Depends(READ)):
    return s.get_run(run_id)


@app.post("/api/benefits/runs/{run_id}/interpret")
def interpret_agent(run_id: int, user: dict = Depends(EDIT_AI)):
    return s.run_interpret(run_id, user)


@app.post("/api/benefits/runs/{run_id}/rules/{rid}/status")
def rule_status(run_id: int, rid: str, body: Status, user: dict = Depends(EDIT)):
    return s.set_rule_status(run_id, rid, body.status, user)


@app.post("/api/benefits/runs/{run_id}/rules/{rid}/decision")
def rule_decision(run_id: int, rid: str, body: Decision, user: dict = Depends(EDIT)):
    return s.set_decision(run_id, rid, body.value, user)


@app.post("/api/benefits/runs/{run_id}/rules/{rid}/edit")
def rule_edit(run_id: int, rid: str, body: Edit, user: dict = Depends(EDIT)):
    return s.edit_rule(run_id, rid, body.value, user)


@app.post("/api/benefits/runs/{run_id}/configure")
def configure_agent(run_id: int, user: dict = Depends(EDIT_AI)):
    return s.run_configure(run_id, user)


@app.post("/api/benefits/runs/{run_id}/mappings/{service}/confirm")
def mapping_confirm(run_id: int, service: str, user: dict = Depends(EDIT)):
    return s.confirm_mapping(run_id, service, user)


@app.post("/api/benefits/runs/{run_id}/validate")
def validate_agent(run_id: int, body: Validate, user: dict = Depends(EDIT)):
    return s.run_validate(run_id, user, body.inject_defect)


@app.post("/api/benefits/runs/{run_id}/approve")
def approve(run_id: int, user: dict = Depends(EDIT)):
    return s.approve_run(run_id, user)


@app.get("/api/benefits/runs/{run_id}/payload.csv")
def payload_csv(run_id: int, user: dict = Depends(READ)):
    return PlainTextResponse(s.csv_for(run_id), headers={"Content-Disposition": f"attachment; filename=configuration_run_{run_id}.csv"})


@app.get("/api/benefits/published")
def published(market: str, user: dict = ANY):
    s.pack(market)
    return s.published(market)


@app.get("/api/kpis")
def kpis(market: str, user: dict = ANY):
    return s.kpis(market) | {"claims": cw.kpis(market)}


# ---------------------------------------------------------------- claims adjustments and reprocessing
@app.get("/api/claims/samples")
def claim_samples(market: str, user: dict = Depends(READ)):
    s.pack(market)
    return {"claims": cw.claim_samples(market), "members": cw.members(market)}


@app.get("/api/claims/cases")
def cases(market: str, user: dict = Depends(READ)):
    s.pack(market)
    return cw.list_cases(market)


@app.post("/api/claims/cases")
def new_case(body: NewCase, user: dict = Depends(WORK_AI)):
    return cw.create_case(body.market, user, claim_id=body.claim_id, text=body.text, filename=body.filename, file_b64=body.file_b64, mime=body.mime, member_id=body.member_id)


@app.get("/api/claims/cases/{case_id}")
def get_case(case_id: int, user: dict = Depends(READ)):
    return cw.get_case(case_id)


@app.post("/api/claims/cases/{case_id}/intake")
def case_intake(case_id: int, user: dict = Depends(WORK_AI)):
    return cw.run_intake(case_id, user)


@app.post("/api/claims/cases/{case_id}/adjudicate")
def case_adjudicate(case_id: int, user: dict = Depends(WORK)):
    return cw.run_adjudicate(case_id, user)


@app.post("/api/claims/cases/{case_id}/explain")
def case_explain(case_id: int, user: dict = Depends(WORK_AI)):
    return cw.run_explain(case_id, user)


@app.post("/api/claims/cases/{case_id}/decide")
def case_decide(case_id: int, body: CaseDecision, user: dict = Depends(WORK)):
    return cw.decide(case_id, body.action, body.note, user)


# ---------------------------------------------------------------- documents as PDFs, rendered page by page for the viewer
def _pdf(data: bytes, name: str) -> Response:
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get("/api/benefits/runs/{run_id}/document.pdf")
def run_pdf(run_id: int, user: dict = Depends(READ)):
    return _pdf(s.doc_pdf(run_id), f"policy_document_{run_id}.pdf")


@app.get("/api/benefits/runs/{run_id}/page/{n}.png")
def run_page(run_id: int, n: int, clause: str | None = None, user: dict = Depends(READ)):
    return Response(content=s.doc_page(run_id, n, (clause or "")[:20] or None), media_type="image/png")


@app.get("/api/claims/cases/{case_id}/doc/{kind}.pdf")
def case_pdf(case_id: int, kind: str, user: dict = Depends(READ)):
    return _pdf(cw.case_doc_pdf(case_id, kind), f"{kind}_{case_id}.pdf")


@app.get("/api/claims/cases/{case_id}/doc/{kind}/page/{n}.png")
def case_page(case_id: int, kind: str, n: int, user: dict = Depends(READ)):
    return Response(content=cw.case_doc_page(case_id, kind, n), media_type="image/png")


# ---------------------------------------------------------------- the patient letter template is configurable
class TemplateBody(BaseModel):
    template: str | None = Field(None, max_length=3000)


@app.get("/api/claims/letter-template")
def letter_template(market: str, user: dict = Depends(READ)):
    s.pack(market)
    return cw.template_view(market)


@app.post("/api/claims/letter-template")
def save_letter_template(market: str, body: TemplateBody, user: dict = Depends(WORK)):
    return cw.set_letter_template(market, body.template, user)


@app.post("/api/claims/letter-template/preview")
def preview_letter_template(market: str, body: TemplateBody, user: dict = Depends(READ)):
    return {"preview": cw.preview_letter_template(market, body.template or "")}


# ---------------------------------------------------------------- denial management and appeals
@app.get("/api/denials/appeals")
def appeals(market: str, user: dict = Depends(READ)):
    s.pack(market)
    return cw.list_appeals(market)


@app.post("/api/denials/appeals")
def new_appeal(body: NewAppeal, user: dict = Depends(WORK)):
    return cw.open_appeal(body.case_id, user, body.letter_text, body.lens)


@app.get("/api/denials/appeals/{case_id}")
def get_appeal(case_id: int, user: dict = Depends(READ)):
    return cw.get_appeal(case_id)


@app.post("/api/denials/appeals/{case_id}/classify")
def appeal_classify(case_id: int, user: dict = Depends(WORK_AI)):
    return cw.run_classify(case_id, user)


@app.post("/api/denials/appeals/{case_id}/recheck")
def appeal_recheck(case_id: int, user: dict = Depends(WORK)):
    return cw.run_recheck(case_id, user)


@app.post("/api/denials/appeals/{case_id}/draft")
def appeal_draft(case_id: int, user: dict = Depends(WORK_AI)):
    return cw.run_draft(case_id, user)


@app.post("/api/denials/appeals/{case_id}/approve")
def appeal_approve(case_id: int, user: dict = Depends(WORK)):
    return cw.approve_appeal(case_id, user)


@app.get("/api/activity")
def activity(user: dict = Depends(require("manager", "benefits_analyst"))):
    return s.activity()


# ---------------------------------------------------------------- reference tables and system connections (every module has them)
UCS = ("benefits", "claims", "denials")


def _uc(uc: str) -> str:
    if uc not in UCS:
        raise LookupError("Unknown module.")
    return uc


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def _may_work(uc: str, user: dict) -> None:
    if user["role"] != cn.WORK_ROLE[_uc(uc)]:
        s.audit(user, "forbidden", f"connections {uc}")
        raise HTTPException(403, "Your role cannot change or use this connection.")


class ConnEdit(BaseModel):
    base_url: str | None = Field(None, max_length=300)
    token: str | None = Field(None, max_length=200)
    environment: str | None = Field(None, max_length=30)
    timeout_ms: int | None = None


class PushBody(BaseModel):
    ref_id: int


@app.get("/api/reference/{uc}/{name}")
def reference_table(uc: str, name: str, market: str, user: dict = Depends(READ)):
    return ref.table(_uc(uc), name, market)


@app.get("/api/connections/{uc}")
def connections(uc: str, market: str, request: Request, user: dict = Depends(READ)):
    s.pack(market)
    return cn.view(_uc(uc), market, _origin(request))


@app.post("/api/connections/{uc}/{name}")
def connection_update(uc: str, name: str, body: ConnEdit, market: str, request: Request, user: dict = Depends(current_user)):
    _may_work(uc, user)
    s.pack(market)
    return cn.update(uc, name, body.model_dump(exclude_none=True), user, market, _origin(request))


@app.post("/api/connections/{uc}/{name}/test")
def connection_test(uc: str, name: str, market: str, request: Request, user: dict = Depends(current_user)):
    _may_work(uc, user)
    if name == "gemini":
        count_agent_run(user)
    s.pack(market)
    return cn.test(uc, name, user, market, _origin(request))


@app.post("/api/push/{uc}")
def connection_push(uc: str, body: PushBody, market: str, request: Request, user: dict = Depends(current_user)):
    _may_work(uc, user)
    s.pack(market)
    return cn.push(uc, body.ref_id, user, market, _origin(request))


@app.get("/api/connections/{uc}/{name}/receipts")
def connection_receipts(uc: str, name: str, user: dict = Depends(READ)):
    return cn.receipts(_uc(uc), name)


# ---------------------------------------------------------------- sandbox: the downstream systems the demo connects to
def _sandbox(system: str, authorization: str | None) -> str:
    if system not in cn.SYSTEMS:
        raise HTTPException(404, "No such sandbox system.")
    if not cn.sandbox_auth(authorization):
        raise HTTPException(401, "Invalid or missing bearer token.")
    return cn.SYSTEMS[system]


@app.get("/sandbox/{system}/health")
def sandbox_health(system: str, request: Request):
    cid = _sandbox(system, request.headers.get("authorization"))
    return {"ok": True, "system": next(d["label"] for uc in cn.DEFS.values() for d in uc if d["id"] == cid) + " (sandbox)"}


@app.post("/sandbox/{system}/receive")
async def sandbox_receive(system: str, request: Request):
    cid = _sandbox(system, request.headers.get("authorization"))
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Expected a JSON object.")
    return cn.sandbox_receive(cid, body)


# Static pages last, so they never shadow the API.
app.mount("/", StaticFiles(directory=ROOT / "portals", html=True), name="portals")
