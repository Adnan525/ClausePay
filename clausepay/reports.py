import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import anthropic

from clausepay.live import LiveSale, LiveService

MODEL = "claude-sonnet-5-5"
MAX_ATTEMPTS = 3
PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

REPORT_PROMPT = """\
You write short payment reports for the partners of a revenue-sharing contract. One report goes to every partner, so \
address them together ("everyone", "all of you"), never as "you" for one person. Write in warm, plain English that a \
non-expert can follow, in 3 to 5 short paragraphs.

You receive: the contract's summary in plain English, whether this is a sale or a refund, a list of placeholders with \
what each one means, flags describing what happened, and a list of state changes (what the contract remembers, \
before and after, without numbers).

Explain: what happened with this transaction, how the contract divided it (using the summary), and what is different \
compared with before, such as an investment now fully repaid, a payout threshold reached or still not reached, or the \
reserve growing or being used. For a refund, say clearly what each partner has to do (use the transfer placeholders) or \
that nothing is needed.

STRICT RULE ABOUT NUMBERS: you are never given any figures. Write every amount by using its placeholder in curly \
braces, for example {payout_alice} or {sale_amount}. Do not write any digit yourself, except digits that already appear \
in the contract summary (such as a 60/40 split). Use only the placeholders you were given. Do not invent names or facts.

Reply with JSON: a short "subject" line and a list of "paragraphs". Placeholders are allowed in the subject too."""

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "paragraphs": {"type": "array", "items": {"type": "string"}}},
    "required": ["subject", "paragraphs"],
    "additionalProperties": False,
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "party"


def money(value: Any) -> str:
    return f"A${Decimal(str(value)):,.2f}"


def _plain(value: Any) -> str:
    return money(value) if isinstance(value, Decimal) else str(value)


def _numbers(text: str) -> set[str]:
    return {token.replace(",", "") for token in NUMBER.findall(text)}


def hide_numbers(text: str) -> str:
    """A contract note with its figures blanked out, so Claude never sees a number it could copy."""
    return NUMBER.sub("[amount]", text)


class Facts:
    """Everything a report needs: figures built by ClausePay, plus numberless hints for Claude."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.values: dict[str, str] = {}  # placeholder -> formatted value
        self.meanings: dict[str, str] = {}  # placeholder -> what it is
        self.figures: list[tuple[str, str]] = []  # the static table shown in the report
        self.flags: list[str] = []
        self.changes: list[dict[str, Any]] = []
        self.notes: list[str] = []


    def add(self, name: str, value: Any, meaning: str, label: str | None = None) -> None:
        self.values[name], self.meanings[name] = _plain(value), meaning
        if label:
            self.figures.append((label, self.values[name]))


    def for_claude(self, summary: list[str], parties: list[str]) -> dict[str, Any]:
        return {"kind": self.kind, "contract_summary": summary, "parties": parties, "placeholders": self.meanings,
                "what_happened": self.flags, "state_changes": self.changes,
                "contract_notes": [hide_numbers(note) for note in self.notes]}


def _state_changes(facts: Facts, before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        base = f"state_{slug(key)}"
        facts.add(f"{base}_before", "none yet" if old is None else old, f"what the contract remembered as '{key}' before")
        facts.add(f"{base}_after", "none" if new is None else new, f"what the contract remembers as '{key}' now")
        change: dict[str, Any] = {"remembered_item": key, "before_placeholder": f"{base}_before", "after_placeholder": f"{base}_after"}
        if isinstance(new, (Decimal, int)) and isinstance(old, (Decimal, int)):
            change["direction"] = "increased" if new > old else "decreased"
            change["reached_zero"] = bool(new == 0 and old != 0)
        else:
            change["direction"] = "changed"
        facts.changes.append(change)
        facts.figures.append((f"Contract memory: {key}", f"{_plain('none yet' if old is None else old)} → {_plain('none' if new is None else new)}"))


def sale_facts(entry: dict[str, Any], parties: list[str]) -> Facts:
    facts = Facts("sale")
    amount, fee = entry["amount"], entry["fee"]
    received = amount - fee
    facts.add("sale_amount", amount, "what the buyer paid", "Buyer paid")
    facts.add("paypal_fee", fee, "PayPal's fee on the sale", "PayPal fee")
    facts.add("received", received, "what the business account received after PayPal's fee", "Business received")
    paid = {name: value for name, value in entry["amounts"].items() if value > 0}
    for name in parties:
        value = entry["amounts"].get(name, Decimal("0.00"))
        facts.add(f"payout_{slug(name)}", value, f"what {name} was paid from this sale", f"Paid to {name}")
    if not paid:
        facts.flags.append("Nobody was paid out from this sale.")
    else:
        facts.flags.append("Paid out: " + ", ".join(sorted(paid)) + ".")
    unpaid = [name for name in parties if name not in paid]
    if paid and unpaid:
        facts.flags.append("Not paid from this sale: " + ", ".join(unpaid) + ".")
    change = entry["reserve_change"]
    if change > 0:
        facts.add("held_back", change, "the amount held back in the reserve from this sale", "Held back")
        facts.flags.append("Some money was held back in the reserve.")
    elif change < 0:
        facts.add("released", -change, "the amount released from the reserve in this payout", "Released from the reserve")
        facts.flags.append("Money that was held back earlier was released and paid out.")
    facts.add("reserve_before", entry["reserve_after"] - change, "the reserve before this sale")
    facts.add("reserve_after", entry["reserve_after"], "the reserve after this sale", "Held back in total now")
    if entry.get("payout_fees", 0) > 0:
        facts.add("payout_fees", entry["payout_fees"], "the fee PayPal charges the business for sending the payouts", "PayPal payout fees")
        facts.add("payout_fees_covered", entry["payout_fees_covered"], "the part of the payout fees taken from this sale's money",
                  "…taken from this sale's money")
        facts.add("payout_fees_absorbed", entry["payout_fees"] - entry["payout_fees_covered"],
                  "the part of the payout fees the business absorbs", "…absorbed by the business")
    _state_changes(facts, entry.get("state_before", {}), entry["state_after"])
    facts.notes = entry["notes"]
    return facts


def refund_facts(entry: dict[str, Any], parties: list[str], charges: dict[str, Any] | None = None) -> Facts:
    facts = Facts("refund")
    amount, returned = entry["amount"], entry["fee"]
    facts.add("refund_amount", amount, "what was refunded to the buyer", "Refunded to the buyer")
    facts.add("fee_returned", returned, "the part of PayPal's fee that PayPal gave back", "PayPal fee returned")
    facts.add("refund_cost", amount - returned, "what the refund cost the business", "Cost of the refund")
    taken = -entry["reserve_change"]
    facts.add("from_reserve", taken, "the part of the cost paid from the reserve", "Paid from the reserve")
    owing = {name: value for name, value in entry["amounts"].items() if value > 0}
    for name in parties:
        facts.add(f"transfer_{slug(name)}", entry["amounts"].get(name, Decimal("0.00")),
                  f"what {name} must transfer into the business account for this refund", f"{name} transfers in")
    facts.flags.append("The partners must transfer money in: " + ", ".join(sorted(owing)) + "." if owing
                       else "No partner has to transfer anything; the reserve covered it.")
    if charges:
        facts.flags.append("Each partner's share was collected automatically from their saved PayPal wallet, and each partner "
                           "paid PayPal's fee on their own charge, so the business received their exact share.")
        for name in parties:
            if name in charges:
                facts.add(f"charged_{slug(name)}", Decimal(charges[name]["charged"]), f"what {name}'s wallet was charged, including PayPal's fee on that charge",
                          f"{name}'s wallet charged")
    facts.add("reserve_after", entry["reserve_after"], "the reserve after this refund", "Held back in total now")
    _state_changes(facts, entry.get("state_before", {}), entry["state_after"])
    facts.notes = entry["notes"]
    return facts


def check_narrative(parts: list[str], allowed: set[str], summary_numbers: set[str]) -> list[str]:
    """Why this text is not acceptable: unknown placeholders, or numbers that did not come from ClausePay."""
    problems: list[str] = []
    if not parts or not all(isinstance(part, str) and part.strip() for part in parts):
        return ["the subject and every paragraph must be non-empty text"]
    for part in parts:
        for name in PLACEHOLDER.findall(part):
            if name not in allowed:
                problems.append(f"{{{name}}} is not one of the placeholders you were given")
        stray = {token for token in _numbers(PLACEHOLDER.sub("", part)) if token not in summary_numbers}
        if stray:
            problems.append(f"the text contains the number(s) {', '.join(sorted(stray))}; never write numbers, use a placeholder")
    return problems


def fallback_narrative(facts: Facts) -> tuple[str, list[str]]:
    """Plain, fixed wording used when Claude is unavailable or keeps writing numbers. Still correct, just less friendly."""
    if facts.kind == "sale":
        subject = "Sale of {sale_amount} processed"
        paragraphs = ["A sale of {sale_amount} came in. After PayPal's fee of {paypal_fee}, {received} was divided according to the contract.",
                      "The figures below show who was paid and what was held back."]
    else:
        subject = "Refund of {refund_amount} processed"
        paragraphs = ["A refund of {refund_amount} was paid to the buyer. It cost the business {refund_cost}.",
                      "The figures below show how that cost was covered and what each partner needs to transfer."]
    return subject, paragraphs


class ReportWriter:
    """Layer two: asks Claude for the explanation, with strict checks. Without Claude it falls back to fixed wording."""

    def __init__(self, client: anthropic.Anthropic | None) -> None:
        self.client = client


    def _ask(self, payload: dict[str, Any], problems: list[str]) -> tuple[str, list[str]]:
        if problems:
            payload = {**payload, "your_previous_attempt_was_rejected_because": problems, "fix": "write the report again"}
        response = self.client.messages.create(  # type: ignore[union-attr]
            model=MODEL, max_tokens=2000, system=REPORT_PROMPT, messages=[{"role": "user", "content": json.dumps(payload)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": REPORT_SCHEMA}})
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(response.stop_reason)
        data = json.loads(next(block.text for block in response.content if block.type == "text"))
        return str(data["subject"]), [str(part) for part in data["paragraphs"]]


    def write(self, facts: Facts, summary: list[str], parties: list[str]) -> tuple[str, list[str], str]:
        """(subject, paragraphs with placeholders, source). Never raises: the worst case is the fixed wording."""
        if self.client is None:
            return (*fallback_narrative(facts), "fallback")
        payload = facts.for_claude(summary, parties)
        summary_numbers = _numbers(" ".join(summary))
        problems: list[str] = []
        for _ in range(MAX_ATTEMPTS):
            try:
                subject, paragraphs = self._ask(payload, problems)
            except Exception:  # network, refusal, bad JSON: try again, then fall back
                problems = ["the reply could not be read as the requested JSON"]
                continue
            problems = check_narrative([subject, *paragraphs], set(facts.values), summary_numbers)
            if not problems:
                return subject, paragraphs, "claude"
        return (*fallback_narrative(facts), "fallback")


def render(facts: Facts, subject: str, paragraphs: list[str], source: str, recipients: list[str], title: str) -> dict[str, Any]:
    def fill(text: str) -> str:
        return PLACEHOLDER.sub(lambda match: facts.values.get(match.group(1), match.group(0)), text)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"kind": facts.kind, "title": title, "subject": fill(subject), "narrative": [fill(part) for part in paragraphs],
            "figures": [list(row) for row in facts.figures], "recipients": recipients, "source": source,
            "generated_at": now, "email_status": "Email sent (simulated)", "sent_at": now}


class ReportService:
    """Finds committed sales and refunds that have no report yet and writes one for each."""

    def __init__(self, live: LiveService, get_writer: Any) -> None:
        self.live = live
        self.get_writer = get_writer


    def _entry(self, entry_id: str) -> dict[str, Any] | None:
        runner = self.live.runner()
        found = [entry for entry in (runner.entries() if runner else []) if entry["entry_id"] == entry_id and entry["status"] == "committed"]
        return found[-1] if found else None


    def generate_pending(self) -> int:
        """Write every report that is due. Returns how many were written. Claude is called without holding any lock."""
        with self.live.store.lock:
            runner = self.live.runner()
            if runner is None:
                return 0
            tasks = [(sale.order_id, "sale", "") for sale in self.live.store.all() if sale.status == "paid_out" and sale.report is None]
            tasks += [(sale.order_id, "refund", refund["refund_id"]) for sale in self.live.store.all()
                      for refund in sale.refunds if refund["status"] == "done" and not refund.get("report")]
            parties, emails = runner.parties, list(runner.emails.values())
            summary = json.loads(runner.meta_path.read_text()).get("summary", [])
        written = 0
        for order_id, kind, refund_id in tasks:
            sale = self.live.store.get(order_id)
            entry = self._entry(sale.entry_id if kind == "sale" and sale else f"refund:{refund_id}") if sale else None
            if sale is None or entry is None:
                continue
            if kind == "sale":
                facts = sale_facts(entry, parties)
            else:
                record = next(r for r in sale.refunds if r["refund_id"] == refund_id)
                facts = refund_facts(entry, parties, record.get("charges") or None)
            subject, paragraphs, source = self.get_writer().write(facts, summary, parties)
            title = f"Sale report: {money(entry['amount'])}" if kind == "sale" else f"Refund report: {money(entry['amount'])}"
            report = render(facts, subject, paragraphs, source, emails, title)
            with self.live.store.lock:
                if kind == "sale":
                    sale.report = report
                else:
                    next(r for r in sale.refunds if r["refund_id"] == refund_id)["report"] = report
                self.live.store.log("report", f"{title} written ({source}); {report['email_status'].lower()} to {len(emails)} partners.", order_id)
                self.live.store.save()
            written += 1
        return written
