"""Service layer for the Benefit Plan Interpretation & Coding tile. In-memory only (no database yet).

A "run" carries one document through the three agents, with a person approving between them:
  load document -> Interpretation Agent -> review -> Configuration & Coding Agent -> Validation Agent -> approve & publish
Agents never advance the run on their own. Every action is audited with the signed-in user, never a client-supplied name.
"""
import base64
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import ai
import configure
import docpdf
import interpret
import logs
import validate
from rulespec import NUMERIC

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()
PACKS = {m: json.loads((ROOT / "data" / f"market_{m}.json").read_text(encoding="utf-8")) for m in ("us", "in")}
STATE: dict = {}
MAX_UPLOAD = 6 * 1024 * 1024


def reset() -> None:
    STATE.clear()
    STATE.update(runs={}, published={"us": None, "in": None}, audit=[], seq=0, cases={}, case_seq=0, appeals={}, connections={}, sandbox=[], letter_templates={})  # cases/appeals: casework.py; connections/sandbox: connectors.py


reset()


def pack(market: str) -> dict:
    if market not in PACKS:
        raise ValueError("Market must be 'us' or 'in'.")
    return PACKS[market]


def audit(user: dict | None, action: str, detail: str = "", market: str | None = None, run_id: int | None = None) -> None:
    """Records an action in the in-app activity log and, as a structured line, in Cloud Logging (which survives restarts)."""
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": user["name"] if user else "System",
             "role": user["role"] if user else "system", "action": action, "detail": detail, "market": market, "run_id": run_id}
    STATE["audit"].append(event)
    logs.audit_event(event)


def _get(run_id: int) -> dict:
    try:
        return STATE["runs"][run_id]
    except KeyError:
        raise LookupError("Run not found.") from None


def _view(run: dict) -> dict:
    out = {k: v for k, v in run.items() if k != "doc_text" and not k.startswith("_")}
    out["doc_clauses"] = run.get("clauses", [])
    return out


# ---------------------------------------------------------------- samples and documents
def samples(market: str) -> list[dict]:
    return [{"id": s["id"], "title": s["title"], "kind": s["kind"]} for s in pack(market)["samples"]]


def _sample_text(market: str, sample_id: str) -> tuple[dict, str]:
    s = next((x for x in pack(market)["samples"] if x["id"] == sample_id), None)
    if not s:
        raise LookupError("Sample document not found.")
    return s, (ROOT / "data" / "docs" / s["file"]).read_text(encoding="utf-8")


def create_run(market: str, user: dict, *, sample_id: str | None = None, text: str | None = None, filename: str | None = None,
               file_b64: str | None = None, mime: str | None = None) -> dict:
    """Load a document into a new run. No agent runs yet: the person clicks Run agent."""
    p = pack(market)
    notes: list[str] = []
    pdf_bytes = None
    if sample_id:
        s, text = _sample_text(market, sample_id)
        title, source, kind = s["title"], "sample", s["kind"]
    elif file_b64:
        data = base64.b64decode(file_b64, validate=False)
        if len(data) > MAX_UPLOAD:
            raise ValueError("File is larger than 6 MB.")
        mime = mime or "application/octet-stream"
        if mime.startswith("text/") or (filename or "").lower().endswith((".txt", ".md")):
            text = data.decode("utf-8", errors="replace")
        elif mime == "application/pdf" or mime.startswith("image/"):
            if not ai.available():
                raise ValueError("Reading PDFs and images needs a Gemini key. Choose a sample document or paste the text instead.")
            pdf_bytes = data if mime == "application/pdf" else None  # kept as uploaded, so the viewer shows the real document
            text = ai.transcribe(data, mime)
            if not text:
                raise ValueError("Gemini could not read this file. Try a clearer copy, or paste the text.")
            notes.append("Transcribed from the uploaded file by Gemini. Rules are verified against this transcription, so check it matches the original.")
        else:
            raise ValueError("Upload a PDF, an image or a text file, or paste the text.")
        title, source, kind = filename or "Uploaded document", "upload", "amendment" if "amend" in (filename or "").lower() else "base"
    elif text and text.strip():
        title, source, kind = "Pasted document", "paste", "base"
    else:
        raise ValueError("Choose a sample document, upload a file or paste text.")
    if len(text) > 200_000:
        raise ValueError("Document text is too long for this demo.")
    from docparse import parse_document
    parsed = parse_document(text)
    layout = None
    if pdf_bytes is None:
        pdf_bytes, layout = docpdf.policy_pdf(text)
    with LOCK:
        STATE["seq"] += 1
        run = {"id": STATE["seq"], "market": market, "title": title, "source": source, "kind": kind, "doc_text": text, "notes": notes,
               "stage": "document_loaded", "created_by": user["name"], "_pdf": pdf_bytes, "_layout": layout, "clauses": parsed["clauses"], "doc_meta": {k: parsed[k] for k in ("title", "effective", "version", "is_amendment", "pages")},
               "interpret": None, "configure": None, "validate": None, "rules": [], "diff": None, "mappings_confirmed": [], "injected": None, "approval": None}
        STATE["runs"][run["id"]] = run
        audit(user, "document_loaded", f"{title} ({source})", market, run["id"])
    return _view(run)


# ---------------------------------------------------------------- agent 1
def run_interpret(run_id: int, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        res = interpret.run(pack(run["market"]), run["doc_text"])
        run.update(interpret={"log": res["log"], "extractor": res["extractor"], "summary": res["summary"], "parsed": res["parsed"]}, rules=res["rules"],
                   stage="interpreted", configure=None, validate=None, mappings_confirmed=[], approval=None)
        pub = STATE["published"][run["market"]]
        run["diff"] = interpret.diff_rules(pub["rules"], res["rules"]) if pub and (run["kind"] == "amendment" or res["parsed"]["is_amendment"]) else None
        audit(user, "agent_interpret", f"{res['summary']['rules']} rules via {res['extractor']}, {res['summary']['flagged']} flagged", run["market"], run_id)
        return _view(run)


def _rule(run: dict, rid: str) -> dict:
    r = next((x for x in run["rules"] if x["id"] == rid), None)
    if not r:
        raise LookupError("Rule not found.")
    return r


def _can_approve(r: dict) -> str | None:
    if not r["evidence"]["quote_found"]:
        return "Its quote was not found in the source document, so it cannot be approved."
    if not r["evidence"].get("page_clause", True):
        return "Its page and clause do not contain its quote, so it cannot be approved."
    if not r["evidence"]["value_in_quote"] and not r.get("edited"):
        return "Its value does not appear in the quote. Correct the value or reject the rule."
    if r.get("needs_decision") and not r.get("decision"):
        return "Choose the scope first."
    return None


def set_rule_status(run_id: int, rid: str, status: str, user: dict) -> dict:
    if status not in ("approved", "rejected", "proposed"):
        raise ValueError("Bad status.")
    with LOCK:
        run = _get(run_id)
        if run["stage"] not in ("interpreted", "configured", "validated"):
            raise ValueError("Run the Interpretation Agent first.")
        r = _rule(run, rid)
        if status == "approved" and (why := _can_approve(r)):
            raise ValueError(why)
        r["status"] = status
        _invalidate_downstream(run)
        audit(user, f"rule_{status}", f"{r['label']} (clause {r['clause']})", run["market"], run_id)
        return _view(run)


def set_decision(run_id: int, rid: str, value: str, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        r = _rule(run, rid)
        if not r.get("needs_decision") or value not in [o["value"] for o in r["ambiguity"]["options"]]:
            raise ValueError("Not a valid decision for this rule.")
        r["decision"] = value
        r["status"] = "proposed"
        _invalidate_downstream(run)
        audit(user, "ambiguity_decision", f"{r['ambiguity']['title']}: {value}", run["market"], run_id)
        return _view(run)


def edit_rule(run_id: int, rid: str, value, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        r = _rule(run, rid)
        if r["category"] not in NUMERIC:
            raise ValueError("Only numeric rules can be edited here.")
        try:
            new = float(value)
        except (TypeError, ValueError):
            raise ValueError("Enter a number.") from None
        new = int(new) if new == int(new) else new
        old, r["value"], r["edited"], r["status"] = r["value"], new, True, "proposed"
        r["flags"] = [f for f in r["flags"] if "does not appear" not in f["text"]] + [{"severity": "warn", "text": f"Value edited by a reviewer: was {old}, now {new}. The quote is unchanged."}]
        _invalidate_downstream(run)
        audit(user, "rule_edited", f"{r['label']}: {old} -> {new}", run["market"], run_id)
        return _view(run)


def _invalidate_downstream(run: dict) -> None:
    if run["stage"] in ("configured", "validated"):
        run.update(configure=None, validate=None, stage="interpreted", mappings_confirmed=[], approval=None)


# ---------------------------------------------------------------- agent 2
def run_configure(run_id: int, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        if run["stage"] not in ("interpreted", "configured", "validated"):
            raise ValueError("Run the Interpretation Agent first.")
        pending = [r["label"] for r in run["rules"] if r["status"] == "proposed"]
        if pending:
            raise ValueError(f"Approve or reject every rule first. Still open: {', '.join(pending[:4])}{'...' if len(pending) > 4 else ''}.")
        approved = {r["category"] for r in run["rules"] if r["status"] == "approved"}
        missing = [c for c in pack(run["market"])["required"] if c not in approved]
        if missing:
            raise ValueError("Required rules are not approved: " + ", ".join(missing) + ".")
        parsed = dict(run["interpret"]["parsed"])
        res = configure.run(pack(run["market"]), run["rules"], parsed)
        run.update(configure={"log": res["log"], "model": res["model"], "fields": res["fields"], "mappings": res["mappings"], "counts": res["counts"],
                              "licensing_note": res["licensing_note"], "csv": configure.payload_csv(res["fields"])},
                   stage="configured", validate=None, mappings_confirmed=[], approval=None)
        audit(user, "agent_configure", f"{len(res['fields'])} fields, mappings {res['counts']}", run["market"], run_id)
        return _view(run)


def confirm_mapping(run_id: int, service: str, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        if not run["configure"] or not any(m["service"] == service for m in run["configure"]["mappings"]):
            raise LookupError("Mapping not found.")
        if service not in run["mappings_confirmed"]:
            run["mappings_confirmed"].append(service)
        run["validate"] and run["validate"].update(stale=True)
        audit(user, "mapping_confirmed", service, run["market"], run_id)
        return _view(run)


# ---------------------------------------------------------------- agent 3
def run_validate(run_id: int, user: dict, inject_defect: bool = False) -> dict:
    with LOCK:
        run = _get(run_id)
        if run["stage"] not in ("configured", "validated"):
            raise ValueError("Run the Configuration & Coding Agent first.")
        cfg = run["configure"]
        fields, note = (validate.inject_defect(cfg["fields"], run["market"]) if inject_defect else (cfg["fields"], None))
        res = validate.run(pack(run["market"]), cfg["model"], fields, cfg["mappings"], run["rules"], set(run["mappings_confirmed"]), injected=note)
        run.update(validate=res, stage="validated", injected=note, approval=None)
        audit(user, "agent_validate", f"{res['package']['tests']['passed']}/{res['package']['tests']['total']} passed" + (" (defect injected)" if note else ""), run["market"], run_id)
        return _view(run)


# ---------------------------------------------------------------- approve and publish
def approve_run(run_id: int, user: dict) -> dict:
    with LOCK:
        run = _get(run_id)
        if run["stage"] != "validated" or not run["validate"]:
            raise ValueError("Run the Validation Agent first.")
        if run["injected"]:
            raise ValueError("The validation ran on a deliberately faulty configuration. Run it again without the simulated error.")
        pk = run["validate"]["package"]
        if not pk["ready"]:
            raise ValueError("The approval package has blocking exceptions.")
        open_map = [m["label"] for m in run["configure"]["mappings"] if m["status"] != "mapped" and m["service"] not in run["mappings_confirmed"]]
        if open_map:
            raise ValueError("A coding reviewer must confirm these mappings first: " + ", ".join(open_map) + ".")
        prev = STATE["published"][run["market"]]
        version = (prev["version"] + 1) if prev else 1
        STATE["published"][run["market"]] = {"version": version, "run_id": run_id, "published_by": user["name"], "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                              "title": run["title"], "effective": run["interpret"]["parsed"]["effective"], "model": run["configure"]["model"], "fields": run["configure"]["fields"],
                                              "rules": [r for r in run["rules"] if r["status"] == "approved"], "tests": pk["tests"], "coverage_pct": pk["coverage_pct"],
                                              "previous": prev["version"] if prev else None}
        run["approval"] = {"by": user["name"], "version": version}
        run["stage"] = "approved"
        audit(user, "rule_set_published", f"Version {version} ({run['title']})", run["market"], run_id)
        return _view(run)


# ---------------------------------------------------------------- reads
def get_run(run_id: int) -> dict:
    return _view(_get(run_id))


def doc_pdf(run_id: int) -> bytes:
    return _get(run_id)["_pdf"]


def doc_page(run_id: int, n: int, clause: str | None = None) -> bytes:
    """One page of the run's document as an image, with the cited clause highlighted."""
    run = _get(run_id)
    rects = []
    if clause:
        lay = (run.get("_layout") or {}).get(clause)
        if lay and lay["page"] == n:
            rects = [lay["rect"]]
        else:  # an uploaded PDF has no layout record: search the page for the clause text
            c = next((x for x in run.get("clauses", []) if str(x["clause"]).strip() == clause and x["page"] == n), None)
            rects = docpdf.find_rects(run["_pdf"], n, c["text"]) if c else []
    return docpdf.render_page(run["_pdf"], n, rects)


def list_runs(market: str) -> list[dict]:
    return [{"id": r["id"], "title": r["title"], "stage": r["stage"], "kind": r["kind"], "created_by": r["created_by"]} for r in STATE["runs"].values() if r["market"] == market][::-1]


def published(market: str) -> dict | None:
    p = STATE["published"][market]
    return None if not p else {k: v for k, v in p.items() if k not in ("fields",)} | {"field_count": len(p["fields"]), "rule_count": len(p["rules"])}


def csv_for(run_id: int) -> str:
    run = _get(run_id)
    if not run["configure"]:
        raise ValueError("Run the Configuration & Coding Agent first.")
    return run["configure"]["csv"]


def activity(limit: int = 40) -> list[dict]:
    return STATE["audit"][-limit:][::-1]


def kpis(market: str) -> dict:
    """Console figures, computed from the live state. Nothing here is typed in."""
    pack(market)
    pub = STATE["published"][market]
    runs = [r for r in STATE["runs"].values() if r["market"] == market]
    latest = next((r for r in reversed(runs) if r["validate"]), None)
    rules = pub["rules"] if pub else []
    verified = sum(1 for r in rules if r["evidence"]["quote_found"] and r["evidence"]["value_in_quote"])
    return {"published_version": pub["version"] if pub else None, "published_rules": len(rules), "verified_rules": verified,
            "runs_total": len(runs), "runs_in_progress": sum(1 for r in runs if r["stage"] != "approved"),
            "latest_tests": latest["validate"]["package"]["tests"] if latest else None}
