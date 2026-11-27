"""Schedule segments and the once-per-segment scheduler."""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

log = logging.getLogger("top_switch.schedule")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEK = 7 * 1440


@dataclass(frozen=True)
class Segment:
    days: frozenset  # weekday numbers of the day the segment STARTS on (mon=0)
    on: time
    off: time

    @property
    def crosses_midnight(self):
        return self.on > self.off


def parse_hhmm(value):
    try:
        h, m = str(value).split(":")
        if len(h) != 2 or len(m) != 2:
            raise ValueError
        return time(int(h), int(m))
    except ValueError:
        raise ValueError(f"invalid time {value!r}, expected HH:MM") from None


def parse_segments(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("schedule must be a non-empty list")
    segments = []
    for item in raw:
        if not isinstance(item, dict) or "on" not in item or "off" not in item:
            raise ValueError("each schedule entry needs 'on' and 'off'")
        extra = set(item) - {"days", "on", "off"}
        if extra:
            raise ValueError(f"unknown schedule keys: {sorted(extra)}")
        days = item.get("days", DAYS)
        if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
            raise ValueError(f"days must be a list of {DAYS}")
        on, off = parse_hhmm(item["on"]), parse_hhmm(item["off"])
        if on == off:
            raise ValueError("on and off must differ")
        segments.append(Segment(frozenset(DAYS.index(d) for d in days), on, off))
    spans = []  # (start, end) in minutes from Monday 00:00, per concrete occurrence
    for s in segments:
        length = (s.off.hour * 60 + s.off.minute - s.on.hour * 60 - s.on.minute) % 1440
        for d in s.days:
            start = d * 1440 + s.on.hour * 60 + s.on.minute
            spans.append((start, start + length))
    for i, (a0, a1) in enumerate(spans):
        for b0, b1 in spans[i + 1:]:
            if any(a0 < b1 + k and b0 + k < a1 for k in (-WEEK, 0, WEEK)):
                raise ValueError("schedule segments overlap")
    return segments


def current_period(segments, tz, now):
    """Return (key, desired) for the on/off period containing `now`, or None.

    key = ISO start (with UTC offset) of the period; it changes exactly when the period changes.
    """
    now_utc = now.astimezone(timezone.utc)
    events = []
    today = now.astimezone(tz).date()
    for back in range(9):
        day = today - timedelta(days=back)
        for s in segments:
            if day.weekday() not in s.days:
                continue
            start = datetime.combine(day, s.on, tzinfo=tz)
            end = datetime.combine(day + timedelta(days=1 if s.crosses_midnight else 0), s.off, tzinfo=tz)
            events.append((start, "on"))
            events.append((end, "off"))
    past = [(dt.astimezone(timezone.utc), kind, dt) for dt, kind in events if dt.astimezone(timezone.utc) <= now_utc]
    if not past:
        return None
    _, kind, dt = max(past, key=lambda e: (e[0], e[1] == "on"))  # at a tie (back-to-back) "on" wins
    return dt.isoformat(), kind


class Scheduler:
    def __init__(self, core, config, state):
        self.core, self.config, self.state = core, config, state
        self._stop = threading.Event()

    def tick(self, now=None):
        now = now or datetime.now(self.config.tz)
        for button in self.config.buttons:
            if not button.schedule:
                continue
            period = current_period(button.schedule, self.config.tz, now)
            if period is None:
                continue
            key, desired = period
            if self.state.schedule_key(button.id) == key:
                continue  # already applied in this period; manual presses win until it changes
            try:
                result = self.core.press(button.id, source="schedule", ip=None, desired=desired)
            except Exception:
                log.exception("schedule: press of %s failed", button.id)
                continue
            if result.ok:
                self.state.set_schedule_key(button.id, key)
            else:
                log.warning("schedule: %s -> %s failed (%s); will retry next tick", button.id, desired,
                            result.body.get("error"))

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick crashed")
            now = datetime.now()
            self._stop.wait(60 - now.second - now.microsecond / 1e6)

    def start(self):
        threading.Thread(target=self.run, name="scheduler", daemon=True).start()

    def stop(self):
        self._stop.set()
