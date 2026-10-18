"""Agent 3: Validation Agent.

Generates boundary-case test scenarios from the approved rules, works out the EXPECTED result from the
approved model (a separate spec calculation), runs the LOADED configuration through the engine to get the
ACTUAL result, and reports every difference as a defect. Then assembles an approval-ready package.
Deterministic: no AI decides pass or fail.
"""
import copy

import configure
import engine
from interpret import Log

TOL = 0.05  # rounding tolerance: the spec rounds once, the engine rounds per line, so up to 5 paise may differ


# ------------------------------------------------------------ expected results: the spec, from the approved model
def expected_us(model, sc):
    cs, svc, allowed = model["cost_share"], sc["service"], sc["allowed"]
    if svc in model["exclusions"]:
        return {"decision": "denied", "reason": "NOT_COVERED", "plan": 0.0, "member": 0.0}
    if svc in model["prior_auth"] and not sc.get("auth"):
        return {"decision": "denied", "reason": "NO_AUTH", "plan": 0.0, "member": 0.0}
    if svc in model["copays"]:
        member = min(model["copays"][svc], allowed)
    else:
        ded_left = 0.0 if svc in model["deductible_exempt"] else max(0.0, cs["deductible"] - sc.get("ded_met", 0))
        ded = min(ded_left, allowed)
        member = ded + (allowed - ded) * cs["coinsurance_member_pct"] / 100.0
        member = min(member, max(0.0, cs["oop_max"] - sc.get("oop_met", 0)))
    return {"decision": "paid", "reason": None, "plan": round(allowed - member, 2), "member": round(member, 2)}


def expected_in(model, sc):
    pct, k = model.get("copay_pct", 0), sc["kind"]
    if k == "waiting":
        days = model["waiting_period_days"][sc["service"]]
        if sc["age"] < days and not (sc.get("accident") and sc["service"] == "initial"):
            return {"decision": "denied", "reason": "WAITING_PERIOD", "plan": 0.0, "member": sc["billed"]}
        return {"decision": "paid", "reason": None, "plan": round(sc["billed"] * (1 - pct / 100), 2), "member": round(sc["billed"] - sc["billed"] * (1 - pct / 100), 2)}
    if k == "nonpayable":
        return {"decision": "denied", "reason": "NON_PAYABLE", "plan": 0.0, "member": sc["billed"]}
    if k == "exclusion":
        return {"decision": "denied", "reason": "NOT_COVERED", "plan": 0.0, "member": sc["billed"]}
    if k == "room":
        cap, rate, days = model["room_rent_cap_per_day"], sc["rate"], sc["days"]
        ratio = min(1.0, cap / rate)
        linked = sc["linked"] * ratio
        icu = sc.get("icu", 0) * (ratio if model["proportionate_scope"] == "linked" else 1.0)  # ICU follows the reviewer's decision
        adm = min(rate, cap) * days + linked + icu + sc["other"]  # implants and other protected items are never reduced
    elif k == "sublimit":
        adm = min(sc["billed"], model["sub_limits"][sc["service"]]["amount"])
    else:  # sum insured
        adm = min(sc["billed"], max(0.0, model["sum_insured"] - sc["used"]))
    plan = round(adm * (1 - pct / 100), 2)
    return {"decision": "paid", "reason": None, "plan": plan, "member": round(sc["total_billed"] - plan if "total_billed" in sc else sc["billed"] - plan, 2)}


# ------------------------------------------------------------ scenarios
def scenarios_us(model, refs):
    cs, out = model["cost_share"], []
    rid = lambda *keys: [refs[k]["id"] for k in keys if k in refs]  # noqa: E731

    def add(title, rules, **sc):
        out.append({"title": title, "rule_ids": rules, "sc": sc})
    d, oop = cs["deductible"], cs["oop_max"]
    add("Deductible not yet met", rid("deductible", "coinsurance_member_pct"), service="generic", allowed=5000, ded_met=0, oop_met=0)
    add("Deductible partly met", rid("deductible"), service="generic", allowed=5000, ded_met=d / 2, oop_met=d / 2)
    add("Deductible already met", rid("deductible", "coinsurance_member_pct"), service="generic", allowed=5000, ded_met=d, oop_met=d)
    add("Member share hits the out-of-pocket cap", rid("oop_max"), service="generic", allowed=5000, ded_met=d, oop_met=oop - 100)
    add("Out-of-pocket maximum already reached", rid("oop_max"), service="generic", allowed=5000, ded_met=d, oop_met=oop)
    for s, v in model["copays"].items():
        add(f"Copay applies: {s.replace('_', ' ')}", rid(f"copay.{s}"), service=s, allowed=v * 4, ded_met=0, oop_met=0)
        add(f"Allowed amount below the copay: {s.replace('_', ' ')}", rid(f"copay.{s}"), service=s, allowed=v / 2, ded_met=0, oop_met=0)
    for s in model["prior_auth"]:
        add(f"Authorization present: {s.replace('_', ' ')}", rid(f"prior_auth.{s}"), service=s, allowed=3000, ded_met=d, oop_met=0, auth=True)
        add(f"Authorization missing: {s.replace('_', ' ')}", rid(f"prior_auth.{s}"), service=s, allowed=3000, ded_met=d, oop_met=0, auth=False)
    for s in model["exclusions"]:
        add(f"Excluded service: {s.replace('_', ' ')}", rid(f"exclusion.{s}"), service=s, allowed=1000, ded_met=0, oop_met=0)
    for s in model["deductible_exempt"]:
        add(f"Deductible does not apply: {s.replace('_', ' ')}", rid(f"deductible_exempt.{s}", "deductible"), service=s, allowed=300, ded_met=0, oop_met=0)
    return out


def scenarios_in(model, refs):
    out, rid = [], lambda *keys: [refs[k]["id"] for k in keys if k in refs]  # noqa: E731

    def add(title, rules, **sc):
        out.append({"title": title, "rule_ids": rules, "sc": sc})
    cap = model["room_rent_cap_per_day"]
    for label, rate in (("Room exactly at the cap", cap), ("Room just above the cap", cap + 500), ("Room at twice the cap", cap * 2)):
        add(label, rid("room_rent_cap", "proportionate_scope"), kind="room", rate=rate, days=3, linked=30000, icu=20000, other=10000, protected_implant="implant" in model.get("protected_categories", []), billed=rate * 3 + 60000, total_billed=rate * 3 + 60000)
    for s, sl in model["sub_limits"].items():
        a = sl["amount"]
        for label, amt in (("just below", a - 5000), ("exactly at", a), ("above", a + 20000)):
            add(f"Sub-limit {label} the limit: {s.replace('_', ' ')}", rid(f"sub_limit.{s}"), kind="sublimit", service=s, billed=amt)
    for s, days in model["waiting_period_days"].items():
        add(f"Waiting period not complete: {s.replace('_', ' ')}", rid(f"waiting_period.{s}"), kind="waiting", service=s, age=days - 1, billed=50000, accident=False)
        add(f"Waiting period just complete: {s.replace('_', ' ')}", rid(f"waiting_period.{s}"), kind="waiting", service=s, age=days, billed=50000, accident=False)
    if "initial" in model["waiting_period_days"]:
        add("Accident during the initial waiting period", rid("waiting_period.initial"), kind="waiting", service="initial", age=3, billed=50000, accident=True)
    if model.get("non_payable"):
        add("Non-medical item", rid("non_payable"), kind="nonpayable", billed=1500)
    for s in model["exclusions"]:
        add(f"Excluded procedure: {s.replace('_', ' ')}", rid(f"exclusion.{s}"), kind="exclusion", service=s, billed=20000)
    add("Claim above the remaining sum insured", rid("sum_insured"), kind="sum_insured", billed=model["sum_insured"], used=model["sum_insured"] - 20000)
    return out


def _engine_us(model, sc, tariff):
    claim = {"lines": [{"id": "L1", "code": "TEST", "desc": sc["service"], "service_key": sc["service"], "qty": 1, "billed": sc["allowed"], "date": "2026-01-01", "allowed_override": sc["allowed"]}],
             "authorizations": [{"service_keys": [sc["service"]]}] if sc.get("auth") else []}
    res = engine.adjudicate_us(model, claim, {"deductible_met": sc.get("ded_met", 0), "oop_met": sc.get("oop_met", 0)}, tariff)
    ln = res["lines"][0]
    reason = next((r["reason"] for r in ln["reductions"] if r["reason"] in ("NOT_COVERED", "NO_AUTH", "DUPLICATE")), None)
    return {"decision": "denied" if ln["status"] == "denied" else "paid", "reason": reason, "plan": ln["plan"], "member": ln["member"]}


def _engine_in(model, sc, tariff):
    k, lines = sc["kind"], []
    claim = {"service_date": "2026-06-01", "accident": sc.get("accident", False), "procedure": sc.get("service")}
    member = {"policy_start": "2020-01-01", "sum_insured": model.get("sum_insured", 0), "used": sc.get("used", 0)}  # long-standing policy unless a waiting-period scenario says otherwise
    if k == "waiting":
        from datetime import date, timedelta
        member["policy_start"] = (date.fromisoformat(claim["service_date"]) - timedelta(days=sc["age"])).isoformat()
        lines = [{"id": "L1", "code": "X", "desc": "procedure", "service_key": "procedure", "category": "procedure", "qty": 1, "billed": sc["billed"], "date": claim["service_date"]}]
    elif k == "nonpayable":
        lines = [{"id": "L1", "code": "X", "desc": "item", "service_key": "non_medical", "category": "non_medical", "qty": 1, "billed": sc["billed"], "date": claim["service_date"]}]
    elif k == "exclusion":
        lines = [{"id": "L1", "code": "X", "desc": "proc", "service_key": sc["service"], "category": "procedure", "qty": 1, "billed": sc["billed"], "date": claim["service_date"]}]
    elif k == "room":
        lines = [{"id": "L1", "code": "R", "desc": "room", "service_key": "room", "category": "room", "qty": sc["days"], "billed": sc["rate"] * sc["days"], "date": claim["service_date"]},
                 {"id": "L2", "code": "S", "desc": "surgeon", "service_key": "procedure", "category": "procedure", "room_linked": True, "qty": 1, "billed": sc["linked"], "date": claim["service_date"]},
                 {"id": "L3", "code": "I", "desc": "implant", "service_key": "implant", "category": "implant", "room_linked": bool(sc.get("protected_implant")), "qty": 1, "billed": sc["other"], "date": claim["service_date"]},
                 {"id": "L4", "code": "C", "desc": "icu", "service_key": "icu", "category": "icu", "room_linked": True, "qty": 1, "billed": sc.get("icu", 0), "date": claim["service_date"]}]
    elif k == "sublimit":
        claim["procedure"] = sc["service"]
        lines = [{"id": "L1", "code": "X", "desc": "procedure", "service_key": "procedure", "category": "procedure", "qty": 1, "billed": sc["billed"], "date": claim["service_date"]}]
    else:
        lines = [{"id": "L1", "code": "X", "desc": "procedure", "service_key": "procedure", "category": "procedure", "qty": 1, "billed": sc["billed"], "date": claim["service_date"]}]
    claim["lines"] = lines
    res = engine.adjudicate_in(model, claim, member, tariff)
    denied = res["totals"]["denied_lines"] == len(lines)
    reason = next((r["reason"] for ln in res["lines"] for r in ln["reductions"] if r["reason"] in ("WAITING_PERIOD", "NON_PAYABLE", "NOT_COVERED")), None) if denied else None
    return {"decision": "denied" if denied else "paid", "reason": reason, "plan": res["totals"]["plan"], "member": res["totals"]["member"]}


def inject_defect(fields: list[dict], market: str) -> tuple[list[dict], str]:
    """Simulate a keying error in the loaded configuration, so the demo can show the agent catching it."""
    fields = copy.deepcopy(fields)
    target, new = ("cost_share.coinsurance_member_pct", 80) if market == "us" else ("room_rent_cap_per_day", 50000)
    for f in fields:
        if f["field"] == target:
            old, f["value"] = f["value"], new
            return fields, f"Simulated keying error: {target} was loaded as {new} instead of {old}."
    return fields, "No field available to alter."


def run(pack: dict, model: dict, fields_loaded: list[dict], mappings: list[dict], rules: list[dict], confirmed: set, *, injected: str | None = None) -> dict:
    log = Log("Validation Agent")
    market, refs = pack["market"], model["refs"]
    loaded = configure.model_from_fields(fields_loaded, model)
    log.step("Load the configuration under test", f"{len(fields_loaded)} fields loaded as the target system would see them." + (f" {injected}" if injected else ""), "warn" if injected else "ok")
    specs = (scenarios_us if market == "us" else scenarios_in)(model, refs)
    log.step("Generate test scenarios from the rules", f"{len(specs)} scenarios: boundaries, caps, authorizations, exclusions and waiting periods.")
    tariff, results = {}, []
    for i, s in enumerate(specs, 1):
        sc = s["sc"]
        exp = (expected_us if market == "us" else expected_in)(model, sc)
        act = (_engine_us if market == "us" else _engine_in)(loaded, sc, tariff)
        same = exp["decision"] == act["decision"] and exp["reason"] == act["reason"] and abs(exp["plan"] - act["plan"]) <= TOL
        results.append({"id": f"T{i}", "title": s["title"], "rule_ids": s["rule_ids"], "input": sc, "expected": exp, "actual": act, "pass": same})
    passed = sum(r["pass"] for r in results)
    log.step("Run every scenario and compare", f"{passed} of {len(results)} matched the expected result.", "ok" if passed == len(results) else "warn")
    approved_ids = {r["id"] for r in rules if r["status"] == "approved"}
    covered = {rid for r in results for rid in r["rule_ids"]}
    uncovered = sorted(approved_ids - covered)
    cov = round(100 * len(approved_ids & covered) / len(approved_ids)) if approved_ids else 0
    log.step("Measure rule coverage", f"{len(approved_ids & covered)} of {len(approved_ids)} approved rules are exercised by at least one scenario ({cov}%)." + (f" Not covered: {', '.join(uncovered)}." if uncovered else ""),
             "ok" if not uncovered else "info")
    exceptions = []
    for r in results:
        if not r["pass"]:
            exceptions.append({"type": "defect", "severity": "block", "text": f"{r['id']} {r['title']}: expected plan pays {r['expected']['plan']:,.2f}, system gave {r['actual']['plan']:,.2f}."})
    for m in mappings:
        if m["status"] != "mapped" and m["service"] not in confirmed:
            exceptions.append({"type": "mapping", "severity": "review", "text": f"Mapping for {m['label']} is {m['status'].replace('_', ' ')} and needs a coding reviewer."})
    for r in rules:
        if r.get("needs_decision") and not r.get("decision") and r["status"] == "approved":
            exceptions.append({"type": "ambiguity", "severity": "block", "text": f"{r['label']} was approved without a scope decision."})
    blockers = [e for e in exceptions if e["severity"] == "block"]
    log.step("Assemble the approval package", f"{len(exceptions)} open exception(s), {len(blockers)} blocking." if exceptions else "No open exceptions.", "ok" if not exceptions else "warn")
    package = {"rules_approved": len(approved_ids), "evidence_verified": sum(1 for r in rules if r["status"] == "approved" and all(f["severity"] != "block" for f in r["flags"])),
               "mappings": {k: sum(1 for m in mappings if m["status"] == k) for k in ("mapped", "one_to_many", "missing", "expired", "review")},
               "tests": {"total": len(results), "passed": passed, "failed": len(results) - passed}, "coverage_pct": cov, "uncovered_rules": uncovered,
               "open_exceptions": exceptions, "ready": not blockers}
    return {"results": results, "package": package, "log": log.steps, "injected": injected}
