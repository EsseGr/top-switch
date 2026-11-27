"""state.json: one shared lock for every write, atomic replace, corrupt-file quarantine."""
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("top_switch.state")


def _empty():
    return {"version": 1, "lost": False, "buttons": {}, "schedule": {}}


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()  # the only lock guarding state.json writes
        self._data = self._load()

    def _load(self):
        if not self.path.exists():
            return _empty()
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state root is not an object")
        except (ValueError, OSError) as exc:
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            dest = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            log.error("state file %s is corrupt (%s); moved to %s, starting empty", self.path, exc, dest)
            try:
                os.replace(self.path, dest)
            except OSError:
                log.exception("could not rename corrupt state file")
            data = _empty()
            data["lost"] = True
            self._write(data)
            return data
        for key, default in _empty().items():
            data.setdefault(key, default)
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def update(self, fn):
        """Apply fn(data) to a copy, persist it atomically, then publish it."""
        with self._lock:
            new = copy.deepcopy(self._data)
            fn(new)
            self._write(new)
            self._data = new

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def button(self, button_id):
        return self.snapshot()["buttons"].get(button_id, {})

    def schedule_key(self, button_id):
        return self.snapshot()["schedule"].get(button_id)

    def set_schedule_key(self, button_id, key):
        self.update(lambda d: d["schedule"].__setitem__(button_id, key))

    def record_press(self, button_id, at, source):
        def fn(d):
            d["buttons"].setdefault(button_id, {})["last_pressed"] = {"at": at, "source": source}
        self.update(fn)
