import copy

GOOD_CONTRACT = {
    "summary": "Fee first, 10% holdback, Bob recoups $1,000, then 60/40.",
    "currency": "AUD",
    "parties": ["Alice", "Bob"],
    "paypal_fee_bearer": "pool",
    "unreturned_refund_fee_bearer": "pool",
    "payout_fee_bearer": "each_recipient",
    "holdback": {"percent": 10, "days": 60, "calculated_on": "after_fee", "release_order": "repay_recoupment_first"},
    "refunds": {"funded_from": "holdback_first", "shortfall_covered_by": "split_percentages", "shortfall_party": None},
    "recoupment": {"party": "Bob", "amount": 1000, "party_share_percent": 100},
    "splits": [{"up_to_cumulative_net": None,
                "shares": [{"party": "Alice", "percent": 60}, {"party": "Bob", "percent": 40}]}],
}


def good_contract():
    return copy.deepcopy(GOOD_CONTRACT)
