"""Reference tables for each module's sidebar: read-only views of the data its agents work from.

Every row comes from the market pack or from live state (the published rule set). Nothing here is typed in for display.
A column's `fmt` tells the page how to show it: "money" uses the market's currency, "num" right-aligns, anything else is text.
"""
import claims
import denials
import service as s


def _t(title, note, columns, rows, **extra):
    return {"title": title, "note": note, "columns": [{"key": k, "label": l, **({"fmt": f} if f else {})} for k, l, f in columns], "rows": rows, **extra}


# ---------------------------------------------------------------- benefits
def crosswalk(p, market):
    rows = [{"service": v["desc"], "system": c["system"], "code": c["code"], "valid_from": c["valid_from"], "valid_to": c["valid_to"] or "open"}
            for v in p["crosswalk"].values() for c in v["codes"]]
    return _t("Service-to-code crosswalk", p.get("licensing_note", ""), [("service", "Service", None), ("system", "Code system", None), ("code", "Code", None), ("valid_from", "Valid from", None), ("valid_to", "Valid to", None)], rows)


def published_rules(p, market):
    pub = s.STATE["published"][market]
    if not pub:
        return _t("Published rule set", "Nothing is published yet. Run the three agents and publish on the last agent page.", [("label", "Rule", None)], [])
    rows = [{"id": r["id"], "label": r["label"], "value": _val(r), "clause": r["clause"], "page": r["page"], "quote": r["quote"]} for r in pub["rules"]]
    return _t(f"Published rule set, version {pub['version']}", f"{pub['title']}. Published by {pub['published_by']}. Claims read only this rule set.",
              [("id", "Rule", None), ("label", "Meaning", None), ("value", "Value", None), ("clause", "Clause", None), ("page", "Page", "num"), ("quote", "Quote from the document", None)], rows)


def _val(r):
    if r.get("category") == "proportionate_scope":
        return "Room-linked charges, ICU not reduced" if (r.get("decision") or r.get("value")) == "icu_exempt" else "Room-linked charges, ICU reduced"
    if r.get("items"):
        return ", ".join(r["items"])
    if r.get("service") and r.get("value") is None:
        return str(r["service"]).replace("_", " ")
    unit = {"pct": "%", "days": " days"}.get(r.get("unit"), "")
    return f"{r.get('value')}{unit}" + (f" per {r['per']}" if r.get("per") else "")


def model_preview(p, market):
    pub = s.STATE["published"][market]
    if not pub:
        return _t("Benefit model preview", "Nothing is published yet.", [], [], json=None)
    model = {k: v for k, v in pub["model"].items() if k != "refs"}
    return _t(f"Benefit model preview, version {pub['version']}", "The configuration the claims engine runs. Each field traces to a rule through the published rule set.", [], [], json=model)


# ---------------------------------------------------------------- claims
def tariff(p, market):
    rows = [{"code": k, "desc": v["desc"], "rate": v["rate"]} for k, v in p["tariff"].items()]
    return _t("Agreed tariff", "The rate each item is allowed at. Charges above it are reduced under a contract rule, not a policy clause.", [("code", "Code", None), ("desc", "Description", None), ("rate", "Agreed rate", "money")], rows)


def members(p, market):
    if market == "in":
        cols = [("id", "Member", None), ("name", "Name", None), ("policy_start", "Policy start", None), ("sum_insured", "Sum insured", "money"), ("used", "Used this year", "money")]
        rows = [{"id": k, "name": v["name"], "policy_start": v["policy_start"], "sum_insured": v["sum_insured"], "used": v["used"]} for k, v in p["members"].items()]
    else:
        cols = [("id", "Member", None), ("name", "Name", None), ("deductible_met", "Deductible met", "money"), ("oop_met", "Out-of-pocket so far", "money")]
        rows = [{"id": k, "name": v["name"], "deductible_met": v["deductible_met"], "oop_met": v["oop_met"]} for k, v in p["members"].items()]
    return _t("Policyholders", "Synthetic members and the balances the engine applies.", cols, rows)


def item_master(p, market):
    rows = [{"code": k, "category": v["category"] or "", "service_key": (v["service_key"] or "").replace("_", " "), "linked": "yes" if v["room_linked"] else "no"} for k, v in claims.item_master(p).items()]
    return _t("Item master", "How each billed item is categorised. The Intake Agent matches bill lines to these codes.", [("code", "Code", None), ("category", "Category", None), ("service_key", "Service", None), ("linked", "Room-linked", None)], rows)


# ---------------------------------------------------------------- denials
def reasons(p, market):
    rows = [{"code": v["code"], "label": v["label"], "kind": v["kind"].replace("_", " "), "member": v["member"], "edit": v["edit"] or "none needed"} for v in p["reasons"].values()]
    return _t("Reason codes", "What each deduction code means, what the patient is told, and the pre-emptive edit that prevents it.", [("code", "Code", None), ("label", "Reason", None), ("kind", "Type", None), ("member", "What the patient is told", None), ("edit", "Pre-emptive edit", None)], rows)


def root_causes(p, market):
    rows = [{"code": v["code"], "label": v["label"], "root": denials.ROOT.get(k, "Not mapped")} for k, v in p["reasons"].items()]
    return _t("Root causes", "The cause the Classification Agent assigns to each reason code.", [("code", "Code", None), ("label", "Reason", None), ("root", "Root cause", None)], rows)


def escalation(p, market):
    rows = [{"step": e["step"], "note": e["note"], "verified": "Verified" if e["verified"] else "Not verified"} for e in p["escalation"]]
    return _t("Escalation path", p["clock_note"], [("step", "Step", None), ("note", "Note", None), ("verified", "Status", None)], rows)


TABLES = {("benefits", "crosswalk"): crosswalk, ("benefits", "published"): published_rules, ("benefits", "model"): model_preview,
          ("claims", "tariff"): tariff, ("claims", "members"): members, ("claims", "items"): item_master,
          ("denials", "reasons"): reasons, ("denials", "causes"): root_causes, ("denials", "escalation"): escalation}


def table(uc: str, name: str, market: str) -> dict:
    fn = TABLES.get((uc, name))
    if not fn:
        raise LookupError("Reference table not found.")
    return fn(s.pack(market), market)
