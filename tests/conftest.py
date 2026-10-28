import copy

GOOD_CONTRACT = {
    "summary": "Fee first, 10% holdback, 5% of it to the foundation, Bob recoups $1,000, then 60/40.",
    "currency": "AUD",
    "payees": [
        {"name": "Alice", "description": "Artist, 60% after recoupment"},
        {"name": "Bob", "description": "Developer, recoups $1,000 then 40%"},
        {"name": "Animal Foundation", "description": "Charity, gets 5% of each holdback"},
    ],
    "rules": [
        "PayPal's fee is deducted from each sale first.",
        "10% of the after-fee amount is held back for 60 days.",
        "5% of each holdback goes to Animal Foundation.",
    ],
}


def good_contract():
    return copy.deepcopy(GOOD_CONTRACT)
