from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import build
from top_switch.schedule import Scheduler, current_period, parse_segments

TZ = ZoneInfo("Europe/Athens")


def at(day, hh, mm=0, month=11):
    return datetime(2026, month, day, hh, mm, tzinfo=TZ)  # 2026-11-23 is a Monday


def period(segments, when):
    p = current_period(parse_segments(segments), TZ, when)
    return p and (p[0][:16], p[1])


def test_regular_segment():
    seg = [{"on": "17:00", "off": "20:00"}]
    assert period(seg, at(23, 16, 59)) == ("2026-11-22T20:00", "off")
    assert period(seg, at(23, 17, 0)) == ("2026-11-23T17:00", "on")
    assert period(seg, at(23, 19, 59)) == ("2026-11-23T17:00", "on")
    assert period(seg, at(23, 20, 0)) == ("2026-11-23T20:00", "off")
    assert period(seg, at(24, 3, 0)) == ("2026-11-23T20:00", "off")


def test_midnight_crossing_belongs_to_start_day():
    seg = [{"days": ["fri"], "on": "22:00", "off": "06:00"}]  # 2026-11-27 is Friday
    assert period(seg, at(27, 21)) == ("2026-11-21T06:00", "off")
    assert period(seg, at(27, 23)) == ("2026-11-27T22:00", "on")
    assert period(seg, at(28, 5, 59)) == ("2026-11-27T22:00", "on")  # Saturday morning still Friday's segment
    assert period(seg, at(28, 6, 0)) == ("2026-11-28T06:00", "off")
    assert period(seg, at(29, 1)) == ("2026-11-28T06:00", "off")  # Saturday night: no segment starts Saturday


def test_days_filter():
    seg = [{"days": ["mon"], "on": "17:00", "off": "20:00"}]
    assert period(seg, at(24, 18)) == ("2026-11-23T20:00", "off")  # Tuesday: not on
    assert period(seg, at(23, 18)) == ("2026-11-23T17:00", "on")


def test_no_period_when_nothing_in_lookback():
    assert current_period([], TZ, at(23, 18)) is None


@pytest.mark.parametrize("segs", [
    [{"on": "17:00", "off": "20:00"}, {"on": "19:00", "off": "21:00"}],
    [{"on": "22:00", "off": "06:00"}, {"days": ["tue"], "on": "05:00", "off": "07:00"}],  # tue 05-07 overlaps mon night
    [{"on": "22:00", "off": "06:00"}, {"on": "23:00", "off": "23:30"}],
    [{"days": ["sun"], "on": "22:00", "off": "06:00"}, {"days": ["mon"], "on": "05:00", "off": "07:00"}],  # week wrap
])
def test_overlap_is_an_error(segs):
    with pytest.raises(ValueError, match="overlap"):
        parse_segments(segs)


def test_adjacent_and_non_overlapping_ok():
    parse_segments([{"on": "17:00", "off": "20:00"}, {"on": "20:00", "off": "21:00"}])
    parse_segments([{"days": ["mon"], "on": "22:00", "off": "06:00"}, {"days": ["tue"], "on": "07:00", "off": "08:00"}])


@pytest.mark.parametrize("bad", [
    [{"on": "25:00", "off": "20:00"}], [{"on": "17:00", "off": "17:00"}], [{"on": "5pm", "off": "20:00"}],
    [{"days": ["funday"], "on": "1:00", "off": "2:00"}], [{"on": "17:00"}], "x", [],
])
def test_invalid_segments(bad):
    with pytest.raises(ValueError):
        parse_segments(bad)


def test_dst_changeover_keys_use_real_instants():
    # Athens leaves DST on 2026-10-25 (04:00 -> 03:00); the 22:00 start is unambiguous either side
    seg = [{"on": "22:00", "off": "06:00"}]
    key, desired = current_period(parse_segments(seg), TZ, datetime(2026, 10, 25, 12, tzinfo=TZ))
    assert desired == "off" and key.startswith("2026-10-25T06:00:00+02:00")


# ---- scheduler

@pytest.fixture
def sched(tmp_path, httpmock):
    for p in ("/on", "/off"):
        httpmock.routes[("POST", p)] = (200, b"{}")
    text = f"""
api_token: x
timezone: Europe/Athens
buttons:
  - id: t
    name: T
    toggle:
      on: {{type: http, method: POST, url: "{httpmock.url}/on"}}
      off: {{type: http, method: POST, url: "{httpmock.url}/off"}}
    schedule: [{{on: "17:00", off: "20:00"}}]
"""
    cfg, state, core = build(tmp_path, text)

    def new():  # a "restart": fresh objects over the same state.json
        cfg2, state2, core2 = build(tmp_path, text)
        return Scheduler(core2, cfg2, state2), core2

    return Scheduler(core, cfg, state), core, httpmock, state, new


def paths(mock):
    return [c[1] for c in mock.calls("POST")]


def test_applies_once_per_segment(sched):
    s, core, mock, *_ = sched
    s.tick(at(23, 17, 0))
    s.tick(at(23, 17, 1))
    s.tick(at(23, 18, 0))
    assert paths(mock) == ["/on"]
    s.tick(at(23, 20, 0))
    s.tick(at(23, 20, 1))
    assert paths(mock) == ["/on", "/off"]


def test_manual_on_at_21_then_tick_at_midnight_makes_no_call(sched):
    s, core, mock, *_ = sched
    s.tick(at(23, 20, 0))
    assert paths(mock) == ["/off"]
    core.press("t", "ui", "192.0.2.1", "on")  # manual at 21:00
    s.tick(at(23, 21, 0))
    s.tick(at(24, 0, 0))
    assert paths(mock) == ["/off", "/on"]  # no scheduler call after the manual press
    s.tick(at(24, 17, 0))
    assert paths(mock)[-1] == "/on" and len(paths(mock)) == 3  # next segment applies again


def test_failure_is_retried_on_next_tick_until_success(sched, caplog):
    s, core, mock, state, _ = sched
    mock.routes[("POST", "/off")] = (500, b"")
    s.tick(at(23, 20, 0))
    s.tick(at(23, 20, 1))
    assert paths(mock) == ["/off", "/off"]
    assert state.schedule_key("t") is None
    mock.routes[("POST", "/off")] = (200, b"{}")
    s.tick(at(23, 20, 2))
    s.tick(at(23, 20, 3))
    assert paths(mock) == ["/off"] * 3
    assert state.schedule_key("t")


def test_failure_stops_retrying_when_segment_changes(sched):
    s, core, mock, state, _ = sched
    mock.routes[("POST", "/off")] = (500, b"")
    s.tick(at(23, 20, 0))
    s.tick(at(24, 17, 0))  # next period is "on": retried with the new target, not the stale off
    assert paths(mock) == ["/off", "/on"]


def test_restart_inside_same_segment_makes_no_call(sched):
    s, core, mock, state, new = sched
    s.tick(at(23, 18, 0))
    assert paths(mock) == ["/on"]
    s2, _ = new()
    s2.tick(at(23, 18, 30))
    assert paths(mock) == ["/on"]


def test_press_source_is_schedule_in_log(sched, tmp_path):
    s, *_ = sched
    s.tick(at(23, 18, 0))
    import json
    rec = json.loads((tmp_path / "logs" / "top-switch.jsonl").read_text().splitlines()[0])
    assert rec["source"] == "schedule" and rec["ip"] is None and rec["desired"] == "on"
