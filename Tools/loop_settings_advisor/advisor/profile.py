"""Profils Loop dans Nightscout, avec l'historique des changements.

Loop envoie un nouveau document de profil à chaque modification des réglages
(champ `startDate`). On reconstruit donc, pour n'importe quel instant, les
réglages en vigueur à ce moment-là.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from .config import MGDL_PER_MMOL


def parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_GMT_RE = re.compile(r"^(?:ETC/)?GMT([+-])(\d{1,2}):?(\d{2})?$", re.IGNORECASE)


def parse_tz(name: str | None) -> tzinfo:
    """Fuseau du profil. Loop utilise souvent un décalage fixe (« GMT-0400 »)."""
    if not name:
        return timezone.utc
    m = _GMT_RE.match(name.strip())
    if m:
        sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        off = timedelta(hours=hh, minutes=mm)
        if name.upper().startswith("ETC/"):
            sign = "-" if sign == "+" else "+"  # convention POSIX inversée
        return timezone(off if sign == "+" else -off)
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


@dataclass
class Schedule:
    starts_sec: list[int]
    values: list[float]

    @classmethod
    def from_ns(cls, items: list[dict] | None, scale: float = 1.0) -> "Schedule":
        rows = []
        for it in items or []:
            if "timeAsSeconds" in it:
                sec = int(float(it["timeAsSeconds"]))
            else:
                hh, mm = str(it.get("time", "00:00")).split(":")[:2]
                sec = int(hh) * 3600 + int(mm) * 60
            rows.append((sec, float(it["value"]) * scale))
        rows.sort()
        if not rows or rows[0][0] != 0:
            # Sécurité : un horaire doit commencer à minuit.
            rows.insert(0, (0, rows[-1][1] if rows else 0.0))
        return cls([r[0] for r in rows], [r[1] for r in rows])

    def value_at_sec(self, sec_of_day: float) -> float:
        i = bisect.bisect_right(self.starts_sec, sec_of_day) - 1
        return self.values[max(i, 0)]

    def segment_index(self, sec_of_day: float) -> int:
        return max(bisect.bisect_right(self.starts_sec, sec_of_day) - 1, 0)

    def segments(self) -> list[tuple[int, int, float]]:
        ends = self.starts_sec[1:] + [86400]
        return list(zip(self.starts_sec, ends, self.values))


@dataclass
class ProfileSnapshot:
    start: datetime
    name: str
    tz: tzinfo
    tz_name: str
    units: str  # "mg/dL" ou "mmol"
    basal: Schedule  # U/h
    isf_mgdl: Schedule  # toujours converti en mg/dL/U
    carbratio: Schedule  # g/U
    target_low_mgdl: Schedule
    target_high_mgdl: Schedule
    raw: dict

    def sec_of_day(self, t: datetime) -> float:
        lt = t.astimezone(self.tz)
        return lt.hour * 3600 + lt.minute * 60 + lt.second


def _snapshot(doc: dict) -> ProfileSnapshot | None:
    store = doc.get("store") or {}
    name = doc.get("defaultProfile") or (next(iter(store)) if store else None)
    p = store.get(name) if name else None
    start = parse_time(doc.get("startDate") or doc.get("created_at") or doc.get("mills"))
    if not p or start is None:
        return None
    units = (p.get("units") or doc.get("units") or "mg/dL").lower()
    scale = MGDL_PER_MMOL if units.startswith("mmol") else 1.0
    tz_name = p.get("timezone") or "UTC"
    return ProfileSnapshot(
        start=start, name=name, tz=parse_tz(tz_name), tz_name=tz_name,
        units="mmol" if scale != 1.0 else "mg/dL",
        basal=Schedule.from_ns(p.get("basal")),
        isf_mgdl=Schedule.from_ns(p.get("sens"), scale),
        carbratio=Schedule.from_ns(p.get("carbratio")),
        target_low_mgdl=Schedule.from_ns(p.get("target_low"), scale),
        target_high_mgdl=Schedule.from_ns(p.get("target_high"), scale),
        raw=doc,
    )


class ProfileTimeline:
    def __init__(self, docs: list[dict]):
        snaps = [s for s in (_snapshot(d) for d in docs) if s is not None]
        snaps.sort(key=lambda s: s.start)
        # Retire les doublons consécutifs identiques (Loop renvoie parfois le même profil).
        dedup: list[ProfileSnapshot] = []
        for s in snaps:
            if dedup and _same_settings(dedup[-1], s):
                continue
            dedup.append(s)
        if not dedup:
            raise ValueError("Aucun profil exploitable dans Nightscout")
        self.snapshots = dedup
        self._starts = [s.start for s in dedup]

    def at(self, t: datetime) -> ProfileSnapshot:
        i = bisect.bisect_right(self._starts, t) - 1
        return self.snapshots[max(i, 0)]

    @property
    def current(self) -> ProfileSnapshot:
        return self.snapshots[-1]

    def basal_at(self, t: datetime) -> float:
        p = self.at(t)
        return p.basal.value_at_sec(p.sec_of_day(t))

    def isf_at(self, t: datetime) -> float:
        p = self.at(t)
        return p.isf_mgdl.value_at_sec(p.sec_of_day(t))

    def changes_between(self, a: datetime, b: datetime) -> list[ProfileSnapshot]:
        return [s for s in self.snapshots if a <= s.start < b]


def _same_settings(x: ProfileSnapshot, y: ProfileSnapshot) -> bool:
    return (x.basal == y.basal and x.isf_mgdl == y.isf_mgdl and x.carbratio == y.carbratio
            and x.target_low_mgdl == y.target_low_mgdl and x.target_high_mgdl == y.target_high_mgdl
            and x.tz_name == y.tz_name)
