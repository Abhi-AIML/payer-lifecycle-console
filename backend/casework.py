"""Service layer for the Claims and Denials tiles. In-memory only, like service.py, whose state and audit log it shares.

A "case" carries one claim through three agents with a person deciding at the end:
  load claim -> Intake Agent -> Adjudication Agent (published rules) -> Explanation Agent -> reviewer decision
An "appeal" opens on an explained claim and carries the insurer's settlement letter through three more agents:
  Classification Agent -> Policy Recheck Agent -> Appeal Drafting Agent -> reviewer approves the letter
Agents never advance on their own, and the actor is always the signed-in user.
"""
import base64
from datetime import datetime, timezone

import ai
import claims
import denials
import docpdf
import service as s

STATE, LOCK, audit, pack, MAX_UPLOAD = s.STATE, s.LOCK, s.audit, s.pack, s.MAX_UPLOAD


def ensure() -> None:
    """service.reset() clears STATE; make sure the case stores exist again afterwards."""
    for k, v in (("cases", {}), ("case_seq", 0), ("appeals", {})):
        STATE.setdefault(k, v)


def _case(case_id: int) -> dict:
    ensure()
    try:
        return STATE["cases"][case_id]
    except KeyError:
        raise LookupError("Claim case not found.") from None


def _case_view(c: dict) -> dict:
    out = {k: v for k, v in c.items() if not k.startswith("_")}
    pub = STATE["published"][c["market"]]
    out["current_version"] = pub["version"] if pub else None
    out["stale"] = bool(c["adjudication"] and pub and c["adjudication"]["rule_version"] != pub["version"])
    out["appeal"] = c["id"] in STATE["appeals"]
    return out


DOC_LABEL = {"bill": "Hospital bill", "discharge": "Discharge summary", "preauth": "Approval letter"}


def _doc_pdf(c: dict, kind: str) -> bytes:
    if kind not in DOC_LABEL or not c["docs"].get(kind):
        raise LookupError("Document not found.")
    return c.get(f"_pdf_{kind}") or docpdf.text_pdf(c["docs"][kind], DOC_LABEL[kind])


def case_doc_pdf(case_id: int, kind: str) -> bytes:
    return _doc_pdf(_case(case_id), kind)


def case_doc_page(case_id: int, kind: str, n: int) -> bytes:
    return docpdf.render_page(_doc_pdf(_case(case_id), kind), n)


# ---- the patient letter template is configurable
def letter_template(market: str) -> str:
    ensure()
    return STATE["letter_templates"].get(market) or claims.DEFAULT_LETTER_TEMPLATE


def template_view(market: str) -> dict:
    t = letter_template(market)
    return {"template": t, "default": claims.DEFAULT_LETTER_TEMPLATE, "is_default": t.strip() == claims.DEFAULT_LETTER_TEMPLATE.strip(),
            "placeholders": [{"name": k, "meaning": v, "required": k in claims.REQUIRED_PLACEHOLDERS} for k, v in claims.PLACEHOLDERS.items()]}


def set_letter_template(market: str, text: str | None, user: dict) -> dict:
    with LOCK:
        pack(market)
        ensure()
        if not (text or "").strip():
            STATE["letter_templates"].pop(market, None)
            audit(user, "letter_template_reset", "", market)
        else:
            STATE["letter_templates"][market] = claims.validate_template(text)
            audit(user, "letter_template_saved", f"{len(text)} characters", market)
        return template_view(market)


def preview_letter_template(market: str, text: str) -> str:
    """Renders a template with clearly labelled sample values, so a reviewer can see the wording before saving it."""
    p = pack(market)
    claims.validate_template(text)
    reasons = list(p["reasons"].values())[:3]
    sample = {"market": market, "claim_id": "SAMPLE-0001", "patient": "Sample Patient", "provider": "Sample Hospital", "service_date": "2026-01-01", "billed": 100000.0,
              "plan_pays": 80000.0, "patient_pays": 20000.0, "outcome": "part_approve", "rule_version": 1, "appeal_days": p["appeal_filing_days"],
              "deductions": [{"code": r["code"], "reason": r["label"], "amount": amt, "explanation": r["member"], "clause": cl, "page": pg, "kind": r["kind"], "lines": ["L1"]}
                             for r, amt, cl, pg in zip(reasons, (12000.0, 5000.0, 3000.0), ("2.1", "5.1", None), (2, 5, None))]}
    return "PREVIEW WITH SAMPLE VALUES\n\n" + claims.template_letter(p, sample, text)


def claim_samples(market: str) -> list[dict]:
    p = pack(market)
    return [{"id": cl["id"], "patient": p["members"][cl["member"]]["name"], "provider": cl["provider"], "service_date": cl["service_date"],
             "billed": round(sum(x["billed"] for x in cl["lines"]), 2), "lines": len(cl["lines"]),
             "summary": (cl.get("procedure") or cl["lines"][0]["desc"]).replace("_", " ")} for cl in p["claims"]]


def members(market: str) -> list[dict]:
    return [{"id": k, "name": v["name"]} for k, v in pack(market)["members"].items()]


def create_case(market: str, user: dict, *, claim_id: str | None = None, text: str | None = None, filename: str | None = None,
                file_b64: str | None = None, mime: str | None = None, member_id: str | None = None) -> dict:
    ensure()
    p = pack(market)
    bill_pdf = None
    if claim_id:
        cl = next((x for x in p["claims"] if x["id"] == claim_id), None)
        if not cl:
            raise LookupError("Sample claim not found.")
        docs, title, source, provider, member_id = claims.documents(p, cl), f"{cl['id']}: {p['members'][cl['member']]['name']}", "sample", cl["provider"], cl["member"]
    else:
        if file_b64:
            data = base64.b64decode(file_b64, validate=False)
            if len(data) > MAX_UPLOAD:
                raise ValueError("File is larger than 6 MB.")
            mime = mime or "application/octet-stream"
            if mime.startswith("text/") or (filename or "").lower().endswith((".txt", ".md")):
                text = data.decode("utf-8", errors="replace")
            elif mime == "application/pdf" or mime.startswith("image/"):
                if not ai.available():
                    raise ValueError("Reading a scanned bill needs a Gemini key. Choose a sample claim, or paste the bill text.")
                text = ai.transcribe(data, mime, "bill")
                if not text:
                    raise ValueError("Gemini could not read this file. Try a clearer copy, or paste the text.")
            else:
                raise ValueError("Upload a PDF, an image or a text file, or paste the bill text.")
        if not text or not text.strip():
            raise ValueError("Choose a sample claim, upload a bill or paste the bill text.")
        if len(text) > 100_000:
            raise ValueError("Bill text is too long for this demo.")
        if not member_id or member_id not in p["members"]:
            raise ValueError("Choose the policyholder from the list.")
        docs, title, source, provider = {"bill": text, "discharge": None, "preauth": None}, filename or "Pasted bill", "upload", "Uploaded bill"
        bill_pdf = data if file_b64 and mime == "application/pdf" else None
    with LOCK:
        STATE["case_seq"] += 1
        c = {"id": STATE["case_seq"], "market": market, "claim_id": claim_id, "title": title, "source": source, "provider": provider, "member_id": member_id, "docs": docs,
             "stage": "loaded", "intake": None, "adjudication": None, "explain": None, "decision": None, "created_by": user["name"], "_pdf_bill": bill_pdf}
        c["doc_pages"] = {k: docpdf.page_count(_doc_pdf(c, k)) for k, v in docs.items() if v}
        STATE["cases"][c["id"]] = c
        audit(user, "claim_loaded", title, market)
    return _case_view(c)


def list_cases(market: str) -> list[dict]:
    ensure()
    return [{"id": c["id"], "title": c["title"], "stage": c["stage"], "outcome": c["adjudication"]["outcome"] if c["adjudication"] else None, "appeal": c["id"] in STATE["appeals"]}
            for c in STATE["cases"].values() if c["market"] == market][::-1]


def get_case(case_id: int) -> dict:
    return _case_view(_case(case_id))


def run_intake(case_id: int, user: dict) -> dict:
    with LOCK:
        c = _case(case_id)
        c["intake"] = claims.run_intake(pack(c["market"]), c)
        c.update(stage="intake", adjudication=None, explain=None, decision=None)
        STATE["appeals"].pop(case_id, None)
        audit(user, "agent_claim_intake", f"{c['intake']['summary']['lines']} lines via {c['intake']['extractor']}, checks {c['intake']['summary']['checks']}", c["market"])
        return _case_view(c)


def run_adjudicate(case_id: int, user: dict) -> dict:
    with LOCK:
        c = _case(case_id)
        if not c["intake"]:
            raise ValueError("Run the Intake Agent first.")
        pub = STATE["published"][c["market"]]
        if not pub:
            raise ValueError("No rule set is published for this market yet. A benefits analyst must publish one on the Benefit tile first.")
        c["adjudication"] = claims.run_adjudicate(pack(c["market"]), c, pub)
        c.update(stage="adjudicated", explain=None, decision=None)
        STATE["appeals"].pop(case_id, None)
        a = c["adjudication"]
        audit(user, "agent_claim_adjudicate", f"{a['outcome']}, plan {a['totals']['plan']} under v{a['rule_version']}", c["market"])
        return _case_view(c)


def run_explain(case_id: int, user: dict) -> dict:
    with LOCK:
        c = _case(case_id)
        if not c["adjudication"]:
            raise ValueError("Run the Adjudication Agent first.")
        if _case_view(c)["stale"]:
            raise ValueError("The published rules changed after this was adjudicated. Run the Adjudication Agent again.")
        c["explain"] = claims.run_explain(pack(c["market"]), c, letter_template(c["market"]))
        c.update(stage="explained", decision=None)
        audit(user, "agent_claim_explain", f"letter via {c['explain']['source']}", c["market"])
        return _case_view(c)


def decide(case_id: int, action: str, note: str, user: dict) -> dict:
    if action not in ("approve", "query", "refer"):
        raise ValueError("Choose approve, query or refer.")
    with LOCK:
        c = _case(case_id)
        if not c["explain"]:
            raise ValueError("Run the Explanation Agent first, so the decision comes with its explanation.")
        if _case_view(c)["stale"]:
            raise ValueError("The published rules changed. Run the Adjudication Agent again.")
        if action == "approve" and c["adjudication"]["outcome"] == "query":
            raise ValueError("A document check failed. Query the hospital instead of approving.")
        c["decision"] = {"action": action, "note": (note or "")[:500], "by": user["name"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "plan": c["adjudication"]["totals"]["plan"], "rule_version": c["adjudication"]["rule_version"]}
        c["stage"] = "decided"
        audit(user, f"claim_{action}", f"{c['title']}: plan pays {c['adjudication']['totals']['plan']}", c["market"])
        return _case_view(c)


# ---------------------------------------------------------------- appeals
def _appeal(case_id: int) -> dict:
    ensure()
    a = STATE["appeals"].get(case_id)
    if not a:
        raise LookupError("No appeal workspace for this claim yet.")
    return a


def _appeal_view(a: dict) -> dict:
    out = dict(a)
    pub = STATE["published"][a["market"]]
    out["stale"] = bool(pub and a.get("recheck") and a["recheck"]["rule_version"] != pub["version"])
    out["case_title"] = _case(a["case_id"])["title"]
    return out


def open_appeal(case_id: int, user: dict, letter_text: str | None = None, lens: str = "audit") -> dict:
    with LOCK:
        c = _case(case_id)
        if not c["adjudication"] or c["stage"] not in ("explained", "decided"):
            raise ValueError("Adjudicate and explain the claim on the Claims tile first.")
        pub = STATE["published"][c["market"]]
        if not pub:
            raise ValueError("No rule set is published for this market.")
        custom = bool(letter_text and letter_text.strip())
        text = letter_text.strip() if custom else denials.insurer_letter(pack(c["market"]), c, pub)
        if lens not in ("audit", "contest"):
            raise ValueError("Choose pre-issue audit or contest.")
        a = {"case_id": case_id, "market": c["market"], "lens": lens, "letter": text, "letter_source": "pasted" if custom else "sample", "stage": "loaded", "classify": None, "recheck": None,
             "draft": None, "approval": None, "case_outcome": c["adjudication"]["outcome"], "created_by": user["name"]}
        STATE["appeals"][case_id] = a
        audit(user, "appeal_opened", f"{c['title']} ({a['letter_source']} letter, {lens} lens)", c["market"])
        return _appeal_view(a)


def get_appeal(case_id: int) -> dict:
    return _appeal_view(_appeal(case_id))


def list_appeals(market: str) -> list[dict]:
    ensure()
    return [{"case_id": a["case_id"], "title": _case(a["case_id"])["title"], "stage": a["stage"], "merit": a["recheck"]["merit"] if a["recheck"] else None}
            for a in STATE["appeals"].values() if a["market"] == market][::-1]


def run_classify(case_id: int, user: dict) -> dict:
    with LOCK:
        a = _appeal(case_id)
        a["classify"] = denials.run_classify(pack(a["market"]), a)
        a.update(stage="classified", recheck=None, draft=None, approval=None)
        audit(user, "agent_classify", f"{a['classify']['summary']['deductions']} deductions via {a['classify']['extractor']}", a["market"])
        return _appeal_view(a)


def run_recheck(case_id: int, user: dict) -> dict:
    with LOCK:
        a = _appeal(case_id)
        if not a["classify"]:
            raise ValueError("Run the Classification Agent first.")
        c, pub = _case(case_id), STATE["published"][a["market"]]
        if not pub or c["adjudication"]["rule_version"] != pub["version"]:
            raise ValueError("The published rules changed. Run the Adjudication Agent again on the Claims tile first.")
        a["recheck"] = denials.run_recheck(pack(a["market"]), a, c, pub)
        a.update(stage="rechecked", draft=None, approval=None)
        r = a["recheck"]
        audit(user, "agent_recheck", f"{r['merit']}, {r['summary']['supported']}/{r['summary']['total']} deductions follow the rules", a["market"])
        return _appeal_view(a)


def run_draft(case_id: int, user: dict) -> dict:
    with LOCK:
        a = _appeal(case_id)
        if not a["recheck"]:
            raise ValueError("Run the Policy Recheck Agent first.")
        a["draft"] = denials.run_draft(pack(a["market"]), a, _case(case_id))
        a.update(stage="drafted", approval=None)
        audit(user, "agent_draft", f"{a['draft']['draft_type']} via {a['draft']['source']}", a["market"])
        return _appeal_view(a)


def approve_appeal(case_id: int, user: dict) -> dict:
    with LOCK:
        a = _appeal(case_id)
        if not a["draft"] or not a["draft"]["letter"]:
            raise ValueError("There is no letter to approve.")
        a["approval"] = {"by": user["name"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "draft_type": a["draft"]["draft_type"]}
        a["stage"] = "approved"
        audit(user, "appeal_approved", f"{a['draft']['draft_type']} letter approved for sending", a["market"])
        return _appeal_view(a)


def kpis(market: str) -> dict:
    ensure()
    cs = [c for c in STATE["cases"].values() if c["market"] == market]
    done = [c for c in cs if c["adjudication"]]
    aps = [a for a in STATE["appeals"].values() if a["market"] == market]
    checked = [a for a in aps if a["recheck"]]
    lines = [c["intake"]["summary"] for c in cs if c["intake"]]
    return {"cases": len(cs), "adjudicated": len(done), "decided": sum(1 for c in cs if c["decision"]),
            "lines_total": sum(x["lines"] for x in lines), "lines_verified": sum(x["verified"] for x in lines),
            "appeals_open": sum(1 for a in aps if a["stage"] != "approved"), "appeals_total": len(aps), "appeals_rechecked": len(checked),
            "deductions_total": sum(a["recheck"]["summary"]["total"] for a in checked), "deductions_supported": sum(a["recheck"]["summary"]["supported"] for a in checked),
            "letters_verified": sum(1 for a in aps if a["draft"] and a["draft"]["verification"] and a["draft"]["verification"]["amounts_ok"] and a["draft"]["verification"]["clauses_ok"]),
            "billed": round(sum(c["adjudication"]["totals"]["billed"] for c in done), 2), "plan": round(sum(c["adjudication"]["totals"]["plan"] for c in done), 2)}
