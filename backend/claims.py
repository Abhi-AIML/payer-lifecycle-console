"""Claims tile agents: Intake, Adjudication, Explanation.

  Intake Agent        reads the bill (and discharge summary / approval letter when present) into structured lines.
                      Gemini reads when a key is set, else a labelled rule-based reader handles the demo bill format.
                      Code then verifies each line against the document and runs cross-document checks.
  Adjudication Agent  NO AI. Runs the claim through the deterministic engine using the PUBLISHED rule set. Every
                      reduction carries the clause and page of the rule that caused it.
  Explanation Agent   Gemini drafts the plain-language letter from the computed facts only. Code then checks that every
                      amount and clause in the draft exists in those facts. A failed check falls back to a template.

Rules decide, AI reads and writes, people confirm.
"""
import copy
import re
from datetime import date, timedelta

import ai
import engine
from interpret import Log

PROCEDURE_NAMES = {"total_knee_replacement": "Total knee replacement", "cataract": "Cataract surgery",
                   "appendectomy": "Appendectomy", "chemotherapy": "Chemotherapy", "angioplasty": "Angioplasty"}
QUIRKS = {"CLM-IN-2004": {"unsigned": True}}  # synthetic document defects, so the checks have something real to find
DATE = r"\d{4}-\d{2}-\d{2}"


# ---------------------------------------------------------------- formatting
def _group_in(x: float) -> str:
    cents = round(abs(x) * 100)
    whole, c = divmod(cents, 100)
    s = str(whole)
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return ("-" if x < 0 else "") + s + (f".{c:02d}" if c else "")


def money(market: str, n: float) -> str:
    if market == "in":
        return "Rs. " + _group_in(n)
    return "$" + (f"{n:,.0f}" if abs(n - round(n)) < 0.005 else f"{n:,.2f}")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _words(s: str) -> set:
    return set(re.findall(r"[a-z]+", s.lower())) - {"the", "and", "of", "per", "day", "days", "charges"}


def item_master(pack: dict) -> dict:
    """Hospital item master: code -> how the item is categorised. Built from the market pack."""
    out = {}
    for c in pack["claims"]:
        for ln in c["lines"]:
            out.setdefault(ln["code"], {k: ln.get(k) for k in ("service_key", "category", "room_linked")})
    return out


# ---------------------------------------------------------------- synthetic documents for a sample case
def documents(pack: dict, claim: dict) -> dict:
    mk, mem = pack["market"], pack["members"][claim["member"]]
    q, prov = QUIRKS.get(claim["id"], {}), claim["provider"].upper()
    proc = PROCEDURE_NAMES.get(claim.get("procedure"))
    auth = claim["authorizations"][0] if claim["authorizations"] else None
    head = [f"{prov}: FINAL BILL (SYNTHETIC)", f"Patient: {mem['name']}", f"Member ID: {claim['member']}", f"Service date: {claim['service_date']}"]
    if claim.get("stay_days"):
        head.append(f"Stay: {claim['stay_days']} days")
    if proc:
        head.append(f"Procedure: {proc}")
    head.append(f"Pre-authorization ref: {auth['ref'] if auth else 'none'}")
    rows = [f"{i} | {ln['code']} | {ln['desc']} | {ln.get('qty', 1)} | {ln['date']} | {_amt(mk, ln['billed'])}" for i, ln in enumerate(claim["lines"], 1)]
    total = sum(ln["billed"] for ln in claim["lines"])
    bill = "\n".join(head + ["ITEMS (item no | item code | description | qty | date | amount)"] + rows + [f"Total billed: {_amt(mk, total)}"])
    docs = {"bill": bill, "discharge": None, "preauth": None}
    if claim.get("stay_days") is not None and mk == "in":
        out = (date.fromisoformat(claim["service_date"]) + timedelta(days=claim["stay_days"])).isoformat()
        docs["discharge"] = "\n".join([f"{prov}: DISCHARGE SUMMARY (SYNTHETIC)", f"Patient: {mem['name']}", f"Admission date: {claim['service_date']}", f"Discharge date: {out}",
                                       f"Procedure performed: {proc or 'Not stated'}", "Accident related: " + ("yes" if claim.get("accident") else "no"),
                                       "Treating doctor: Dr. A. Menon", "Doctor signature: " + ("NOT SIGNED" if q.get("unsigned") else "signed")])
    if auth:
        svc = auth["service_keys"][0]
        docs["preauth"] = "\n".join(["PRE-AUTHORIZATION APPROVAL (SYNTHETIC)", f"Authorization ref: {auth['ref']}", f"Patient: {mem['name']}",
                                     f"Approved service: {PROCEDURE_NAMES.get(svc, svc.replace('_', ' ').capitalize())}"])
    return docs


def _amt(market: str, n: float) -> str:
    return _group_in(n) if market == "in" else f"{n:,.2f}"


# ---------------------------------------------------------------- readers
def _kv(text: str | None, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", text or "", re.I | re.M)
    return m.group(1) if m else None


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("Rs.", "").replace("$", "").strip())
    except ValueError:
        return None


ROW = re.compile(rf"^\s*\d+\s*\|\s*([^|]*?)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*({DATE})\s*\|\s*([\d,]+(?:\.\d+)?)\s*$", re.M)


def parse_bill(text: str) -> dict:
    """Rule-based reader for the demo bill format."""
    stay = _kv(text, "Stay")
    return {"patient": _kv(text, "Patient"), "member_id": _kv(text, "Member ID"), "service_date": _kv(text, "Service date"),
            "stay_days": int(re.match(r"\d+", stay).group()) if stay and re.match(r"\d+", stay) else None, "procedure": _kv(text, "Procedure"),
            "preauth_ref": None if (_kv(text, "Pre-authorization ref") or "none").lower() == "none" else _kv(text, "Pre-authorization ref"),
            "stated_total": _num(_kv(text, "Total billed")),
            "lines": [{"code": c or None, "desc": d, "qty": int(q), "date": dt, "billed": _num(a), "category": None} for c, d, q, dt, a in ROW.findall(text)]}


GEMINI_BILL = ("You read hospital bills for an insurance claims reviewer. Return JSON only: "
               '{"patient": str, "member_id": str|null, "service_date": "YYYY-MM-DD", "stay_days": int|null, "procedure": str|null, "preauth_ref": str|null, '
               '"stated_total": number, "lines": [{"desc": str, "code": str|null, "qty": int, "date": "YYYY-MM-DD", "amount": number, '
               '"category": "room|procedure|implant|daycare|non_medical|consultation|investigation|pharmacy|other"}]}. '
               "Copy values exactly as printed. Do not infer, merge or add lines.")


def gemini_bill(text: str) -> dict | None:
    out = ai.generate_json(GEMINI_BILL, text)
    if not out or not isinstance(out.get("lines"), list) or not out["lines"]:
        return None
    lines = []
    for ln in out["lines"]:
        amt, qty = _num(ln.get("amount")), ln.get("qty")
        if amt is None or not ln.get("desc"):
            return None
        lines.append({"code": ln.get("code") or None, "desc": str(ln["desc"]), "qty": int(qty) if str(qty).isdigit() else 1, "date": ln.get("date") or out.get("service_date"),
                      "billed": amt, "category": ln.get("category")})
    return {"patient": out.get("patient"), "member_id": out.get("member_id"), "service_date": out.get("service_date"), "stay_days": out.get("stay_days"),
            "procedure": out.get("procedure"), "preauth_ref": out.get("preauth_ref"), "stated_total": _num(out.get("stated_total")), "lines": lines}


def _present(text: str, amount: float) -> bool:
    flat = re.sub(r"(?<=\d),(?=\d)", "", text)
    s = f"{amount:.2f}".rstrip("0").rstrip(".")
    return re.search(rf"(?<![\d.]){re.escape(s)}(?:\.0+)?(?![\d])", flat) is not None


def _enrich(pack: dict, lines: list[dict], text: str) -> list[dict]:
    master, tariff, out = item_master(pack), pack["tariff"], []
    for i, ln in enumerate(lines, 1):
        code, how = ln.get("code"), "item code on the bill"
        if code not in master and code not in tariff:
            best = max(((len(_words(ln["desc"]) & _words(t["desc"])) / max(1, len(_words(ln["desc"]) | _words(t["desc"]))), k) for k, t in tariff.items()), default=(0, None))
            code, how = (best[1], f"matched by description ({best[0]:.0%} overlap)") if best[0] >= 0.5 else (None, "no tariff code matched")
        info = master.get(code, {})
        out.append({"id": f"L{i}", "code": code, "desc": ln["desc"], "qty": ln["qty"], "date": ln["date"], "billed": ln["billed"], "service_key": info.get("service_key") or ln.get("category") or "other",
                    "category": info.get("category") or ln.get("category") or "other", "room_linked": bool(info.get("room_linked")), "coding": how,
                    "evidence": {"amount_found": _present(text, ln["billed"]), "desc_found": re.sub(r"\s+", " ", ln["desc"].lower()) in re.sub(r"\s+", " ", text.lower())}})
    return out


# ---------------------------------------------------------------- cross-document checks
def _check(cid, title, status, detail, sources=()):
    return {"id": cid, "title": title, "status": status, "detail": detail, "sources": list(sources)}


def _checks(market: str, bill: dict, lines: list[dict], discharge: str | None, preauth: str | None, member: dict | None) -> list[dict]:
    out, total = [], round(sum(ln["billed"] for ln in lines), 2)
    st = bill.get("stated_total")
    out.append(_check("total", "Bill total equals the sum of its lines", "ok" if st is not None and abs(st - total) < 0.5 else "fail",
                      f"Lines add to {money(market, total)}; the bill states {money(market, st) if st is not None else 'no total'}.", ["bill"]))
    out.append(_check("member", "Patient matches the member record", "ok" if member and member["name"] == bill.get("patient") else "fail",
                      f"Bill says {bill.get('patient') or 'no name'} (member {bill.get('member_id') or 'unknown'}); the member record says {member['name'] if member else 'no such member'}.", ["bill"]))
    bad_ev = [ln["id"] for ln in lines if not (ln["evidence"]["amount_found"] and ln["evidence"]["desc_found"])]
    out.append(_check("evidence", "Every extracted line and amount is found in the bill text", "fail" if bad_ev else "ok",
                      f"Not found: {', '.join(bad_ev)}." if bad_ev else f"All {len(lines)} lines verified against the bill text.", ["bill"]))
    unmatched = [ln["id"] for ln in lines if ln["code"] is None]
    if unmatched:
        out.append(_check("coding", "Every line matched a tariff code", "warn", f"No tariff code for {', '.join(unmatched)}. They are charged as billed. A reviewer should check them.", ["bill"]))
    if discharge:
        dp, dd = _kv(discharge, "Patient"), _kv(discharge, "Admission date")
        out.append(_check("dis_patient", "Discharge summary names the same patient", "ok" if dp == bill.get("patient") else "fail", f"Discharge summary: {dp}. Bill: {bill.get('patient')}.", ["bill", "discharge"]))
        out.append(_check("dis_date", "Admission date matches between bill and discharge summary", "ok" if dd == bill.get("service_date") else "fail", f"Discharge summary: {dd}. Bill: {bill.get('service_date')}.", ["bill", "discharge"]))
        pp, bp = (_kv(discharge, "Procedure performed") or "").lower(), (bill.get("procedure") or "").lower()
        out.append(_check("dis_proc", "Procedure matches between bill and discharge summary", "ok" if pp == bp else "fail", f"Discharge summary: {pp or 'none'}. Bill: {bp or 'none'}.", ["bill", "discharge"]))
        try:
            stay = (date.fromisoformat(_kv(discharge, "Discharge date")) - date.fromisoformat(dd)).days
            bed = sum(ln["qty"] for ln in lines if ln["category"] in ("room", "icu"))
            bad = bool(bed) and bed != stay
            out.append(_check("stay", "Room and ICU days match the length of stay", "warn" if bad else "ok", f"Length of stay {stay} day(s). Room and ICU lines add up to {bed} day(s)." if bad else f"Length of stay {stay} day(s) matches the room and ICU charges.", ["bill", "discharge"]))
        except (TypeError, ValueError):
            pass
        sig = (_kv(discharge, "Doctor signature") or "").lower()
        out.append(_check("signature", "Discharge summary is signed by the treating doctor", "ok" if sig == "signed" else "fail",
                          "Signed." if sig == "signed" else "The summary is not signed. A hospital query is needed before this can be decided.", ["discharge"]))
    if preauth:
        ap, ref = (_kv(preauth, "Approved service") or "").lower(), _kv(preauth, "Authorization ref")
        out.append(_check("auth_ref", "Authorization reference matches between bill and approval letter", "ok" if ref and ref == bill.get("preauth_ref") else "fail", f"Approval letter: {ref}. Bill: {bill.get('preauth_ref') or 'none'}.", ["bill", "preauth"]))
        if bill.get("procedure"):
            out.append(_check("auth_proc", "Approved service matches the procedure billed", "ok" if ap == bill["procedure"].lower() or slug(ap) in slug(bill["procedure"]) else "warn", f"Approved: {ap}. Billed: {bill['procedure'].lower()}.", ["bill", "preauth"]))
    return out


# ---------------------------------------------------------------- agent 1
def run_intake(pack: dict, case: dict) -> dict:
    log, docs, market = Log("Intake Agent"), case["docs"], pack["market"]
    have = [k for k in ("bill", "discharge", "preauth") if docs.get(k)]
    log.step("Read documents", f"{len(have)} document(s): {', '.join(have)}")
    text = docs["bill"]
    bill, extractor = (gemini_bill(text) if ai.available() else None), "gemini"
    if not bill:
        if ai.available():
            log.step("Gemini extraction failed", "Using the rule-based reader instead.", "warn")
        bill, extractor = parse_bill(text), "fallback"
    if not bill["lines"]:
        raise ValueError("No bill lines could be read from this document. Check the format, or add a Gemini key to read scans.")
    log.step("Extract bill lines", f"{len(bill['lines'])} lines read by {'Gemini' if extractor == 'gemini' else 'the rule-based reader (no Gemini key)'}")
    if case.get("member_id") and not bill.get("member_id"):
        bill["member_id"] = case["member_id"]
    lines = _enrich(pack, bill["lines"], text)
    verified = sum(1 for ln in lines if ln["evidence"]["amount_found"] and ln["evidence"]["desc_found"])
    log.step("Verify each line against the bill text", f"{verified} of {len(lines)} lines: description and amount both found in the document", "ok" if verified == len(lines) else "warn")
    log.step("Match items to tariff codes", f"{sum(1 for ln in lines if ln['code'])} matched, {sum(1 for ln in lines if not ln['code'])} unmatched")
    member = pack["members"].get(bill.get("member_id") or case.get("member_id"))
    checks = _checks(market, bill, lines, docs.get("discharge"), docs.get("preauth"), member)
    tally = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")}
    log.step("Cross-document checks", f"{tally['ok']} passed, {tally['warn']} to look at, {tally['fail']} failed", "ok" if not tally["fail"] else "warn")
    if not member:
        raise ValueError("The member on this bill is not in the demo member list. Choose a member and try again.")
    proc = bill.get("procedure")
    proc_key = next((k for k, v in PROCEDURE_NAMES.items() if proc and v.lower() == proc.lower()), slug(proc) if proc else None)
    approved = _kv(docs.get("preauth"), "Approved service")
    auth_ref = _kv(docs.get("preauth"), "Authorization ref") or bill.get("preauth_ref")
    auths = [{"ref": auth_ref, "service_keys": [slug(approved) if market == "us" and approved else proc_key] if (approved or proc_key) else []}] if auth_ref else []
    acc = (_kv(docs.get("discharge"), "Accident related") or "no").lower() == "yes"
    claim = {"id": case.get("claim_id") or f"UPL-{case['id']}", "member": bill.get("member_id") or case.get("member_id"), "provider": case.get("provider", "Hospital"),
             "service_date": bill["service_date"], "stay_days": bill.get("stay_days"), "accident": acc, "procedure": proc_key, "authorizations": auths,
             "lines": [{k: ln[k] for k in ("id", "code", "desc", "service_key", "category", "room_linked", "qty", "date", "billed")} for ln in lines]}
    return {"log": log.steps, "extractor": extractor, "claim": claim, "lines": lines, "checks": checks,
            "summary": {"lines": len(lines), "verified": verified, "billed": round(sum(ln["billed"] for ln in lines), 2), "checks": tally}}


# ---------------------------------------------------------------- agent 2
def _by_reason(pack: dict, res: dict, rules_by_id: dict) -> list[dict]:
    agg = {}
    for ln in res["lines"]:
        for r in ln["reductions"]:
            meta = pack["reasons"].get(r["reason"], {})
            a = agg.setdefault(r["reason"], {"reason": r["reason"], "code": meta.get("code", r["reason"]), "label": meta.get("label", r["reason"]), "kind": meta.get("kind", "reduction"),
                                              "amount": 0.0, "lines": [], "ref": r["ref"], "quote": (rules_by_id.get((r["ref"] or {}).get("id")) or {}).get("quote")})
            a["amount"] = round(a["amount"] + r["amount"], 2)
            a["lines"].append(ln["id"])
    return sorted(agg.values(), key=lambda a: -a["amount"])


def run_adjudicate(pack: dict, case: dict, pub: dict) -> dict:
    log, market, intake = Log("Adjudication Agent"), pack["market"], case["intake"]
    claim, model = copy.deepcopy(intake["claim"]), pub["model"]
    rules_by_id = {r["id"]: r for r in pub["rules"]}
    log.step("Load published rule set", f"Version {pub['version']}, {len(pub['rules'])} approved rules, published by {pub['published_by']}")
    member = pack["members"][claim["member"]]
    if market == "in":
        res = engine.adjudicate_in(model, claim, member, pack["tariff"])
    else:
        res = engine.adjudicate_us(model, claim, {"deductible_met": member["deductible_met"], "oop_met": member["oop_met"]}, pack["tariff"])
    log.step("Run each line through the engine", f"{len(res['lines'])} lines. No AI is used here: the same input always gives the same answer.")
    for ln in res["lines"]:
        for r in ln["reductions"]:
            if r["ref"]:
                r["ref"] = {**r["ref"], "quote": (rules_by_id.get(r["ref"]["id"]) or {}).get("quote")}
    t, by = res["totals"], _by_reason(pack, res, rules_by_id)
    log.step("Attach the clause behind every reduction", f"{sum(len(ln['reductions']) for ln in res['lines'])} reductions, {sum(1 for ln in res['lines'] for r in ln['reductions'] if r['ref'])} with a clause reference")
    flags = []
    for ln in intake["lines"]:
        tf = pack["tariff"].get(ln["code"])
        if tf and ln["billed"] > pack["above_tariff_ratio"] * tf["rate"] * ln["qty"]:
            flags.append({"severity": "warn", "text": f"{ln['id']} {ln['desc']} is billed at {ln['billed'] / (tf['rate'] * ln['qty']):.2f}x the agreed tariff."})
    for ln in res["lines"]:
        if any(r["reason"] == "DUPLICATE" for r in ln["reductions"]):
            flags.append({"severity": "warn", "text": f"{ln['id']} repeats an earlier charge (same item, date and amount)."})
    if market == "in" and not claim["authorizations"] and "cashless" in model.get("prior_auth", []):
        r = (model.get("refs") or {}).get("prior_auth.cashless")
        flags.append({"severity": "warn", "text": "No pre-authorization is on file for this cashless claim." + (f" The policy requires it (clause {r['clause']})." if r else "")})
    fails = [c for c in intake["checks"] if c["status"] == "fail"]
    for c in fails:
        flags.append({"severity": "block", "text": f"Document check failed: {c['title']}. {c['detail']}"})
    log.step("Check for anomalies", f"{len(flags)} flag(s) raised", "ok" if not flags else "warn")
    payable = t["plan"]
    if fails:
        outcome, route = "query", "Query the hospital: a document check failed, so no decision is proposed."
    elif payable <= 0:
        outcome = "reject"
        route = "Proposed rejection. In India the TPA cannot reject alone: send to the insurer's medical team for confirmation." if market == "in" else "Proposed denial. Send to denial review."
    else:
        outcome = "approve" if t["denied_lines"] == 0 and not by_kind(by, ("reduction", "contractual", "denial")) else "part_approve"
        route = (f"Within the auto-settle limit of {money(market, pack['auto_settle_max'])} and no flags: eligible for fast-track approval." if payable <= pack["auto_settle_max"] and not flags
                 else "Reviewer decision needed" + (f" (flags raised)." if flags else f" (above the auto-settle limit of {money(market, pack['auto_settle_max'])})."))
    log.step("Propose a decision", f"{outcome.replace('_', ' ')}. {route}", "ok" if outcome in ("approve", "part_approve") else "warn")
    prev = case.get("adjudication")
    return {"log": log.steps, "result": res, "by_reason": by, "flags": flags, "outcome": outcome, "route": route, "rule_version": pub["version"],
            "totals": {"billed": t["billed"], "plan": t["plan"], "member": t["member"], "denied_lines": t["denied_lines"]},
            "room_ratio": res.get("room_ratio"), "reprocessed_from": ({"version": prev["rule_version"], "plan": prev["totals"]["plan"]} if prev and prev["rule_version"] != pub["version"] else None)}


def by_kind(by: list, kinds: tuple) -> list:
    return [b for b in by if b["kind"] in kinds]


# ---------------------------------------------------------------- agent 3
def facts_for(pack: dict, case: dict) -> dict:
    market, adj, claim = pack["market"], case["adjudication"], case["intake"]["claim"]
    mem = pack["members"][claim["member"]]
    ded = []
    for b in adj["by_reason"]:
        meta = pack["reasons"].get(b["reason"], {})
        ded.append({"code": b["code"], "reason": b["label"], "amount": b["amount"], "explanation": meta.get("member", ""), "clause": (b["ref"] or {}).get("clause"), "page": (b["ref"] or {}).get("page"),
                    "kind": b["kind"], "lines": b["lines"]})
    return {"claim_id": claim["id"], "patient": mem["name"], "provider": claim["provider"], "service_date": claim["service_date"], "billed": adj["totals"]["billed"],
            "plan_pays": adj["totals"]["plan"], "patient_pays": adj["totals"]["member"], "outcome": adj["outcome"], "deductions": ded, "market": market,
            "rule_version": adj["rule_version"], "appeal_days": pack["appeal_filing_days"]}


DEFAULT_LETTER_TEMPLATE = """Dear {patient},

Claim {claim_id} at {provider} (service date {service_date}). {outcome_line}

Amount billed: {billed}
Payable by the {payer}: {plan_pays}
Payable by you: {patient_pays}

How we reached this amount:
{deductions}

{review_line}

DRAFT for reviewer approval. This letter is not legal advice."""
PLACEHOLDERS = {"patient": "The policyholder's name", "claim_id": "The claim reference", "provider": "The hospital", "service_date": "Admission or service date",
                "outcome_line": "One sentence: approved, approved in part, could not be approved, or more information needed", "billed": "Amount billed",
                "payer": "The insurer (or plan)", "plan_pays": "Amount the insurer pays", "patient_pays": "Amount the patient pays",
                "deductions": "Every deduction with its explanation and clause", "review_line": "The review period, when the decision can be challenged", "rule_version": "The published rule-set version"}
REQUIRED_PLACEHOLDERS = ("deductions", "plan_pays", "patient_pays")


def validate_template(text: str) -> str:
    text = (text or "").strip()
    if not 40 <= len(text) <= 3000:
        raise ValueError("The template must be between 40 and 3000 characters.")
    used = set(re.findall(r"\{(\w+)\}", text))
    unknown = sorted(used - set(PLACEHOLDERS))
    if unknown:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + u + "}" for u in unknown) + ".")
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in used]
    if missing:
        raise ValueError("The template must include " + ", ".join("{" + m + "}" for m in missing) + ".")
    return text


def template_letter(pack: dict, f: dict, template: str | None = None) -> str:
    m, mk = pack["terms"], f["market"]
    rows = []
    for i, d in enumerate(f["deductions"], 1):
        cite = f" [{('Policy' if mk == 'in' else 'Plan')} clause {d['clause']}, page {d['page']}]" if d["clause"] else ""
        rows.append(f"{i}. {d['reason']}: {money(mk, d['amount'])}. {d['explanation']}{cite}")
    head = {"approve": "Your claim is approved in full.", "part_approve": "Your claim is approved in part.", "reject": "Your claim could not be approved.", "query": "We need more information before we can decide your claim."}[f["outcome"]]
    review = (f"If you disagree, you may ask for a review within {f['appeal_days']} days of this letter (illustrative period: check the current rules before relying on it)."
              if f["outcome"] in ("part_approve", "reject") else "")
    values = {"patient": f["patient"], "claim_id": f["claim_id"], "provider": f["provider"], "service_date": f["service_date"], "outcome_line": head, "billed": money(mk, f["billed"]),
              "payer": m["payer"].split(" / ")[0].lower(), "plan_pays": money(mk, f["plan_pays"]), "patient_pays": money(mk, f["patient_pays"]), "deductions": "\n".join(rows),
              "review_line": review, "rule_version": f"version {f['rule_version']}"}
    out = re.sub(r"\{(\w+)\}", lambda x: values.get(x.group(1), x.group(0)), template or DEFAULT_LETTER_TEMPLATE)
    return re.sub(r"\n{3,}", "\n\n", out)


NUMBER = re.compile(r"(?:Rs\.?|INR|₹|\$)\s*([\d,]+(?:\.\d+)?)", re.I)
CLAUSE = re.compile(r"(?:clause|cl\.|section)\s*(\d+(?:\.\d+)*)", re.I)


def verify_letter(text: str, facts: dict) -> dict:
    """Every amount and clause in a draft must exist in the computed facts. Used for every AI-written letter."""
    allowed, ok_clause = set(), set()

    def walk(x, key=""):
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            allowed.add(round(float(x), 2))
        elif isinstance(x, str):
            allowed.update(round(float(m.replace(",", "")), 2) for m in NUMBER.findall(x))  # amounts quoted inside the facts' own text
            ok_clause.update(CLAUSE.findall(x))
            if key == "clause" and x:
                ok_clause.add(x)
        elif isinstance(x, dict):
            [walk(v, k) for k, v in x.items()]
        elif isinstance(x, list):
            [walk(v, key) for v in x]
    walk({k: v for k, v in facts.items() if k not in ("appeal_days",)})
    bad_amt = sorted({a for a in (float(m.replace(",", "")) for m in NUMBER.findall(text)) if round(a, 2) not in allowed})
    bad_cl = sorted({c for c in CLAUSE.findall(text) if c not in ok_clause})
    return {"amounts_ok": not bad_amt, "clauses_ok": not bad_cl, "unknown_amounts": bad_amt, "unknown_clauses": bad_cl}


LETTER_SYSTEM = ("You write settlement letters for a health insurance claims team. Write to the patient in plain, polite language. "
                 "Use ONLY the amounts, clause numbers and facts in FACTS. Never invent or recalculate a number. For every deduction give its amount, its explanation and its clause. "
                 "Do not give legal advice or promise any outcome. End with the line: DRAFT for reviewer approval.")


def run_explain(pack: dict, case: dict, template: str | None = None) -> dict:
    log, f = Log("Explanation Agent"), facts_for(pack, case)
    log.step("Load the letter template", "The reviewer's configured template." if template and template.strip() != DEFAULT_LETTER_TEMPLATE.strip() else "The default template.")
    log.step("Collect the computed facts", f"{len(f['deductions'])} deductions, each with its clause. Gemini will see only these facts.")
    import json
    text, source, ver, note = None, "template", None, None
    if ai.available():
        text = ai.generate_text(LETTER_SYSTEM, "HOUSE TEMPLATE (follow its sections and tone; replace each placeholder with the real value from FACTS):\n" + (template or DEFAULT_LETTER_TEMPLATE) + "\n\nFACTS:\n" + json.dumps(f, indent=1))
        if text:
            ver = verify_letter(text, f)
            if ver["amounts_ok"] and ver["clauses_ok"]:
                source = "gemini"
                log.step("Gemini drafts the letter", "Draft written from the facts only")
                log.step("Verify the draft", "Every amount and every clause in the draft exists in the facts")
            else:
                note = f"The Gemini draft was rejected: unknown amounts {ver['unknown_amounts']}, unknown clauses {ver['unknown_clauses']}. The template was used instead."
                log.step("Verify the draft", note, "warn")
                text = None
        else:
            log.step("Gemini call failed", "Using the template instead.", "warn")
    if not text:
        text = template_letter(pack, f, template)
        log.step("Write the letter", "Template used" + ("" if ai.available() else " (no Gemini key)"))
        ver = verify_letter(text, f)
    log.step("Per-deduction explanations", f"{len(f['deductions'])} explanations, each citing its clause")
    return {"log": log.steps, "letter": text, "source": source, "verification": ver, "note": note, "facts": f}
