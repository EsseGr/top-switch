"""Press logic shared by the HTTP API and the scheduler, plus the JSON-lines press log."""
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import actions, status
from .actions import ActionError
from .plugins.adguard import AdGuard

log = logging.getLogger("top_switch.core")

UNKNOWN_MSG = "άγνωστη κατάσταση — διάλεξε on ή off"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


@dataclass
class PressResult:
    ok: bool
    http_status: int
    body: dict


class PressLog:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class Core:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.buttons = {b.id: b for b in config.buttons}
        self.plog = PressLog(config.log_path)
        cfg = config.plugins.get("adguard")
        self.adguard = AdGuard(cfg["url"], cfg["username"], cfg["password"], state) if cfg else None

    # -- status
    def status_of(self, button):
        spec = button.toggle and button.toggle.get("status")
        if not spec:
            return status.unknown()
        try:
            if spec["type"] == "http":
                return status.http_status(spec)
            return self.adguard.status(button.id, spec["selector"])
        except Exception as exc:  # a status probe must never break the listing
            log.exception("status of %s failed", button.id)
            return status.unknown(f"{type(exc).__name__}: {exc}")

    def buttons_view(self):
        out = []
        for b in self.config.buttons:
            st = self.state.button(b.id)
            out.append({
                "id": b.id, "name": b.name, "icon": b.icon, "confirm": b.confirm,
                "toggle": b.toggle is not None,
                "scheduled": bool(b.schedule),
                "status": self.status_of(b),
                "last_pressed": {"at": (st.get("last_pressed") or {}).get("at")} if st.get("last_pressed") else None,
            })
        return out

    # -- actions
    def _run(self, button, spec):
        try:
            if spec["type"] == "http":
                actions.run_http(spec)
            elif spec["type"] == "script":
                actions.run_script(spec, self.config.scripts_dir)
            else:
                name = spec["plugin"]
                if name == "webhook.call":
                    actions.run_http(spec)
                elif name == "adguard.block":
                    return self.adguard.block(button.id, spec["selector"])
                else:
                    return self.adguard.unblock(button.id, spec.get("selector"))
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "warnings": []}

    def press(self, button_id, source, ip, desired=None):
        button = self.buttons.get(button_id)
        if button is None:
            raise NotFound(button_id)
        if desired is not None and desired not in ("on", "off"):
            raise Invalid("desired must be 'on' or 'off'")
        if button.toggle is None:
            if desired is not None:
                raise Invalid("desired is only valid for toggle buttons")
            spec = button.action
        else:
            if desired is None:
                current = self.status_of(button)["state"]
                if current == "unknown":
                    raise Conflict(UNKNOWN_MSG)
                desired = "off" if current == "on" else "on"  # off and partial -> on
            spec = button.toggle[desired]
        result = self._run(button, spec)
        now = datetime.now(timezone.utc).isoformat()
        rec = {"button": button_id, "time": now, "source": source, "ip": ip, "desired": desired,
               "result": "ok" if result["ok"] else "error"}
        if not result["ok"]:
            rec["error"] = result["error"]
        self.plog.write(rec)
        if result["ok"]:
            self.state.record_press(button_id, now, source)
            return PressResult(True, 200, {"ok": True, "desired": desired, "warnings": result.get("warnings", [])})
        return PressResult(False, 502, {"ok": False, "error": result["error"]})
