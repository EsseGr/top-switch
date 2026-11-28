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


def _aggregate(states):
    """on = all on, off = all off, partial = mixed, unknown = nothing known."""
    known = [s for s in states if s in ("on", "off", "partial")]
    if not known:
        return "unknown"
    if len(known) == len(states) and all(s == "on" for s in known):
        return "on"
    if all(s == "off" for s in known) and len(known) == len(states):
        return "off"
    return "partial"


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
        self.groups = {g.id: g for g in getattr(config, "groups", [])}
        self.plog = PressLog(config.log_path)
        cfg = config.plugins.get("adguard")
        self.adguard = AdGuard(cfg["url"], cfg["username"], cfg["password"], state) if cfg else None
        self.adopt_orphans()

    # -- ownership hand-over when buttons are renamed/split (e.g. one "reading-time" button → one switch per device)
    def adopt_orphans(self):
        """Owned AdGuard rules of buttons that are no longer in the config are handed to the configured adguard
        toggle whose static ids produce the same rule. Without this a rule would stay in AdGuard as "foreign"
        and could never be switched off from top-switch. Rules nobody claims stay where they are (logged)."""
        snap = self.state.snapshot()["buttons"]
        orphans = {bid: b for bid, b in snap.items()
                   if bid not in self.buttons and (b.get("owned_rules") or b.get("pending_rules"))}
        if not orphans:
            return []
        claim = {}
        for b in self.config.buttons:
            spec = b.toggle and b.toggle.get("on")
            if spec and spec.get("plugin") == "adguard.block":
                for ip in spec["selector"].ids:
                    claim.setdefault(f"/.*/$client={ip}", b.id)
        moves = []

        def fn(d):
            for old, data in orphans.items():
                for key in ("owned_rules", "pending_rules"):
                    keep = []
                    for rule in d["buttons"].get(old, {}).get(key) or []:
                        new = claim.get(rule)
                        if new is None:
                            keep.append(rule)
                            continue
                        target = d["buttons"].setdefault(new, {})
                        target[key] = list(dict.fromkeys((target.get(key) or []) + [rule]))
                        moves.append((rule, old, new, key))
                    d["buttons"].setdefault(old, {})[key] = keep
        try:
            self.state.update(fn)
        except OSError:
            log.exception("could not hand over rules of removed buttons %s", sorted(orphans))
            return []
        for rule, old, new, key in moves:
            log.warning("rule %s (%s) of removed button %s is now owned by %s", rule, key, old, new)
        left = {bid: (self.state.button(bid).get("owned_rules") or []) for bid in orphans}
        for bid, rules in left.items():
            if rules:
                log.warning("removed button %s still owns %s: no configured switch claims them", bid, rules)
        return moves

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
                "group": b.group,
                "detail": b.detail,
                "labels": b.labels,
                "uses": b.uses,
                "status": self.status_of(b),
                "last_pressed": {"at": (st.get("last_pressed") or {}).get("at")} if st.get("last_pressed") else None,
            })
        return out

    def groups_view(self, buttons=None):
        """Groups with an aggregate status computed from the members' (already fetched) statuses."""
        by_id = {b["id"]: b for b in (buttons if buttons is not None else self.buttons_view())}
        out = []
        for g in self.groups.values():
            states = [by_id[m]["status"]["state"] for m in g.members if m in by_id]
            out.append({"id": g.id, "name": g.name, "icon": g.icon, "confirm": g.confirm,
                        "all_switch": g.all_switch, "integration": g.integration, "members": list(g.members),
                        "status": {"state": _aggregate(states), "on": states.count("on"), "total": len(states)}})
        return out

    def press_group(self, group_id, source, ip, desired=None):
        """Master switch: press every member toggle towards one state. Members already there are skipped."""
        group = self.groups.get(group_id)
        if group is None or not group.all_switch:
            raise NotFound(group_id)
        if desired is not None and desired not in ("on", "off"):
            raise Invalid("desired must be 'on' or 'off'")
        statuses = {m: self.status_of(self.buttons[m]) for m in group.members}
        current = {m: st["state"] for m, st in statuses.items()}
        if desired is None:
            agg = _aggregate(list(current.values()))
            if agg == "unknown":
                raise Conflict(UNKNOWN_MSG)
            desired = "off" if agg == "on" else "on"
        results, errors, warnings = {}, [], []
        for m in group.members:
            if current[m] == desired:
                results[m] = {"ok": True, "skipped": True}
                continue
            foreign = statuses[m].get("foreign") or []
            if desired == "off" and foreign and len(foreign) == statuses[m].get("blocked"):
                # blocked only by a rule someone added by hand in AdGuard: never ours to remove
                results[m] = {"ok": True, "skipped": True, "manual": True}
                warnings.append(f"{self.buttons[m].name}: μπλοκαρισμένο χειροκίνητα στο AdGuard — "
                                "το top-switch δεν αφαιρεί κανόνες που δεν έβαλε")
                continue
            r = self.press(m, source=source, ip=ip, desired=desired)
            results[m] = r.body
            if not r.ok:
                errors.append(f"{self.buttons[m].name}: {r.body.get('error')}")
            warnings.extend(r.body.get("warnings") or [])
        now = datetime.now(timezone.utc).isoformat()
        self.plog.write({"group": group_id, "time": now, "source": source, "ip": ip, "desired": desired,
                         "result": "error" if errors else "ok", **({"error": "; ".join(errors)} if errors else {})})
        body = {"ok": not errors, "desired": desired, "results": results,
                "warnings": list(dict.fromkeys(warnings))}
        if errors:
            body["error"] = "; ".join(errors)
        return PressResult(not errors, 200 if not errors else 502, body)

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
        try:
            result = self._run(button, spec)
        except Exception as exc:  # noqa: BLE001 — every press must reach the press log
            log.exception("button %s: unexpected error while running the action", button_id)
            result = {"ok": False, "error": f"απρόσμενο σφάλμα στην ενέργεια: {type(exc).__name__}: {exc}"}
        now = datetime.now(timezone.utc).isoformat()
        rec = {"button": button_id, "time": now, "source": source, "ip": ip, "desired": desired,
               "result": "ok" if result["ok"] else "error"}
        if not result["ok"]:
            rec["error"] = result["error"]
        self.plog.write(rec)
        if result["ok"]:
            try:
                self.state.record_press(button_id, now, source)
            except Exception as exc:  # noqa: BLE001
                log.exception("button %s: the action ran but last_pressed could not be saved", button_id)
                self.plog.write({"button": button_id, "time": now, "source": source, "ip": ip, "desired": desired,
                                 "result": "error", "error": f"state.json: {exc}"})
                return PressResult(False, 502, {"ok": False,
                                                "error": f"η ενέργεια έγινε αλλά δεν αποθηκεύτηκε το state.json: {exc}"})
            return PressResult(True, 200, {"ok": True, "desired": desired, "warnings": result.get("warnings", [])})
        return PressResult(False, 502, {"ok": False, "error": result["error"]})
