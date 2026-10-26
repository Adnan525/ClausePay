import json

import pytest

from clausepay.contract_store import JsonContractStore, DeedNotFound
from clausepay.deed import Deed

PAYEES = [{"name": "Alice", "email": "alice@example.com"}, {"name": "Bob", "email": "bob@example.com"}]


@pytest.fixture
def store(tmp_path):
    return JsonContractStore(str(tmp_path / "deeds.json"))


def test_add_and_get_roundtrip(store):
    deed = store.add(Deed(payees=PAYEES, contract_text="60/40 split"))
    assert deed.deed_id.startswith("deed_")
    assert store.get(deed.deed_id) == deed


def test_save_updates_fields_and_timestamp(store):
    deed = store.add(Deed(payees=PAYEES, contract_text="x", updated_at="2000-01-01T00:00:00+00:00"))
    deed.status = "approved"
    deed.refined_contract = {"split": {"Alice": 0.6, "Bob": 0.4}}
    store.save(deed)
    loaded = store.get(deed.deed_id)
    assert loaded.status == "approved"
    assert loaded.refined_contract["split"]["Bob"] == 0.4
    assert loaded.updated_at != "2000-01-01T00:00:00+00:00"


def test_list_and_delete(store):
    a = store.add(Deed(payees=PAYEES, contract_text="a"))
    b = store.add(Deed(payees=PAYEES, contract_text="b"))
    assert [d.deed_id for d in store.list()] == [a.deed_id, b.deed_id]
    store.delete(a.deed_id)
    assert [d.deed_id for d in store.list()] == [b.deed_id]


def test_missing_deed_raises(store):
    with pytest.raises(DeedNotFound):
        store.get("deed_nope")
    with pytest.raises(DeedNotFound):
        store.delete("deed_nope")
    with pytest.raises(DeedNotFound):
        store.save(Deed(payees=PAYEES, contract_text="x"))


def test_duplicate_id_rejected(store):
    deed = store.add(Deed(payees=PAYEES, contract_text="x"))
    with pytest.raises(ValueError):
        store.add(deed)


def test_file_is_keyed_by_deed_id(store):
    deed = store.add(Deed(payees=PAYEES, contract_text="x"))
    with open(store.path) as f:
        assert list(json.load(f)) == [deed.deed_id]


@pytest.mark.parametrize("payees", [[], [{"name": "A", "email": "nope"}], [{"email": "a@b.com"}]])
def test_invalid_payees_rejected(payees):
    with pytest.raises(ValueError):
        Deed(payees=payees, contract_text="x")


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        Deed(payees=PAYEES, contract_text="x", status="bogus")
