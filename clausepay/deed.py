"""The record kept for each contract ("deed")."""
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

STATUSES = ("draft", "clarifying", "pending_approval", "approved", "active")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Deed:
    payees: list  # [{"name": ..., "email": ...}], from a form, never from the contract text
    contract_text: str
    deed_id: str = field(default_factory=lambda: f"deed_{uuid.uuid4().hex[:8]}")
    refined_contract: dict = None  # filled in once the missing details are captured
    status: str = "draft"
    ledger: list = field(default_factory=list)  # sales, holdbacks, payouts
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if not self.payees:
            raise ValueError("a deed needs at least one payee")
        for payee in self.payees:
            if not payee.get("name") or "@" not in payee.get("email", ""):
                raise ValueError(f"payee needs a name and an email: {payee}")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)
