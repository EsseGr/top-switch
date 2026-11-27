# top-switch

Μικρή self-hosted υπηρεσία με κουμπιά/διακόπτες που ορίζονται σε config (YAML). Το UI είναι
απλό «skin» πάνω από ένα γενικό API· όλες οι κλήσεις γίνονται από τον server, ποτέ από τον browser.

Stack: Python 3.11, μόνο stdlib (`http.server`, `urllib`, `zoneinfo`) + PyYAML. Δεν χρειάζεται
framework για 4 routes, και κάθε εξάρτηση λιγότερη είναι ένα pin λιγότερο να σπάσει.

## Config

Δες `config.example.yaml` (ψεύτικες τιμές). Το πραγματικό config μένει εκτός git (`.gitignore`).
Μυστικά γράφονται ως `${ΟΝΟΜΑ}` και διαβάζονται από το περιβάλλον (ή από αρχείο μέσω `ΟΝΟΜΑ_FILE`)·
αν λείπει μεταβλητή, ο server δεν ξεκινά.

Κουμπί: `id`, `name`, `icon` (όνομα ή emoji), `confirm` (`true` ή κείμενο), και είτε `action` είτε
`toggle: {on, off, status?}`, προαιρετικά `schedule`.

- Ενέργειες: `http` (method, url, headers, body), `script` (αρχείο μέσα στο `scripts/`, χωρίς shell,
  χωρίς παραμέτρους, με timeout), `plugin` (`adguard.block`, `adguard.unblock`, `webhook.call`).
- Status (μόνο live): `http` με `json_path` (τελείες), `on_value`, προαιρετικά `off_value`, ή `adguard.status`.
  Αποτυχία → `unknown` + `error`. Κουμπί χωρίς status → πάντα `unknown`. Το `last_pressed` είναι μόνο πληροφορία.
- Schedule: λίστα `{days, on, off}`. Αν `on > off` το τμήμα περνά τα μεσάνυχτα και ανήκει στη μέρα που
  ξεκινά. Επικάλυψη = σφάλμα. Ο scheduler (tick κάθε λεπτό) εφαρμόζει την κατάσταση μία φορά ανά τμήμα
  (επιτυχία γράφεται στο `state.json`), οπότε ένα χειροκίνητο πάτημα υπερισχύει μέχρι την αλλαγή τμήματος.
  Η ζώνη ώρας: `timezone` στο config, αλλιώς `TZ`, αλλιώς `Europe/Athens`.

## API

Όλα τα `/api/*` θέλουν `Authorization: Bearer <api_token>`. Ανοιχτά: `/healthz` και τα στατικά `/skins/...`.

- `GET /api/buttons`
- `POST /api/press/<id>` — για toggle προαιρετικό body `{"desired":"on"|"off"}` (άλλη τιμή → 422).
  Χωρίς `desired`: on→off, off→on, partial→on, unknown → 409.
- `GET /api/plugins/adguard/clients` — μόνο ανάγνωση (για να διαλέξεις ids)

Ο client δεν στέλνει ποτέ URL, εντολή ή επιλογή συσκευών.

## Ασφάλεια

- Το `api_token` είναι υποχρεωτικό· χωρίς αυτό ο server δεν ξεκινά. Μόνο για ανάπτυξη:
  `TOP_SWITCH_INSECURE_NO_AUTH=1` (προειδοποίηση στο log και `"insecure": true` στο `/api/buttons`).
- Το skin δέχεται το token από πεδίο ή από link `…/#token=…` (αποθηκεύεται στο localStorage και το
  fragment σβήνεται αμέσως). Σε 401 σβήνει το token.
- Ο server μιλά απλό HTTP (χωρίς TLS): τρέξ' τον σε LAN/Tailscale ή πίσω από reverse proxy.
- Το IP που γράφεται στο log είναι η διεύθυνση της σύνδεσης (όχι `X-Forwarded-For`).

## Plugin AdGuard Home

Δοκιμασμένο ως προς το schema του v0.107.79. Μπλοκάρει μέσω `GET /control/access/list` +
`POST /control/access/set`, αλλάζοντας **μόνο** τα δικά του entries στο `disallowed_clients`
(όλα τα άλλα πεδία μένουν ως έχουν). Ο selector (`ids` και/ή `clients`) μπαίνει στο config.
Επιτρεπτά ids: IP, CIDR, ClientID· **MAC δεν επιτρέπεται** (απόρριψη στη φόρτωση ή, για persistent
clients, απενεργοποίηση του κουμπιού με error στο press/status — ποτέ μερικό μπλοκάρισμα).
Το `state.json` κρατά ανά κουμπί τα `owned_ids`: το `off` αφαιρεί ακριβώς αυτά. Αν το state χαθεί,
το off δεν αφαιρεί τίποτα και εμφανίζεται προειδοποίηση· τα μπλοκαρισμένα ids φαίνονται στο `foreign`.

Παραδοχές (δεν επιβεβαιώνονται offline): η πρόσβαση στο host από container μέσω `host-gateway`·
η συμπεριφορά του `/access/set` σε μη έγκυρες τιμές (αν αποτύχει → error, το state δεν αλλάζει)·
το read-modify-write δεν είναι ατομικό στον AdGuard (αλλαγή ανάμεσα σε GET και POST μπορεί να χαθεί).

**Κανόνας «συσκευές εκτός Tailscale»: stub.** Υπάρχει μόνο το σημείο επέκτασης
(`Selector.resolve()` στο `top_switch/plugins/adguard.py`)· δεν υλοποιείται.

## Skins

Κάθε skin είναι φάκελος `skins/<όνομα>/` με στατικά αρχεία (χωρίς build)· επιλέγεται με `skin:` στο config.
Ένα skin χρειάζεται μόνο `GET /api/buttons` και `POST /api/press/<id>`.

## Τρέξιμο

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
TOP_SWITCH_CONFIG=./data/config.yaml .venv/bin/python -m top_switch
```

Docker: δες `Dockerfile` και `compose.example.yaml` (port 8000, volume `./data` με config, state, scripts, logs).
Το log πατημάτων (JSON lines): κουμπί, ώρα, πηγή (`ui`/`schedule`), IP, αποτέλεσμα.

## Tests

```
python3 -m pytest -q
```

Τρέχουν χωρίς δίκτυο· mock AdGuard/http servers τοπικά.
