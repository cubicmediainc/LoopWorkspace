"""Modèle d'action de l'insuline, identique à LoopKit (ExponentialInsulinModel)."""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .config import BRAND_TO_PRESET, INSULIN_PRESETS, InsulinModelParams


class ExponentialInsulinModel:
    def __init__(self, params: InsulinModelParams):
        td = params.action_duration_min
        tp = params.peak_min
        self.td = td
        self.delay = params.delay_min
        self.tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
        self.a = 2 * self.tau / td
        self.S = 1 / (1 - self.a + (1 + self.a) * math.exp(-td / self.tau))

    def effect_remaining(self, minutes: np.ndarray | float) -> np.ndarray:
        """Fraction de l'effet restant `minutes` après l'administration."""
        t = np.asarray(minutes, dtype=float) - self.delay
        tc = np.clip(t, 0.0, self.td)
        r = 1 - self.S * (1 - self.a) * (
            (tc ** 2 / (self.tau * self.td * (1 - self.a)) - tc / self.tau - 1) * np.exp(-tc / self.tau) + 1)
        r = np.where(t <= 0, 1.0, r)
        r = np.where(t >= self.td, 0.0, r)
        return r

    @property
    def total_minutes(self) -> float:
        return self.td + self.delay


@lru_cache(maxsize=None)
def model_for(preset: str) -> ExponentialInsulinModel:
    return ExponentialInsulinModel(INSULIN_PRESETS[preset])


def preset_for_brand(brand: str | None, default_preset: str) -> str:
    if not brand:
        return default_preset
    return BRAND_TO_PRESET.get(brand.strip().lower(), default_preset)


def absorbed_per_interval(dose_times_min: np.ndarray, dose_units: np.ndarray,
                          dose_presets: list[str], grid_min: np.ndarray, step_min: float = 5.0) -> np.ndarray:
    """Unités d'insuline « absorbées » (effet réalisé) dans chaque intervalle [g, g+step).

    Les doses sont des mini-bolus ponctuels (les basales temporaires ont été
    découpées en tranches de 5 min au préalable). Les unités peuvent être
    négatives : ce sont alors des basales nettes inférieures à la programmée,
    exactement comme Loop les modélise.
    """
    out = np.zeros(len(grid_min))
    if len(dose_units) == 0:
        return out
    for preset in set(dose_presets):
        m = model_for(preset)
        mask = np.array([p == preset for p in dose_presets])
        t = dose_times_min[mask][None, :]
        u = dose_units[mask][None, :]
        g = grid_min[:, None]
        r0 = m.effect_remaining(g - t)
        r1 = m.effect_remaining(g + step_min - t)
        out += ((r0 - r1) * u).sum(axis=1)
    return out
