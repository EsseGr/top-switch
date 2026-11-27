import json

import pytest

from conftest import build


def make(tmp_path, httpmock, status_extra='on_value: "ON", off_value: "OFF"', path="a.b.c", timeout=None):
    t = f", timeout: {timeout}" if timeout else ""
    text = f"""
api_token: a
buttons:
  - id: t
    name: T
    toggle:
      on: {{type: http, url: "{httpmock.url}/on"}}
      off: {{type: http, url: "{httpmock.url}/off"}}
      status: {{type: http, url: "{httpmock.url}/s", json_path: "{path}", {status_extra}{t}}}
"""
    return build(tmp_path, text)[2]


def st(core):
    return core.buttons_view()[0]["status"]


def serve(httpmock, value):
    httpmock.set_json("GET", "/s", {"a": {"b": {"c": value}}})


def test_on_and_off(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    serve(httpmock, "ON")
    assert st(core) == {"state": "on", "source": "live", "error": None}
    serve(httpmock, "OFF")
    assert st(core)["state"] == "off"


def test_without_off_value_any_other_valid_value_is_off(tmp_path, httpmock):
    core = make(tmp_path, httpmock, status_extra='on_value: "ON"')
    serve(httpmock, "whatever")
    assert st(core)["state"] == "off"
    serve(httpmock, "ON")
    assert st(core)["state"] == "on"


def test_with_off_value_other_value_is_unknown(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    serve(httpmock, "WAT")
    assert st(core)["state"] == "unknown" and st(core)["error"]


def test_bool_and_number_values_are_not_confused(tmp_path, httpmock):
    core = make(tmp_path, httpmock, status_extra="on_value: true")
    serve(httpmock, 1)
    assert st(core)["state"] == "off"
    serve(httpmock, True)
    assert st(core)["state"] == "on"


def test_non_2xx(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    httpmock.routes[("GET", "/s")] = (503, b'{"a":{"b":{"c":"ON"}}}')
    s = st(core)
    assert s["state"] == "unknown" and "503" in s["error"]


def test_not_json(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    httpmock.routes[("GET", "/s")] = (200, b"<html>")
    assert st(core)["state"] == "unknown" and "JSON" in st(core)["error"]


def test_timeout(tmp_path, httpmock):
    core = make(tmp_path, httpmock, timeout=0.2)
    httpmock.routes[("GET", "/s")] = (200, b'{"a":{"b":{"c":"ON"}}}', 1.0)
    assert st(core)["state"] == "unknown"


def test_connection_refused(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    httpmock.close()
    assert st(core)["state"] == "unknown"


def test_missing_path(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    httpmock.set_json("GET", "/s", {"a": {"x": 1}})
    s = st(core)
    assert s["state"] == "unknown" and "json_path" in s["error"]


def test_config_only_vs_state_json(tmp_path, httpmock):
    core = make(tmp_path, httpmock)
    serve(httpmock, "ON")
    v = core.buttons_view()[0]
    assert v["status"]["state"] == "on" and v["last_pressed"] is None
    # with a state.json present: same status, last_pressed filled
    (tmp_path / "state.json").write_text(json.dumps(
        {"buttons": {"t": {"last_pressed": {"at": "2026-11-27T10:00:00+00:00", "source": "ui"}}}}))
    core = make(tmp_path, httpmock)
    v = core.buttons_view()[0]
    assert v["status"]["state"] == "on" and v["last_pressed"] == {"at": "2026-11-27T10:00:00+00:00"}
    # live failure: unknown, last_pressed still only information
    httpmock.routes[("GET", "/s")] = (500, b"")
    v = core.buttons_view()[0]
    assert v["status"]["state"] == "unknown" and v["last_pressed"]["at"]


def test_no_status_is_always_unknown(tmp_path, httpmock):
    text = f"""
api_token: a
buttons:
  - id: t
    name: T
    toggle: {{on: {{type: http, url: "{httpmock.url}/on"}}, off: {{type: http, url: "{httpmock.url}/off"}}}}
"""
    core = build(tmp_path, text)[2]
    v = core.buttons_view()[0]
    assert v["status"]["state"] == "unknown" and v["last_pressed"] is None
    (tmp_path / "state.json").write_text(json.dumps({"buttons": {"t": {"last_pressed": {"at": "x", "source": "ui"}}}}))
    core = build(tmp_path, text)[2]
    v = core.buttons_view()[0]
    assert v["status"]["state"] == "unknown" and v["last_pressed"]["at"] == "x"
