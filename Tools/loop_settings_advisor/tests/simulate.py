"""Simulateur synthétique (tests uniquement — jamais présenté comme des données réelles).

Génère des documents au format Nightscout/Loop : profil, glycémies, basales
temporaires d'un contrôleur simplifié façon Loop, avec un besoin basal réel
différent de la basale programmée.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from advisor.config import MGDL_PER_MMOL
from advisor.insulin import model_for


def simulate(days: int = 30, programmed: float = 0.8, true_need: float = 1.0, isf_mmol: float = 2.5,
             target_mgdl: float = 110.0, noise_sd: float = 2.0, seed: int = 1,
             end: datetime | None = None, tz_name: str = "GMT-0400"):
    rng = np.random.default_rng(seed)
    isf = isf_mmol * MGDL_PER_MMOL
    end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    n = days * 288
    m = model_for("rapidActingAdult")
    klen = int(m.total_minutes // 5) + 2
    j = np.arange(klen) * 5.0
    kernel = m.effect_remaining(j) - m.effect_remaining(j + 5)

    # Historique : régime permanent à la basale vraie avant le début.
    hist = [true_need / 12] * klen
    delivered = []
    bg = target_mgdl
    bgs, rates = [], []
    k_gain = 1.0 / 30  # fraction de l'écart corrigée par 5 min
    for i in range(n):
        rate = programmed + k_gain * 12 * (bg - target_mgdl) / isf
        rate = float(np.clip(rate, 0.0, 3.0))
        rate = round(rate / 0.05) * 0.05
        rates.append(rate)
        delivered.append(rate / 12)
        series = (hist + delivered)[-klen:][::-1]  # plus récent en premier
        absorbed = float(np.dot(series, kernel))
        bg = bg + isf * (true_need / 12 - absorbed)
        bgs.append(bg)

    entries, treatments = [], []
    for i in range(n):
        t = start + timedelta(minutes=5 * (i + 1))
        entries.append({"type": "sgv", "sgv": round(bgs[i] + rng.normal(0, noise_sd)),
                        "date": int(t.timestamp() * 1000)})
        t0 = start + timedelta(minutes=5 * i)
        treatments.append({"eventType": "Temp Basal", "created_at": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "rate": rates[i], "absolute": rates[i], "duration": 30, "automatic": True,
                           "syncIdentifier": f"tb-{i}", "insulinType": "Novolog"})
    profile = {"startDate": (start - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "defaultProfile": "Default",
               "store": {"Default": {"units": "mmol", "timezone": tz_name,
                                     "basal": [{"time": "00:00", "value": programmed, "timeAsSeconds": 0}],
                                     "sens": [{"time": "00:00", "value": isf_mmol, "timeAsSeconds": 0}],
                                     "carbratio": [{"time": "00:00", "value": 10, "timeAsSeconds": 0}],
                                     "target_low": [{"time": "00:00", "value": 5.5}],
                                     "target_high": [{"time": "00:00", "value": 6.5}]}}}
    return entries, treatments, [profile], end
