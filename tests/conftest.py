import pytest


@pytest.fixture(autouse=True)
def no_group_scan_break(monkeypatch):
    """Tests shouldn't sit through the 20-second break between group scan steps."""
    from bot import group_scan

    monkeypatch.setattr(group_scan, "BREAK_SECONDS", 0)
