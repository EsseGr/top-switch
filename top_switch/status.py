"""Live status. Result shape: {"state": on|off|partial|unknown, "source": "live"|None, "error": str|None}."""
import json

from .actions import ActionError, http_request

UNKNOWN = {"state": "unknown", "source": None, "error": None}


def unknown(error=None):
    return {"state": "unknown", "source": None, "error": error}


def _same(a, b):
    # strict about bool vs number (True == 1 in Python)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _walk(doc, path):
    cur = doc
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def http_status(spec):
    try:
        _, raw = http_request(spec.get("method", "GET"), spec["url"], spec.get("headers"),
                              spec.get("body"), spec.get("timeout", 5))
        doc = json.loads(raw)
    except ActionError as exc:
        return unknown(str(exc))
    except ValueError:
        return unknown("response is not JSON")
    try:
        value = _walk(doc, spec["json_path"])
    except KeyError:
        return unknown(f"json_path {spec['json_path']!r} not found")
    if value is None:
        return unknown("value at json_path is null")
    if _same(value, spec["on_value"]):
        state = "on"
    elif "off_value" not in spec or _same(value, spec["off_value"]):
        state = "off"
    else:
        return unknown(f"unexpected value {value!r}")
    return {"state": state, "source": "live", "error": None}
