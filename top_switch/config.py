"""Config loading: YAML, ${VAR} substitution (env or VAR_FILE), strict validation."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .actions import ActionError, resolve_script
from .plugins.adguard import StaticSelector
from .schedule import parse_segments

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ConfigError(Exception):
    pass


class _Loader(yaml.SafeLoader):
    """YAML 1.1 reads bare on/off/yes/no as booleans, which would break `on:`/`off:` keys."""


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


@dataclass
class Button:
    id: str
    name: str
    icon: str = ""
    confirm: object = False  # False | True | question text
    action: dict = None
    toggle: dict = None  # {"on": spec, "off": spec, "status": spec|None}
    schedule: list = field(default_factory=list)
    group: str = None  # id of a group (e.g. a device group); None = stand-alone
    labels: dict = None  # optional {"on": text, "off": text} shown by the skin instead of on/off
    uses: list = field(default_factory=list)  # integrations this button needs (http, script, webhook, adguard)
    detail: str = None  # short extra text for the skin (a device's IP)


@dataclass
class Group:
    id: str
    name: str
    icon: str = ""
    confirm: object = False
    all_switch: bool = True  # show a master switch that presses every member
    integration: str = None
    members: list = field(default_factory=list)  # button ids, in config order


@dataclass
class Config:
    api_token: str
    insecure: bool
    tz: ZoneInfo
    buttons: list
    plugins: dict
    state_path: Path
    log_path: Path
    scripts_dir: Path
    skins_dir: Path
    skin: str
    groups: list = field(default_factory=list)


def _lookup(name, env):
    if name in env:
        return env[name]
    if env.get(name + "_FILE"):
        try:
            return Path(env[name + "_FILE"]).read_text("utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read {name}_FILE: {exc}") from None
    raise ConfigError(f"missing environment variable ${{{name}}} referenced in the config")


def _subst(node, env):
    if isinstance(node, str):
        return _VAR.sub(lambda m: _lookup(m.group(1), env), node)
    if isinstance(node, list):
        return [_subst(i, env) for i in node]
    if isinstance(node, dict):
        return {k: _subst(v, env) for k, v in node.items()}
    return node


def _check_http(spec, where):
    url = spec.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        raise ConfigError(f"{where}: 'url' must start with http:// or https://")
    spec["method"] = str(spec.get("method", "GET")).upper()
    if spec["method"] not in _METHODS:
        raise ConfigError(f"{where}: unsupported method {spec['method']!r}")
    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise ConfigError(f"{where}: 'headers' must be a mapping")
    spec["headers"] = {str(k): str(v) for k, v in headers.items()}
    if not isinstance(spec.get("body", ""), (str, dict, list)):
        raise ConfigError(f"{where}: 'body' must be a string, mapping or list")


def _selector(spec, where):
    raw = spec.get("selector")
    if not isinstance(raw, dict) or set(raw) - {"ids", "clients"}:
        raise ConfigError(f"{where}: 'selector' must be a mapping with only 'ids' and/or 'clients'")
    try:
        spec["selector"] = StaticSelector(raw.get("ids") or [], raw.get("clients") or [])
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _check_action(spec, where, scripts_dir, plugins, status=False):
    if not isinstance(spec, dict):
        raise ConfigError(f"{where}: must be a mapping")
    spec = dict(spec)
    kind = spec.get("type")
    if status:
        if kind == "http":
            _check_http(spec, where)
            for key in ("json_path", "on_value"):
                if key not in spec:
                    raise ConfigError(f"{where}: status of type http requires '{key}'")
            return spec
        if kind == "plugin" and spec.get("plugin") == "adguard.status":
            _need_adguard(where, plugins)
            _selector(spec, where)
            return spec
        raise ConfigError(f"{where}: status type must be http or plugin adguard.status")
    if kind == "http":
        _check_http(spec, where)
    elif kind == "script":
        try:
            resolve_script(scripts_dir, spec.get("script"))
        except ActionError as exc:
            raise ConfigError(f"{where}: {exc}") from None
    elif kind == "plugin":
        name = spec.get("plugin")
        if name == "webhook.call":
            _check_http(spec, where)
        elif name in ("adguard.block", "adguard.unblock"):
            _need_adguard(where, plugins)
            if name == "adguard.block" or "selector" in spec:
                _selector(spec, where)
        else:
            raise ConfigError(f"{where}: unknown plugin {name!r}")
    else:
        raise ConfigError(f"{where}: type must be http, script or plugin")
    return spec


def _uses(spec):
    if not spec:
        return None
    if spec["type"] in ("http", "script"):
        return spec["type"]
    name = spec.get("plugin", "")
    return "webhook" if name == "webhook.call" else name.split(".", 1)[0]


def _labels(raw, where):
    labels = raw.get("labels")
    if labels is None:
        return None
    if not isinstance(labels, dict) or set(labels) - {"on", "off"} or not all(isinstance(v, str) for v in labels.values()):
        raise ConfigError(f"{where}: 'labels' must be a mapping with text 'on' and/or 'off'")
    return dict(labels)


def _need_adguard(where, plugins):
    cfg = plugins.get("adguard")
    if not isinstance(cfg, dict) or not all(cfg.get(k) for k in ("url", "username", "password")):
        raise ConfigError(f"{where}: needs plugins.adguard with url, username and password")


def _button(raw, scripts_dir, plugins):
    if not isinstance(raw, dict):
        raise ConfigError("each button must be a mapping")
    bid = raw.get("id")
    if not isinstance(bid, str) or not _ID.match(bid):
        raise ConfigError(f"invalid button id {bid!r} (use letters, digits, _ and -)")
    where = f"button {bid}"
    if not raw.get("name"):
        raise ConfigError(f"{where}: 'name' is required")
    if ("action" in raw) == ("toggle" in raw):
        raise ConfigError(f"{where}: exactly one of 'action' or 'toggle' is required")
    confirm = raw.get("confirm", False)
    if not isinstance(confirm, (bool, str)):
        raise ConfigError(f"{where}: 'confirm' must be true or a question text")
    btn = Button(bid, str(raw["name"]), str(raw.get("icon") or ""), confirm)
    if "action" in raw:
        btn.action = _check_action(raw["action"], f"{where}.action", scripts_dir, plugins)
    else:
        t = raw["toggle"]
        if not isinstance(t, dict) or "on" not in t or "off" not in t:
            raise ConfigError(f"{where}: toggle needs 'on' and 'off'")
        btn.toggle = {
            "on": _check_action(t["on"], f"{where}.toggle.on", scripts_dir, plugins),
            "off": _check_action(t["off"], f"{where}.toggle.off", scripts_dir, plugins),
            "status": _check_action(t["status"], f"{where}.toggle.status", scripts_dir, plugins, status=True)
            if t.get("status") else None,
        }
    btn.labels = _labels(raw, where)
    specs = [btn.action] if btn.action else [btn.toggle["on"], btn.toggle["off"], btn.toggle["status"]]
    btn.uses = list(dict.fromkeys(u for u in map(_uses, specs) if u))
    if "schedule" in raw:
        if btn.toggle is None:
            raise ConfigError(f"{where}: schedule requires a toggle")
        try:
            btn.schedule = parse_segments(raw["schedule"])
        except ValueError as exc:
            raise ConfigError(f"{where}: schedule: {exc}") from None
    return btn


_DEVICE_KEYS = {"id", "name", "ip", "icon", "confirm"}
_GROUP_KEYS = {"id", "name", "icon", "confirm", "all_switch", "integration", "labels", "devices"}


def _device_group(raw, plugins):
    """Shorthand: one AdGuard toggle per device + an optional master switch for the group.

    Expands to ordinary buttons (id = device id) so press/status/ownership work exactly like any other
    adguard toggle; the master switch only presses the members (no shared ownership of rules).
    """
    if not isinstance(raw, dict) or set(raw) - _GROUP_KEYS:
        raise ConfigError(f"device group: unknown keys {sorted(set(raw) - _GROUP_KEYS) if isinstance(raw, dict) else raw!r}"
                          f" (allowed: {', '.join(sorted(_GROUP_KEYS))})")
    gid = raw.get("id")
    if not isinstance(gid, str) or not _ID.match(gid):
        raise ConfigError(f"invalid device group id {gid!r} (use letters, digits, _ and -)")
    where = f"device group {gid}"
    if not raw.get("name"):
        raise ConfigError(f"{where}: 'name' is required")
    integration = raw.get("integration", "adguard")
    if integration != "adguard":
        raise ConfigError(f"{where}: integration {integration!r} is not supported (only 'adguard')")
    _need_adguard(where, plugins)
    devices = raw.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ConfigError(f"{where}: 'devices' must be a non-empty list")
    all_switch = raw.get("all_switch", True)
    if not isinstance(all_switch, bool):
        raise ConfigError(f"{where}: 'all_switch' must be true or false")
    confirm = raw.get("confirm", False)
    if not isinstance(confirm, (bool, str)):
        raise ConfigError(f"{where}: 'confirm' must be true or a question text")
    labels = _labels(raw, where) or {"on": "Κομμένο", "off": "Ελεύθερο"}
    group = Group(gid, str(raw["name"]), str(raw.get("icon") or ""), confirm, all_switch, integration)
    buttons = []
    for d in devices:
        if not isinstance(d, dict) or set(d) - _DEVICE_KEYS:
            raise ConfigError(f"{where}: each device is a mapping with {', '.join(sorted(_DEVICE_KEYS))}")
        selector = {"ids": [d.get("ip")]}
        btn = _button({"id": d.get("id"), "name": d.get("name"), "icon": d.get("icon", ""),
                       "confirm": d.get("confirm", False), "labels": labels,
                       "toggle": {"on": {"type": "plugin", "plugin": "adguard.block", "selector": selector},
                                  "off": {"type": "plugin", "plugin": "adguard.unblock"},
                                  "status": {"type": "plugin", "plugin": "adguard.status", "selector": selector}}},
                      None, plugins)
        if btn.toggle["on"]["selector"].invalid_reason:
            raise ConfigError(f"{where}: device {btn.id}: {btn.toggle['on']['selector'].invalid_reason}")
        btn.group = gid
        btn.detail = d["ip"]
        group.members.append(btn.id)
        buttons.append(btn)
    return group, buttons


def load_config(path, env=None):
    env = os.environ if env is None else env
    path = Path(path)
    try:
        raw = yaml.load(path.read_text("utf-8"), Loader=_Loader) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    raw = _subst(raw, env)

    token = str(raw.get("api_token") or "")
    insecure = env.get("TOP_SWITCH_INSECURE_NO_AUTH") == "1"
    if not token and not insecure:
        raise ConfigError("api_token is required (for development only: TOP_SWITCH_INSECURE_NO_AUTH=1)")

    tzname = raw.get("timezone") or env.get("TZ") or "Europe/Athens"
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"unknown timezone {tzname!r}") from None

    base = path.resolve().parent
    scripts_dir = base / raw.get("scripts_dir", "scripts")
    if raw.get("plugins") and raw.get("integrations"):
        raise ConfigError("use either 'integrations' or the older name 'plugins', not both")
    plugins = raw.get("integrations") or raw.get("plugins") or {}
    if not isinstance(plugins, dict) or set(plugins) - {"adguard"}:
        raise ConfigError("integrations: only 'adguard' needs settings (http, webhook and script need none)")
    buttons, seen, groups = [], set(), []
    items = [("button", item) for item in raw.get("buttons") or []]
    items += [("group", item) for item in raw.get("device_groups") or []]
    for kind, item in items:
        if kind == "button":
            new = [_button(item, scripts_dir, plugins)]
        else:
            group, new = _device_group(item, plugins)
            if group.id in {g.id for g in groups}:
                raise ConfigError(f"duplicate device group id {group.id!r}")
            groups.append(group)
        for btn in new:
            if btn.id in seen:
                raise ConfigError(f"duplicate button/device id {btn.id!r}")
            seen.add(btn.id)
            buttons.append(btn)
    return Config(
        api_token=token, insecure=insecure and not token, tz=tz, buttons=buttons, plugins=plugins,
        state_path=base / raw.get("state_file", "state.json"),
        log_path=base / raw.get("log_file", "logs/top-switch.jsonl"),
        scripts_dir=scripts_dir,
        skins_dir=Path(raw["skins_dir"]) if raw.get("skins_dir") else Path(__file__).resolve().parent.parent / "skins",
        skin=str(raw.get("skin", "default")),
        groups=groups,
    )
