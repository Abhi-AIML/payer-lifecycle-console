"""Deterministic benefit engine. No AI here: the same input always gives the same answer.

Used by the Validation Agent (to test a configuration) and by the Claims tile (to adjudicate claims).
Every reduction carries the rule reference (clause and page) that caused it, so explanations can cite it.
"""
from datetime import date


def _r2(x: float) -> float:
    return round(x + 1e-9, 2)


def _ref(model: dict, key: str) -> dict | None:
    return model.get("refs", {}).get(key)


def _red(model, reason, amount, ref_key=None, note=""):
    return {"reason": reason, "amount": _r2(amount), "ref": _ref(model, ref_key) if ref_key else None, "note": note}


def _line(ln):
    return {"id": ln["id"], "desc": ln.get("desc", ""), "code": ln.get("code"), "service_key": ln.get("service_key"), "billed": float(ln["billed"]),
            "allowed": 0.0, "plan": 0.0, "member": 0.0, "status": "paid", "reductions": []}


def _summ(lines):
    return {"billed": _r2(sum(x["billed"] for x in lines)), "plan": _r2(sum(x["plan"] for x in lines)),
            "member": _r2(sum(x["member"] for x in lines)), "denied_lines": sum(x["status"] == "denied" for x in lines)}


# ---------------------------------------------------------------- United States
def adjudicate_us(model: dict, claim: dict, acc: dict, tariff: dict, apply: bool = True) -> dict:
    """Commercial plan logic: coverage, authorization, contracted rate, copay, deductible, coinsurance, out-of-pocket cap."""
    cs, out, seen = model["cost_share"], [], set()
    ded_met, oop_met = acc.get("deductible_met", 0.0), acc.get("oop_met", 0.0)
    authorized = {s for a in claim.get("authorizations", []) for s in a.get("service_keys", [])}
    for ln in claim["lines"]:
        res, svc = _line(ln), ln.get("service_key")
        key = (ln.get("code"), ln.get("date"), ln.get("qty"), ln.get("billed"))
        if key in seen:
            res["status"] = "denied"
            res["reductions"].append(_red(model, "DUPLICATE", ln["billed"], None, "Same code, date and amount as an earlier line."))
            out.append(res)
            continue
        seen.add(key)
        if svc in model.get("exclusions", []):
            res["status"] = "denied"
            res["reductions"].append(_red(model, "NOT_COVERED", ln["billed"], f"exclusion.{svc}"))
            out.append(res)
            continue
        if svc in model.get("prior_auth", []) and svc not in authorized:
            res["status"] = "denied"
            res["reductions"].append(_red(model, "NO_AUTH", ln["billed"], f"prior_auth.{svc}"))
            out.append(res)
            continue
        rate = ln.get("allowed_override")
        if rate is None:
            t = tariff.get(str(ln.get("code")))
            rate = min(res["billed"], t["rate"] * ln.get("qty", 1)) if t else res["billed"]
        res["allowed"] = _r2(rate)
        if res["billed"] > res["allowed"] + 0.004:
            res["reductions"].append(_red(model, "FEE_SCHEDULE", res["billed"] - res["allowed"], None, "Charge above the contracted rate."))
        allowed = res["allowed"]
        if svc in model.get("copays", {}):
            member = min(float(model["copays"][svc]), allowed)
            res["reductions"].append(_red(model, "COPAY", member, f"copay.{svc}"))
        else:
            ded_left = 0.0 if svc in model.get("deductible_exempt", []) else max(0.0, cs["deductible"] - ded_met)
            ded_part = min(ded_left, allowed)
            coins = _r2((allowed - ded_part) * cs["coinsurance_member_pct"] / 100.0)
            member = ded_part + coins
            oop_left = max(0.0, cs["oop_max"] - oop_met)
            capped = min(member, oop_left)
            if ded_part:
                res["reductions"].append(_red(model, "DEDUCTIBLE", ded_part, "deductible"))
            if coins:
                res["reductions"].append(_red(model, "COINSURANCE", coins, "coinsurance_member_pct"))
            if capped < member - 0.004:
                res["reductions"].append({"reason": "OOP_CAP", "amount": _r2(member - capped), "ref": _ref(model, "oop_max"), "note": "Member share capped at the out-of-pocket maximum."})
            if apply:
                ded_met += ded_part
            member = capped
        if apply:
            oop_met += member
        res["member"], res["plan"] = _r2(member), _r2(allowed - member)
        if res["plan"] <= 0 and res["allowed"] > 0:
            res["status"] = "member_pays_all"
        elif res["reductions"]:
            res["status"] = "reduced"
        out.append(res)
    return {"lines": out, "totals": _summ(out), "accumulators": {"deductible_met": _r2(ded_met), "oop_met": _r2(oop_met)}}


# ---------------------------------------------------------------- India
def _age_days(start: str, on: str) -> int:
    return (date.fromisoformat(on) - date.fromisoformat(start)).days


def adjudicate_in(model: dict, claim: dict, member: dict, tariff: dict) -> dict:
    """Indian retail health policy logic: waiting periods, room-rent cap with proportionate deduction, sub-limit, co-pay, sum insured."""
    lines = [_line(ln) for ln in claim["lines"]]
    src = {ln["id"]: ln for ln in claim["lines"]}
    age = _age_days(member["policy_start"], claim["service_date"])
    wait = model.get("waiting_period_days", {})
    wp_key = None
    if not claim.get("accident"):
        if "initial" in wait and age < wait["initial"]:
            wp_key = "initial"
        elif claim.get("procedure") in wait and age < wait[claim["procedure"]]:
            wp_key = claim["procedure"]
    if wp_key:
        for ln in lines:
            ln["status"] = "denied"
            ln["reductions"].append(_red(model, "WAITING_PERIOD", ln["billed"], f"waiting_period.{wp_key}",
                                         f"Policy is {age} days old; the waiting period is {wait[wp_key]} days."))
            ln["member"] = ln["billed"]
        return {"lines": lines, "totals": _summ(lines), "waiting_denied": True}
    cap = model["room_rent_cap_per_day"]
    scope, ratio, seen = model.get("proportionate_scope", "linked"), 1.0, set()
    protected = set(model.get("protected_categories", []))
    apply_all = bool(model.get("proportionate_apply_all"))  # a mis-set claims system, used only to reproduce an insurer's wrong letter
    for ln in lines:
        s = src[ln["id"]]
        key = (s.get("code"), s.get("date"), s.get("qty"), s.get("billed"))
        if key in seen:
            ln["status"] = "denied"
            ln["reductions"].append(_red(model, "DUPLICATE", ln["billed"], None, "Same item, date and amount as an earlier charge."))
            continue
        seen.add(key)
        if s.get("category") == "non_medical" or s.get("service_key") == "non_medical":
            ln["status"] = "denied"
            ln["reductions"].append(_red(model, "NON_PAYABLE", ln["billed"], "non_payable"))
            continue
        if s.get("service_key") in model.get("exclusions", []):
            ln["status"] = "denied"
            ln["reductions"].append(_red(model, "NOT_COVERED", ln["billed"], f"exclusion.{s.get('service_key')}"))
            continue
        t = tariff.get(s.get("code"))
        allowed = min(ln["billed"], t["rate"] * s.get("qty", 1)) if t else ln["billed"]
        if ln["billed"] > allowed + 0.004:
            ln["reductions"].append(_red(model, "TARIFF", ln["billed"] - allowed, None, "Charge above the hospital's agreed tariff."))
        if s.get("category") == "room":
            qty = max(1, s.get("qty", 1))
            daily = ln["billed"] / qty
            if daily > cap + 0.004:
                capped = min(allowed, cap * qty)
                ln["reductions"].append(_red(model, "ROOM_CAP", allowed - capped, "room_rent_cap", f"{daily:,.0f} per day against a cap of {cap:,.0f}."))
                allowed, ratio = capped, cap / daily
        ln["allowed"] = _r2(allowed)
    if ratio < 1.0:
        for ln in lines:
            s = src[ln["id"]]
            cat = s.get("category")
            # The policy wording decides what is room-linked. Protected categories (implants, medicines, consumables, diagnostics) are never reduced,
            # and ICU follows the reviewer's decision.
            eligible = cat != "room" and (apply_all or (s.get("room_linked") and cat not in protected and not (scope == "icu_exempt" and cat == "icu")))
            if ln["status"] != "denied" and eligible:
                cut = ln["allowed"] * (1 - ratio)
                ln["reductions"].append(_red(model, "PROPORTIONATE", cut, "proportionate_scope", f"Ratio {ratio:.3f} applied to a room-linked charge."))
                ln["allowed"] = _r2(ln["allowed"] - cut)
    payable = [ln for ln in lines if ln["status"] != "denied"]
    subtotal = sum(ln["allowed"] for ln in payable)
    sl = model.get("sub_limits", {}).get(claim.get("procedure"))
    if sl and subtotal > sl["amount"] + 0.004 and subtotal:
        k, cut_total = sl["amount"] / subtotal, subtotal - sl["amount"]
        for ln in payable:
            cut = ln["allowed"] * (1 - k)
            ln["reductions"].append(_red(model, "SUB_LIMIT", cut, f"sub_limit.{claim.get('procedure')}", f"Procedure capped at {sl['amount']:,.0f} per {sl['per']}."))
            ln["allowed"] = _r2(ln["allowed"] - cut)
        subtotal -= cut_total
    left = max(0.0, model["sum_insured"] - member.get("used", 0.0))
    if subtotal > left + 0.004 and subtotal:
        k = left / subtotal
        for ln in payable:
            cut = ln["allowed"] * (1 - k)
            ln["reductions"].append(_red(model, "SUM_INSURED", cut, "sum_insured"))
            ln["allowed"] = _r2(ln["allowed"] - cut)
    pct = model.get("copay_pct", 0)
    for ln in payable:
        co = _r2(ln["allowed"] * pct / 100.0)
        if co:
            ln["reductions"].append(_red(model, "COPAY_PCT", co, "copay_pct"))
        ln["plan"] = _r2(ln["allowed"] - co)
    for ln in lines:
        ln["member"] = _r2(ln["billed"] - ln["plan"])
        if ln["status"] != "denied":
            ln["status"] = "reduced" if ln["reductions"] else "paid"
    return {"lines": lines, "totals": _summ(lines), "room_ratio": round(ratio, 3)}
