from pathlib import Path

import pytest

from top_switch.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
ENV = {"TOP_SWITCH_API_TOKEN": "t", "ADGUARD_URL": "http://x", "ADGUARD_USERNAME": "u", "ADGUARD_PASSWORD": "p"}


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, "utf-8")
    return p


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yaml", ENV)
    b = cfg.buttons[0]
    assert b.id == "reading-time" and b.toggle["status"] and len(b.schedule) == 1
    assert str(cfg.tz) == "Europe/Athens"


@pytest.mark.parametrize("missing", ["ADGUARD_URL", "ADGUARD_PASSWORD", "TOP_SWITCH_API_TOKEN"])
def test_missing_variable_fails_with_clear_message(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(ROOT / "config.example.yaml", env)


def test_var_file_fallback(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("filetoken\n")
    p = write(tmp_path, 'api_token: "${X}"\n')
    assert load_config(p, {"X_FILE": str(secret)}).api_token == "filetoken"


def test_timezone_from_env_and_default(tmp_path):
    p = write(tmp_path, 'api_token: a\n')
    assert str(load_config(p, {"TZ": "UTC"}).tz) == "UTC"
    assert str(load_config(p, {}).tz) == "Europe/Athens"
    with pytest.raises(ConfigError, match="timezone"):
        load_config(write(tmp_path, "api_token: a\ntimezone: Nope/Nowhere\n"), {})


def body(extra):
    return "api_token: a\nbuttons:\n  - id: b\n    name: B\n" + extra


@pytest.mark.parametrize("script", ["../evil.sh", "sub/../../evil.sh", "/bin/sh", ""])
def test_script_path_outside_scripts_rejected(tmp_path, script):
    p = write(tmp_path, body(f'    action: {{type: script, script: "{script}"}}\n'))
    with pytest.raises(ConfigError):
        load_config(p, {})


def test_script_inside_scripts_accepted_even_if_not_there_yet(tmp_path):
    p = write(tmp_path, body('    action: {type: script, script: ok.sh}\n'))
    assert load_config(p, {}).buttons[0].action["script"] == "ok.sh"


@pytest.mark.parametrize("extra", [
    "    action: {type: http, url: 'ftp://x'}\n",
    "    action: {type: nope}\n",
    "    action: {type: http, url: 'http://x'}\n    toggle: {on: {}, off: {}}\n",
    "    toggle: {on: {type: http, url: 'http://x'}}\n",
    "    action: {type: http, url: 'http://x'}\n    schedule: [{on: '17:00', off: '18:00'}]\n",  # schedule needs toggle
    "    action: {type: http, url: 'http://x'}\n    confirm: 5\n",
    "    action: {type: plugin, plugin: adguard.block, selector: {ids: ['192.0.2.1']}}\n",  # no plugins.adguard
    "    toggle: {on: {type: http, url: 'http://x'}, off: {type: http, url: 'http://x'}, status: {type: http, url: 'http://x'}}\n",
])
def test_invalid_buttons_rejected(tmp_path, extra):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body(extra)), {})


def test_duplicate_ids_rejected(tmp_path):
    p = write(tmp_path, "api_token: a\nbuttons:\n"
                        "  - {id: b, name: B, action: {type: http, url: 'http://x'}}\n"
                        "  - {id: b, name: C, action: {type: http, url: 'http://x'}}\n")
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p, {})
