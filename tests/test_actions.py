import json
import os
import stat

import pytest

from conftest import build
from top_switch.config import ConfigError

HOOK = """
api_token: a
scripts_dir: scripts
buttons:
  - id: h
    name: H
    action: {type: plugin, plugin: webhook.call, method: POST, url: "%s/hook", headers: {X-A: "1"}, body: {k: v}}
"""


def script(tmp_path, name, text):
    d = tmp_path / "scripts"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def cfg_with(tmp_path, spec):
    return build(tmp_path, f"api_token: a\nbuttons:\n  - id: s\n    name: S\n    action: {spec}\n")[2]


def test_http_action_sends_method_headers_json_body(tmp_path, httpmock):
    httpmock.routes[("POST", "/hook")] = (200, b"{}")
    core = build(tmp_path, HOOK % httpmock.url)[2]
    assert core.press("h", "ui", None).ok
    method, path, headers, body = httpmock.requests[0]
    assert (method, path, json.loads(body), headers["X-A"]) == ("POST", "/hook", {"k": "v"}, "1")
    assert headers["Content-Type"] == "application/json"


def test_http_non_2xx_and_redirect_are_errors(tmp_path, httpmock):
    httpmock.routes[("POST", "/hook")] = (500, b"")
    core = build(tmp_path, HOOK % httpmock.url)[2]
    assert not core.press("h", "ui", None).ok
    httpmock.routes[("POST", "/hook")] = (302, b"")
    assert not core.press("h", "ui", None).ok


def test_script_runs_and_failure_is_reported(tmp_path):
    out = tmp_path / "out.txt"
    script(tmp_path, "ok.sh", f"#!/bin/sh\necho ran > {out}\n")
    script(tmp_path, "bad.sh", "#!/bin/sh\necho oops >&2\nexit 3\n")
    assert cfg_with(tmp_path, "{type: script, script: ok.sh}").press("s", "ui", None).ok
    assert out.read_text() == "ran\n"
    res = cfg_with(tmp_path, "{type: script, script: bad.sh}").press("s", "ui", None)
    assert not res.ok and "3" in res.body["error"] and "oops" in res.body["error"]


def test_script_timeout(tmp_path):
    script(tmp_path, "slow.sh", "#!/bin/sh\nsleep 5\n")
    res = cfg_with(tmp_path, "{type: script, script: slow.sh, timeout: 0.3}").press("s", "ui", None)
    assert not res.ok and "timed out" in res.body["error"]


def test_script_has_no_shell_and_no_expansion(tmp_path):
    marker = tmp_path / "scripts" / "pwned"  # scripts run with cwd = scripts/
    # a file whose *name* contains shell metacharacters must be run as one literal path
    script(tmp_path, "a;touch pwned $HOME.sh", "#!/bin/sh\nexit 0\n")
    core = cfg_with(tmp_path, '{type: script, script: "a;touch pwned $HOME.sh"}')
    assert core.press("s", "ui", None).ok
    assert not marker.exists()
    # a name that does not exist is just "not found"; nothing goes through a shell
    core = cfg_with(tmp_path, '{type: script, script: "x.sh; touch pwned"}')
    assert not core.press("s", "ui", None).ok
    assert not marker.exists()


def test_script_symlink_escape_is_blocked_at_load_and_at_run(tmp_path):
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\ntouch %s\n" % (tmp_path / "escaped"))
    outside.chmod(0o755)
    (tmp_path / "scripts").mkdir()
    os.symlink(outside, tmp_path / "scripts" / "link.sh")
    with pytest.raises(ConfigError, match="outside"):
        cfg_with(tmp_path, "{type: script, script: link.sh}")
    # swapped for a symlink after the config was loaded: still refused when pressed
    script(tmp_path, "later.sh", "#!/bin/sh\nexit 0\n")
    core = cfg_with(tmp_path, "{type: script, script: later.sh}")
    (tmp_path / "scripts" / "later.sh").unlink()
    os.symlink(outside, tmp_path / "scripts" / "later.sh")
    res = core.press("s", "ui", None)
    assert not res.ok and not (tmp_path / "escaped").exists()


def test_script_missing_file(tmp_path):
    (tmp_path / "scripts").mkdir()
    assert not cfg_with(tmp_path, "{type: script, script: nope.sh}").press("s", "ui", None).ok
