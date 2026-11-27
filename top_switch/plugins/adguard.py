"""AdGuard Home plugin (API schema: v0.107.79 openapi.yaml).

block/unblock edit only our own entries of `disallowed_clients` via GET /control/access/list +
POST /control/access/set (read-modify-write); ownership is tracked per button in state.json.
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

_MAC = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$|^(?:[0-9a-f]{2}[:-]){7}[0-9a-f]{2}$"
                  r"|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.I)
_CLIENTID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)


class SelectorError(Exception):
    pass


def classify_id(value):
    """'mac' | 'ip' | 'cidr' | 'clientid' | None (None = not usable)."""
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


def norm(value):
    """Comparison form (AdGuard may canonicalise what we send)."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return str(value).lower()


def _dedupe(ids):
    seen, out = set(), []
    for i in ids:
        if norm(i) not in seen:
            seen.add(norm(i))
            out.append(i)
    return out


class Selector(ABC):
    """Extension point: decides which client ids a button controls.

    The "devices not on Tailscale" rule is intentionally NOT implemented; it would be another
    Selector subclass returning ids from resolve().
    """

    @abstractmethod
    def resolve(self, adguard):
        """Return a list of permitted ids (IP, CIDR, ClientID) or raise SelectorError."""


class StaticSelector(Selector):
    def __init__(self, ids=(), clients=()):
        self.ids, self.clients = list(ids), list(clients)
        if not self.ids and not self.clients:
            raise ValueError("selector needs at least one id or client name")
        for i in self.ids:
            kind = classify_id(i)
            if kind == "mac":
                raise ValueError(f"id {i!r} is a MAC address; AdGuard accepts only IP, CIDR or ClientID")
            if kind is None:
                raise ValueError(f"id {i!r} is not a valid IP, CIDR or ClientID")
        for n in self.clients:
            if not isinstance(n, str) or not n:
                raise ValueError("client names must be non-empty strings")

    def resolve(self, adguard):
        out = list(self.ids)
        if self.clients:
            by_name = {c["name"]: c for c in adguard.persistent_clients()}
            for name in self.clients:  # expanded on every call, never cached
                client = by_name.get(name)
                if client is None:
                    raise SelectorError(f"persistent client {name!r} not found in AdGuard")
                kinds = [(i, classify_id(i)) for i in (client.get("ids") or [])]
                if any(k == "mac" for _, k in kinds):
                    raise SelectorError(f"client {name!r} has a MAC id, which AdGuard cannot block; button disabled")
                usable = [i for i, k in kinds if k]
                if not usable:
                    raise SelectorError(f"client {name!r} has no usable id (IP, CIDR or ClientID); button disabled")
                out.extend(usable)
        return _dedupe(out)


class AdGuard:
    def __init__(self, url, username, password, state, timeout=5):
        self.base = url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.state = state
        self.timeout = timeout
        self._lock = threading.Lock()  # serialises read-modify-write of the access list

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

    def _access(self):
        acl = self._call("GET", "/access/list")
        if not isinstance(acl, dict):
            raise ActionError("unexpected /access/list response")
        return acl

    # -- read-only endpoint exposed at GET /api/plugins/adguard/clients
    def clients(self):
        doc = self._clients_doc()
        persistent = []
        for c in doc.get("clients") or []:
            ids = c.get("ids") or []
            problem = None
            if any(classify_id(i) == "mac" for i in ids):
                problem = "έχει id τύπου MAC"
            elif not any(classify_id(i) for i in ids):
                problem = "δεν έχει επιτρεπτό id"
            persistent.append({"name": c.get("name"), "ids": ids, "blockable": problem is None, "problem": problem})
        runtime = [{"ip": c.get("ip"), "name": c.get("name"), "source": c.get("source"), "ids": [c.get("ip")]}
                   for c in doc.get("auto_clients") or []]
        return {"persistent": persistent, "runtime": runtime}

    read_endpoints = {"clients": clients}

    # -- state helpers
    def _owned(self, button_id):
        return list(self.state.button(button_id).get("owned_ids") or [])

    def _warnings(self, button_id):
        snap = self.state.snapshot()
        if snap.get("lost") and not snap["buttons"].get(button_id, {}).get("recovered"):
            return [LOST_WARNING]
        return []

    def _set_disallowed(self, acl, disallowed):
        body = dict(acl)  # keep allowed_clients, blocked_hosts and any other field untouched
        body["disallowed_clients"] = disallowed
        self._call("POST", "/access/set", body)

    # -- actions
    def block(self, button_id, selector):
        try:
            with self._lock:
                ids = selector.resolve(self)
                acl = self._access()
                current = acl.get("disallowed_clients") or []
                have = {norm(i) for i in current}
                missing = [i for i in ids if norm(i) not in have]
                if missing:
                    self._set_disallowed(acl, list(current) + missing)

                def fn(d):
                    b = d["buttons"].setdefault(button_id, {})
                    b["owned_ids"] = _dedupe(list(b.get("owned_ids") or []) + missing)
                try:
                    self.state.update(fn)
                except OSError as exc:
                    log.error("button %s: ids %s were blocked but state.json could not be written: %s",
                              button_id, missing, exc)
                    return {"ok": False, "error": "τα ids μπλοκαρίστηκαν αλλά δεν καταγράφηκαν ως owned "
                                                  f"(αποτυχία εγγραφής state.json: {exc}) — δες foreign"}
        except (SelectorError, ActionError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "warnings": self._warnings(button_id)}

    def unblock(self, button_id, selector=None):
        try:
            with self._lock:
                owned = self._owned(button_id)
                if not owned:
                    warnings = ["δεν υπάρχουν owned ids στο state.json — δεν αφαιρέθηκε τίποτα"]
                    return {"ok": True, "warnings": warnings + self._warnings(button_id)}
                acl = self._access()
                current = acl.get("disallowed_clients") or []
                mine = {norm(i) for i in owned}
                kept = [i for i in current if norm(i) not in mine]
                if len(kept) != len(current):
                    self._set_disallowed(acl, kept)

                def fn(d):
                    b = d["buttons"].setdefault(button_id, {})
                    b["owned_ids"] = []
                    b["recovered"] = True
                self.state.update(fn)
        except (SelectorError, ActionError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "warnings": []}

    def status(self, button_id, selector):
        try:
            ids = selector.resolve(self)
            blocked_set = {norm(i) for i in self._access().get("disallowed_clients") or []}
        except (SelectorError, ActionError) as exc:
            return unknown(str(exc))
        blocked = [i for i in ids if norm(i) in blocked_set]
        owned = {norm(i) for i in self._owned(button_id)}
        if ids and len(blocked) == len(ids):
            state = "on"
        elif not blocked:
            state = "off"
        else:
            state = "partial"
        return {"state": state, "source": "live", "error": None, "blocked": len(blocked), "total": len(ids),
                "foreign": [i for i in blocked if norm(i) not in owned], "warnings": self._warnings(button_id)}
