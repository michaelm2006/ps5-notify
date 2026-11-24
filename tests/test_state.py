from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ps5_notify.models import StockStatus
from ps5_notify.state import StateStore


def test_state_store_roundtrip(tmp_path: Path):
    state_path = tmp_path / "nested" / "state.json"
    store = StateStore(state_path)

    ts = datetime(2026, 11, 24, 20, 0, 0, tzinfo=timezone.utc)
    ps_state = store.get("playstation_uk")
    ps_state.last_status = StockStatus.IN_STOCK
    ps_state.last_checked_at = ts
    ps_state.last_notified_at = ts
    ps_state.consecutive_errors = 2
    ps_state.last_error = "HTTP 429"
    ps_state.error_alert_sent = True
    ps_state.backoff_until = ts
    store.save()

    reloaded = StateStore(state_path)
    loaded_ps = reloaded.get("playstation_uk")
    assert loaded_ps.last_status == StockStatus.IN_STOCK
    assert loaded_ps.last_checked_at == ts
    assert loaded_ps.last_notified_at == ts
    assert loaded_ps.consecutive_errors == 2
    assert loaded_ps.last_error == "HTTP 429"
    assert loaded_ps.error_alert_sent is True
    assert loaded_ps.backoff_until == ts


def test_state_store_handles_corrupt_json(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    store = StateStore(state_path)
    assert store.get("argos").last_status is None
    assert (tmp_path / "state.json.corrupt").exists()
