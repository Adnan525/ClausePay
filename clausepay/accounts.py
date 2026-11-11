import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    label: str
    role: str
    email: str | None


def _env_email(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_accounts() -> list[Account]:
    return [
        Account("Business", "Receives every sale, pays out and refunds", _env_email("business_email")),
        Account("Seller A", "Partner in the contract", _env_email("u_1_email")),
        Account("Seller B", "Partner in the contract", _env_email("u_2_email")),
        Account("Buyer", "Pays for a sale", _env_email("buyer_email")),
    ]
