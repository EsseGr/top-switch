import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from top_switch.config import load_config
from top_switch.core import Core
from top_switch.server import make_server
from top_switch.state import StateStore

TOKEN = "test-token"


class _Srv:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class MockHttp:
    """Generic mock: routes[(METHOD, path)] = (status, body_bytes[, delay_seconds])."""

    def __init__(self):
        self.routes, self.requests = {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                route = outer.routes.get((self.command, self.path), (404, b"{}"))
                if len(route) > 2:
                    time.sleep(route[2])
                self.send_response(route[0])
                self.send_header("Content-Length", str(len(route[1])))
                self.end_headers()
                try:
                    self.wfile.write(route[1])
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = _do

        self._srv = _Srv(H)
        self.url = self._srv.url

    def set_json(self, method, path, obj, status=200):
        self.routes[(method, path)] = (status, json.dumps(obj).encode())

    def calls(self, method=None, path=None):
        return [r for r in self.requests if (method is None or r[0] == method) and (path is None or r[1] == path)]

    def close(self):
        self._srv.close()


class MockAdGuard:
    """Follows AdGuard Home v0.107.79 openapi: basic auth, GET /control/filtering/status (user_rules),
    POST /control/filtering/set_rules ({rules: [str]}), /control/clients. There is deliberately no access-list endpoint."""

    USER, PASSWORD = "u", "p"

    def __init__(self):
        self.rules = []  # user_rules, stored and returned verbatim (comments and blank lines included)
        self.filtering_enabled = True
        self.clients = {"clients": [], "auto_clients": [], "supported_tags": ["user_child"]}
        self.down = False
        self.fail_set = False
        self.apply_then = None  # None | "error" | "reset": set_rules is applied, then the reply fails
        self.set_calls = []
        self.unauth = 0
        outer = self
        expected = "Basic " + base64.b64encode(f"{self.USER}:{self.PASSWORD}".encode()).decode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if outer.down:
                    return self._reply(500, {"message": "down"})
                if self.headers.get("Authorization") != expected:
                    outer.unauth += 1
                    return self._reply(401)
                if self.command == "GET" and self.path == "/control/filtering/status":
                    return self._reply(200, {"enabled": outer.filtering_enabled, "interval": 24, "filters": [],
                                             "whitelist_filters": [], "user_rules": outer.rules})
                if self.command == "GET" and self.path == "/control/clients":
                    return self._reply(200, outer.clients)
                if self.command == "POST" and self.path == "/control/filtering/set_rules":
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = None
                    ok = (isinstance(body, dict) and set(body) <= {"rules"} and isinstance(body.get("rules"), list)
                          and all(isinstance(r, str) for r in body["rules"]))
                    if outer.fail_set or not ok:
                        return self._reply(400, {"message": "bad"})
                    outer.set_calls.append(body)
                    outer.rules = body["rules"]
                    if outer.apply_then == "error":  # applied, but the client sees a failure
                        return self._reply(500, {"message": "boom"})
                    if outer.apply_then == "reset":  # applied, connection dropped without a response
                        self.close_connection = True
                        return None
                    return self._reply(200)
                return self._reply(404)

            do_GET = do_POST = _do

        self._srv = _Srv(H)
        self.url = self._srv.url

    def close(self):
        self._srv.close()


@pytest.fixture
def adguard():
    m = MockAdGuard()
    yield m
    m.close()


@pytest.fixture
def httpmock():
    m = MockHttp()
    yield m
    m.close()


def build(tmp_path, yaml_text, env=None):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, "utf-8")
    full_env = {"TOP_SWITCH_API_TOKEN": TOKEN}
    full_env.update(env or {})
    cfg = load_config(path, full_env)
    state = StateStore(cfg.state_path)
    return cfg, state, Core(cfg, state)


def adguard_yaml(mock, selector, extra=""):
    return f"""
api_token: "${{TOP_SWITCH_API_TOKEN}}"
plugins:
  adguard: {{url: "{mock.url}", username: "{mock.USER}", password: "{mock.PASSWORD}"}}
buttons:
  - id: kids
    name: Kids
    icon: "📚"
    confirm: "Sure?"
    toggle:
      on: {{type: plugin, plugin: adguard.block, selector: &sel {selector}}}
      off: {{type: plugin, plugin: adguard.unblock}}
      status: {{type: plugin, plugin: adguard.status, selector: *sel}}
{extra}
"""


class Api:
    def __init__(self, cfg, core, token=TOKEN):
        self.srv = make_server(core, cfg, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.token = token

    def req(self, method, path, body=None, token="default", raw=None):
        token = self.token if token == "default" else token
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload) if resp.headers.get_content_type() == "application/json" else payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def api_factory():
    made = []

    def make(cfg, core, **kw):
        a = Api(cfg, core, **kw)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()
