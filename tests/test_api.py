import json

import pytest

from conftest import build


def cfg_text(httpmock):
    u = httpmock.url
    return f"""
api_token: "${{TOP_SWITCH_API_TOKEN}}"
buttons:
  - id: lamp
    name: Lamp
    icon: "💡"
    confirm: true
    toggle:
      on: {{type: http, method: POST, url: "{u}/on"}}
      off: {{type: http, method: POST, url: "{u}/off"}}
      status: {{type: http, url: "{u}/state", json_path: "power", on_value: "ON", off_value: "OFF"}}
  - id: nostatus
    name: No status
    icon: bulb
    confirm: "Really?"
    toggle:
      on: {{type: http, method: POST, url: "{u}/on2"}}
      off: {{type: http, method: POST, url: "{u}/off2"}}
  - id: once
    name: Once
    action: {{type: plugin, plugin: webhook.call, method: POST, url: "{u}/hook", body: {{a: 1}}}}
"""


@pytest.fixture
def env(tmp_path, httpmock, api_factory):
    for p in ("/on", "/off", "/on2", "/off2", "/hook"):
        httpmock.routes[("POST", p)] = (200, b"{}")
    httpmock.set_json("GET", "/state", {"power": "ON"})
    cfg, state, core = build(tmp_path, cfg_text(httpmock))
    return api_factory(cfg, core), httpmock, state


def test_buttons_returns_icon_and_confirm(env):
    api, _, _ = env
    code, body = api.req("GET", "/api/buttons")
    by = {b["id"]: b for b in body["buttons"]}
    assert code == 200
    assert (by["lamp"]["icon"], by["lamp"]["confirm"]) == ("💡", True)
    assert (by["nostatus"]["icon"], by["nostatus"]["confirm"]) == ("bulb", "Really?")
    assert by["once"]["confirm"] is False and by["once"]["toggle"] is False
    assert by["lamp"]["status"]["state"] == "on" and by["lamp"]["last_pressed"] is None


def test_unknown_button_404(env):
    assert env[0].req("POST", "/api/press/nope")[0] == 404


def test_plugin_endpoints_are_get_only_and_unknown_404(env):
    api, _, _ = env
    assert api.req("POST", "/api/plugins/adguard/clients", {})[0] == 405
    assert api.req("PUT", "/api/plugins/adguard/clients", {})[0] == 405
    assert api.req("GET", "/api/plugins/adguard/clients")[0] == 404  # plugin not configured
    assert api.req("GET", "/api/plugins/other/x")[0] == 404


@pytest.mark.parametrize("body", [{"desired": "maybe"}, {"desired": 1}, {"desired": None}, {"desired": True},
                                  {"desired": "ON"}, {"other": "on"}, ["on"], "on"])
def test_invalid_desired_is_422(env, body):
    api, mock, _ = env
    assert api.req("POST", "/api/press/lamp", body)[0] == 422
    assert not mock.calls("POST")


def test_invalid_json_body_is_422(env):
    assert env[0].req("POST", "/api/press/lamp", raw=b"{nope")[0] == 422


def test_desired_on_action_button_is_422(env):
    assert env[0].req("POST", "/api/press/once", {"desired": "on"})[0] == 422


@pytest.mark.parametrize("live,expected", [("ON", "/off"), ("OFF", "/on")])
def test_toggle_without_desired_flips_known_state(env, live, expected):
    api, mock, _ = env
    mock.set_json("GET", "/state", {"power": live})
    code, body = api.req("POST", "/api/press/lamp")
    assert code == 200
    assert [c[1] for c in mock.calls("POST")] == [expected]


def test_unknown_state_without_desired_is_409_and_calls_nothing(env):
    api, mock, _ = env
    mock.routes[("GET", "/state")] = (500, b"")
    code, body = api.req("POST", "/api/press/lamp")
    assert code == 409 and "άγνωστη κατάσταση" in body["error"]
    assert not mock.calls("POST")


def test_toggle_without_status_is_409_without_desired_and_works_with_it(env):
    api, mock, _ = env
    assert api.req("POST", "/api/press/nostatus")[0] == 409
    assert api.req("POST", "/api/press/nostatus", {"desired": "off"})[0] == 200
    assert [c[1] for c in mock.calls("POST")] == ["/off2"]


def test_desired_overrides_even_if_state_known(env):
    api, mock, _ = env
    assert api.req("POST", "/api/press/lamp", {"desired": "on"})[0] == 200
    assert [c[1] for c in mock.calls("POST")] == ["/on"]


def test_last_pressed_is_information_only(env):
    api, mock, state = env
    api.req("POST", "/api/press/nostatus", {"desired": "on"})
    view = {b["id"]: b for b in api.req("GET", "/api/buttons")[1]["buttons"]}["nostatus"]
    assert view["last_pressed"]["at"] and view["status"]["state"] == "unknown"  # never used as state
    assert api.req("POST", "/api/press/nostatus")[0] == 409


def test_failed_action_is_502_and_not_recorded(env):
    api, mock, state = env
    mock.routes[("POST", "/on")] = (500, b"")
    code, body = api.req("POST", "/api/press/lamp", {"desired": "on"})
    assert code == 502 and body["ok"] is False
    assert state.button("lamp").get("last_pressed") is None


def test_press_log_lines(env, tmp_path):
    api, _, _ = env
    api.req("POST", "/api/press/once")
    api.req("POST", "/api/press/lamp", {"desired": "on"})
    lines = [json.loads(l) for l in (tmp_path / "logs" / "top-switch.jsonl").read_text().splitlines()]
    assert [l["button"] for l in lines] == ["once", "lamp"]
    assert lines[0]["source"] == "ui" and lines[0]["ip"] == "127.0.0.1" and lines[0]["result"] == "ok" and lines[0]["time"]
