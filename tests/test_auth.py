import pytest

from conftest import TOKEN, build
from top_switch.config import ConfigError, load_config

CFG = """
api_token: "${TOP_SWITCH_API_TOKEN}"
buttons:
  - {id: b, name: B, icon: "x", action: {type: http, url: "http://127.0.0.1:9/x"}}
"""


def test_server_refuses_to_start_without_token(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(CFG)
    with pytest.raises(ConfigError, match="api_token"):
        load_config(p, {"TOP_SWITCH_API_TOKEN": ""})
    with pytest.raises(ConfigError, match="api_token"):
        load_config(p, {"TOP_SWITCH_API_TOKEN": "", "TOP_SWITCH_INSECURE_NO_AUTH": "0"})


def test_insecure_flag_allows_no_token_and_reports_it(tmp_path, api_factory):
    cfg, _, core = build(tmp_path, CFG, {"TOP_SWITCH_API_TOKEN": "", "TOP_SWITCH_INSECURE_NO_AUTH": "1"})
    assert cfg.insecure
    code, body = api_factory(cfg, core).req("GET", "/api/buttons", token=None)
    assert code == 200 and body["insecure"] is True


def test_insecure_flag_is_ignored_when_token_present(tmp_path, api_factory):
    cfg, _, core = build(tmp_path, CFG, {"TOP_SWITCH_INSECURE_NO_AUTH": "1"})
    api = api_factory(cfg, core)
    assert not cfg.insecure
    assert api.req("GET", "/api/buttons", token=None)[0] == 401
    code, body = api.req("GET", "/api/buttons")
    assert code == 200 and body["insecure"] is False


def test_all_api_requires_token(tmp_path, api_factory):
    cfg, _, core = build(tmp_path, CFG)
    api = api_factory(cfg, core)
    for method, path in [("GET", "/api/buttons"), ("POST", "/api/press/b"), ("GET", "/api/plugins/adguard/clients"),
                         ("GET", "/api/nothing"), ("POST", "/api/plugins/adguard/clients"), ("DELETE", "/api/buttons")]:
        assert api.req(method, path, token=None)[0] == 401, (method, path)
        assert api.req(method, path, token="wrong")[0] == 401, (method, path)


def test_open_paths_and_correct_token(tmp_path, api_factory):
    cfg, _, core = build(tmp_path, CFG)
    api = api_factory(cfg, core)
    assert api.req("GET", "/healthz", token=None)[0] == 200
    code, html = api.req("GET", "/skins/default/index.html", token=None)
    assert code == 200 and b"top-switch" in html
    assert api.req("GET", "/api/buttons", token=TOKEN)[0] == 200


@pytest.mark.parametrize("path", ["/skins/../config.yaml", "/skins/%2e%2e/config.yaml", "/skins/default/../../config.yaml",
                                  "/skins/%2e%2e%2fconfig.yaml", "/skins//etc/passwd", "/etc/passwd"])
def test_static_path_traversal_blocked(tmp_path, api_factory, path):
    cfg, _, core = build(tmp_path, CFG)
    (tmp_path / "secret.txt").write_text("secret")
    api = api_factory(cfg, core)
    code, body = api.req("GET", path, token=None)
    assert code == 404 and b"api_token" not in (body if isinstance(body, bytes) else b"")
