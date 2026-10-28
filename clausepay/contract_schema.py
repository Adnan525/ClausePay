"""The shape of a refined contract.

Deliberately loose: Claude states the deal as precise, ordered rules, and lists everyone who can receive
money. Emails are never part of it; they come from the payee form.
"""

REFINED_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Plain-English restatement of the whole deal, 2-5 sentences."},
        "currency": {"type": "string", "enum": ["AUD"]},
        "payees": {
            "type": "array",
            "description": "Everyone or everything that can receive money under the rules, including charities.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "description": "Who they are / why they are paid, one short line."},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        },
        "rules": {
            "type": "array",
            "description": "Self-contained, unambiguous rules in the order a sale is processed, covering sales, "
                           "refunds, disputes and holdback release. Every answered clarification is built in.",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "currency", "payees", "rules"],
    "additionalProperties": False,
}


def validate_refined_contract(rc):
    """Returns a list of problems (empty if fine)."""
    errors = []
    names = [p["name"].strip() for p in rc.get("payees", [])]
    if not names:
        errors.append("payees must list at least one payee")
    if any(not n for n in names):
        errors.append("every payee needs a name")
    if len(set(names)) != len(names):
        errors.append("payee names must be unique")
    if not rc.get("rules"):
        errors.append("rules must not be empty")
    return errors
