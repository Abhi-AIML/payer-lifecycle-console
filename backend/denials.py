"""Denial Mgmt & Appeals tile agents: Classification, Policy Recheck, Appeal Drafting.

  Classification Agent  reads the insurer's settlement letter into deductions, then classifies each one: reason, root cause,
                        and the pre-emptive edit that would have prevented it. Gemini reads letters in other formats;
                        code verifies every amount against the letter text.
  Policy Recheck Agent  NO AI. Recomputes the claim from the published rules and compares it with the letter, deduction by
                        deduction. It says plainly when a deduction follows the policy and no appeal is warranted.
  Appeal Drafting Agent Gemini drafts the letter from the recheck facts only, and code verifies every amount and clause.
                        If there are no grounds, it drafts a clarification request or nothing at all.

The agent never decides that an appeal should win. It shows what the published rules say.
"""
import copy
import json
import re

import ai
import claims
import engine
from claims import money, verify_letter
from interpret import Log

# What the insurer's system did differently from the published rules, for the synthetic sample letters.
VARIANTS = {"CLM-IN-2001": {"proportionate_apply_all": True}, "CLM-US-1002": {"coinsurance_bump": 10}}
MISLABEL = {"CLM-IN-2003": {"IN-WAIT": ("IN-PED", "Pre-existing condition exclusion", "4.2")}}
ROOT = {"TARIFF": "Provider billing: charge above the agreed tariff", "ROOM_CAP": "Patient choice: room above the entitlement", "PROPORTIONATE": "Patient choice: room above the entitlement (knock-on)",
        "SUB_LIMIT": "Policy limit reached", "COPAY_PCT": "Policy cost-sharing", "NON_PAYABLE": "Provider billing: non-payable items billed", "NOT_COVERED": "Coverage: excluded service",
        "WAITING_PERIOD": "Eligibility: waiting period not completed", "DUPLICATE": "Provider billing: repeated charge", "SUM_INSURED": "Policy limit reached",
        "DEDUCTIBLE": "Member cost-sharing", "COINSURANCE": "Member cost-sharing", "COPAY": "Member cost-sharing", "FEE_SCHEDULE": "Provider billing: charge above the contracted rate",
        "NO_AUTH": "Process: authorization missing", "OOP_CAP": "Member cost-sharing"}
ROW = re.compile(r"^\s*\d+\s*\|\s*([\w-]+)\s*\|\s*([^|]+?)\s*\|\s*(?:Rs\.\s*|\$)?([\d,]+(?:\.\d+)?)\s*\|\s*(?:(?:Policy|Plan) clause\s*(\S+)|none)\s*$", re.M)


# ---------------------------------------------------------------- the insurer's letter (synthetic sample)
def insurer_letter(pack: dict, case: dict, pub: dict) -> str:
    """What the insurer's system sent. Built by running the claim with the insurer's settings, which may differ from the published rules."""
    mk, claim, model = pack["market"], case["intake"]["claim"], copy.deepcopy(pub["model"])
    v = VARIANTS.get(claim["id"], {})
    if v.get("proportionate_apply_all"):
        model["proportionate_apply_all"] = True
    if "coinsurance_bump" in v:
        model["cost_share"]["coinsurance_member_pct"] += v["coinsurance_bump"]
    mem = pack["members"][claim["member"]]
    res = (engine.adjudicate_in(model, copy.deepcopy(claim), mem, pack["tariff"]) if mk == "in"
           else engine.adjudicate_us(model, copy.deepcopy(claim), {"deductible_met": mem["deductible_met"], "oop_met": mem["oop_met"]}, pack["tariff"]))
    by = claims._by_reason(pack, res, {})
    rows = []
    for i, b in enumerate(by, 1):
        code, label, clause = b["code"], b["label"], (b["ref"] or {}).get("clause")
        if (o := MISLABEL.get(claim["id"], {}).get(code)):
            code, label, clause = o
        rows.append(f"{i} | {code} | {label} | {claims._amt(mk, b['amount'])} | " + (f"{'Policy' if mk == 'in' else 'Plan'} clause {clause}" if clause else "none"))
    p = pack["parties"]
    head = [f"{p['insurer'].upper()}{' / ' + p['tpa'] if p.get('tpa') else ''}: CLAIM SETTLEMENT LETTER (SYNTHETIC)", f"Claim: {claim['id']}", f"Policyholder: {mem['name']}",
            f"Amount billed: {money(mk, res['totals']['billed'])}", f"Amount payable: {money(mk, res['totals']['plan'])}", "DEDUCTIONS (no | code | reason | amount | clause)"]
    return "\n".join(head + rows)


def parse_letter(text: str) -> dict:
    def amt(key):
        m = re.search(rf"^\s*{key}:\s*(?:Rs\.\s*|\$)?([\d,]+(?:\.\d+)?)", text, re.I | re.M)
        return float(m.group(1).replace(",", "")) if m else None
    return {"billed": amt("Amount billed"), "payable": amt("Amount payable"),
            "deductions": [{"code": c, "label": l, "amount": float(a.replace(",", "")), "clause": cl or None} for c, l, a, cl in ROW.findall(text)]}


GEMINI_LETTER = ('Read this insurance settlement or rejection letter. Return JSON only: {"billed": number|null, "payable": number, "deductions": '
                 '[{"code": str|null, "label": str, "amount": number, "clause": str|null}]}. Copy exactly what is printed. Do not infer or add deductions.')


def extract_letter(text: str) -> tuple[dict, str]:
    if ai.available():
        out = ai.generate_json(GEMINI_LETTER, text)
        if out and isinstance(out.get("deductions"), list) and out.get("payable") is not None:
            try:
                ds = [{"code": d.get("code") or "?", "label": str(d["label"]), "amount": float(d["amount"]), "clause": (str(d["clause"]) if d.get("clause") else None)} for d in out["deductions"]]
                return {"billed": out.get("billed"), "payable": float(out["payable"]), "deductions": ds}, "gemini"
            except (KeyError, TypeError, ValueError):
                pass
    return parse_letter(text), "fallback"


def _in_text(text: str, amount: float) -> bool:
    return claims._present(text, amount)


# ---------------------------------------------------------------- agent A
def run_classify(pack: dict, appeal: dict) -> dict:
    log, text = Log("Classification Agent"), appeal["letter"]
    letter, extractor = extract_letter(text)
    if not letter["deductions"] and letter["payable"] is None:
        raise ValueError("No deductions could be read from this letter. Check the format, or add a Gemini key to read other formats.")
    log.step("Read the settlement letter", f"{len(letter['deductions'])} deductions read by {'Gemini' if extractor == 'gemini' else 'the rule-based reader (no Gemini key)'}")
    by_code = {r["code"]: (k, r) for k, r in pack["reasons"].items()}
    items = []
    for d in letter["deductions"]:
        key, meta = by_code.get(d["code"], (None, None))
        found = _in_text(text, d["amount"])
        items.append({**d, "reason": key, "known": key is not None, "kind": meta["kind"] if meta else "unknown", "root_cause": ROOT.get(key, "Not recognised: needs a reviewer"),
                      "prevention": meta["edit"] if meta else "", "amount_found": found})
    ok = sum(1 for i in items if i["amount_found"])
    log.step("Verify each amount against the letter", f"{ok} of {len(items)} amounts found in the letter text", "ok" if ok == len(items) else "warn")
    unk = [i["code"] for i in items if not i["known"]]
    log.step("Classify each deduction", f"{len(items) - len(unk)} recognised" + (f", {len(unk)} not recognised ({', '.join(unk)})" if unk else ""), "ok" if not unk else "warn")
    prevent = {}
    for i in items:
        if i["prevention"]:
            prevent.setdefault(i["prevention"], []).append(i["label"])
    log.step("Suggest pre-emptive edits", f"{len(prevent)} edit(s) that would have stopped these deductions before submission")
    return {"log": log.steps, "extractor": extractor, "billed": letter["billed"], "payable": letter["payable"], "items": items,
            "edits": [{"edit": e, "for": f} for e, f in prevent.items()], "summary": {"deductions": len(items), "verified": ok, "unknown": len(unk)}}


# ---------------------------------------------------------------- agent B
def run_recheck(pack: dict, appeal: dict, case: dict, pub: dict) -> dict:
    log, mk = Log("Policy Recheck Agent"), pack["market"]
    audit = appeal.get("lens") == "audit"
    cls, adj = appeal["classify"], case["adjudication"]
    eng = {b["code"]: b for b in adj["by_reason"]}
    log.step("Load the published rules", f"Version {pub['version']}. The claim was recomputed by the engine when it was adjudicated (no AI).")
    verdicts, used = [], set()
    for d in cls["items"]:
        e = eng.get(d["code"])
        if e:
            used.add(d["code"])
            diff = round(d["amount"] - e["amount"], 2)
            verdicts.append({"code": d["code"], "label": d["label"], "letter": d["amount"], "rules": e["amount"], "diff": diff, "clause": (e["ref"] or {}).get("clause"), "page": (e["ref"] or {}).get("page"),
                             "verdict": "supported" if abs(diff) <= 1 else "overstated" if diff > 0 else "understated", "reason": e["reason"]})
        else:
            verdicts.append({"code": d["code"], "label": d["label"], "letter": d["amount"], "rules": None, "diff": None, "clause": d.get("clause"), "page": None, "verdict": "unmatched", "reason": d["reason"]})
    missing = [b for b in adj["by_reason"] if b["code"] not in used]
    for v in verdicts:  # a deduction the rules do not list, but whose amount equals deductions the rules do list under another reason
        if v["verdict"] == "unmatched":
            alt = [b for b in missing if abs(b["amount"] - v["letter"]) <= 1 or abs(sum(x["amount"] for x in missing) - v["letter"]) <= 1]
            if alt:
                names = [b for b in missing if abs(sum(x["amount"] for x in missing) - v["letter"]) <= 1] or alt
                v.update(verdict="other_reason", rules=round(sum(b["amount"] for b in names), 2), diff=round(v["letter"] - sum(b["amount"] for b in names), 2), clause=(names[0]["ref"] or {}).get("clause"),
                         page=(names[0]["ref"] or {}).get("page"), applies=[b["label"] for b in names])
                used |= {b["code"] for b in names}
    not_in_letter = [{"code": b["code"], "label": b["label"], "amount": b["amount"]} for b in adj["by_reason"] if b["code"] not in used]
    rules_pay, letter_pay = adj["totals"]["plan"], cls["payable"]
    pay_diff = round(rules_pay - (letter_pay or 0), 2)
    log.step("Compare deduction by deduction", f"{sum(v['verdict'] == 'supported' for v in verdicts)} follow the rules, {sum(v['verdict'] in ('overstated', 'unmatched') for v in verdicts)} do not, {sum(v['verdict'] == 'other_reason' for v in verdicts)} cite a different reason than the one that applies",
             "ok" if pay_diff <= 1 else "warn")
    grounds = []
    over = [v for v in verdicts if v["verdict"] in ("overstated", "unmatched")]
    scope = next((r for r in pub["rules"] if r["category"] == "proportionate_scope"), None)
    for v in over:
        if v["code"] == "IN-PROP" and v["verdict"] == "overstated" and scope:
            prot = ", ".join(scope.get("protected") or []) or "the items the wording protects"
            grounds.append({"id": f"G{len(grounds) + 1}", "strength": "strong", "title": "Proportionate deduction applied to items the policy does not list as room-linked",
                            "text": (f"The letter deducts {money(mk, v['letter'])}. Applying the published rules gives {money(mk, v['rules'])}. The policy wording names the room-linked charges "
                                     f"({', '.join(scope.get('items') or [])}) and says {prot} are not reduced."),
                            "clause": scope["clause"], "page": scope["page"], "amount": v["diff"]})
            continue
        grounds.append({"id": f"G{len(grounds) + 1}", "strength": "strong", "title": f"{v['label']} is higher than the published rules allow" if v["verdict"] == "overstated" else f"{v['label']} is not in the published rules",
                        "text": (f"The letter deducts {money(mk, v['letter'])}. Applying the published rules gives {money(mk, v['rules'])}." if v["rules"] is not None else f"The letter deducts {money(mk, v['letter'])} under a reason the published rules do not contain."),
                        "clause": v["clause"], "page": v["page"], "amount": v["letter"] - (v["rules"] or 0)})
    if pay_diff > 1 and not over:
        grounds.append({"id": "G1", "strength": "strong", "title": "The amount payable is lower than the published rules give", "text": f"The letter pays {money(mk, letter_pay)}; the rules give {money(mk, rules_pay)}.", "clause": None, "page": None, "amount": pay_diff})
    for v in verdicts:
        if v["verdict"] == "supported" and v["reason"] == "DUPLICATE":
            grounds.append({"id": f"G{len(grounds) + 1}", "strength": "possible", "title": "The repeated charge may be a genuine second service",
                            "text": "The deduction follows the rules as the bill stands. If both charges are real, the hospital can supply a certificate or case notes showing two separate services.", "clause": None, "page": None, "amount": v["letter"]})
    for v in verdicts:
        if v["verdict"] == "other_reason":
            grounds.append({"id": f"G{len(grounds) + 1}", "strength": "info", "title": "The reason cited does not match the rule that applies",
                            "text": f"The letter cites '{v['label']}'" + (f" (clause {cited})" if (cited := next((d["clause"] for d in cls["items"] if d["code"] == v["code"] and d["clause"]), None)) else "")
                            + f". The published rules support the same amount under: {', '.join(v.get('applies', []))}. Ask the insurer to state the correct clause.",
                            "clause": v["clause"], "page": v["page"], "amount": 0})
    strong, possible = [g for g in grounds if g["strength"] == "strong"], [g for g in grounds if g["strength"] == "possible"]
    info = any(g["strength"] == "info" for g in grounds)
    draft = ("correction" if strong or possible or info else "none") if audit else ("appeal" if strong or possible else "clarification" if info else "none")
    merit = "grounds" if strong else "contestable" if possible else "clarify" if info else "none"
    checklist = ["Copy of the settlement letter", "Itemised hospital bill", "Discharge summary", "Policy wording (the pages cited in the grounds)"]
    if any(g["amount"] and "repeated" in g["title"] for g in grounds):
        checklist.append("Hospital certificate or case notes showing the repeated service took place twice")
    if any("proportionate" in g["title"].lower() for g in grounds) and scope:
        checklist.append(f"The policy wording page that lists the room-linked charges (clause {scope['clause']}, page {scope['page']})")
    if audit:
        checklist = ["The decision letter as drafted by the claims system", "The adjudication calculation for this claim", "The policy wording pages cited in the findings"]
    elif appeal["case_outcome"] == "reject":
        checklist.append("Treating doctor's letter stating why the treatment was needed")
    if audit:
        msg = {"grounds": "The drafted decision differs from the published rules. It should be corrected before the letter is issued.", "contestable": "No clear error, but a point should be confirmed before issue.",
               "clarify": "The amounts follow the rules but the reason cited is not the one that applies. Correct the reason before issue.", "none": "Every deduction follows the published rules. The decision is cleared to issue."}[merit]
        log.step("Decide whether the decision can be issued", msg, "ok" if merit == "none" else "warn")
    else:
        log.step("Decide what, if anything, to contest", {"grounds": "Grounds found where the letter differs from the published rules.", "contestable": "No clear error. One point could be supported with evidence.",
                                                            "clarify": "The amounts follow the rules but a different reason applies. A clarification request is suggested.", "none": "Every deduction follows the published rules. No appeal is suggested."}[merit], "ok" if merit != "grounds" else "warn")
    return {"log": log.steps, "verdicts": verdicts, "not_in_letter": not_in_letter, "grounds": grounds, "draft_type": draft, "merit": merit, "checklist": checklist,
            "letter_payable": letter_pay, "rules_payable": rules_pay, "pay_diff": pay_diff, "rule_version": pub["version"],
            "summary": {"supported": sum(v["verdict"] == "supported" for v in verdicts), "total": len(verdicts), "disputed_amount": round(sum(g["amount"] for g in strong), 2)}}


# ---------------------------------------------------------------- agent C
APPEAL_SYSTEM = ("You draft letters for a health insurance claims desk. Be factual, polite and concise. "
                 "Use ONLY the amounts, clause numbers and facts in FACTS. Never invent or recalculate a number. For 'appeal' write to the insurer on behalf of the policyholder and list each ground with its clause. "
                 "For 'clarification' ask the insurer to state the clause relied on for each deduction. "
                 "For 'correction' write an internal note from the pre-issue audit team to the adjudication team, listing each finding with its clause and asking for the decision to be corrected before the letter is issued. "
                 "Do not give legal advice or promise an outcome. End with the line: DRAFT for reviewer approval.")


def facts_for(pack: dict, appeal: dict, case: dict) -> dict:
    rc, mk, p = appeal["recheck"], pack["market"], pack["parties"]
    claim = case["intake"]["claim"]
    return {"draft_type": rc["draft_type"], "lens": appeal.get("lens", "contest"), "claim_id": claim["id"], "patient": pack["members"][claim["member"]]["name"], "insurer": p["insurer"], "office": p["grievance_office"],
            "billed": case["adjudication"]["totals"]["billed"], "letter_pays": rc["letter_payable"], "rules_pay": rc["rules_payable"], "difference": max(0, rc["pay_diff"]),
            "grounds": [{"title": g["title"], "text": g["text"], "clause": g["clause"], "page": g["page"], "amount": g["amount"], "strength": g["strength"]} for g in rc["grounds"]],
            "documents": rc["checklist"], "market": mk}


def template_appeal(pack: dict, f: dict) -> str:
    mk = f["market"]
    if f["draft_type"] == "correction":
        lead = ([f"The decision letter drafted for {f['patient']} pays {money(mk, f['letter_pays'])}. Applying the published rules gives {money(mk, f['rules_pay'])}, a difference of {money(mk, f['difference'])}."]
                if f["difference"] else [f"The decision letter drafted for {f['patient']} follows the published amounts, but the reason it cites needs correcting."])
        body = lead + ["", "Findings:"]
        for i, g in enumerate(f["grounds"], 1):
            body.append(f"{i}. {g['title']}. {g['text']}" + (f" [{'Policy' if mk == 'in' else 'Plan'} clause {g['clause']}" + (f", page {g['page']}" if g["page"] else "") + "]" if g["clause"] else ""))
        body += ["", "Please correct the decision before the letter is issued, and keep the calculation on file."]
        return "\n".join([f"To: The adjudication team, {f['insurer']}", "From: Pre-issue audit", f"Subject: Correction needed before issue (claim {f['claim_id']})", "", *body, "",
                          "Documents to keep with this note:", *[f"- {d}" for d in f["documents"]], "", "DRAFT for reviewer approval."])
    if f["draft_type"] == "clarification":
        body = ["Please send us, in writing, the exact policy clause relied on for each deduction in the settlement letter, and the calculation of each amount.", ""]
        body += [f"- {g['title']}. {g['text']}" for g in f["grounds"]]
        subj = "Request for the clause and calculation behind the settlement"
    else:
        body = ([f"The settlement letter shows {money(mk, f['letter_pays'])} payable. Applying the policy as published gives {money(mk, f['rules_pay'])}, a difference of {money(mk, f['difference'])}."]
                if f["difference"] else ["We have rechecked the settlement against the policy as published. The amounts follow it as worded, and we ask you to review the following point(s)."])
        body += ["", "Grounds:"]
        for i, g in enumerate(f["grounds"], 1):
            body.append(f"{i}. {g['title']}. {g['text']}" + (f" [{'Policy' if mk == 'in' else 'Plan'} clause {g['clause']}" + (f", page {g['page']}" if g["page"] else "") + "]" if g["clause"] else ""))
        body += ["", "Please review the claim and send a written reply that names the clause relied on for any deduction you maintain."]
        subj = "Request for review of claim settlement"
    return "\n".join([f"To: The {f['office']}, {f['insurer']}", f"On behalf of: {f['patient']}", f"Subject: {subj} (claim {f['claim_id']})", "", "Dear Sir or Madam,", "", *body, "",
                      "Documents enclosed:", *[f"- {d}" for d in f["documents"]], "", "Yours faithfully,", "", "DRAFT for reviewer approval. This letter is not legal advice."])


def run_draft(pack: dict, appeal: dict, case: dict) -> dict:
    audit = appeal.get("lens") == "audit"
    log = Log("Correction Drafting Agent" if audit else "Appeal Drafting Agent")
    rc = appeal["recheck"]
    if rc["draft_type"] == "none":
        log.step("Check for findings" if audit else "Check for grounds", "Every deduction follows the published rules. The decision is cleared to issue, so no note is drafted." if audit else "Every deduction follows the published rules, so no appeal letter is drafted.")
        return {"log": log.steps, "letter": None, "source": None, "verification": None, "note": None, "facts": facts_for(pack, appeal, case), "escalation": pack["escalation"], "draft_type": "none"}
    f = facts_for(pack, appeal, case)
    log.step("Collect the recheck facts", f"{len(f['grounds'])} {'finding(s)' if audit else 'ground(s)'}, {len(f['documents'])} document(s) to {'keep' if audit else 'enclose'}. Gemini will see only these facts.")
    text, source, note = None, "template", None
    if ai.available():
        text = ai.generate_text(APPEAL_SYSTEM, "FACTS:\n" + json.dumps(f, indent=1))
        if text:
            ver = verify_letter(text, f)
            if ver["amounts_ok"] and ver["clauses_ok"]:
                source = "gemini"
                log.step("Gemini drafts the letter", "Draft written from the facts only")
                log.step("Verify the draft", "Every amount and clause in the draft exists in the facts")
            else:
                note = f"The Gemini draft was rejected: unknown amounts {ver['unknown_amounts']}, unknown clauses {ver['unknown_clauses']}. The template was used instead."
                log.step("Verify the draft", note, "warn")
                text = None
        else:
            log.step("Gemini call failed", "Using the template instead.", "warn")
    if not text:
        text = template_appeal(pack, f)
        log.step("Write the letter", "Template used" + ("" if ai.available() else " (no Gemini key)"))
    ver = verify_letter(text, f)
    if not audit:
        log.step("Escalation path", f"{len(pack['escalation'])} steps listed. Their timelines are not verified and are labelled so.")
    return {"log": log.steps, "letter": text, "source": source, "verification": ver, "note": note, "facts": f, "escalation": pack["escalation"], "draft_type": rc["draft_type"]}
