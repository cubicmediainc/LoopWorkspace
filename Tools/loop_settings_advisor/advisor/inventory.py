"""Évaluation de l'historique disponible dans Nightscout (étape b)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from .nightscout import NightscoutClient
from .profile import parse_time

EXPECTED_CGM_PER_DAY = 288  # Dexcom G7 : une lecture toutes les 5 min
EXPECTED_DEVICESTATUS_PER_DAY = 288  # une boucle Loop toutes les 5 min environ


def run_inventory(client: NightscoutClient, days: int = 90) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    out: dict = {"generated_at": now.isoformat(timespec="seconds"), "days_scanned": days}

    out["oldest"] = {
        "entries": _fmt(client.oldest_date("/api/v1/entries/sgv.json", "date", as_millis=True)),
        "treatments": _fmt(client.oldest_date("/api/v1/treatments.json", "created_at", as_millis=False)),
        "devicestatus": _fmt(client.oldest_date("/api/v1/devicestatus.json", "created_at", as_millis=False)),
    }

    entries = client.entries(start, now)
    per_day = Counter()
    for e in entries:
        t = parse_time(e.get("date"))
        if t:
            per_day[t.date()] += 1
    out["entries"] = _daily_summary(per_day, EXPECTED_CGM_PER_DAY, start, now)

    treatments = client.treatments(start, now)
    types = Counter((t.get("eventType") or "?") for t in treatments)
    tdays = Counter(parse_time(t.get("created_at")).date() for t in treatments if parse_time(t.get("created_at")))
    out["treatments"] = {
        "total": len(treatments),
        "by_event_type": dict(types.most_common()),
        "days_with_any": len(tdays),
        "days_with_temp_basal": len({parse_time(t["created_at"]).date() for t in treatments
                                     if t.get("eventType") == "Temp Basal" and parse_time(t.get("created_at"))}),
        "days_with_carbs": len({parse_time(t["created_at"]).date() for t in treatments
                                if (t.get("carbs") or 0) > 0 and parse_time(t.get("created_at"))}),
    }

    # devicestatus : les documents sont volumineux, on se limite aux 14 derniers jours.
    ds_start = now - timedelta(days=min(days, 14))
    statuses = client.devicestatus(ds_start, now)
    sdays = Counter(parse_time(s.get("created_at")).date() for s in statuses if parse_time(s.get("created_at")))
    out["devicestatus"] = _daily_summary(sdays, EXPECTED_DEVICESTATUS_PER_DAY, ds_start, now)
    out["devicestatus"]["has_loop_fields"] = sum(1 for s in statuses if "loop" in s)

    profiles = client.profiles()
    pstarts = sorted(filter(None, (parse_time(p.get("startDate") or p.get("created_at")) for p in profiles)))
    out["profiles"] = {
        "documents": len(profiles),
        "oldest": _fmt(pstarts[0]) if pstarts else None,
        "newest": _fmt(pstarts[-1]) if pstarts else None,
        "changes_in_period": sum(1 for p in pstarts if p >= start),
    }
    return out


def _fmt(t):
    return t.isoformat(timespec="minutes") if t else None


def _daily_summary(per_day: Counter, expected: int, start: datetime, end: datetime) -> dict:
    days = []
    d = start.date()
    while d <= end.date():
        days.append(d)
        d += timedelta(days=1)
    full_days = days[1:-1]  # jours partiels exclus
    coverage = [min(per_day.get(d, 0) / expected, 1.0) for d in full_days]
    return {
        "total": sum(per_day.values()),
        "days_with_data": sum(1 for d in full_days if per_day.get(d, 0) > 0),
        "days_scanned": len(full_days),
        "mean_daily_coverage": round(sum(coverage) / len(coverage), 3) if coverage else None,
        "days_over_90pct": sum(1 for c in coverage if c >= 0.9),
        "days_under_70pct": sum(1 for c in coverage if c < 0.7),
        "worst_days": sorted(((d.isoformat(), per_day.get(d, 0)) for d in full_days), key=lambda x: x[1])[:5],
    }


def format_inventory(inv: dict) -> str:
    e, t, s, p = inv["entries"], inv["treatments"], inv["devicestatus"], inv["profiles"]
    lines = [
        f"# Inventaire Nightscout ({inv['days_scanned']} derniers jours)",
        "",
        "## Données les plus anciennes",
        *(f"- {k} : {v or 'aucune'}" for k, v in inv["oldest"].items()),
        f"- profils : {p['oldest']}",
        "",
        "## Glycémies (CGM)",
        f"- {e['total']} lectures ; complétude moyenne {(e['mean_daily_coverage'] or 0):.0%} (base 288/jour)",
        f"- jours ≥ 90 % : {e['days_over_90pct']} / {e['days_scanned']} ; jours < 70 % : {e['days_under_70pct']}",
        f"- jours les plus incomplets : {e['worst_days']}",
        "",
        "## Traitements",
        f"- {t['total']} documents ; jours avec basale temporaire : {t['days_with_temp_basal']} ; avec glucides : {t['days_with_carbs']}",
        *(f"  - {k} : {v}" for k, v in t["by_event_type"].items()),
        "",
        "## Devicestatus (14 derniers jours)",
        f"- {s['total']} documents ; complétude moyenne {(s['mean_daily_coverage'] or 0):.0%} ; avec champ `loop` : {s['has_loop_fields']}",
        "",
        "## Profils",
        f"- {p['documents']} documents ; plus récent {p['newest']} ; changements dans la période : {p['changes_in_period']}",
    ]
    return "\n".join(lines)
