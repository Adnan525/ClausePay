import pytest


@pytest.fixture(autouse=True)
def no_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests drive reconciliation and reports by hand; the app's background thread stays off."""
    monkeypatch.setenv("background_worker", "off")
