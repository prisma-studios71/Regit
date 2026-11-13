"""Parsing "when" specs (``14m``, ``14:30``, ``@3``, sha prefix) and humanizing times."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

_DUR_RE = re.compile(
    r"^(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m(?:in)?)?(?:(?P<s>\d+)s)?$"
)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_INDEX_RE = re.compile(r"^@(\d+)$")


@dataclass(frozen=True)
class Spec:
    kind: str  # "time" | "index" | "rev"
    value: object


def parse_duration(text: str) -> int | None:
    """``90s``, ``14m``, ``14min``, ``2h30m``, ``1d``, bare ``14`` (minutes) -> seconds."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        return None
    if t.isdigit():
        return int(t) * 60
    m = _DUR_RE.match(t)
    if not m or not any(m.groupdict().values()):
        return None
    g = {k: int(v or 0) for k, v in m.groupdict().items()}
    return g["d"] * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"]


def parse_spec(text: str, now: float | None = None) -> Spec:
    now = time.time() if now is None else now
    t = text.strip()

    m = _INDEX_RE.match(t)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ValueError("snapshot indexes start at @1")
        return Spec("index", n)

    dur = parse_duration(t)
    if dur is not None:
        return Spec("time", now - dur)

    m = _CLOCK_RE.match(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError(f"invalid time of day: {t}")
        base = datetime.fromtimestamp(now)
        when = base.replace(hour=hh, minute=mm, second=59, microsecond=0)
        if when.timestamp() > now:
            when -= timedelta(days=1)
        return Spec("time", when.timestamp())

    try:
        return Spec("time", datetime.fromisoformat(t).timestamp())
    except ValueError:
        pass

    if re.fullmatch(r"[0-9a-fA-F]{4,40}", t):
        return Spec("rev", t)

    raise ValueError(
        f"can't parse '{text}'. Use e.g. 14m, 2h, 1d, 14:30, '2026-11-13 14:30', @3 or a hash"
    )


def ago(ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    d = int(now - ts)
    if d < 45:
        return "just now"
    if d < 3600:
        return f"{max(1, round(d / 60))} min ago"
    if d < 86400:
        h, rem = divmod(d, 3600)
        mins = rem // 60
        return f"{h} h {mins} min ago" if mins and h < 6 else f"{h} h ago"
    days = d // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
