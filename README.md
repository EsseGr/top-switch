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

Schema v0.107.79. Μπλοκάρει με κανόνες ανά συσκευή στα **user rules** του AdGuard, μορφή
`/.*/$client=<ip>` (`GET /control/filtering/status`, πεδίο `user_rules`, και
`POST /control/filtering/set_rules` με σώμα `{"rules": [...]}`, read-modify-write). Δεν χρησιμοποιείται
πια το `disallowed_clients` (στο Pi η συσκευή έπαιρνε 0.0.0.0 και δεν πήγαινε στον εφεδρικό DNS του router).

- **Επιτρεπτά ids:** μόνο απλή IPv4 (π.χ. `192.0.2.10`, χωρίς μηδενικά μπροστά ή κενά). Ο κανόνας
  παράγεται μόνο από αυτή τη μορφή, ποτέ από ελεύθερο κείμενο. MAC → σφάλμα στη φόρτωση του config.
  CIDR, IPv6 και ClientID **δεν ρίχνουν την υπηρεσία**: γράφεται warning στο startup και μόνο το
  συγκεκριμένο κουμπί απενεργοποιείται (status `unknown` με error, block με `ok:false`, δεν στέλνεται τίποτα).
  Persistent clients: όλα τα ids τους πρέπει να είναι απλή IPv4, αλλιώς όλο το κουμπί απενεργοποιείται.
- **Ownership:** το `state.json` κρατά ανά κουμπί `owned_rules` και `pending_rules`. Το `block` προσθέτει
  (στο τέλος) μόνο όσους κανόνες λείπουν και τους καταγράφει ως owned· ένας κανόνας που υπάρχει ήδη
  (π.χ. χειροκίνητος) δεν προστίθεται και **δεν γίνεται owned**. Το `unblock` αφαιρεί μόνο τους owned κανόνες
  (την τελευταία εμφάνιση του καθενός). Όταν αλλάζουν τα ids του selector, το επόμενο block αφαιρεί τους
  owned κανόνες που δεν ισχύουν πια. Όλες οι άλλες γραμμές (σχόλια, κενές, σειρά) περνούν αυτούσιες.
- **Pending:** ο κανόνας γράφεται στο `pending_rules` *πριν* το POST και γίνεται owned μετά την
  επιτυχία. Αν το POST αποτύχει γίνεται επαναφορά· αν αποτύχει και αυτή, επιστρέφεται error και ο κανόνας
  μένει pending. Οι pending κανόνες δεν διαγράφονται ούτε προάγονται αυτόματα· εμφανίζονται ως προειδοποίηση.
- **Αποτυχία εγγραφής `state.json`** (block και unblock): `{ok:false, error}`, log.error και εγγραφή στο
  log πατημάτων (`result: error`).
- **Status:** `on` αν όλα τα ids έχουν κανόνα, `off` αν κανένα, αλλιώς `partial` (το press από partial → on).
  Αποτυχία GET ή selector → `unknown` + error. Στο `foreign` μπαίνουν οι κανόνες που υπάρχουν αλλά δεν είναι
  owned. Αν το φιλτράρισμα του AdGuard είναι ανενεργό, προστίθεται προειδοποίηση.

### Μετάβαση από PR #1

Το PR #1 έγραφε στο `disallowed_clients` και κρατούσε `owned_ids`. Αυτά **δεν αγγίζονται** ούτε
καθαρίζονται αυτόματα. Αν το `state.json` έχει μη κενά `owned_ids`, τα status/block/unblock δείχνουν
προειδοποίηση· καθάρισέ τα χειροκίνητα στο AdGuard → Settings → DNS → Access settings.

Παραδοχές (δεν επιβεβαιώνονται offline): το mock ακολουθεί μόνο το openapi· ο AdGuard μπορεί να
κανονικοποιεί τα user rules· το read-modify-write δεν είναι ατομικό (αλλαγή από το UI ανάμεσα σε GET και POST
μπορεί να χαθεί)· ο κανόνας `$client` με IP δεν καλύπτει συσκευές που αλλάζουν IP (DHCP)· η πρόσβαση στο
host από container μέσω `host-gateway`.

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
