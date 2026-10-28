import json
from types import SimpleNamespace

import pytest

from clausepay.contract_analyzer import AnalyzerError, ContractAnalyzer
from tests.conftest import good_contract


class FakeClient:
    """Stands in for anthropic.Anthropic; replies with the queued payloads in order."""

    def __init__(self, *payloads, stop_reason="end_turn"):
        self.payloads = list(payloads)
        self.calls = []
        self.stop_reason = stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps(self.payloads.pop(0))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def complete(contract):
    return {"status": "complete", "questions": [], "refined_contract": contract}


def test_returns_questions():
    questions = [{"question": "Before or after fee?", "options": ["after", "before"]}]
    client = FakeClient({"status": "needs_answers", "questions": questions, "refined_contract": None})
    result = ContractAnalyzer(client=client).analyze("deal")
    assert result["status"] == "needs_answers" and result["questions"] == questions


def test_returns_complete_contract_and_sends_clarifications():
    client = FakeClient(complete(good_contract()))
    result = ContractAnalyzer(client=client).analyze("deal", [{"question": "Q1", "answer": "A1"}])
    assert result["refined_contract"]["payees"][2]["name"] == "Animal Foundation"
    prompt = client.calls[0]["messages"][0]["content"]
    assert "<contract>\ndeal\n</contract>" in prompt and "Q: Q1\nA: A1" in prompt
    assert client.calls[0]["model"] == "claude-sonnet-5-5"


def test_invalid_contract_is_retried_once():
    bad = good_contract()
    bad["rules"] = []
    client = FakeClient(complete(bad), complete(good_contract()))
    result = ContractAnalyzer(client=client).analyze("deal")
    assert result["status"] == "complete"
    assert "rules must not be empty" in client.calls[1]["messages"][-1]["content"]


def test_gives_up_after_second_invalid_answer():
    bad = good_contract()
    bad["payees"] = []
    with pytest.raises(AnalyzerError):
        ContractAnalyzer(client=FakeClient(complete(bad), complete(bad))).analyze("deal")


def test_refusal_raises():
    with pytest.raises(AnalyzerError):
        ContractAnalyzer(client=FakeClient(complete(good_contract()), stop_reason="refusal")).analyze("deal")
