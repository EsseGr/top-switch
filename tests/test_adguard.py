import json

import pytest

from conftest import adguard_yaml, build
from top_switch.config import ConfigError
from top_switch.plugins.adguard import LOST_WARNING, SelectorError, StaticSelector, _rule, classify_id, is_plain_ipv4

SEL = '{ids: ["192.0.2.10", "192.0.2.20"]}'
R10, R20 = "/.*/$client=192.0.2.10", "/.*/$client=192.0.2.20"
MANUAL = "/.*/$client=192.168.1.171"  # added by hand for testing: never owned, never touched
# user rules as typed by a human: comments, blank lines, odd order
HAND_RULES = ["! my notes", "", "||ads.example.invalid^", MANUAL, "", "@@||ok.example.invalid^", "# end"]


def make(tmp_path, adguard, selector=SEL, extra=""):
    return build(tmp_path, adguard_yaml(adguard, selector, extra))


def status(core):
    return core.buttons_view()[0]["status"]


def press(core, desired=None):
    return core.press("kids", "ui", "198.51.100.1", desired)


def press_log(tmp_path):
    return [json.loads(line) for line in (tmp_path / "logs" / "top-switch.jsonl").read_text("utf-8").splitlines()]


def failing_update(state, fail_on):
    """state.update raising OSError on the calls whose 1-based index is in fail_on."""
    real, n = state.update, [0]

    def update(fn):
        n[0] += 1
        if n[0] in fail_on:
            raise OSError("disk full")
        return real(fn)
    state.update = update
    return n


def test_basic_auth_is_sent(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    assert press(core, "on").ok
    assert adguard.unauth == 0


def test_hand_written_rules_survive_block_and_unblock_in_order(tmp_path, adguard):
    adguard.rules = list(HAND_RULES)
    _, _, core = make(tmp_path, adguard)
    assert press(core, "on").ok
    assert adguard.rules == HAND_RULES + [R10, R20]  # theirs untouched, same order, comments and blanks kept
    assert press(core, "off").ok
    assert adguard.rules == HAND_RULES


def test_manual_rule_for_a_selected_ip_is_never_touched(tmp_path, adguard):
    adguard.rules = list(HAND_RULES)
    _, state, core = make(tmp_path, adguard, f'{{ids: ["192.168.1.171", "192.0.2.10"]}}')
    assert status(core)["state"] == "partial"
    assert press(core, "on").ok
    assert adguard.rules == HAND_RULES + [R10]  # the manual rule is not duplicated
    assert state.button("kids")["owned_rules"] == [R10]  # ... and not owned
    assert status(core)["foreign"] == [MANUAL]
    assert press(core, "off").ok
    assert adguard.rules == HAND_RULES


def test_manual_rule_stays_when_only_it_is_selected_and_unblock_has_nothing_owned(tmp_path, adguard):
    adguard.rules = [MANUAL]
    _, _, core = make(tmp_path, adguard, '{ids: ["192.168.1.171"]}')
    assert status(core)["state"] == "on"
    res = press(core, "off")
    assert res.ok and res.body["warnings"]
    assert adguard.rules == [MANUAL] and adguard.set_calls == []
    assert press(core, "on").ok and adguard.set_calls == []
    assert adguard.rules == [MANUAL]


def test_selector_change_removes_stale_owned_rules_and_adds_new(tmp_path, adguard):
    adguard.rules = list(HAND_RULES)
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    _, state2, core2 = build(tmp_path, adguard_yaml(adguard, '{ids: ["192.0.2.20", "192.0.2.77"]}'))
    assert core2.press("kids", "ui", None, "on").ok
    assert adguard.rules == HAND_RULES + [R20, "/.*/$client=192.0.2.77"]
    assert state2.button("kids")["owned_rules"] == [R20, "/.*/$client=192.0.2.77"]
    assert core2.press("kids", "ui", None, "off").ok
    assert adguard.rules == HAND_RULES
    assert state2.button("kids")["owned_rules"] == []


def test_selector_shrink_only_removes_stale_in_one_post(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    _, state2, core2 = build(tmp_path, adguard_yaml(adguard, '{ids: ["192.0.2.10"]}'))
    n = len(adguard.set_calls)
    assert core2.press("kids", "ui", None, "on").ok
    assert len(adguard.set_calls) == n + 1 and adguard.rules == [R10]
    assert state2.button("kids")["owned_rules"] == [R10]


def test_selector_change_before_off_leaves_no_stale_rules(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    _, state2, core2 = build(tmp_path, adguard_yaml(adguard, '{ids: ["192.0.2.77"]}'))
    assert core2.press("kids", "ui", None, "off").ok
    assert adguard.rules == []
    assert state2.button("kids")["owned_rules"] == []


def test_two_consecutive_block_are_idempotent_without_duplicates(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    press(core, "on")
    assert len(adguard.set_calls) == 1  # the second one had nothing to add
    assert adguard.rules == [R10, R20]
    assert state.button("kids")["owned_rules"] == [R10, R20]
    assert state.button("kids").get("pending_rules") == []
    press(core, "off")
    assert adguard.rules == []


def test_block_re_adds_an_owned_rule_that_was_deleted_in_the_ui(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    adguard.rules = [R20]
    assert status(core)["state"] == "partial"
    assert press(core, "on").ok
    assert adguard.rules == [R20, R10] and state.button("kids")["owned_rules"] == [R10, R20]


def test_unblock_without_block_does_nothing(tmp_path, adguard):
    adguard.rules = list(HAND_RULES) + [R10]  # same rule as the selector, added by hand
    _, _, core = make(tmp_path, adguard)
    res = press(core, "off")
    assert res.ok and res.body["warnings"]
    assert adguard.rules == HAND_RULES + [R10]
    assert adguard.set_calls == []


def test_status_on_off_partial_unknown_foreign(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    assert status(core)["state"] == "off"
    adguard.rules = [R10]
    st = status(core)
    assert (st["state"], st["blocked"], st["total"]) == ("partial", 1, 2)
    assert st["foreign"] == [R10]
    adguard.rules = [R10, R20]
    st = status(core)
    assert st["state"] == "on" and st["source"] == "live"
    assert st["foreign"] == [R10, R20]  # not ours
    press(core, "off")  # owns nothing -> nothing removed
    adguard.rules = []
    press(core, "on")
    assert status(core)["foreign"] == []
    adguard.down = True
    st = status(core)
    assert st["state"] == "unknown" and st["error"]


def test_status_ignores_unrelated_rules(tmp_path, adguard):
    adguard.rules = ["! /.*/$client=192.0.2.10", "||x.invalid^$client=192.0.2.10", "/.*/$client=192.0.2.100"]
    _, _, core = make(tmp_path, adguard)
    assert status(core)["state"] == "off"


def test_status_warns_when_filtering_is_disabled(tmp_path, adguard):
    adguard.filtering_enabled = False
    _, _, core = make(tmp_path, adguard)
    assert any("ανενεργό" in w for w in status(core)["warnings"])
    assert any("ανενεργό" in w for w in press(core, "on").body["warnings"])


def test_status_unknown_on_malformed_filtering_status(tmp_path, adguard, httpmock):
    httpmock.set_json("GET", "/control/filtering/status", {"user_rules": "nope"})
    _, _, core = build(tmp_path, adguard_yaml(adguard, SEL).replace(adguard.url, httpmock.url))
    st = status(core)
    assert st["state"] == "unknown" and st["error"]


def test_press_on_partial_goes_on(tmp_path, adguard):
    adguard.rules = [R10]
    _, _, core = make(tmp_path, adguard)
    assert press(core).body["desired"] == "on"
    assert status(core)["state"] == "on"


def test_partial_reaches_the_buttons_api(tmp_path, adguard, api_factory):
    adguard.rules = [R10]
    cfg, _, core = make(tmp_path, adguard)
    code, body = api_factory(cfg, core).req("GET", "/api/buttons")
    st = body["buttons"][0]["status"] if isinstance(body, dict) else body[0]["status"]
    assert code == 200 and (st["state"], st["blocked"], st["total"]) == ("partial", 1, 2)


def test_only_set_rules_body_shape_is_sent(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard)
    press(core, "on")
    assert adguard.set_calls == [{"rules": [R10, R20]}]


def test_failed_set_rules_leaves_no_ownership_and_no_pending(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    adguard.fail_set = True
    res = press(core, "on")
    assert not res.ok
    assert state.button("kids").get("owned_rules", []) == []
    assert state.button("kids").get("pending_rules", []) == []  # rolled back


def test_pending_is_written_before_the_post(tmp_path, adguard, monkeypatch):
    _, state, core = make(tmp_path, adguard)
    seen = []
    orig = core.adguard._set_rules

    def spy(rules):
        seen.append(list(state.button("kids").get("pending_rules", [])))
        orig(rules)
    monkeypatch.setattr(core.adguard, "_set_rules", spy)
    assert press(core, "on").ok
    assert seen == [[R10, R20]]


def test_pending_write_failure_means_adguard_is_untouched(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    failing_update(state, {1})
    res = press(core, "on")
    assert not res.ok and "state.json" in res.body["error"]
    assert adguard.set_calls == [] and adguard.rules == []


def test_post_fails_and_rollback_fails_leaves_pending_and_logs(tmp_path, adguard, caplog):
    adguard.rules = list(HAND_RULES)
    _, state, core = make(tmp_path, adguard)
    adguard.fail_set = True
    failing_update(state, {2})  # 1 = pending write ok, 2 = rollback fails
    with caplog.at_level("ERROR"):
        res = press(core, "on")
    assert not res.ok and "pending" in res.body["error"] and R10 in res.body["error"]
    assert "pending" in caplog.text
    assert state.button("kids")["pending_rules"] == [R10, R20]
    # pending is never promoted nor deleted automatically
    adguard.fail_set = False
    adguard.rules = HAND_RULES + [R10]  # the same rule shows up by hand
    assert press(core, "off").ok
    assert adguard.rules == HAND_RULES + [R10]
    assert any("pending" in w for w in status(core)["warnings"])
    assert state.button("kids")["pending_rules"] == [R10, R20]
    assert press_log(tmp_path)[0]["result"] == "error"


def test_state_write_failure_after_post_returns_clear_error_and_keeps_pending(tmp_path, adguard, caplog):
    _, state, core = make(tmp_path, adguard)
    failing_update(state, {2})  # pending write ok, promote fails
    with caplog.at_level("ERROR"):
        res = press(core, "on")
    assert not res.ok and "δεν καταγράφηκαν ως owned" in res.body["error"]
    assert "could not be written" in caplog.text
    assert adguard.rules == [R10, R20]  # the rules really were set
    assert state.button("kids")["pending_rules"] == [R10, R20]
    assert state.button("kids").get("owned_rules", []) == []
    rec = press_log(tmp_path)[-1]
    assert rec["result"] == "error" and "state.json" in rec["error"]


def test_state_write_failure_on_unblock_returns_clear_error(tmp_path, adguard, caplog):
    adguard.rules = list(HAND_RULES)
    _, state, core = make(tmp_path, adguard)
    assert press(core, "on").ok
    failing_update(state, {1})
    with caplog.at_level("ERROR"):
        res = press(core, "off")
    assert not res.ok and res.http_status == 502
    assert "δεν ενημερώθηκε το state.json" in res.body["error"]
    assert "could not be written" in caplog.text
    assert adguard.rules == HAND_RULES  # the rules really were removed
    rec = press_log(tmp_path)[-1]
    assert rec["result"] == "error" and rec["desired"] == "off" and "state.json" in rec["error"]
    # owned stayed, so a retry is safe: nothing more is removed or sent
    n = len(adguard.set_calls)
    assert press(core, "off").ok
    assert adguard.rules == HAND_RULES and len(adguard.set_calls) == n


def test_off_failure_keeps_owned_rules(tmp_path, adguard):
    _, state, core = make(tmp_path, adguard)
    press(core, "on")
    adguard.fail_set = True
    assert not press(core, "off").ok
    assert state.button("kids")["owned_rules"] == [R10, R20]
    adguard.fail_set = False
    assert press(core, "off").ok and adguard.rules == []


def test_persistent_clients_are_expanded_on_every_press(tmp_path, adguard):
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.50"]}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Tablet"]}')
    press(core, "on")
    assert adguard.rules == ["/.*/$client=192.0.2.50"]
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.51", "192.0.2.52"]}]  # changed in AdGuard
    press(core, "on")
    assert adguard.rules == ["/.*/$client=192.0.2.51", "/.*/$client=192.0.2.52"]  # stale 50 removed
    assert status(core)["total"] == 2


def test_adguard_down_gives_unknown_and_next_press_retries(tmp_path, adguard):
    adguard.clients["clients"] = [{"name": "Tablet", "ids": ["192.0.2.50"]}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Tablet"]}')
    adguard.down = True
    assert status(core)["state"] == "unknown"
    assert not press(core, "on").ok
    adguard.down = False
    assert press(core, "on").ok
    assert adguard.rules == ["/.*/$client=192.0.2.50"]


@pytest.mark.parametrize("ids", [
    ["aa:bb:cc:dd:ee:ff"],
    ["192.0.2.50", "aa:bb:cc:dd:ee:ff"],  # MAC even next to a permitted id
    ["192.0.2.50", "kid-tablet"],  # ClientID
    ["192.0.2.0/28"],
    ["2001:db8::1"],
    [],
])
def test_persistent_client_without_plain_ipv4_ids_disables_the_whole_button(tmp_path, adguard, ids):
    adguard.clients["clients"] = [{"name": "Good", "ids": ["192.0.2.60"]}, {"name": "Bad", "ids": ids}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Good", "Bad"]}')
    res = press(core, "on")
    assert not res.ok and "Bad" in res.body["error"]
    assert adguard.rules == [] and adguard.set_calls == []  # never a partial block
    st = status(core)
    assert st["state"] == "unknown" and "Bad" in st["error"]


def test_missing_persistent_client_is_an_error(tmp_path, adguard):
    _, _, core = make(tmp_path, adguard, '{clients: ["Nope"]}')
    assert not press(core, "on").ok


def test_static_mac_is_rejected_at_load(tmp_path, adguard):
    with pytest.raises(ConfigError, match="MAC"):
        make(tmp_path, adguard, '{ids: ["aa:bb:cc:dd:ee:ff"]}')


def test_static_garbage_id_and_empty_selector_rejected(tmp_path, adguard):
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, '{ids: ["not valid!"]}')
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, "{}")
    with pytest.raises(ConfigError):
        make(tmp_path, adguard, '{ids: ["192.0.2.1"], type: tailscale}')


def test_clientid_in_config_loads_and_disables_only_that_button(tmp_path, adguard, caplog):
    extra = """
  - id: other
    name: Other
    toggle:
      on: {type: plugin, plugin: adguard.block, selector: {ids: ["192.0.2.30"]}}
      off: {type: plugin, plugin: adguard.unblock}
"""
    with caplog.at_level("WARNING"):
        _, _, core = make(tmp_path, adguard, '{ids: ["192.0.2.10", "my-client-id"]}', extra)
    assert "my-client-id" in caplog.text  # startup warning
    st = status(core)
    assert st["state"] == "unknown" and "my-client-id" in st["error"]
    res = press(core, "on")
    assert not res.ok and "my-client-id" in res.body["error"]
    assert adguard.rules == [] and adguard.set_calls == []
    assert core.press("other", "ui", None, "on").ok  # the other buttons keep working
    assert adguard.rules == ["/.*/$client=192.0.2.30"]


@pytest.mark.parametrize("bad", [
    "aa:bb:cc:dd:ee:ff", "192.0.2.0/28", "2001:db8::1", "my-client-id", "1.2.3.4\n/.*/$client=x",
    "010.0.0.1", " 1.2.3.4", "1.2.3.4 ", "1.2.3.4\r", "1.2.3.4\n", "1.2.3", "256.1.1.1", "١.٢.٣.٤", "", None, 5,
    "fe80::1%eth0", "1.2.3.4,5.6.7.8",
])
def test_non_plain_ipv4_never_produces_a_rule(bad, adguard):
    assert not is_plain_ipv4(bad)
    with pytest.raises(SelectorError):
        _rule(bad)


@pytest.mark.parametrize("bad", ["192.0.2.0/28", "2001:db8::1", "my-client-id", "010.0.0.1", "1.2.3.4\n/.*/$client=x"])
def test_static_selector_never_resolves_non_ipv4(bad):
    try:
        sel = StaticSelector([bad])
    except ValueError:
        return  # rejected at load
    with pytest.raises(SelectorError):
        sel.resolve(None)


def test_plain_ipv4_is_accepted():
    assert _rule("192.0.2.10") == R10


def test_injected_persistent_client_id_never_reaches_adguard(tmp_path, adguard):
    adguard.clients["clients"] = [{"name": "Evil", "ids": ["192.0.2.5\n/.*/$client=192.0.2.6"]}]
    _, _, core = make(tmp_path, adguard, '{clients: ["Evil"]}')
    assert not press(core, "on").ok
    assert adguard.set_calls == [] and adguard.rules == []


def test_classify_id():
    assert classify_id("192.0.2.1") == "ip"
    assert classify_id("2001:db8::1") == "ip"
    assert classify_id("192.0.2.0/24") == "cidr"
    assert classify_id("my-client") == "clientid"
    for mac in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff"):
        assert classify_id(mac) == "mac"
    assert classify_id("-bad-") is None


def test_legacy_owned_ids_warn_and_are_never_touched(tmp_path, adguard):
    legacy = {"version": 1, "lost": False, "schedule": {}, "buttons": {"kids": {"owned_ids": ["192.0.2.10"]}}}
    (tmp_path / "state.json").write_text(json.dumps(legacy))
    _, state, core = make(tmp_path, adguard)
    assert any("PR #1" in w and "192.0.2.10" in w for w in status(core)["warnings"])
    assert any("PR #1" in w for w in press(core, "on").body["warnings"])
    assert any("PR #1" in w for w in press(core, "off").body["warnings"])
    assert state.button("kids")["owned_ids"] == ["192.0.2.10"]


def test_lost_state_warns_until_next_successful_off(tmp_path, adguard):
    (tmp_path / "state.json").write_text("{not json")
    _, state, core = make(tmp_path, adguard)
    assert list(tmp_path.glob("state.json.corrupt-*"))
    assert LOST_WARNING in status(core)["warnings"]
    adguard.rules = [R10]
    assert status(core)["foreign"] == [R10]
    assert press(core, "off").body["warnings"]  # nothing owned: nothing removed
    assert adguard.rules == [R10]
    assert LOST_WARNING in status(core)["warnings"]
    adguard.rules = []
    press(core, "on")
    press(core, "off")
    assert LOST_WARNING not in status(core)["warnings"]


def test_plugin_clients_endpoint_lists_runtime_and_persistent(tmp_path, adguard, api_factory):
    adguard.clients["clients"] = [{"name": "A", "ids": ["192.0.2.1"]}, {"name": "B", "ids": ["aa:bb:cc:dd:ee:ff"]},
                                  {"name": "C", "ids": ["kid"]}]
    adguard.clients["auto_clients"] = [{"ip": "192.0.2.9", "name": "phone", "source": "ARP"}]
    cfg, _, core = make(tmp_path, adguard)
    code, body = api_factory(cfg, core).req("GET", "/api/plugins/adguard/clients")
    assert code == 200
    assert [c["blockable"] for c in body["persistent"]] == [True, False, False]
    assert body["runtime"][0]["ids"] == ["192.0.2.9"]
