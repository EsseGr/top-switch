"""Outbound actions: http, script. Everything runs server-side; nothing comes from the client."""
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

MAX_BODY = 1_000_000


class ActionError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never follow redirects: credentials/headers must not leak to another host.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http_request(method, url, headers=None, body=None, timeout=10):
    """Return (status, bytes). Any non-2xx, timeout or network problem raises ActionError."""
    headers = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"
        else:
            data = str(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        raise ActionError(f"HTTP {exc.code}") from None
    except Exception as exc:  # URLError, timeout, connection reset, bad URL ...
        raise ActionError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def run_http(spec):
    http_request(spec.get("method", "GET"), spec["url"], spec.get("headers"), spec.get("body"),
                 spec.get("timeout", 10))


def resolve_script(scripts_dir, name):
    """Resolve a script name strictly inside scripts_dir (blocks '..', absolute paths, symlink escapes)."""
    root = Path(scripts_dir).resolve()
    if not isinstance(name, str) or not name or "\x00" in name or Path(name).is_absolute():
        raise ActionError(f"invalid script name: {name!r}")
    path = (root / name).resolve()
    if path == root or not path.is_relative_to(root):
        raise ActionError(f"script {name!r} is outside the scripts folder")
    return path


def run_script(spec, scripts_dir):
    path = resolve_script(scripts_dir, spec["script"])
    if not path.is_file():
        raise ActionError(f"script {spec['script']!r} not found")
    try:
        proc = subprocess.run([str(path)], shell=False, timeout=spec.get("timeout", 30),
                              cwd=str(Path(scripts_dir).resolve()), stdin=subprocess.DEVNULL,
                              capture_output=True, env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        raise ActionError("script timed out") from None
    except OSError as exc:
        raise ActionError(f"cannot execute script: {exc}") from None
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip()[-200:]
        raise ActionError(f"script exited with {proc.returncode}: {tail}")
