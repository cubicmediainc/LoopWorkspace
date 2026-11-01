"""Normalisation des documents Nightscout envoyés par Loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .profile import parse_time


@dataclass
class GlucoseReading:
    time: datetime
    mgdl: float


@dataclass
class TempBasal:
    start: datetime
    end: datetime
    rate: float  # U/h, absolue
    automatic: bool
    suspend: bool
    insulin_type: str | None


@dataclass
class Bolus:
    time: datetime
    units: float
    duration_min: float
    automatic: bool
    insulin_type: str | None


@dataclass
class CarbEntry:
    time: datetime
    grams: float
    absorption_min: float | None


@dataclass
class Override:
    start: datetime
    end: datetime
    name: str
    insulin_scale: float | None


@dataclass
class Dataset:
    glucose: list[GlucoseReading] = field(default_factory=list)
    temp_basals: list[TempBasal] = field(default_factory=list)
    boluses: list[Bolus] = field(default_factory=list)
    carbs: list[CarbEntry] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)
    site_changes: list[datetime] = field(default_factory=list)
    sensor_starts: list[datetime] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _t(doc: dict) -> datetime | None:
    return parse_time(doc.get("timestamp") or doc.get("created_at") or doc.get("mills"))


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_entries(docs: list[dict]) -> list[GlucoseReading]:
    seen: dict[int, GlucoseReading] = {}
    for d in docs:
        if d.get("type", "sgv") != "sgv":
            continue
        v = _f(d.get("sgv"))
        t = parse_time(d.get("date") or d.get("dateString"))
        if v is None or t is None:
            continue
        # Dédoublonnage à la minute (plusieurs téléverseurs possibles).
        key = int(t.timestamp() // 60)
        seen[key] = GlucoseReading(t, v)
    return sorted(seen.values(), key=lambda g: g.time)


def parse_treatments(docs: list[dict], max_override_hours: float = 24.0) -> Dataset:
    ds = Dataset()
    seen_ids: set = set()
    temps: list[TempBasal] = []
    raw_overrides: list[tuple[datetime, float | None, dict]] = []
    for d in docs:
        sid = d.get("syncIdentifier") or d.get("_id")
        et = (d.get("eventType") or "").strip()
        dedup_key = (et, sid) if d.get("syncIdentifier") else (et, d.get("created_at"), d.get("insulin"),
                                                                d.get("rate"), d.get("carbs"))
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)
        t = _t(d)
        if t is None:
            continue
        etl = et.lower()
        if etl == "temp basal":
            dur = _f(d.get("duration"), 0.0) or 0.0
            rate = _f(d.get("absolute"), _f(d.get("rate")))
            if rate is None:
                continue
            temps.append(TempBasal(t, t + timedelta(minutes=dur), rate,
                                   bool(d.get("automatic", True)),
                                   (d.get("reason") or "").lower() == "suspend",
                                   d.get("insulinType")))
        elif _f(d.get("insulin"), 0) and etl in ("correction bolus", "meal bolus", "bolus", "snack bolus",
                                                 "smb", "combo bolus", ""):
            ds.boluses.append(Bolus(t, _f(d["insulin"]), _f(d.get("duration"), 0.0) or 0.0,
                                    bool(d.get("automatic", False)), d.get("insulinType")))
        elif etl == "temporary override":
            dur = _f(d.get("duration"))
            raw_overrides.append((t, dur, d))
        elif etl == "site change":
            ds.site_changes.append(t)
        elif etl in ("sensor start", "sensor change"):
            ds.sensor_starts.append(t)
        elif etl == "temporary target":
            dur = _f(d.get("duration"), 0.0) or 0.0
            ds.overrides.append(Override(t, t + timedelta(minutes=dur), "temporary target", None))

        # Les glucides peuvent être portés par plusieurs types d'événement.
        carbs = _f(d.get("carbs"), 0.0)
        if carbs and carbs > 0:
            absorb = _f(d.get("absorptionTime"))
            ds.carbs.append(CarbEntry(t, carbs, absorb))

    # Basales temporaires : une nouvelle commande annule la précédente.
    temps.sort(key=lambda x: x.start)
    for i, tb in enumerate(temps):
        if i + 1 < len(temps) and temps[i + 1].start < tb.end:
            tb.end = temps[i + 1].start
    ds.temp_basals = [tb for tb in temps if tb.end > tb.start]

    # Overrides : durée absente ou nulle = « indéfini » ; on ferme au prochain override
    # ou après `max_override_hours` (choix prudent, signalé).
    raw_overrides.sort(key=lambda x: x[0])
    for i, (t, dur, d) in enumerate(raw_overrides):
        nxt = raw_overrides[i + 1][0] if i + 1 < len(raw_overrides) else None
        if dur and dur > 0:
            end = t + timedelta(minutes=dur)
        else:
            end = t + timedelta(hours=max_override_hours)
            ds.warnings.append(f"Override sans durée le {t:%Y-%m-%d %H:%M} UTC : borné à {max_override_hours:.0f} h")
        if nxt and nxt < end:
            end = nxt
        ds.overrides.append(Override(t, end, str(d.get("reason") or d.get("notes") or "override"),
                                     _f(d.get("insulinNeedsScaleFactor"))))

    ds.boluses.sort(key=lambda b: b.time)
    ds.carbs.sort(key=lambda c: c.time)
    ds.overrides.sort(key=lambda o: o.start)
    ds.site_changes.sort()
    ds.sensor_starts.sort()
    return ds
