"""The fixed schema every refined contract must fit.

Parties are referred to by the names used in the contract. Their PayPal emails
are attached later from the payee form, never taken from the contract text.
"""

POOL = "pool"  # "taken off the top / shared by everyone", rather than one party
EACH_RECIPIENT = "each_recipient"

_NULLABLE_STRING = {"type": ["string", "null"]}

REFINED_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Plain-English restatement of the whole deal, 2-5 sentences."},
        "currency": {"type": "string", "enum": ["AUD"]},
        "parties": {"type": "array", "items": {"type": "string"}, "description": "Names of everyone who gets paid."},
        "paypal_fee_bearer": {
            "type": "string",
            "description": f"'{POOL}' if the fee comes off every sale first, otherwise one party's name.",
        },
        "unreturned_refund_fee_bearer": {
            "type": "string",
            "description": f"Who bears the PayPal fee that PayPal does not return on a refund: '{POOL}' or a party.",
        },
        "payout_fee_bearer": {
            "type": "string",
            "description": f"Who bears PayPal's payout fee: '{POOL}', '{EACH_RECIPIENT}' or a party.",
        },
        "holdback": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "percent": {"type": "number", "description": "Percent of each sale held back, 0-100."},
                        "days": {"type": "integer"},
                        "calculated_on": {"type": "string", "enum": ["after_fee", "gross"]},
                        "release_order": {"type": "string", "enum": ["repay_recoupment_first", "straight_to_split"]},
                    },
                    "required": ["percent", "days", "calculated_on", "release_order"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "refunds": {
            "type": "object",
            "properties": {
                "funded_from": {"type": "string", "enum": ["holdback_first", "future_payouts_only"]},
                "shortfall_covered_by": {"type": "string", "enum": ["split_percentages", "single_party"]},
                "shortfall_party": _NULLABLE_STRING,
            },
            "required": ["funded_from", "shortfall_covered_by", "shortfall_party"],
            "additionalProperties": False,
        },
        "recoupment": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "party": {"type": "string"},
                        "amount": {"type": "number", "description": "Upfront cost to repay, in the contract currency."},
                        "party_share_percent": {
                            "type": "number",
                            "description": "Percent of the remaining pool that goes to the party until repaid (e.g. 100).",
                        },
                    },
                    "required": ["party", "amount", "party_share_percent"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "splits": {
            "type": "array",
            "description": "Ordered tiers. Each applies until cumulative net reaches its threshold; the last has null.",
            "items": {
                "type": "object",
                "properties": {
                    "up_to_cumulative_net": {"type": ["number", "null"]},
                    "shares": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"party": {"type": "string"}, "percent": {"type": "number"}},
                            "required": ["party", "percent"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["up_to_cumulative_net", "shares"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary", "currency", "parties", "paypal_fee_bearer", "unreturned_refund_fee_bearer",
        "payout_fee_bearer", "holdback", "refunds", "recoupment", "splits",
    ],
    "additionalProperties": False,
}


def validate_refined_contract(rc):
    """Checks the things the JSON schema cannot. Returns a list of problems (empty if fine)."""
    errors = []
    parties = rc.get("parties", [])
    if not parties or len(set(parties)) != len(parties):
        errors.append("parties must be a non-empty list of unique names")
    party_set = set(parties)

    def check_party(value, field, extra=()):
        if value not in party_set and value not in extra:
            errors.append(f"{field} {value!r} is not a listed party" + (f" or one of {list(extra)}" if extra else ""))

    check_party(rc.get("paypal_fee_bearer"), "paypal_fee_bearer", (POOL,))
    check_party(rc.get("unreturned_refund_fee_bearer"), "unreturned_refund_fee_bearer", (POOL,))
    check_party(rc.get("payout_fee_bearer"), "payout_fee_bearer", (POOL, EACH_RECIPIENT))

    holdback = rc.get("holdback")
    if holdback and not (0 <= holdback["percent"] < 100 and holdback["days"] >= 0):
        errors.append("holdback percent must be in [0, 100) and days >= 0")

    refunds = rc.get("refunds", {})
    if refunds.get("shortfall_covered_by") == "single_party":
        check_party(refunds.get("shortfall_party"), "refunds.shortfall_party")

    recoup = rc.get("recoupment")
    if recoup:
        check_party(recoup["party"], "recoupment.party")
        if recoup["amount"] <= 0:
            errors.append("recoupment amount must be positive")
        if not 0 < recoup["party_share_percent"] <= 100:
            errors.append("recoupment party_share_percent must be in (0, 100]")

    tiers = rc.get("splits", [])
    if not tiers:
        errors.append("splits needs at least one tier")
    previous = 0
    for i, tier in enumerate(tiers):
        threshold = tier["up_to_cumulative_net"]
        last = i == len(tiers) - 1
        if last and threshold is not None:
            errors.append("the last split tier must have a null threshold")
        if not last:
            if threshold is None or threshold <= previous:
                errors.append("split thresholds must be increasing, with only the last null")
            else:
                previous = threshold
        if abs(sum(s["percent"] for s in tier["shares"]) - 100) > 1e-6:
            errors.append(f"split tier {i + 1} percentages must add up to 100")
        for share in tier["shares"]:
            check_party(share["party"], f"split tier {i + 1} party")
    return errors
