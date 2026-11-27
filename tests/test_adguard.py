import pytest

from conftest import adguard_yaml, build
from top_switch.config import ConfigError
from top_switch.plugins.adguard import LOST_WARNING, classify_id

SEL = '{ids: ["192.0.2.10", "192.0.2.20"]}'


def make(tmp_path, adguard, selector=SEL, extra=""):
    return build(tmp_path, adguard_yaml(adguard, selector, extra))


def status(core):
    return core.buttons_view()[0]["status"]


def press(core, desired=None):
    return core.press("kids", "ui", "198.51.100.1", desired)


def test_basic_auth_is_sent(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    assert press(core, "on").ok
    assert adguard.unauth == 0


def test_a_manual_entry_survives_off(tmp_path, adguard):
    adguard.acl["disallowed_clients"] = ["192.0.2.99"]
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    assert set(adguard.disallowed) == {"192.0.2.99", "192.0.2.10", "192.0.2.20"}
    press(core, "off")
    assert adguard.disallowed == ["192.0.2.99"]


def test_b_selector_change_between_on_and_off_leaves_no_stale_ids(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    # new config (same state file) with a different selector, then off
    cfg2, state2, core2 = build(tmp_path, adguard_yaml(adguard, '{ids: ["192.0.2.77"]}'))
    assert core2.press("kids", "ui", None, "off").ok
    assert adguard.disallowed == []
    assert state2.button("kids")["owned_ids"] == []


def test_c_two_consecutive_on_are_idempotent_and_off_removes_first_ones(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    press(core, "on")
    assert len(adguard.set_calls) == 1  # second on had nothing to add
    assert sorted(adguard.disallowed) == ["192.0.2.10", "192.0.2.20"]
    assert sorted(state.button("kids")["owned_ids"]) == ["192.0.2.10", "192.0.2.20"]  # union, never emptied
    press(core, "off")
    assert adguard.disallowed == []


def test_second_on_keeps_ownership_of_first_when_selector_grows(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard, '{ids: ["192.0.2.10"]}')
    press(core, "on")
    _, state2, core2 = build(tmp_path, adguard_yaml(adguard, SEL))
    core2.press("kids", "ui", None, "on")
    assert sorted(state2.button("kids")["owned_ids"]) == ["192.0.2.10", "192.0.2.20"]
    core2.press("kids", "ui", None, "off")
    assert adguard.disallowed == []


def test_d_off_without_previous_on_does_nothing(tmp_path, adguard):
    adguard.acl["disallowed_clients"] = ["192.0.2.10"]  # manual, same id as selector
    _, _, core = make(tmp_path, adguard)
    res = press(core, "off")
    assert res.ok and res.body["warnings"]
    assert adguard.disallowed == ["192.0.2.10"]
    assert adguard.set_calls == []


def test_e_status_on_off_partial_unknown_foreign(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    assert status(core)["state"] == "off"
    adguard.acl["disallowed_clients"] = ["192.0.2.10"]
    st = status(core)
    assert (st["state"], st["blocked"], st["total"]) == ("partial", 1, 2)
    assert st["foreign"] == ["192.0.2.10"]
    adguard.acl["disallowed_clients"] = ["192.0.2.10", "192.0.2.20"]
    st = status(core)
    assert st["state"] == "on" and st["source"] == "live"
    assert sorted(st["foreign"]) == ["192.0.2.10", "192.0.2.20"]  # blocked by someone else
    press(core, "off")  # owns nothing -> nothing removed
    adguard.acl["disallowed_clients"] = []
    press(core, "on")
    assert status(core)["foreign"] == []  # ours, not foreign
    adguard.down = True
    st = status(core)
    assert st["state"] == "unknown" and st["error"]


def test_press_on_partial_goes_on(tmp_path, adguard):
    adguard.acl["disallowed_clients"] = ["192.0.2.10"]
    _, _, core = make(tmp_path, adguard)
    assert press(core).body["desired"] == "on"
    assert status(core)["state"] == "on"


def test_f_other_access_list_fields_are_unchanged(tmp_path, adguard):
    adguard.acl = {"allowed_clients": ["203.0.113.5"], "disallowed_clients": ["192.0.2.99"],
                   "blocked_hosts": ["a.invalid", "b.invalid"]}
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    press(core, "off")
    for body in adguard.set_calls:
        assert body["allowed_clients"] == ["203.0.113.5"]
        assert body["blocked_hosts"] == ["a.invalid", "b.invalid"]
    assert adguard.disallowed == ["192.0.2.99"]


def test_failed_set_does_not_record_ownership(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    adguard.fail_set = True
    res = press(core, "on")
    assert not res.ok
    assert state.button("kids").get("owned_ids", []) == []


def test_state_write_failure_after_set_returns_clear_error(tmp_path, adguard, monkeypatch):
    _, state, core = make(tmp_path, adguard)

    def boom(fn):
        raise OSError("disk full")
    monkeypatch.setattr(state, "update", boom)
    res = press(core, "on")
    assert not res.ok
    assert "δεν καταγράφηκαν ως owned" in res.body["error"]
    assert adguard.disallowed  # the ids really were blocked


def test_off_failure_keeps_owned_ids(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    adguard.fail_set = True
    assert not press(core, "off").ok
    assert len(state.button("kids")["owned_ids"]) == 2
    adguard.fail_set = False
    assert press(core, "off").ok and adguard.disallowed == []


def test_persistent_clients_are_expanded_on_every_press(tmp_path, adguard):
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.50"]}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Tablet"]}')
    press(core, "on")
    assert adguard.disallowed == ["192.0.2.50"]
    press(core, "off")
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.51", "kid-tablet"]}]  # changed in AdGuard
    press(core, "on")
    assert adguard.disallowed == ["192.0.2.51", "kid-tablet"]
    assert status(core)["total"] == 2


def test_adguard_down_gives_unknown_and_next_press_retries(tmp_path, adguard):
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.50"]}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Tablet"]}')
    adguard.down = True
    assert status(core)["state"] == "unknown"
    assert not press(core, "on").ok
    adguard.down = False
    assert press(core, "on").ok
    assert adguard.disallowed == ["192.0.2.50"]


@pytest.mark.parametrize("ids", [
    ["aa:bb:cc:dd:ee:ff"],
    ["192.0.2.50", "aa:bb:cc:dd:ee:ff"],  # MAC even next to a permitted id
    [],
])
def test_persistent_client_with_mac_or_no_permitted_id_disables_the_whole_button(tmp_path, adguard, ids):
    adguard.clients["clients"] = [{"name": "Good", "ids": ["192.0.2.60"]}, {"name": "Bad", "ids": ids}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Good", "Bad"]}')
    res = press(core, "on")
    assert not res.ok and "Bad" in res.body["error"]
    assert adguard.disallowed == [] and adguard.set_calls == []  # never a partial block
    st = status(core)
    assert st["state"] == "unknown" and "Bad" in st["error"]


def test_missing_persistent_client_is_an_error(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard, '{clients: ["Nope"]}')
    assert not press(core, "on").ok


def test_static_mac_is_rejected_at_load(tmp_path, adguard):
    with pytest.raises(ConfigError, match="MAC"):
        make(tmp_path, adguard, '{ids: ["aa:bb:cc:dd:ee:ff"]}')


def test_static_invalid_id_and_empty_selector_rejected(tmp_path, adguard):
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, '{ids: ["not valid!"]}')
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, "{}")
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, '{ids: ["192.0.2.1"], type: tailscale}')


def test_classify_id():
    assert classify_id("192.0.2.1") == "ip"
    assert classify_id("2001:db8::1") == "ip"
    assert classify_id("192.0.2.0/24") == "cidr"
    assert classify_id("my-client") == "clientid"
    for mac in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff"):
        assert classify_id(mac) == "mac"
    assert classify_id("-bad-") is None


def test_lost_state_warns_until_next_successful_off(tmp_path, adguard):
    (tmp_path / "state.json").write_text("{not json")
    _, state, core = make(tmp_path, adguard)
    assert list(tmp_path.glob("state.json.corrupt-*"))
    assert LOST_WARNING in status(core)["warnings"]
    adguard.acl["disallowed_clients"] = ["192.0.2.10"]
    assert status(core)["foreign"] == ["192.0.2.10"]
    assert press(core, "off").body["warnings"]  # nothing owned: nothing removed
    assert adguard.disallowed == ["192.0.2.10"]
    assert LOST_WARNING in status(core)["warnings"]
    press(core, "on")
    press(core, "off")
    assert LOST_WARNING not in status(core)["warnings"]


def test_plugin_clients_endpoint_lists_runtime_and_persistent(tmp_path, adguard, api_factory):
    adguard.clients["clients"] = [{"name": "A", "ids": ["192.0.2.1"]}, {"name": "B", "ids": ["aa:bb:cc:dd:ee:ff"]}]
    adguard.clients["auto_clients"] = [{"ip": "192.0.2.9", "name": "phone", "source": "ARP"}]
    cfg, _, core = make(tmp_path, adguard)
    code, body = api_factory(cfg, core).req("GET", "/api/plugins/adguard/clients")
    assert code == 200
    assert [c["blockable"] for c in body["persistent"]] == [True, False]
    assert body["runtime"][0]["ids"] == ["192.0.2.9"]


def test_state_write_failure_on_unblock_returns_clear_error(tmp_path, adguard, monkeypatch):
    _, state, core = make(tmp_path, adguard)
    assert press(core, "on").ok

    def boom(fn):
        raise OSError("disk full")
    monkeypatch.setattr(state, "update", boom)
    res = press(core, "off")
    assert not res.ok and res.http_status == 502
    assert "δεν ενημερώθηκε το state.json" in res.body["error"]
    assert adguard.disallowed == []  # the ids really were unblocked
