import json
import logging
from datetime import datetime, timezone

from utils.state import (
    get_last_alert_key,
    load_state,
    record_alert_sent,
    write_state_atomic,
)


def test_duplicate_alert_key_is_recorded() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    state: dict = {}
    record_alert_sent(
        state,
        "bitmex_bvol",
        "2026-07-20",
        now,
        now,
        metadata={"value": 13.5},
    )
    assert get_last_alert_key(state, "bitmex_bvol") == "2026-07-20"
    assert state["last_alert_date"] == "2026-07-20"


def test_corrupt_state_file_is_backed_up(tmp_path) -> None:
    logger = logging.getLogger("test_corrupt_state_file_is_backed_up")
    state_file = tmp_path / "state.json"
    state_file.write_text("{broken", encoding="utf-8")

    state = load_state(state_file, logger)

    assert state["last_alert_keys"] == {}
    assert state["system"]["state_error"]["path"] == str(state_file)
    assert list(tmp_path.glob("state.json.corrupt.*"))


def test_atomic_state_write(tmp_path) -> None:
    logger = logging.getLogger("test_atomic_state_write")
    state_file = tmp_path / "state.json"

    write_state_atomic(state_file, {"monitors": {"x": {"status": "ok"}}}, logger)

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["monitors"]["x"]["status"] == "ok"
    assert not list(tmp_path.glob(".state.json.*.tmp"))
