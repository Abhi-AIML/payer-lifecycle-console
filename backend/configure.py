"""Agent 2: Configuration & Coding Agent.

Turns APPROVED rules into (1) a canonical benefit model, (2) a system-ready configuration payload, and
(3) service-to-code mappings from an approved, effective-dated crosswalk. It flags missing, one-to-many and
expired mappings for a coding reviewer. Mapping is deterministic. Gemini may only PROPOSE a mapping for a
service missing from the crosswalk, and any such proposal is marked unverified.
"""
import csv
import io
from datetime import date

import ai
from interpret import Log
from rulespec import PROTECTED_MAP, SERVICES

CURRENCY_CODE = {"us": "USD", "in": "INR"}
UNCODED = {"cosmetic", "experimental", "cashless", "initial"}  # categories, not codeable services


def _ref(r):
    return {"id": r["id"], "clause": r["clause"], "page": r["page"]}


def to_canonical(rules: list[dict], pack: dict, parsed: dict) -> dict:
    """Approved rules (with any reviewer decisions applied) become the benefit model the engine and payload use."""
    m = pack["market"]
    model = {"market": m, "plan": {"name": pack["plan"]["name"], "effective_from": parsed.get("effective"), "version": parsed.get("version", 1)}, "refs": {}}
    refs = model["refs"]
    if m == "us":
        model.update(cost_share={}, copays={}, deductible_exempt=[], prior_auth=[], exclusions=[])
    else:
        model.update(sub_limits={}, waiting_period_days={}, non_payable=[], exclusions=[], prior_auth=[], proportionate_scope="linked", protected_categories=[])
    for r in rules:
        c, s, v = r["category"], r.get("service"), r["value"]
        if c == "deductible":
            model["cost_share"]["deductible"] = v; refs["deductible"] = _ref(r)
        elif c == "coinsurance_member_pct":
            model["cost_share"]["coinsurance_member_pct"] = v; refs[c] = _ref(r)
        elif c == "oop_max":
            model["cost_share"]["oop_max"] = v; refs[c] = _ref(r)
        elif c == "copay":
            model["copays"][s] = v; refs[f"copay.{s}"] = _ref(r)
        elif c == "deductible_exempt":
            model["deductible_exempt"].append(s); refs[f"deductible_exempt.{s}"] = _ref(r)
        elif c == "prior_auth":
            model["prior_auth"].append(s); refs[f"prior_auth.{s}"] = _ref(r)
        elif c == "exclusion":
            model["exclusions"].append(s); refs[f"exclusion.{s}"] = _ref(r)
        elif c == "sum_insured":
            model["sum_insured"] = v; refs[c] = _ref(r)
        elif c == "room_rent_cap":
            model["room_rent_cap_per_day"] = v; refs[c] = _ref(r)
        elif c == "proportionate_scope":
            model["proportionate_scope"] = r.get("decision") or v; refs[c] = _ref(r)
            model["proportionate_items"] = r.get("items") or []
            model["protected_categories"] = sorted({PROTECTED_MAP[x.lower()] for x in (r.get("protected") or []) if x.lower() in PROTECTED_MAP})
        elif c == "sub_limit":
            model["sub_limits"][s] = {"amount": v, "per": r.get("per") or "admission"}; refs[f"sub_limit.{s}"] = _ref(r)
        elif c == "waiting_period":
            model["waiting_period_days"][s] = v; refs[f"waiting_period.{s}"] = _ref(r)
        elif c == "copay_pct":
            model["copay_pct"] = v; refs[c] = _ref(r)
        elif c == "non_payable":
            model["non_payable"] = r.get("items") or []; refs[c] = _ref(r)
    return model


def fields_from_model(model: dict) -> list[dict]:
    """Flatten the model into the field list a payer system would load. Each field points at its source rule."""
    cur, f, refs = CURRENCY_CODE[model["market"]], [], model["refs"]

    def add(path, value, ref_key, unit=""):
        f.append({"field": path, "value": value, "unit": unit, "source_rule": (refs.get(ref_key) or {}).get("id"), "clause": (refs.get(ref_key) or {}).get("clause")})

    if model["market"] == "us":
        cs = model["cost_share"]
        add("cost_share.deductible", cs.get("deductible"), "deductible", cur)
        add("cost_share.coinsurance_member_pct", cs.get("coinsurance_member_pct"), "coinsurance_member_pct", "%")
        add("cost_share.oop_max", cs.get("oop_max"), "oop_max", cur)
        for s, v in model["copays"].items():
            add(f"copays.{s}", v, f"copay.{s}", cur)
        for s in model["deductible_exempt"]:
            add(f"deductible_exempt.{s}", True, f"deductible_exempt.{s}")
        for s in model["prior_auth"]:
            add(f"prior_auth.{s}", True, f"prior_auth.{s}")
        for s in model["exclusions"]:
            add(f"exclusions.{s}", True, f"exclusion.{s}")
    else:
        add("sum_insured", model.get("sum_insured"), "sum_insured", cur)
        add("room_rent_cap_per_day", model.get("room_rent_cap_per_day"), "room_rent_cap", cur)
        add("proportionate_scope", model.get("proportionate_scope"), "proportionate_scope")
        add("proportionate_protected_categories", ", ".join(model.get("protected_categories", [])), "proportionate_scope")
        for s, v in model["sub_limits"].items():
            add(f"sub_limits.{s}.amount", v["amount"], f"sub_limit.{s}", cur)
            add(f"sub_limits.{s}.per", v["per"], f"sub_limit.{s}")
        for s, v in model["waiting_period_days"].items():
            add(f"waiting_period_days.{s}", v, f"waiting_period.{s}", "days")
        add("copay_pct", model.get("copay_pct", 0), "copay_pct", "%")
        add("non_payable", ", ".join(model.get("non_payable", [])), "non_payable")
        for s in model["exclusions"]:
            add(f"exclusions.{s}", True, f"exclusion.{s}")
        for s in model["prior_auth"]:
            add(f"prior_auth.{s}", True, f"prior_auth.{s}")
    return f


def model_from_fields(fields: list[dict], template: dict) -> dict:
    """Rebuild the model a system would behave as, from the loaded fields. This is what the Validation Agent tests."""
    m = template["market"]
    model = {"market": m, "plan": template["plan"], "refs": template["refs"]}
    if m == "us":
        model.update(cost_share={}, copays={}, deductible_exempt=[], prior_auth=[], exclusions=[])
    else:
        model.update(sub_limits={}, waiting_period_days={}, non_payable=template.get("non_payable", []), exclusions=[], prior_auth=[], proportionate_scope="linked", protected_categories=[])
    for x in fields:
        p, v = x["field"].split("."), x["value"]
        if p[0] == "cost_share":
            model["cost_share"][p[1]] = v
        elif p[0] == "copays":
            model["copays"][p[1]] = v
        elif p[0] in ("deductible_exempt", "prior_auth", "exclusions") and v is True:
            model[p[0]].append(p[1])
        elif p[0] == "sub_limits":
            model["sub_limits"].setdefault(p[1], {})[p[2]] = v
        elif p[0] == "waiting_period_days":
            model["waiting_period_days"][p[1]] = v
        elif p[0] == "proportionate_protected_categories":
            model["protected_categories"] = [c.strip() for c in str(v).split(",") if c.strip()]
        elif p[0] in ("sum_insured", "room_rent_cap_per_day", "proportionate_scope", "copay_pct"):
            model[p[0]] = v
    return model


def payload_csv(fields: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["field", "value", "unit", "source_rule", "clause"])
    for x in fields:
        w.writerow([x["field"], x["value"], x["unit"], x["source_rule"], x["clause"]])
    return buf.getvalue()


def _services_in_model(model: dict) -> list[str]:
    s = list(model.get("copays", {})) + model.get("prior_auth", []) + model.get("exclusions", []) + model.get("deductible_exempt", []) + list(model.get("sub_limits", {}))
    s += [k for k in model.get("waiting_period_days", {}) if k != "initial"]
    if model.get("non_payable"):
        s.append("non_medical")
    return sorted(set(x for x in s if x and x not in UNCODED))


def map_codes(model: dict, pack: dict, effective: str | None, *, allow_ai: bool = True) -> list[dict]:
    """Service-to-code mapping from the approved crosswalk, checked against the plan's effective date."""
    eff = date.fromisoformat(effective) if effective and effective[:4].isdigit() else date.today()
    out = []
    for svc in _services_in_model(model):
        entry = pack["crosswalk"].get(svc)
        label = SERVICES.get(svc, svc.replace("_", " "))
        if not entry:
            item = {"service": svc, "label": label, "status": "missing", "codes": [], "excluded": [], "note": "No approved mapping in the crosswalk. A coding reviewer must assign one.", "proposed_by_ai": False}
            if allow_ai and ai.available():
                res = ai.generate_json("You suggest medical code families for a health benefit service. Reply with JSON {\"candidates\":[{\"system\":\"\",\"code\":\"\"}]}. Suggestions only; a coding expert will verify.",
                                       f"Service: {label}. Market: {pack['label']}.")
                if res and isinstance(res.get("candidates"), list) and res["candidates"]:
                    item["codes"] = [{"system": str(c.get("system", "")), "code": str(c.get("code", ""))} for c in res["candidates"][:3]]
                    item.update(status="review", proposed_by_ai=True, note="Proposed by AI and NOT verified against a code table. A coding reviewer must confirm.")
            out.append(item)
            continue
        valid, excl = [], []
        for c in entry["codes"]:
            vf, vt = date.fromisoformat(c["valid_from"]), (date.fromisoformat(c["valid_to"]) if c["valid_to"] else None)
            (valid if vf <= eff and (vt is None or eff <= vt) else excl).append({"system": c["system"], "code": c["code"]})
        if not valid:
            out.append({"service": svc, "label": label, "status": "expired", "codes": [], "excluded": excl, "note": "Every mapped code is outside its validity window on the plan's effective date.", "proposed_by_ai": False})
        elif len({c["system"] for c in valid}) and len(valid) > len({c["system"] for c in valid}):
            out.append({"service": svc, "label": label, "status": "one_to_many", "codes": valid, "excluded": excl,
                        "note": "One service maps to several codes in the same code set. A coding reviewer confirms they all apply.", "proposed_by_ai": False})
        else:
            out.append({"service": svc, "label": label, "status": "mapped", "codes": valid, "excluded": excl,
                        "note": f"{len(excl)} superseded code(s) excluded by effective date." if excl else "", "proposed_by_ai": False})
    return out


def run(pack: dict, rules: list[dict], parsed: dict, *, allow_ai: bool = True) -> dict:
    log = Log("Configuration & Coding Agent")
    approved = [r for r in rules if r["status"] == "approved"]
    log.step("Collect approved rules", f"{len(approved)} approved rules go in. Rejected or unapproved rules stay out.")
    model = to_canonical(approved, pack, parsed)
    log.step("Build the standard benefit model", f"Effective {model['plan']['effective_from'] or 'date not found'}, version {model['plan']['version']}. Every field keeps a link to its source rule.")
    fields = fields_from_model(model)
    log.step("Generate the configuration payload", f"{len(fields)} fields in a system-ready list (JSON and CSV). Each field names the rule and clause it came from.")
    mappings = map_codes(model, pack, parsed.get("effective_iso") or _iso(parsed.get("effective")), allow_ai=allow_ai)
    counts = {k: sum(1 for m in mappings if m["status"] == k) for k in ("mapped", "one_to_many", "missing", "expired", "review")}
    log.step("Map services to codes", f"{counts['mapped']} mapped, {counts['one_to_many']} one-to-many, {counts['missing']} missing, {counts['expired']} expired"
             + (f", {counts['review']} AI proposals to verify" if counts["review"] else "") + ".",
             "ok" if not (counts["one_to_many"] or counts["missing"] or counts["expired"] or counts["review"]) else "warn")
    return {"model": model, "fields": fields, "mappings": mappings, "log": log.steps, "counts": counts,
            "licensing_note": pack.get("licensing_note", "")}


def _iso(text: str | None) -> str | None:
    if not text:
        return None
    from datetime import datetime
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None
