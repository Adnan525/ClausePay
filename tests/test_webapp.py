import io

import pytest
from reportlab.pdfgen import canvas

from clausepay.contract_analyzer import AnalyzerError
from clausepay.contract_store import JsonContractStore
from clausepay.webapp import create_app
from tests.conftest import good_contract

QUESTION = {"question": "Before or after fee?", "options": ["after", "before"]}


class FakeAnalyzer:
    """Asks one question until it has been answered, then completes."""

    def __init__(self):
        self.calls = []
        self.fail = False

    def analyze(self, text, clarifications=()):
        self.calls.append((text, list(clarifications)))
        if self.fail:
            raise AnalyzerError("boom")
        if clarifications:
            return {"status": "complete", "questions": [], "refined_contract": good_contract()}
        return {"status": "needs_answers", "questions": [QUESTION], "refined_contract": None}


@pytest.fixture
def analyzer():
    return FakeAnalyzer()


@pytest.fixture
def store(tmp_path):
    return JsonContractStore(str(tmp_path / "deeds.json"))


@pytest.fixture
def client(store, analyzer):
    return create_app(store=store, analyzer=analyzer).test_client()


def test_index_served(client):
    assert b"ClausePay" in client.get("/").data


def test_full_flow_with_pasted_text(client, store, analyzer):
    created = client.post("/api/deeds", data={"contract_text": "60/40"})
    assert created.status_code == 201
    body = created.get_json()
    assert body["status"] == "clarifying" and body["questions"] == [QUESTION]

    done = client.post(f"/api/deeds/{body['deed_id']}/answers",
                       json={"answers": [{"question": QUESTION["question"], "answer": "after"}]})
    assert done.status_code == 200
    assert done.get_json()["status"] == "refined"
    assert len(done.get_json()["refined_contract"]["payees"]) == 3

    saved = store.get(body["deed_id"])
    assert saved.status == "refined" and saved.clarifications[0]["answer"] == "after"
    assert client.get(f"/api/deeds/{body['deed_id']}").get_json()["status"] == "refined"


def test_pdf_upload(client, analyzer, tmp_path):
    path = tmp_path / "c.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 750, "Alice gets 60 percent")
    c.save()
    res = client.post("/api/deeds", data={"file": (io.BytesIO(path.read_bytes()), "c.pdf")},
                      content_type="multipart/form-data")
    assert res.status_code == 201
    assert "Alice gets 60 percent" in analyzer.calls[0][0]


def test_empty_and_unsupported_input_rejected(client):
    assert client.post("/api/deeds", data={"contract_text": "  "}).status_code == 400
    res = client.post("/api/deeds", data={"file": (io.BytesIO(b"x"), "c.docx")}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_analyzer_failure_saves_nothing(client, store, analyzer):
    analyzer.fail = True
    assert client.post("/api/deeds", data={"contract_text": "deal"}).status_code == 502
    assert store.list() == []


def test_failed_answer_round_leaves_deed_unchanged(client, store, analyzer):
    deed_id = client.post("/api/deeds", data={"contract_text": "deal"}).get_json()["deed_id"]
    analyzer.fail = True
    res = client.post(f"/api/deeds/{deed_id}/answers",
                      json={"answers": [{"question": QUESTION["question"], "answer": "after"}]})
    assert res.status_code == 502
    assert store.get(deed_id).clarifications == []


@pytest.mark.parametrize("answers", [[], [{"question": "other?", "answer": "x"}],
                                      [{"question": QUESTION["question"], "answer": " "}]])
def test_bad_answers_rejected(client, answers):
    deed_id = client.post("/api/deeds", data={"contract_text": "deal"}).get_json()["deed_id"]
    assert client.post(f"/api/deeds/{deed_id}/answers", json={"answers": answers}).status_code == 400


def test_unknown_deed_404(client):
    assert client.get("/api/deeds/deed_nope").status_code == 404


def refined_deed(client):
    deed_id = client.post("/api/deeds", data={"contract_text": "deal"}).get_json()["deed_id"]
    client.post(f"/api/deeds/{deed_id}/answers", json={"answers": [{"question": QUESTION["question"], "answer": "after"}]})
    return deed_id


EMAILS = {"Alice": "alice@example.com", "Bob": "bob@example.com", "Animal Foundation": "foundation@example.org"}


def payee_body(**overrides):
    emails = {**EMAILS, **overrides}
    return {"payees": [{"name": n, "email": e} for n, e in emails.items()]}


def test_payees_saved_for_every_listed_payee(client, store):
    deed_id = refined_deed(client)
    res = client.post(f"/api/deeds/{deed_id}/payees", json=payee_body())
    assert res.status_code == 200 and res.get_json()["status"] == "payees_collected"
    assert store.get(deed_id).payees == [{"name": n, "email": e} for n, e in EMAILS.items()]


def test_payees_can_be_resubmitted(client, store):
    deed_id = refined_deed(client)
    client.post(f"/api/deeds/{deed_id}/payees", json=payee_body())
    assert client.post(f"/api/deeds/{deed_id}/payees", json=payee_body(Bob="new@example.com")).status_code == 200
    assert store.get(deed_id).payees[1]["email"] == "new@example.com"


@pytest.mark.parametrize("body", [
    {"payees": []},
    {"payees": [{"name": "Alice", "email": "alice@example.com"}]},               # missing payees
    {"payees": payee_body()["payees"] + [{"name": "Mallory", "email": "m@x.com"}]},  # extra recipient
    payee_body(Bob="not-an-email"),
    payee_body(Bob=""),
])
def test_bad_payees_rejected(client, store, body):
    deed_id = refined_deed(client)
    assert client.post(f"/api/deeds/{deed_id}/payees", json=body).status_code == 400
    assert store.get(deed_id).payees == []


def test_payees_rejected_before_refinement(client):
    deed_id = client.post("/api/deeds", data={"contract_text": "deal"}).get_json()["deed_id"]
    assert client.post(f"/api/deeds/{deed_id}/payees", json=payee_body()).status_code == 409
