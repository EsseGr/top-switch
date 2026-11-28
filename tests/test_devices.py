"""Device groups (one AdGuard switch per device + master switch), integrations, ownership hand-over."""
import json

import pytest

from conftest import build
from top_switch.config import ConfigError
from top_switch.core import Conflict, NotFound
from top_switch.state import StateStore


def group_yaml(mock, devices=None, extra_group="", extra="", key="integrations"):
    devices = devices or """
      - {id: tv, name: "Living room TV", ip: 192.0.2.10, icon: "📺"}
      - {id: tablet, name: Tablet, ip: 192.0.2.11, confirm: "Sure?"}"""
    return f"""
api_token: "${{TOP_SWITCH_API_TOKEN}}"
{key}:
  adguard: {{url: "{mock.url}", username: "{mock.USER}", password: "{mock.PASSWORD}"}}
device_groups:
  - id: kids
    name: Kids internet
    icon: "📵"
    integration: adguard{extra_group}
    devices:{devices}
{extra}
"""


R10, R11 = "/.*/$client=192.0.2.10", "/.*/$client=192.0.2.11"


def view(core):
    buttons = core.buttons_view()
    return {b["id"]: b for b in buttons}, {g["id"]: g for g in core.groups_view(buttons)}


# -- config

def test_device_group_expands_to_one_switch_per_device(tmp_path, adguard):
    cfg, _, core = build(tmp_path, group_yaml(adguard))
    assert [b.id for b in cfg.buttons] == ["tv", "tablet"]
    by, groups = view(core)
    assert by["tv"]["group"] == "kids" and by["tv"]["toggle"] is True
    assert by["tv"]["uses"] == ["adguard"] and by["tv"]["detail"] == "192.0.2.10"
    assert by["tv"]["labels"] == {"on": "Κομμένο", "off": "Ελεύθερο"}
    assert by["tablet"]["confirm"] == "Sure?" and by["tv"]["icon"] == "📺"
    g = groups["kids"]
    assert (g["name"], g["integration"], g["members"], g["all_switch"]) == ("Kids internet", "adguard",
                                                                            ["tv", "tablet"], True)
    assert g["status"] == {"state": "off", "on": 0, "total": 2}


def test_plugins_is_still_accepted_but_not_together_with_integrations(tmp_path, adguard):
    build(tmp_path, group_yaml(adguard, key="plugins"))
    text = group_yaml(adguard) + f'plugins: {{adguard: {{url: "{adguard.url}", username: u, password: p}}}}\n'
    with pytest.raises(ConfigError, match="either"):
        build(tmp_path, text)


@pytest.mark.parametrize("ip, msg", [
    ("aa:bb:cc:dd:ee:ff", "MAC"),
    ("192.0.2.0/24", "not a plain IPv4"),
    ("2001:db8::1", "not a plain IPv4"),
    ("192.0.2.010", "not a valid"),
    ("", "not a valid"),
])
def test_device_ip_must_be_plain_ipv4(tmp_path, adguard, ip, msg):
    with pytest.raises(ConfigError, match=msg):
        build(tmp_path, group_yaml(adguard, devices=f'\n      - {{id: x, name: X, ip: "{ip}"}}'))


def test_device_group_validation(tmp_path, adguard):
    with pytest.raises(ConfigError, match="only 'adguard'"):
        build(tmp_path, group_yaml(adguard).replace("integration: adguard", "integration: pihole"))
    with pytest.raises(ConfigError, match="unknown keys"):
        build(tmp_path, group_yaml(adguard, extra_group="\n    color: red"))
    with pytest.raises(ConfigError, match="duplicate"):
        build(tmp_path, group_yaml(adguard, devices="""
      - {id: tv, name: A, ip: 192.0.2.10}
      - {id: tv, name: B, ip: 192.0.2.11}"""))
    with pytest.raises(ConfigError, match="non-empty"):
        build(tmp_path, group_yaml(adguard, devices=" []"))
    with pytest.raises(ConfigError, match="needs plugins.adguard"):
        build(tmp_path, group_yaml(adguard).replace("integrations:", "other:"))


def test_uses_lists_the_integration_of_every_button(tmp_path, httpmock):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.sh").write_text("#!/bin/sh\n")
    u = httpmock.url
    cfg, _, _ = build(tmp_path, f"""
api_token: "${{TOP_SWITCH_API_TOKEN}}"
buttons:
  - {{id: a, name: A, action: {{type: http, url: "{u}/a"}}}}
  - {{id: b, name: B, action: {{type: script, script: x.sh}}}}
  - {{id: c, name: C, action: {{type: plugin, plugin: webhook.call, url: "{u}/c"}}, labels: {{on: x}}}}
""")
    assert [b.uses for b in cfg.buttons] == [["http"], ["script"], ["webhook"]]
    with pytest.raises(ConfigError, match="labels"):
        build(tmp_path, f"""
api_token: "${{TOP_SWITCH_API_TOKEN}}"
buttons:
  - {{id: a, name: A, action: {{type: http, url: "{u}/a"}}, labels: [x]}}
""")


# -- switching

def test_device_switch_blocks_and_unblocks_only_its_device(tmp_path, adguard):
    _, _, core = build(tmp_path, group_yaml(adguard))
    assert core.press("tv", "ui", "127.0.0.1", "on").ok
    assert adguard.rules == [R10]
    by, groups = view(core)
    assert (by["tv"]["status"]["state"], by["tablet"]["status"]["state"]) == ("on", "off")
    assert groups["kids"]["status"] == {"state": "partial", "on": 1, "total": 2}
    assert core.press("tv", "ui", "127.0.0.1", "off").ok
    assert adguard.rules == []


def test_master_switch_presses_every_member_and_skips_those_already_there(tmp_path, adguard):
    _, _, core = build(tmp_path, group_yaml(adguard))
    core.press("tv", "ui", None, "on")
    calls = len(adguard.set_calls)
    r = core.press_group("kids", "ui", "127.0.0.1")  # partial -> on
    assert r.ok and r.body["desired"] == "on"
    assert r.body["results"]["tv"] == {"ok": True, "skipped": True}
    assert sorted(adguard.rules) == sorted([R10, R11])
    assert len(adguard.set_calls) == calls + 1  # only the tablet was written
    assert view(core)[1]["kids"]["status"]["state"] == "on"
    r = core.press_group("kids", "ui", "127.0.0.1")  # on -> off
    assert r.ok and r.body["desired"] == "off" and adguard.rules == []


def test_master_switch_never_removes_a_manual_rule(tmp_path, adguard):
    adguard.rules = ["# mine", R11]
    _, _, core = build(tmp_path, group_yaml(adguard))
    assert core.press_group("kids", "ui", None, "on").ok
    r = core.press_group("kids", "ui", None, "off")
    assert r.ok and r.body["results"]["tablet"] == {"ok": True, "skipped": True, "manual": True}
    assert any("χειροκίνητα" in w for w in r.body["warnings"])
    assert adguard.rules == ["# mine", R11]  # tablet's rule was manual: not owned, not removed
    by, _ = view(core)
    assert by["tablet"]["status"]["foreign"] == [R11]


def test_master_switch_reports_member_errors(tmp_path, adguard):
    _, _, core = build(tmp_path, group_yaml(adguard))
    adguard.fail_set = True
    r = core.press_group("kids", "ui", None, "on")
    assert not r.ok and r.http_status == 502 and "Living room TV" in r.body["error"]
    log = [json.loads(line) for line in (tmp_path / "logs" / "top-switch.jsonl").read_text().splitlines()]
    assert log[-1]["group"] == "kids" and log[-1]["result"] == "error"


def test_master_switch_unknown_and_missing(tmp_path, adguard):
    _, _, core = build(tmp_path, group_yaml(adguard))
    adguard.down = True
    with pytest.raises(Conflict):
        core.press_group("kids", "ui", None)
    with pytest.raises(NotFound):
        core.press_group("nope", "ui", None, "on")
    _, _, core2 = build(tmp_path, group_yaml(adguard, extra_group="\n    all_switch: false"))
    with pytest.raises(NotFound):
        core2.press_group("kids", "ui", None, "on")


def test_group_api(tmp_path, adguard, api_factory):
    cfg, _, core = build(tmp_path, group_yaml(adguard))
    api = api_factory(cfg, core)
    code, body = api.req("GET", "/api/buttons")
    assert code == 200 and body["groups"][0]["id"] == "kids"
    assert api.req("POST", "/api/groups/kids", {"desired": "on"})[0] == 200
    assert sorted(adguard.rules) == sorted([R10, R11])
    assert api.req("POST", "/api/groups/kids", {"desired": "maybe"})[0] == 422
    assert api.req("POST", "/api/groups/kids", {"ip": "1.2.3.4"})[0] == 422
    assert api.req("POST", "/api/groups/nope", {})[0] == 404
    assert api.req("GET", "/api/groups/kids")[0] == 405
    assert api.req("POST", "/api/groups/kids", {}, token=None)[0] == 401


# -- hand-over from a removed button (e.g. the old single "reading-time" button)

def test_rules_of_a_removed_button_are_handed_to_the_device_switches(tmp_path, adguard):
    adguard.rules = ["# keep", R10, R11, "/.*/$client=192.0.2.99"]
    state = StateStore(tmp_path / "state.json")
    state.update(lambda d: d["buttons"].__setitem__("reading-time", {
        "owned_rules": [R10, R11, "/.*/$client=192.0.2.99"], "pending_rules": [], "owned_ids": []}))
    _, state, core = build(tmp_path, group_yaml(adguard))
    assert state.button("tv")["owned_rules"] == [R10]
    assert state.button("tablet")["owned_rules"] == [R11]
    assert state.button("reading-time")["owned_rules"] == ["/.*/$client=192.0.2.99"]  # nobody claims it
    by, groups = view(core)
    assert by["tv"]["status"]["foreign"] == [] and groups["kids"]["status"]["state"] == "on"
    assert core.press_group("kids", "ui", None, "off").ok
    assert adguard.rules == ["# keep", "/.*/$client=192.0.2.99"]


def test_hand_over_is_a_no_op_without_orphans(tmp_path, adguard):
    _, state, core = build(tmp_path, group_yaml(adguard))
    assert core.adopt_orphans() == []
    assert state.snapshot()["buttons"] == {}
