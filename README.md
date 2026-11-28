# top-switch

**One switch does one thing.** top-switch is a tiny self-hosted web page with switches and buttons. Each one is
wired, in a YAML config, to an **integration** that does the real work: block a device's internet in AdGuard Home,
call a webhook, send an HTTP request to a smart plug, run a script. The page itself never talks to those services;
the server does, and only to what the config names.

> Ελληνικά: μικρή self-hosted σελίδα με διακόπτες. Κάθε διακόπτης δηλώνει στο config ποια υπηρεσία
> (integration) χρησιμοποιεί, π.χ. AdGuard Home για κόψιμο internet ανά συσκευή, webhook, HTTP ή script.
> Το UI δείχνει δίπλα σε κάθε διακόπτη ποια υπηρεσία χρειάζεται.

```
 phone / browser ──► top-switch (this repo) ──► integration ──► the thing that changes
   tap a switch       checks token + config     adguard          a device loses internet
                                                http / webhook   a lamp, a webhook, an automation
                                                script           anything a local script can do
```

Python 3.11+, standard library + PyYAML. No build step, no database (a small `state.json`).

## What you need behind it

| Integration | Used by | You need running | Settings in `integrations:` |
|---|---|---|---|
| `adguard` | `device_groups`, `adguard.block/unblock/status` | **[AdGuard Home](https://github.com/AdguardTeam/AdGuardHome)** ≥ v0.107 as the DNS server of your network, reachable from top-switch, with an admin user | `url`, `username`, `password` |
| `http` | `type: http` actions and live status | any HTTP endpoint (smart plug, Home Assistant webhook, …) | none |
| `webhook` | `plugin: webhook.call` | any URL that accepts a request | none |
| `script` | `type: script` | an executable file in `scripts/` next to the config | none |

If a switch uses `adguard` and `integrations.adguard` is missing, the server refuses to start and says which switch
needs it. Every switch shows its integration as a small label in the UI (e.g. *AdGuard Home*, *Webhook*).

**AdGuard blocking only works for devices that use AdGuard as their DNS** (set it as the DNS server in your router's
DHCP, and give the devices a fixed IP / DHCP reservation, because rules are per IP).

## Quick start (Docker)

```sh
git clone https://github.com/EsseGr/top-switch.git && cd top-switch
mkdir -p data && cp config.example.yaml data/config.yaml     # then edit data/config.yaml
cp compose.example.yaml compose.yaml
export TOP_SWITCH_API_TOKEN=$(openssl rand -hex 24)          # the password of the page
export ADGUARD_URL=http://host.docker.internal:3080 ADGUARD_USERNAME=admin ADGUARD_PASSWORD=...
docker compose up -d --build
```

Open `http://<host>:8000/#token=<TOP_SWITCH_API_TOKEN>` once on the phone; the token is stored and removed from the
address bar. Add it to the home screen. Without Docker:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TOP_SWITCH_CONFIG=./data/config.yaml .venv/bin/python -m top_switch
```

## Config

See [`config.example.yaml`](config.example.yaml) (fake values only). Keep your real config out of git.
Secrets are written as `${NAME}` and read from the environment (or from a file via `NAME_FILE`); a missing
variable stops the server at startup.

### Device groups (one switch per device)

```yaml
integrations:
  adguard: {url: "${ADGUARD_URL}", username: "${ADGUARD_USERNAME}", password: "${ADGUARD_PASSWORD}"}

device_groups:
  - id: kids
    name: "Kids' internet"
    icon: "📵"
    integration: adguard          # the only supported one today; written out so the config says what it needs
    confirm: "Block every device?" # asked before switching ON (blocking); switching off never asks
    all_switch: true              # master switch for the whole group (default true)
    labels: {on: "Blocked", off: "Free"}   # optional; default Greek «Κομμένο» / «Ελεύθερο»
    devices:
      - {id: tv,     name: "Living room TV", ip: 192.0.2.10, icon: "📺"}
      - {id: tablet, name: Tablet,           ip: 192.0.2.11, confirm: "Block the tablet?"}
```

- One switch per device: **on = blocked**. The state is read live from AdGuard every time.
- The master switch only presses the device switches (members already in the wanted state are skipped). It shows
  *on* when every device is blocked, *off* when none is, otherwise *partial* (tapping partial → all on).
- Devices are only ever the ones in the config: the API never accepts an IP from the client.
- `ip` must be a plain IPv4 address (no MAC, CIDR, IPv6 or ClientID); anything else stops the server at startup.

### Buttons and toggles (any integration)

`buttons:` entries have `id`, `name`, `icon` (name or emoji), `confirm` (`true` or a question), optional `labels`,
and either `action` (a one-shot button) or `toggle: {on, off, status?}`, optionally `schedule`.

- Actions: `http` (method, url, headers, body), `script` (file inside `scripts/`, no shell, no arguments, timeout),
  `plugin` (`adguard.block`, `adguard.unblock`, `webhook.call`).
- Status (live only): `http` with `json_path` (dots), `on_value`, optional `off_value`, or `adguard.status`.
  Failure → `unknown` + `error`. A toggle without status is always `unknown` (the UI then offers both directions).
- Schedule: a list of `{days, on, off}`. If `on > off` the segment crosses midnight and belongs to the day it starts.
  Overlap = error. The scheduler (one tick per minute) applies the state once per segment, so a manual press wins
  until the next segment. Time zone: `timezone` in the config, else `TZ`, else `Europe/Athens`.

`plugins:` is the older name of `integrations:` and still works (not both at once).

## API

Everything under `/api/*` needs `Authorization: Bearer <api_token>`. Open: `/healthz` and the static `/skins/...`.

- `GET /api/buttons` → `{buttons: [...], groups: [...]}`; every button has `uses` (its integrations), `group`,
  `labels`, `detail` (a device's IP) and a live `status`.
- `POST /api/press/<id>` — for a toggle an optional body `{"desired":"on"|"off"}` (anything else → 422).
  Without `desired`: on→off, off→on, partial→on, unknown → 409.
- `POST /api/groups/<id>` — master switch of a device group, same body and rules.
- `GET /api/plugins/adguard/clients` — read-only list of AdGuard clients (to help you write the config).

The client never sends a URL, a command, an IP or a device list.

## Security

- `api_token` is mandatory; without it the server does not start. For development only:
  `TOP_SWITCH_INSECURE_NO_AUTH=1` (warning in the log and `"insecure": true` in `/api/buttons`).
- The page accepts the token from the field or from a `…/#token=…` link (stored in localStorage; the fragment is
  removed at once). On 401 the token is forgotten.
- The server speaks plain HTTP: run it on your LAN/VPN (e.g. Tailscale) or behind a reverse proxy with TLS.
  **Do not publish it on the internet.**
- The logged IP is the connection's address (not `X-Forwarded-For`). Every press is logged (JSON lines):
  switch or group, time, source (`ui`/`schedule`), IP, result.

## AdGuard Home integration — how blocking works

Schema v0.107.79. It blocks with one **user rule** per device, `/.*/$client=<ip>`
(`GET /control/filtering/status` field `user_rules`, then `POST /control/filtering/set_rules` with
`{"rules": [...]}`, read-modify-write). A blocked device gets a blocked answer from AdGuard, so it does not fall back
to a second DNS server. (`disallowed_clients` is not used: with it the device got no answer and used the router's
backup DNS.)

- **Ownership:** `state.json` keeps per switch the rules it added (`owned_rules`, plus `pending_rules` while a write is
  in flight). Block adds only missing rules (at the end) and owns them; a rule that already exists (e.g. you added it
  by hand) is never added nor owned. Unblock removes only owned rules. Every other line (comments, blank lines,
  order) is passed through untouched. Rules someone added by hand show up as *foreign* and are never removed.
- **Renamed or split switches:** at startup, rules owned by a switch that is no longer in the config are handed to
  the configured switch whose IP produces the same rule (e.g. one old "reading time" button → one switch per device),
  so nothing stays blocked forever. Rules nobody claims stay with the old id and are logged.
- **Pending:** the rule is written to `pending_rules` *before* the POST and becomes owned after success. If the POST
  fails it is rolled back; if the rollback fails too, an error is returned and the rule stays pending (shown as a
  warning, never removed or promoted automatically).
- **Status:** `on` if every IP has a rule, `off` if none, otherwise `partial`. GET failure → `unknown` + error.
  A warning is added if AdGuard filtering is disabled.
- Known limits: read-modify-write is not atomic (an edit in the AdGuard UI between GET and POST can be lost); rules
  are per IP, so a device that changes IP (DHCP) escapes; a container reaches AdGuard on the host through
  `host-gateway`.
- Persistent AdGuard clients can be used in `buttons` selectors (`clients: [name]`); all their ids must be plain IPv4.
- Extension point for other ways to pick devices: `Selector.resolve()` in `top_switch/plugins/adguard.py`.

### Upgrading from PR #1

PR #1 wrote to `disallowed_clients` and kept `owned_ids`. Those are **not** touched or cleaned automatically; if
`state.json` has non-empty `owned_ids` a warning is shown — clean them in AdGuard → Settings → DNS → Access settings.

## Skins

A skin is a folder `skins/<name>/` with static files (no build), chosen with `skin:` in the config. It needs only
`GET /api/buttons`, `POST /api/press/<id>` and, for device groups, `POST /api/groups/<id>`. The default skin is
mobile-first, follows the system light/dark theme, refreshes every 30 s and while a switch is busy it is disabled
until the server has read the real state back.

## Tests

```sh
python3 -m pytest -q
```

They run offline against local mock AdGuard/HTTP servers.

## License

[MIT](LICENSE)
