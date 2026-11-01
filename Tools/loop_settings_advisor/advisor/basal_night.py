"""Module 1 — Analyse de la basale de nuit.

Principe (adapté d'Autotune à la façon dont Loop modélise l'insuline) :

Loop considère que la basale programmée S(t) compense exactement la
production de glucose du foie. Il ne modélise donc que l'insuline *nette* :
basales temporaires moins la basale programmée, plus les bolus. Pendant la
nuit, sans glucides actifs :

    ΔBG_observé = BGI_net + déviation
    BGI_net     = −ISF × (insuline nette absorbée dans l'intervalle)

Si la basale programmée était juste, la déviation serait nulle en moyenne.
Une déviation positive persistante signifie qu'il manque de l'insuline, même
si Loop l'a compensée par des basales temporaires (qui sont, elles, comptées
dans BGI_net). En régime stable :

    déviation (mg/dL / 5 min) × 12 / ISF  ≈  B_réelle − S   (U/h)

Cet écart est attribué aux basales des 3 heures précédentes (délai d'action
de l'insuline), puis on agrège nuit par nuit, et on prend la médiane des nuits.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

import numpy as np

from .config import NightBasalConfig
from .data import Dataset
from .insulin import absorbed_per_interval, preset_for_brand
from .profile import ProfileTimeline

STEP = 5  # minutes


@dataclass
class NightResult:
    night: str  # date locale du matin
    valid: bool
    exclusion_reasons: list[str] = field(default_factory=list)
    cgm_coverage: float = 0.0
    valid_intervals: int = 0
    mean_bg_mgdl: float | None = None
    min_bg_mgdl: float | None = None
    bg_change_mgdl: float | None = None
    time_below_70: float | None = None
    time_70_180: float | None = None
    scheduled_basal_u: float | None = None
    delivered_basal_u: float | None = None
    bolus_u: float | None = None
    delivery_ratio: float | None = None
    mean_deviation_mgdl_5min: float | None = None
    # Estimation de la basale nécessaire par heure locale {heure: U/h}
    hourly_need: dict[int, float] = field(default_factory=dict)
    hourly_scheduled: dict[int, float] = field(default_factory=dict)


@dataclass
class SegmentRecommendation:
    start: str
    end: str
    current_u_h: float
    estimated_need_u_h: float | None
    proposed_u_h: float | None
    change_pct: float | None
    nights_used: int
    sign_agreement: float | None
    iqr_u_h: float | None
    confidence: str  # "élevée" / "moyenne" / "faible" / "insuffisante"
    action: str  # "augmenter" / "diminuer" / "aucun changement" / "pas de recommandation"
    notes: list[str] = field(default_factory=list)


@dataclass
class NightBasalReport:
    generated_at: str
    period_start: str
    period_end: str
    window: str
    profile_timezone: str
    units: str
    nights_total: int
    nights_valid: int
    exclusion_summary: dict[str, int]
    median_delivery_ratio: float | None
    segments: list[SegmentRecommendation]
    hourly: list[dict]
    nights: list[NightResult]
    warnings: list[str]
    config: dict

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- utilitaires
def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def _night_window(day: date, cfg: NightBasalConfig, tz) -> tuple[datetime, datetime]:
    """Fenêtre de nuit se terminant le matin du jour `day` (heure locale du profil)."""
    end = datetime.combine(day, time(cfg.night_end_hour), tzinfo=tz)
    start_day = day if cfg.night_start_hour < cfg.night_end_hour else day - timedelta(days=1)
    start = datetime.combine(start_day, time(cfg.night_start_hour), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _cgm_grid(ds: Dataset, t0: datetime, n: int) -> np.ndarray:
    """Glycémie sur une grille de 5 min (NaN si aucune lecture à ±2,5 min)."""
    grid = np.full(n, np.nan)
    base = t0.timestamp()
    for g in ds.glucose:
        k = (g.time.timestamp() - base) / 60 / STEP
        i = int(round(k))
        if 0 <= i < n and abs(k - i) * STEP <= 2.5:
            if 40 <= g.mgdl <= 400:
                grid[i] = g.mgdl
    return grid


def _net_doses(ds: Dataset, prof: ProfileTimeline, a: datetime, b: datetime, cfg: NightBasalConfig):
    """Insuline nette (par rapport à la basale programmée) découpée en tranches de 5 min."""
    times, units, presets = [], [], []
    base = a.timestamp()
    for tb in ds.temp_basals:
        if not _overlaps(tb.start, tb.end, a, b):
            continue
        preset = preset_for_brand(tb.insulin_type, cfg.default_insulin_preset)
        cur = max(tb.start, a)
        end = min(tb.end, b)
        while cur < end:
            nxt = min(cur + timedelta(minutes=STEP), end)
            hours = (nxt - cur).total_seconds() / 3600
            net = (tb.rate - prof.basal_at(cur)) * hours
            if net != 0:
                times.append((cur.timestamp() - base) / 60)
                units.append(net)
                presets.append(preset)
            cur = nxt
    for bo in ds.boluses:
        if not (a <= bo.time < b):
            continue
        preset = preset_for_brand(bo.insulin_type, cfg.default_insulin_preset)
        if bo.duration_min >= 30:  # bolus carré : réparti
            k = max(int(bo.duration_min // STEP), 1)
            for j in range(k):
                times.append((bo.time.timestamp() - base) / 60 + j * STEP)
                units.append(bo.units / k)
                presets.append(preset)
        else:
            times.append((bo.time.timestamp() - base) / 60)
            units.append(bo.units)
            presets.append(preset)
    return np.array(times, dtype=float), np.array(units, dtype=float), presets


def _delivered_in_window(ds: Dataset, prof: ProfileTimeline, a: datetime, b: datetime):
    """Basale programmée vs réellement livrée (U) dans [a, b)."""
    sched = delivered = 0.0
    cur = a
    temps = [tb for tb in ds.temp_basals if _overlaps(tb.start, tb.end, a, b)]
    while cur < b:
        nxt = min(cur + timedelta(minutes=STEP), b)
        h = (nxt - cur).total_seconds() / 3600
        s = prof.basal_at(cur)
        sched += s * h
        rate = s
        for tb in temps:
            if tb.start <= cur < tb.end:
                rate = tb.rate
        delivered += rate * h
        cur = nxt
    bolus = sum(bo.units for bo in ds.boluses if a <= bo.time < b)
    return sched, delivered, bolus


# --------------------------------------------------------------------------- analyse d'une nuit
def analyze_night(day: date, ds: Dataset, prof: ProfileTimeline, cfg: NightBasalConfig) -> NightResult:
    tz = prof.current.tz
    w0, w1 = _night_window(day, cfg, tz)
    res = NightResult(night=day.isoformat(), valid=True)
    reasons = res.exclusion_reasons

    # --- exclusions de nuit entière
    if any(_overlaps(o.start, o.end, w0, w1) for o in ds.overrides):
        reasons.append("override actif")
    if any(w0 - timedelta(hours=cfg.site_change_exclusion_hours_before) <= s < w1 for s in ds.site_changes):
        reasons.append("changement de site")
    if any(w0 - timedelta(hours=cfg.sensor_start_exclusion_hours) <= s < w1 for s in ds.sensor_starts):
        reasons.append("démarrage de capteur")
    if any(w0 <= c.time < w1 for c in ds.carbs):
        reasons.append("glucides pendant la nuit")
    if prof.changes_between(w0 - timedelta(hours=6), w1):
        reasons.append("réglages modifiés pendant la nuit")

    # --- grille CGM (fenêtre + 1 point avant pour la première variation)
    g0 = w0 - timedelta(minutes=STEP)
    n = int((w1 - g0).total_seconds() // 60 // STEP) + 1
    bg = _cgm_grid(ds, g0, n)
    in_window = bg[1:]
    res.cgm_coverage = float(np.mean(~np.isnan(in_window)))
    if res.cgm_coverage < cfg.min_cgm_coverage:
        reasons.append(f"CGM incomplet ({res.cgm_coverage:.0%})")

    vals = in_window[~np.isnan(in_window)]
    if len(vals):
        res.mean_bg_mgdl = float(np.mean(vals))
        res.min_bg_mgdl = float(np.min(vals))
        res.time_below_70 = float(np.mean(vals < 70))
        res.time_70_180 = float(np.mean((vals >= 70) & (vals <= 180)))
        first = in_window[~np.isnan(in_window)][0]
        last = in_window[~np.isnan(in_window)][-1]
        res.bg_change_mgdl = float(last - first)

    sched, deliv, bol = _delivered_in_window(ds, prof, w0, w1)
    res.scheduled_basal_u, res.delivered_basal_u, res.bolus_u = round(sched, 3), round(deliv, 3), round(bol, 3)
    res.delivery_ratio = round((deliv + bol) / sched, 3) if sched > 0 else None

    # --- BGI net et déviations par intervalle de 5 min
    hist_start = g0 - timedelta(minutes=6 * 60 + 15)
    t_d, u_d, p_d = _net_doses(ds, prof, hist_start, w1, cfg)
    offset = (g0 - hist_start).total_seconds() / 60
    grid_min = offset + np.arange(n - 1) * STEP  # intervalles [g_i, g_{i+1})
    absorbed = absorbed_per_interval(t_d, u_d, p_d, grid_min, STEP)

    times = [g0 + timedelta(minutes=STEP * i) for i in range(n - 1)]
    isf = np.array([prof.isf_at(t) for t in times])
    delta = bg[1:] - bg[:-1]
    bgi = -isf * absorbed
    dev = delta - bgi

    valid = ~np.isnan(delta)
    valid &= np.abs(np.nan_to_num(delta)) <= cfg.max_abs_delta_mgdl_per_5min
    # glucides encore actifs avant la fenêtre
    for c in ds.carbs:
        absorb = c.absorption_min or cfg.carb_absorption_default_min
        c_end = c.time + timedelta(minutes=absorb * cfg.carb_absorption_margin)
        for i, t in enumerate(times):
            if c.time <= t < c_end:
                valid[i] = False
    res.valid_intervals = int(valid.sum())
    if valid.mean() < cfg.min_valid_interval_fraction:
        reasons.append(f"trop peu d'intervalles utilisables ({valid.mean():.0%})")

    if valid.any():
        res.mean_deviation_mgdl_5min = round(float(np.mean(dev[valid])), 3)

    # --- attribution aux heures précédentes : besoin = S + écart
    span = cfg.attribution_minutes // STEP
    tz_local = prof.current.tz
    need_sum: dict[int, float] = {}
    need_n: dict[int, int] = {}
    sched_sum: dict[int, float] = {}
    for i in np.nonzero(valid)[0]:
        gap = dev[i] * 12 / isf[i]  # U/h
        t_obs = times[i]
        for k in range(1, span + 1):
            ts = t_obs - timedelta(minutes=STEP * k)
            h = ts.astimezone(tz_local).hour
            s = prof.basal_at(ts)
            need_sum[h] = need_sum.get(h, 0.0) + s + gap
            sched_sum[h] = sched_sum.get(h, 0.0) + s
            need_n[h] = need_n.get(h, 0) + 1
    # Une heure n'est retenue que si elle a reçu au moins la moitié des attributions possibles.
    full = 12 * span
    for h, cnt in need_n.items():
        if cnt >= full * 0.5:
            res.hourly_need[h] = round(need_sum[h] / cnt, 4)
            res.hourly_scheduled[h] = round(sched_sum[h] / cnt, 4)

    res.valid = not reasons
    return res


# --------------------------------------------------------------------------- agrégation
def _round_to(x: float, inc: float) -> float:
    return round(round(x / inc) * inc, 4)


def _confidence(n: int, agreement: float | None, coherent: bool, cfg: NightBasalConfig) -> str:
    if n < cfg.min_nights_for_recommendation:
        return "insuffisante"
    levels = ["faible", "moyenne", "élevée"]
    lvl = 2 if n >= cfg.nights_for_high_confidence else 1 if n >= cfg.nights_for_medium_confidence else 0
    if agreement is not None and agreement < cfg.min_sign_agreement:
        lvl -= 1
    if not coherent:
        lvl -= 1
    return levels[max(lvl, 0)]


def analyze(ds: Dataset, prof: ProfileTimeline, cfg: NightBasalConfig, end_day: date | None = None) -> NightBasalReport:
    tz = prof.current.tz
    end_day = end_day or datetime.now(tz).date()
    days = [end_day - timedelta(days=i) for i in range(cfg.days - 1, -1, -1)]
    # La nuit du jour courant n'est retenue que si elle est terminée.
    now = datetime.now(timezone.utc)
    days = [d for d in days if _night_window(d, cfg, tz)[1] <= now]

    nights = [analyze_night(d, ds, prof, cfg) for d in days]
    valid = [n for n in nights if n.valid]

    excl: dict[str, int] = {}
    for n in nights:
        for r in n.exclusion_reasons:
            key = r.split(" (")[0]
            excl[key] = excl.get(key, 0) + 1

    ratios = [n.delivery_ratio for n in valid if n.delivery_ratio is not None]
    med_ratio = round(median(ratios), 3) if ratios else None

    # --- par heure
    hourly = []
    first_h = (cfg.night_start_hour - cfg.attribution_minutes // 60) % 24
    hours_seen = sorted({h for n in valid for h in n.hourly_need}, key=lambda h: (h - first_h) % 24)
    per_hour_gap: dict[int, list[float]] = {}
    for h in hours_seen:
        gaps = [n.hourly_need[h] - n.hourly_scheduled[h] for n in valid if h in n.hourly_need]
        needs = [n.hourly_need[h] for n in valid if h in n.hourly_need]
        per_hour_gap[h] = gaps
        hourly.append({
            "hour": h, "nights": len(needs),
            "median_need_u_h": round(median(needs), 3),
            "median_gap_u_h": round(median(gaps), 3),
            "iqr_gap_u_h": round(float(np.subtract(*np.percentile(gaps, [75, 25]))), 3) if len(gaps) > 1 else None,
        })

    # --- par segment de la basale actuelle
    cur = prof.current
    segments: list[SegmentRecommendation] = []
    for s0, s1, rate in cur.basal.segments():
        hrs = [h for h in hours_seen if s0 <= h * 3600 < s1]
        def fmt(sec): return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"
        if not hrs:
            continue
        # écart relatif moyen par nuit sur les heures du segment, puis médiane des nuits
        night_gaps = []
        for n in valid:
            g = [n.hourly_need[h] - n.hourly_scheduled[h] for h in hrs if h in n.hourly_need]
            if g:
                night_gaps.append(float(np.mean(g)))
        n_used = len(night_gaps)
        notes = []
        if n_used == 0:
            continue
        med_gap = median(night_gaps)
        agreement = float(np.mean([np.sign(x) == np.sign(med_gap) for x in night_gaps])) if med_gap != 0 else None
        iqr = float(np.subtract(*np.percentile(night_gaps, [75, 25]))) if n_used > 1 else None
        need = rate + med_gap
        # Cohérence avec l'insuline réellement livrée par Loop
        coherent = True
        if med_ratio is not None:
            if med_gap > 0 and med_ratio < 0.95 or med_gap < 0 and med_ratio > 1.05:
                coherent = False
                notes.append(f"Signal contradictoire : Loop a livré {med_ratio:.0%} de la basale programmée la nuit.")
        conf = _confidence(n_used, agreement, coherent, cfg)
        if conf == "insuffisante":
            segments.append(SegmentRecommendation(fmt(s0), fmt(s1), rate, round(need, 3), None, None, n_used,
                                                  agreement, iqr, conf, "pas de recommandation",
                                                  [f"Seulement {n_used} nuit(s) valide(s) ; minimum {cfg.min_nights_for_recommendation}."]))
            continue
        raw = rate + cfg.damping * med_gap
        lo, hi = rate * (1 - cfg.max_change_fraction), rate * (1 + cfg.max_change_fraction)
        bounded = min(max(raw, lo), hi)
        if bounded != raw:
            notes.append(f"Ajustement plafonné à ±{cfg.max_change_fraction:.0%}.")
        proposed = _round_to(bounded, cfg.basal_increment)
        change = (proposed - rate) / rate if rate > 0 else None
        if change is None or abs(change) < cfg.min_change_fraction:
            action, proposed_out = "aucun changement", rate
        else:
            action, proposed_out = ("augmenter" if change > 0 else "diminuer"), proposed
        if action == "augmenter" and any((n.time_below_70 or 0) > 0.04 for n in valid):
            notes.append("Attention : certaines nuits ont plus de 4 % du temps sous 70 mg/dL.")
        segments.append(SegmentRecommendation(
            fmt(s0), fmt(s1), rate, round(need, 3), proposed_out,
            round(((proposed_out - rate) / rate) * 100, 1) if rate else None,
            n_used, round(agreement, 2) if agreement is not None else None,
            round(iqr, 3) if iqr is not None else None, conf, action, notes))

    warnings = list(ds.warnings)
    if len(valid) < cfg.nights_for_medium_confidence:
        warnings.append(f"Peu de nuits valides ({len(valid)}). Visez 2 à 4 semaines de données propres.")
    warnings.append("L'estimation dépend de l'ISF programmé : si l'ISF est faux, l'écart de basale l'est aussi.")

    return NightBasalReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        period_start=days[0].isoformat() if days else "", period_end=days[-1].isoformat() if days else "",
        window=f"{cfg.night_start_hour:02d}:00–{cfg.night_end_hour:02d}:00",
        profile_timezone=cur.tz_name, units=cur.units,
        nights_total=len(nights), nights_valid=len(valid), exclusion_summary=excl,
        median_delivery_ratio=med_ratio, segments=segments, hourly=hourly, nights=nights,
        warnings=warnings, config=asdict(cfg),
    )
