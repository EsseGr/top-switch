"""AdGuard Home plugin (API schema: v0.107.79 openapi.yaml).

block/unblock edit only our own lines of the user rules (`/.*/$client=<ip>`) with GET /control/filtering/status
(field user_rules) + POST /control/filtering/set_rules ({rules: [...]}), read-modify-write. Ownership
(`owned_rules`, plus `pending_rules` for a POST whose outcome is unknown) is tracked per button in state.json.
"""
import base64
import ipaddress
import json
import logging
import re
import threading
from abc import ABC, abstractmethod

from ..actions import ActionError, http_request
from ..status import unknown

log = logging.getLogger("top_switch.adguard")

LOST_WARNING = "χάθηκε το state — μπορεί να έχουν μείνει ids μπλοκαρισμένα (δες foreign)"
DISABLED_WARNING = "το φιλτράρισμα του AdGuard είναι ανενεργό: τα user rules δεν εφαρμόζονται"

_MAC = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$|^(?:[0-9a-f]{2}[:-]){7}[0-9a-f]{2}$"
                  r"|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.I)
_CLIENTID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
_IPV4 = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", re.A)


class SelectorError(Exception):
    pass


def classify_id(value):
    """'mac' | 'ip' | 'cidr' | 'clientid' | None (None = not even syntactically an id)."""
    if not isinstance(value, str):
        return None
    if _MAC.match(value):
        return "mac"
    try:
        ipaddress.ip_address(value)
        return "ip"
    except ValueError:
        pass
    if "/" in value:
        try:
            ipaddress.ip_network(value, strict=False)
            return "cidr"
        except ValueError:
            return None
    return "clientid" if _CLIENTID.match(value) else None


def is_plain_ipv4(value):
    """The only form allowed into a rule: canonical dotted IPv4 (no whitespace, newline, zone, leading zeros)."""
    if not isinstance(value, str) or not _IPV4.fullmatch(value):
        return False
    try:
        return str(ipaddress.ip_address(value)) == value
    except ValueError:
        return False


def _rule(ip):
    """The only place a rule line is produced; refuses anything that is not a plain IPv4."""
    if not is_plain_ipv4(ip):
        raise SelectorError(f"id {ip!r} is not a plain IPv4 address; no rule generated")
    return f"/.*/$client={ip}"


def _not_ipv4_reason(value):
    if classify_id(value) == "mac":
        return f"id {value!r} is a MAC address; user rules accept only a plain IPv4 in $client"
    return f"id {value!r} is not a plain IPv4 address (CIDR, IPv6 and ClientID are not supported)"


def _dedupe(items):
    return list(dict.fromkeys(items))


class Selector(ABC):
    """Extension point: decides which client ids a button controls.

    The "devices not on Tailscale" rule is intentionally NOT implemented; it would be another
    Selector subclass returning ids from resolve().
    """

    @abstractmethod
    def resolve(self, adguard):
        """Return a list of plain IPv4 ids or raise SelectorError."""


class StaticSelector(Selector):
    def __init__(self, ids=(), clients=()):
        self.ids, self.clients = list(ids), list(clients)
        self.invalid_reason = None  # set -> only this button is disabled, the service still starts
        if not self.ids and not self.clients:
            raise ValueError("selector needs at least one id or client name")
        for i in self.ids:
            kind = classify_id(i)
            if kind == "mac":
                raise ValueError(f"id {i!r} is a MAC address; AdGuard rules accept only a plain IPv4")
            if kind is None:
                raise ValueError(f"id {i!r} is not a valid IP, CIDR or ClientID")
            if not is_plain_ipv4(i) and self.invalid_reason is None:
                self.invalid_reason = _not_ipv4_reason(i)
                log.warning("selector id %r is not a plain IPv4: the button using it is disabled", i)
        for n in self.clients:
            if not isinstance(n, str) or not n:
                raise ValueError("client names must be non-empty strings")

    def resolve(self, adguard):
        if self.invalid_reason:
            raise SelectorError(f"{self.invalid_reason}; button disabled")
        out = list(self.ids)
        if self.clients:
            by_name = {c["name"]: c for c in adguard.persistent_clients()}
            for name in self.clients:  # expanded on every call, never cached
                client = by_name.get(name)
                if client is None:
                    raise SelectorError(f"persistent client {name!r} not found in AdGuard")
                ids = client.get("ids") or []
                if not ids:
                    raise SelectorError(f"client {name!r} has no id; button disabled")
                bad = [i for i in ids if not is_plain_ipv4(i)]
                if bad:
                    raise SelectorError(f"client {name!r}: {_not_ipv4_reason(bad[0])}; button disabled")
                out.extend(ids)
        return _dedupe(out)


class AdGuard:
    def __init__(self, url, username, password, state, timeout=5):
        self.base = url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.state = state
        self.timeout = timeout
        self._lock = threading.Lock()  # serialises read-modify-write of the user rules

    def _call(self, method, path, body=None):
        headers = dict(self._headers)
        _, raw = http_request(method, self.base + "/control" + path, headers, body, self.timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _clients_doc(self):
        doc = self._call("GET", "/clients")
        if not isinstance(doc, dict):
            raise ActionError("unexpected /clients response")
        return doc

    def persistent_clients(self):
        return self._clients_doc().get("clients") or []

    def _user_rules(self):
        """(user_rules, filtering enabled) from GET /filtering/status; lines are passed through untouched."""
        doc = self._call("GET", "/filtering/status")
        if not isinstance(doc, dict):
            raise ActionError("unexpected /filtering/status response")
        rules = doc.get("user_rules")
        if rules is None:
            rules = []
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise ActionError("unexpected user_rules in /filtering/status response")
        return rules, doc.get("enabled") is not False

    def _set_rules(self, rules):
        self._call("POST", "/filtering/set_rules", {"rules": rules})

    # -- read-only endpoint exposed at GET /api/plugins/adguard/clients
    def clients(self):
        doc = self._clients_doc()
        persistent = []
        for c in doc.get("clients") or []:
            ids = c.get("ids") or []
            problem = None
            if any(classify_id(i) == "mac" for i in ids):
                problem = "έχει id τύπου MAC"
            elif not ids or not all(is_plain_ipv4(i) for i in ids):
                problem = "δεν έχει μόνο απλά IPv4 ids"
            persistent.append({"name": c.get("name"), "ids": ids, "blockable": problem is None, "problem": problem})
        runtime = [{"ip": c.get("ip"), "name": c.get("name"), "source": c.get("source"), "ids": [c.get("ip")]}
                   for c in doc.get("auto_clients") or []]
        return {"persistent": persistent, "runtime": runtime}

    read_endpoints = {"clients": clients}

    # -- state helpers
    def _state_rules(self, button_id, key):
        return list(self.state.button(button_id).get(key) or [])

    def _set_state(self, button_id, **fields):
        def fn(d):
            d["buttons"].setdefault(button_id, {}).update(fields)
        self.state.update(fn)

    def _warnings(self, button_id):
        snap = self.state.snapshot()
        b = snap["buttons"].get(button_id, {})
        out = []
        if snap.get("lost") and not b.get("recovered"):
            out.append(LOST_WARNING)
        if b.get("owned_ids"):
            out.append(f"παλιά owned_ids από το PR #1 ({', '.join(map(str, b['owned_ids']))}) στο disallowed_clients: "
                       "δεν αφαιρούνται αυτόματα — καθάρισέ τα χειροκίνητα στο AdGuard → Settings → DNS → Access settings")
        if b.get("pending_rules"):
            out.append(f"pending κανόνες με άγνωστη κατάσταση ({', '.join(b['pending_rules'])}): έλεγξέ τους "
                       "χειροκίνητα στα user rules· δεν διαγράφονται αυτόματα")
        return out

    @staticmethod
    def _without_last(rules, targets):
        """Copy of rules minus the last occurrence of each target (the one we appended)."""
        out = list(rules)
        for t in targets:
            for idx in range(len(out) - 1, -1, -1):
                if out[idx] == t:
                    del out[idx]
                    break
        return out

    # -- actions
    def block(self, button_id, selector):
        warnings = []
        try:
            with self._lock:
                wanted = [_rule(i) for i in selector.resolve(self)]
                rules, enabled = self._user_rules()
                if not enabled:
                    warnings.append(DISABLED_WARNING)
                owned = self._state_rules(button_id, "owned_rules")
                pending = self._state_rules(button_id, "pending_rules")
                present = set(rules)
                missing = [r for r in wanted if r not in present]  # a manual rule is never added nor owned
                stale = [r for r in owned if r not in wanted and r in present]
                if missing or stale:
                    new_pending = [r for r in missing if r not in owned]
                    if new_pending:
                        try:
                            self._set_state(button_id, pending_rules=_dedupe(pending + new_pending))
                        except OSError as exc:
                            log.error("button %s: pending rules could not be recorded, AdGuard untouched: %s",
                                      button_id, exc)
                            return {"ok": False, "error": "δεν γράφτηκε το state.json — δεν έγινε καμία αλλαγή "
                                                          f"στον AdGuard (αποτυχία εγγραφής: {exc})"}
                    try:
                        self._set_rules(self._without_last(rules, stale) + missing)
                    except ActionError:
                        if new_pending:
                            try:
                                self._set_state(button_id, pending_rules=pending)
                            except OSError as exc:
                                log.error("button %s: set_rules failed and the pending rollback failed too; "
                                          "rules %s stay pending: %s", button_id, new_pending, exc)
                                return {"ok": False, "error": "αποτυχία set_rules και αποτυχία εγγραφής state.json "
                                                              f"({exc}): έμεινε pending κανόνας {', '.join(new_pending)}"}
                        raise
                    try:
                        self._set_state(button_id,
                                        owned_rules=_dedupe([r for r in owned if r not in stale] + missing),
                                        pending_rules=[r for r in pending if r not in missing])
                    except OSError as exc:
                        log.error("button %s: rules %s were set but state.json could not be written: %s",
                                  button_id, missing, exc)
                        return {"ok": False, "error": "οι κανόνες μπήκαν αλλά δεν καταγράφηκαν ως owned "
                                                      f"(αποτυχία εγγραφής state.json: {exc}) — μένουν pending"}
        except (SelectorError, ActionError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "warnings": warnings + self._warnings(button_id)}

    def unblock(self, button_id, selector=None):
        # The selector is deliberately ignored: only the owned rules recorded in state.json are removed.
        try:
            with self._lock:
                owned = self._state_rules(button_id, "owned_rules")
                if not owned:
                    warnings = ["δεν υπάρχουν owned κανόνες στο state.json — δεν αφαιρέθηκε τίποτα"]
                    return {"ok": True, "warnings": warnings + self._warnings(button_id)}
                rules, _ = self._user_rules()
                kept = self._without_last(rules, [r for r in owned if r in rules])
                if kept != rules:
                    self._set_rules(kept)
                try:
                    self._set_state(button_id, owned_rules=[], recovered=True)
                except OSError as exc:
                    log.error("button %s: rules %s were removed but state.json could not be written: %s",
                              button_id, owned, exc)
                    return {"ok": False, "error": "οι κανόνες αφαιρέθηκαν αλλά δεν ενημερώθηκε το state.json "
                                                  f"(αποτυχία εγγραφής: {exc}) — τα owned_rules μένουν ως είχαν"}
        except (SelectorError, ActionError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "warnings": self._warnings(button_id)}

    def status(self, button_id, selector):
        try:
            wanted = [_rule(i) for i in selector.resolve(self)]
            rules, enabled = self._user_rules()
        except (SelectorError, ActionError) as exc:
            return unknown(str(exc))
        present = set(rules)
        blocked = [r for r in wanted if r in present]
        owned = set(self._state_rules(button_id, "owned_rules"))
        if wanted and len(blocked) == len(wanted):
            state = "on"
        elif not blocked:
            state = "off"
        else:
            state = "partial"
        warnings = [] if enabled else [DISABLED_WARNING]
        return {"state": state, "source": "live", "error": None, "blocked": len(blocked), "total": len(wanted),
                "foreign": [r for r in blocked if r not in owned], "warnings": warnings + self._warnings(button_id)}
