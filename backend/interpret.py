"""Agent 1: Interpretation Agent.

Reads a plan document and proposes benefit rules, each with its page, clause and exact quote.
Gemini does the reading when a key is set; otherwise a rule-based fallback reads the demo document format.
Either way, EVERY rule is then verified by code: the quote must exist in the document and the value must
appear in the quote. Verification replaces AI "confidence scores". A person approves every rule afterwards.
"""
import re
import time

import ai
from docparse import norm, parse_document
from rulespec import CATEGORIES, CATEGORICAL, NUMERIC, SERVICES, _ALIASES, money_number, rule_label, slug_service

CUR = r"(?:rs\.?|₹|\$)?\s*"


class Log:
    def __init__(self, agent: str):
        self.agent, self.t0, self.steps = agent, time.perf_counter(), []

    def step(self, name: str, detail: str = "", status: str = "ok") -> None:
        self.steps.append({"agent": self.agent, "step": name, "detail": detail, "status": status,
                           "ms": round((time.perf_counter() - self.t0) * 1000)})


def _r(clause, category, *, service=None, value=None, raw=None, unit=None, per=None, items=None, protected=None):
    return {"category": category, "service": service, "value": value, "raw_value": raw, "unit": unit, "per": per, "items": items, "protected": protected,
            "clause": clause["clause"], "page": clause["page"], "quote": clause["text"]}


def fallback_extract(parsed: dict) -> list[dict]:
    """Rule-based reader for the demo document format. Used when no Gemini key is set."""
    out = []
    for c in parsed["clauses"]:
        t = c["text"]
        m = re.search(r"annual deductible is " + CUR + r"([\d,]+)", t, re.I)
        if m:
            out.append(_r(c, "deductible", value=money_number(m.group(1)), raw=money_number(m.group(1)), unit="currency"))
        m = re.match(r"^(.+?) (?:are|is) not subject to the deductible", t, re.I)
        if m:
            out.append(_r(c, "deductible_exempt", service=slug_service(m.group(1))))
        m = re.search(r"plan pays (\d+)% and the member pays (\d+)%", t, re.I)
        if m:
            out.append(_r(c, "coinsurance_member_pct", value=int(m.group(2)), raw=int(m.group(2)), unit="pct"))
        m = re.search(r"out-of-pocket maximum is " + CUR + r"([\d,]+)", t, re.I)
        if m:
            out.append(_r(c, "oop_max", value=money_number(m.group(1)), raw=money_number(m.group(1)), unit="currency"))
        m = re.match(r"^(.+?):\s*" + CUR + r"([\d,]+) copay", t, re.I)
        if m:
            out.append(_r(c, "copay", service=slug_service(m.group(1)), value=money_number(m.group(2)), raw=money_number(m.group(2)), unit="currency"))
        m = re.match(r"^(.+?) requires (?:prior|pre)-? ?authori[sz]ation", t, re.I)
        if m:
            out.append(_r(c, "prior_auth", service=slug_service(m.group(1))))
        m = re.match(r"^(.+?) (?:are|is) not covered", t, re.I)
        if m:
            out.append(_r(c, "exclusion", service=slug_service(m.group(1))))
        m = re.search(r"sum insured is " + CUR + r"([\d,]+)", t, re.I)
        if m:
            out.append(_r(c, "sum_insured", value=money_number(m.group(1)), raw=money_number(m.group(1)), unit="currency"))
        m = re.search(r"room rent is covered up to " + CUR + r"([\d,]+) per day", t, re.I)
        if m:
            out.append(_r(c, "room_rent_cap", value=money_number(m.group(1)), raw=money_number(m.group(1)), unit="currency", per="day"))
        if re.search(r"room-linked charges.*reduced in the same proportion", t, re.I):
            items = re.search(r"\((.+?)\)", t)
            prot = re.search(r"([A-Za-z, ]+?) (?:are|is) not room-linked and (?:are|is) not reduced", t)
            out.append(_r(c, "proportionate_scope", value="linked", items=[x.strip() for x in re.split(r",| and ", items.group(1)) if x.strip()] if items else None,
                          protected=[x.strip().lower() for x in re.split(r",| and ", prot.group(1)) if x.strip()] if prot else None))
        m = re.match(r"^(.+?) (?:is|are) covered up to " + CUR + r"([\d,]+) per (admission|eye)", t, re.I)
        if m:
            out.append(_r(c, "sub_limit", service=slug_service(m.group(1)), value=money_number(m.group(2)), raw=money_number(m.group(2)), unit="currency", per=m.group(3).lower()))
        m = re.search(r"initial waiting period of (\d+) days", t, re.I)
        if m:
            out.append(_r(c, "waiting_period", service="initial", value=int(m.group(1)), raw=int(m.group(1)), unit="days"))
        m = re.match(r"^(.+?) is covered only after (\d+) years? of continuous cover", t, re.I)
        if m:
            out.append(_r(c, "waiting_period", service=slug_service(m.group(1)), value=int(m.group(2)) * 365, raw=int(m.group(2)), unit="days"))
        m = re.search(r"co-payment of (\d+)%", t, re.I)
        if m:
            out.append(_r(c, "copay_pct", value=int(m.group(1)), raw=int(m.group(1)), unit="pct"))
        m = re.search(r"non-medical items such as (.+?) (?:are|is) not payable", t, re.I)
        if m:
            out.append(_r(c, "non_payable", service="non_medical", items=[x.strip() for x in re.split(r",| and ", m.group(1)) if x.strip()]))
    return out


GEMINI_SYSTEM = (
    "You extract insurance benefit rules from a plan document. Return ONE JSON object: {\"rules\":[...]}. "
    "Each rule has: category (one of " + ", ".join(CATEGORIES) + "), service (one of " + ", ".join(SERVICES) + " or null), "
    "value (a number, or for proportionate_scope the string 'linked'), raw_value (the number exactly as written in the clause, or null), "
    "unit (currency, pct, days or null), per (day, admission, eye or null), items (list of strings or null), protected (for proportionate_scope: the list of items the clause says are NOT reduced, else null), "
    "clause (the clause number as written), page (integer), quote (the EXACT sentence copied verbatim from the document). "
    "For waiting_period give value in days and raw_value as written. For coinsurance_member_pct give the percentage the MEMBER pays. "
    "Include only rules explicitly stated in the text. Never invent a rule or a number. JSON only."
)


def gemini_extract(parsed: dict, text: str) -> list[dict] | None:
    result = ai.generate_json(GEMINI_SYSTEM, "Document:\n" + text)
    if not result or not isinstance(result.get("rules"), list):
        return None
    out = []
    for r in result["rules"]:
        try:
            cat = r["category"]
            if cat not in CATEGORIES:
                continue
            svc = r.get("service") if r.get("service") in SERVICES else (slug_service(str(r.get("service") or "")) or None)
            val = r.get("value")
            if cat in NUMERIC:
                val = float(val)
                val = int(val) if val == int(val) else val
            out.append({"category": cat, "service": svc, "value": val, "raw_value": r.get("raw_value"), "unit": r.get("unit"), "per": r.get("per"),
                        "items": r.get("items"), "protected": r.get("protected"), "clause": str(r.get("clause") or ""), "page": int(r.get("page") or 1), "quote": str(r.get("quote") or "")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _numbers(s: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]


def verify_rule(rule: dict, doc_text: str) -> dict:
    """Deterministic evidence check. Returns the evidence record and appends flags to the rule."""
    q = norm(rule["quote"])
    quote_found = bool(q) and q in norm(doc_text)
    ql, value_ok = rule["quote"].lower(), True
    if rule["category"] in NUMERIC:
        raw = rule.get("raw_value")
        raw = float(raw) if isinstance(raw, (int, float, str)) and str(raw).replace(".", "", 1).isdigit() else None
        value_ok = raw is not None and any(abs(n - raw) < 1e-9 for n in _numbers(rule["quote"]))
    elif rule["category"] in ("prior_auth", "exclusion", "deductible_exempt"):
        svc = rule.get("service")
        value_ok = bool(svc) and any(alias in ql for alias, key in _ALIASES if key == svc)
    elif rule["category"] == "non_payable":
        value_ok = bool(rule.get("items")) and any(i.lower() in ql for i in rule["items"])
    elif rule["category"] == "proportionate_scope":
        value_ok = "proportion" in ql
    return {"quote_found": quote_found, "value_in_quote": value_ok}


def _finish(rules: list[dict], parsed: dict, text: str, pack: dict, extractor: str) -> list[dict]:
    amb = {a["category"]: a for a in pack.get("ambiguities", [])}
    seen: dict = {}
    for i, r in enumerate(rules, 1):
        r["id"] = f"R{i}"
        r["label"] = rule_label(r["category"], r.get("service"))
        r["extractor"], r["status"], r["flags"] = extractor, "proposed", []
        r["evidence"] = verify_rule(r, text)
        # Page and clause are read from the document itself: the "[Page N]" marker and the number printed at the start of the clause.
        # Whoever extracted them (code or Gemini), this check confirms the quote sits in exactly that clause on exactly that page.
        q = norm(r.get("quote") or "")
        r["evidence"]["page_clause"] = bool(q) and any(str(c["page"]) == str(r.get("page")) and str(c["clause"]).strip() == str(r.get("clause")).strip() and q in norm(c["text"]) for c in parsed["clauses"])
        if r["evidence"]["quote_found"] and not r["evidence"]["page_clause"]:
            r["flags"].append({"severity": "block", "text": f"The quote is in the document, but not in clause {r.get('clause')} on page {r.get('page')}."})
        if not r["evidence"]["quote_found"]:
            r["flags"].append({"severity": "block", "text": "The quote was not found in the source document."})
        if not r["evidence"]["value_in_quote"]:
            r["flags"].append({"severity": "block", "text": "The value does not appear in the quoted clause."})
        if re.search(r"(?<!not )subject to|where applicable|unless otherwise|may be", r["quote"], re.I):
            r["flags"].append({"severity": "warn", "text": "Conditional wording. Confirm how it applies."})
        if r["category"] in amb:
            r["needs_decision"], r["ambiguity"], r["decision"] = True, amb[r["category"]], None
        seen.setdefault((r["category"], r.get("service"), r.get("per")), []).append(r)
    for group in seen.values():
        if len(group) > 1 and len({str(g["value"]) for g in group}) > 1:
            for g in group:
                g["flags"].append({"severity": "block", "text": "Conflicts with another clause that gives a different value."})
    return rules


def run(pack: dict, text: str, *, allow_ai: bool = True) -> dict:
    """Run the Interpretation Agent. Returns rules, a parsed summary and a step-by-step log."""
    log = Log("Interpretation Agent")
    parsed = parse_document(text)
    log.step("Read and classify the document", f"{parsed['title']}. {parsed['pages']} page(s), {len(parsed['clauses'])} clauses, "
             f"{'amendment' if parsed['is_amendment'] else 'base document'}, effective {parsed['effective'] or 'date not found'}.")
    rules, extractor = None, "rules-fallback"
    if allow_ai and ai.available():
        log.step("Ask Gemini to extract rules", f"Model {ai.model()}. Each rule must carry its page, clause and exact quote.")
        rules = gemini_extract(parsed, text)
        if rules is not None:
            extractor = "gemini"
        else:
            log.step("Gemini did not return usable rules", "Falling back to the rule-based reader.", "warn")
    if rules is None:
        rules = fallback_extract(parsed)
        log.step("Extract rules (rule-based fallback)", "No Gemini key, so the rule-based reader handled the demo document format." if not ai.available() else "Fallback reader used.",
                 "info")
    rules = _finish(rules, parsed, text, pack, extractor)
    ok = sum(1 for r in rules if not any(f["severity"] == "block" for f in r["flags"]))
    log.step("Verify evidence for every rule", f"{ok} of {len(rules)} rules passed: the quote exists in the document and the value appears in the quote.",
             "ok" if ok == len(rules) else "warn")
    decisions = [r for r in rules if r.get("needs_decision")]
    if decisions:
        log.step("Flag ambiguity for a person", "; ".join(f"{d['label']}: needs a reviewer decision" for d in decisions), "warn")
    have = {r["category"] for r in rules}
    missing = [c for c in pack["required"] if c not in have]
    log.step("Check required rules", "All required rules found." if not missing else "Missing: " + ", ".join(missing), "ok" if not missing else "warn")
    return {"parsed": {k: parsed[k] for k in ("title", "effective", "version", "is_amendment", "pages")} | {"clauses": len(parsed["clauses"])},
            "rules": rules, "extractor": extractor, "log": log.steps,
            "summary": {"rules": len(rules), "verified": ok, "flagged": len(rules) - ok, "needs_decision": len(decisions), "missing_required": missing}}


def diff_rules(old: list[dict], new: list[dict]) -> list[dict]:
    key = lambda r: (r["category"], r.get("service"), r.get("per"))  # noqa: E731
    o, n, out = {key(r): r for r in old}, {key(r): r for r in new}, []
    for k, r in n.items():
        if k not in o:
            out.append({"change": "added", "label": r["label"], "old": None, "new": r["value"], "old_clause": None, "new_clause": r["clause"], "category": r["category"], "service": r.get("service")})
        elif str(o[k]["value"]) != str(r["value"]):
            out.append({"change": "changed", "label": r["label"], "old": o[k]["value"], "new": r["value"], "old_clause": o[k]["clause"], "new_clause": r["clause"], "category": r["category"], "service": r.get("service")})
    for k, r in o.items():
        if k not in n:
            out.append({"change": "removed", "label": r["label"], "old": r["value"], "new": None, "old_clause": r["clause"], "new_clause": None, "category": r["category"], "service": r.get("service")})
    return out
