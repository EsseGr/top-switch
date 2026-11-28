"""Stdlib HTTP server: /healthz, static skins, and the token-protected /api/*."""
import hmac
import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .core import Conflict, Invalid, NotFound

log = logging.getLogger("top_switch.server")
MAX_REQUEST_BODY = 64 * 1024


def make_server(core, config, host="0.0.0.0", port=8000):
    class Handler(BaseHTTPRequestHandler):
        server_version = "top-switch"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        # -- helpers
        def _send(self, code, payload, headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if config.insecure:
                return True
            header = self.headers.get("Authorization", "")
            scheme, _, token = header.partition(" ")
            return scheme.lower() == "bearer" and hmac.compare_digest(
                token.strip().encode(), config.api_token.encode())

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise Invalid("bad Content-Length") from None
            if length > MAX_REQUEST_BODY:
                raise Invalid("body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise Invalid("body is not valid JSON") from None

        # -- routing
        def _handle(self, method):
            path = unquote(urlsplit(self.path).path)
            try:
                if path == "/healthz" and method == "GET":
                    return self._send(200, {"ok": True})
                if path.startswith("/api/") or path == "/api":
                    return self._api(method, path)
                if method != "GET":
                    return self._send(405, {"error": "method not allowed"}, {"Allow": "GET"})
                return self._static(path)
            except Exception:
                log.exception("unhandled error")
                self._send(500, {"error": "internal error"})

        def _api(self, method, path):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            parts = [p for p in path.split("/") if p][1:]
            try:
                if parts == ["buttons"]:
                    if method != "GET":
                        return self._send(405, {"error": "method not allowed"}, {"Allow": "GET"})
                    buttons = core.buttons_view()
                    return self._send(200, {"insecure": config.insecure, "buttons": buttons,
                                            "groups": core.groups_view(buttons)})
                if len(parts) == 2 and parts[0] == "groups":
                    if method != "POST":
                        return self._send(405, {"error": "method not allowed"}, {"Allow": "POST"})
                    body = self._read_json()
                    if not isinstance(body, dict) or set(body) - {"desired"}:
                        raise Invalid("body must be an object with only 'desired'")
                    if "desired" in body and body["desired"] not in ("on", "off"):
                        raise Invalid("desired must be 'on' or 'off'")
                    result = core.press_group(parts[1], source="ui", ip=self.client_address[0],
                                              desired=body.get("desired"))
                    return self._send(result.http_status, result.body)
                if len(parts) == 2 and parts[0] == "press":
                    if method != "POST":
                        return self._send(405, {"error": "method not allowed"}, {"Allow": "POST"})
                    return self._press(parts[1])
                if len(parts) == 3 and parts[0] == "plugins":
                    if method != "GET":
                        return self._send(405, {"error": "plugins are read-only; use POST /api/press/<id>"},
                                          {"Allow": "GET"})
                    return self._plugin(parts[1], parts[2])
            except NotFound:
                return self._send(404, {"error": "not found"})
            except Conflict as exc:
                return self._send(409, {"error": str(exc)})
            except Invalid as exc:
                return self._send(422, {"error": str(exc)})
            self._send(404, {"error": "not found"})

        def _press(self, button_id):
            body = self._read_json()
            if not isinstance(body, dict) or set(body) - {"desired"}:
                raise Invalid("body must be an object with only 'desired'")
            desired = body.get("desired")
            if "desired" in body and desired not in ("on", "off"):
                raise Invalid("desired must be 'on' or 'off'")
            result = core.press(button_id, source="ui", ip=self.client_address[0], desired=desired)
            self._send(result.http_status, result.body)

        def _plugin(self, plugin, endpoint):
            ag = core.adguard
            fn = ag.read_endpoints.get(endpoint) if plugin == "adguard" and ag else None
            if fn is None:
                raise NotFound(endpoint)
            try:
                self._send(200, fn(ag))
            except Exception as exc:
                self._send(502, {"error": str(exc)})

        def _static(self, path):
            if path == "/":
                return self._send_redirect(f"/skins/{config.skin}/")
            root = config.skins_dir.resolve()
            if not path.startswith("/skins/") or "\x00" in path:
                return self._send(404, {"error": "not found"})
            target = (root / path[len("/skins/"):]).resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.is_relative_to(root) or not target.is_file():
                return self._send(404, {"error": "not found"})
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _send_redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        do_DELETE = do_PATCH = do_PUT

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
