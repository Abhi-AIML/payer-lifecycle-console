"""Shared vocabulary for benefit rules: categories, services, labels. No logic that depends on a market."""
import re

CATEGORIES = {
    "deductible": "Annual deductible", "deductible_exempt": "Exempt from deductible", "coinsurance_member_pct": "Coinsurance (member pays)",
    "oop_max": "Out-of-pocket maximum", "copay": "Copay", "prior_auth": "Prior authorization required", "exclusion": "Exclusion",
    "sum_insured": "Sum insured", "room_rent_cap": "Room-rent cap per day", "proportionate_scope": "Proportionate deduction scope",
    "sub_limit": "Sub-limit", "waiting_period": "Waiting period", "copay_pct": "Co-payment", "non_payable": "Non-payable items",
}
SERVICES = {
    "office_visit": "office visit", "physical_therapy": "physical therapy", "total_knee_replacement": "knee replacement", "advanced_imaging": "advanced imaging (MRI)",
    "cosmetic": "cosmetic procedures", "experimental": "experimental treatments", "cataract": "cataract surgery", "joint_replacement": "joint replacement",
    "cashless": "cashless treatment", "preventive": "preventive visits", "initial": "all illnesses (initial)", "non_medical": "non-medical items",
}
_ALIASES = [("primary care or specialist office visit", "office_visit"), ("office visit", "office_visit"), ("physical therapy", "physical_therapy"),
            ("total knee replacement", "total_knee_replacement"), ("knee replacement", "total_knee_replacement"), ("advanced imaging", "advanced_imaging"),
            ("cosmetic", "cosmetic"), ("experimental", "experimental"), ("cataract", "cataract"), ("joint replacement", "joint_replacement"),
            ("cashless", "cashless"), ("preventive", "preventive")]
NUMERIC = {"deductible", "coinsurance_member_pct", "oop_max", "copay", "sum_insured", "room_rent_cap", "sub_limit", "waiting_period", "copay_pct"}
CATEGORICAL = {"deductible_exempt", "prior_auth", "exclusion", "non_payable", "proportionate_scope"}


# what a clause may name as NOT reduced, mapped to the bill-item categories the engine uses
PROTECTED_MAP = {"implants": "implant", "implant": "implant", "medicines": "pharmacy", "medicine": "pharmacy", "pharmacy": "pharmacy",
                 "consumables": "consumable", "consumable": "consumable", "diagnostic tests": "diagnostic", "diagnostics": "diagnostic", "investigations": "diagnostic"}


def slug_service(text: str) -> str | None:
    t = (text or "").lower()
    for alias, key in _ALIASES:
        if alias in t:
            return key
    return None


def rule_label(category: str, service: str | None) -> str:
    base = CATEGORIES.get(category, category)
    if category in ("copay", "sub_limit", "waiting_period", "prior_auth", "exclusion", "deductible_exempt") and service:
        return f"{base}: {SERVICES.get(service, service.replace('_', ' '))}"
    return base


def money_number(text: str) -> float:
    return float(re.sub(r"[^\d.]", "", text.replace(",", "")) or 0)
