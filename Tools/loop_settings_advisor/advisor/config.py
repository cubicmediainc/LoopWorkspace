"""Paramètres de l'analyse et garde-fous.

Toutes les valeurs ici sont des réglages de l'*outil*, pas des réglages
thérapeutiques. Elles bornent ce que le moteur a le droit de proposer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MGDL_PER_MMOL = 18.0182


@dataclass
class InsulinModelParams:
    """Modèle exponentiel de Loop (LoopKit ExponentialInsulinModelPreset)."""

    action_duration_min: float
    peak_min: float
    delay_min: float = 10.0


# Préréglages copiés de LoopKit/InsulinKit/ExponentialInsulinModelPreset.swift
INSULIN_PRESETS: dict[str, InsulinModelParams] = {
    "rapidActingAdult": InsulinModelParams(360, 75),
    "rapidActingChild": InsulinModelParams(360, 65),
    "fiasp": InsulinModelParams(360, 55),
    "lyumjev": InsulinModelParams(360, 55),
    "afrezza": InsulinModelParams(300, 29),
}

# Correspondance entre le champ « insulinType » (brandName) envoyé par Loop
# sur chaque dose et le préréglage. Humalog/Novolog/Apidra : Loop laisse le
# choix adulte/enfant dans les réglages, qui ne sont pas envoyés à Nightscout.
# On utilise donc `default_insulin_preset` pour ces insulines.
BRAND_TO_PRESET = {
    "fiasp": "fiasp",
    "lyumjev": "lyumjev",
    "afrezza": "afrezza",
}


@dataclass
class NightBasalConfig:
    # Fenêtre de nuit, heure locale (fuseau du profil Loop).
    night_start_hour: int = 0
    night_end_hour: int = 6
    # Nombre de jours analysés (en remontant depuis aujourd'hui).
    days: int = 28

    # Attribution d'une déviation aux basales des heures précédentes
    # (comme Autotune : la basale des 3 h précédentes explique l'écart observé).
    attribution_minutes: int = 180

    # --- Exclusions ---
    carb_absorption_default_min: float = 180.0
    carb_absorption_margin: float = 1.5  # glucides « actifs » = absorption × 1,5
    site_change_exclusion_hours_before: float = 12.0  # nuit exclue si changement de site dans les 12 h avant
    sensor_start_exclusion_hours: float = 12.0  # premières heures d'un capteur G7
    min_cgm_coverage: float = 0.75  # fraction minimale de lectures présentes dans la fenêtre
    max_abs_delta_mgdl_per_5min: float = 15.0  # variation > 3 mg/dL/min = bruit ou compression
    min_valid_interval_fraction: float = 0.5  # fraction minimale d'intervalles utilisables par nuit

    # --- Garde-fous sur la recommandation ---
    damping: float = 0.5  # on n'applique que la moitié de l'écart estimé
    max_change_fraction: float = 0.15  # ±15 % par itération (borne entre 10 et 20 %)
    min_change_fraction: float = 0.05  # en dessous : « pas de changement »
    basal_increment: float = 0.05  # U/h (Omnipod)
    min_nights_for_recommendation: int = 5
    nights_for_medium_confidence: int = 7
    nights_for_high_confidence: int = 14
    min_sign_agreement: float = 0.65  # fraction des nuits allant dans le même sens

    default_insulin_preset: str = "rapidActingAdult"

    def __post_init__(self) -> None:
        if not 0.0 < self.max_change_fraction <= 0.20:
            raise ValueError("max_change_fraction doit rester entre 0 et 20 %")
        if not 0.0 < self.damping <= 1.0:
            raise ValueError("damping doit être entre 0 et 1")


@dataclass
class AppConfig:
    night_basal: NightBasalConfig = field(default_factory=NightBasalConfig)
