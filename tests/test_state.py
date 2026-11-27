import json
import threading

from conftest import build
from top_switch.state import StateStore


def test_concurrent_presses_on_different_buttons_lose_no_update(tmp_path, httpmock):
    for p in ("/a", "/b"):
        httpmock.routes[("POST", p)] = (200, b"{}", 0.2)  # overlap the two presses
    text = f"""
api_token: x
buttons:
  - {{id: a, name: A, action: {{type: http, method: POST, url: "{httpmock.url}/a"}}}}
  - {{id: b, name: B, action: {{type: http, method: POST, url: "{httpmock.url}/b"}}}}
"""
    _, state, core = build(tmp_path, text)
    threads = [threading.Thread(target=core.press, args=(i, "ui", None)) for i in ("a", "b") * 5]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["buttons"]["a"]["last_pressed"] and data["buttons"]["b"]["last_pressed"]


def test_many_parallel_updates_all_land(tmp_path):
    s = StateStore(tmp_path / "state.json")
    ts = [threading.Thread(target=s.set_schedule_key, args=(f"b{i}", str(i))) for i in range(30)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(json.loads((tmp_path / "state.json").read_text())["schedule"]) == 30


def test_write_is_atomic_no_tmp_left(tmp_path):
    s = StateStore(tmp_path / "state.json")
    s.set_schedule_key("x", "k")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert StateStore(tmp_path / "state.json").schedule_key("x") == "k"


def test_failed_write_does_not_publish_in_memory_change(tmp_path, monkeypatch):
    s = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(s, "_write", lambda d: (_ for _ in ()).throw(OSError("disk full")))
    try:
        s.set_schedule_key("x", "k")
    except OSError:
        pass
    assert s.schedule_key("x") is None


def test_corrupt_state_is_renamed_and_start_is_empty(tmp_path, caplog):
    (tmp_path / "state.json").write_text("{broken")
    s = StateStore(tmp_path / "state.json")
    corrupt = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(corrupt) == 1 and corrupt[0].read_text() == "{broken"
    assert s.snapshot()["lost"] is True and s.snapshot()["buttons"] == {}
    assert "corrupt" in caplog.text
    assert StateStore(tmp_path / "state.json").snapshot()["lost"] is True  # survives restart


def test_non_object_state_is_corrupt_too(tmp_path):
    (tmp_path / "state.json").write_text("[1]")
    assert StateStore(tmp_path / "state.json").snapshot()["lost"] is True


def test_missing_state_is_a_normal_first_start(tmp_path):
    assert StateStore(tmp_path / "state.json").snapshot()["lost"] is False
