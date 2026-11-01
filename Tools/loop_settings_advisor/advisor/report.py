"""Rapport lisible du module basale de nuit.

Le rapport ne contient que des valeurs calculées par le moteur. Le JSON
(`NightBasalReport.to_dict()`) est l'unique entrée prévue pour la future
étape d'explication par l'IA.
"""
from __future__ import annotations

from .basal_night import NightBasalReport
from .config import MGDL_PER_MMOL


def _bg(v, units):
    if v is None:
        return "—"
    return f"{v / MGDL_PER_MMOL:.1f} mmol/L" if units == "mmol" else f"{v:.0f} mg/dL"


def _num(v):
    return '—' if v is None else f"{v:.2f}"


def format_night_basal(r: NightBasalReport) -> str:
    u = r.units
    out = [
        "# Basale de nuit — recommandations (à valider, jamais appliquées automatiquement)",
        "",
        f"Période : {r.period_start} → {r.period_end} · fenêtre {r.window} ({r.profile_timezone})",
        f"Nuits valides : **{r.nights_valid} / {r.nights_total}**",
    ]
    if r.exclusion_summary:
        out.append("Exclusions : " + ", ".join(f"{k} ({v})" for k, v in sorted(r.exclusion_summary.items())))
    if r.median_delivery_ratio is not None:
        out.append(f"Insuline livrée par Loop / basale programmée (médiane des nuits valides) : "
                   f"**{r.median_delivery_ratio:.0%}**")
    out += ["", "## Par segment de basale",
            "", "| Segment | Actuelle (U/h) | Besoin estimé | Proposée | Variation | Nuits | Accord | Confiance | Action |",
            "|---|---|---|---|---|---|---|---|---|"]
    for s in r.segments:
        out.append(
            f"| {s.start}–{s.end} | {s.current_u_h:.2f} | "
            f"{'—' if s.estimated_need_u_h is None else f'{s.estimated_need_u_h:.2f}'} | "
            f"{'—' if s.proposed_u_h is None else f'{s.proposed_u_h:.2f}'} | "
            f"{'—' if s.change_pct is None else f'{s.change_pct:+.0f} %'} | {s.nights_used} | "
            f"{'—' if s.sign_agreement is None else f'{s.sign_agreement:.0%}'} | {s.confidence} | {s.action} |")
    notes = [(s, n) for s in r.segments for n in s.notes]
    if notes:
        out += ["", *(f"- {s.start}–{s.end} : {n}" for s, n in notes)]
    out += ["", "## Par heure (médiane des nuits valides)", "",
            "| Heure | Nuits | Besoin médian (U/h) | Écart médian (U/h) | IQR écart |", "|---|---|---|---|---|"]
    for h in r.hourly:
        out.append(f"| {h['hour']:02d}h | {h['nights']} | {h['median_need_u_h']:.2f} | "
                   f"{h['median_gap_u_h']:+.2f} | {_num(h['iqr_gap_u_h'])} |")
    out += ["", "## Détail des nuits", "",
            "| Nuit | Valide | Moy. | Min | Variation | <70 | Livré/prog. | Déviation moy. (mg/dL/5 min) | Raisons d'exclusion |",
            "|---|---|---|---|---|---|---|---|---|"]
    for n in r.nights:
        out.append(
            f"| {n.night} | {'oui' if n.valid else 'non'} | {_bg(n.mean_bg_mgdl, u)} | {_bg(n.min_bg_mgdl, u)} | "
            f"{'—' if n.bg_change_mgdl is None else _bg(n.bg_change_mgdl, u)} | "
            f"{'—' if n.time_below_70 is None else f'{n.time_below_70:.0%}'} | "
            f"{'—' if n.delivery_ratio is None else f'{n.delivery_ratio:.0%}'} | "
            f"{'—' if n.mean_deviation_mgdl_5min is None else f'{n.mean_deviation_mgdl_5min:+.2f}'} | "
            f"{', '.join(n.exclusion_reasons)} |")
    if r.warnings:
        out += ["", "## Avertissements", *(f"- {w}" for w in r.warnings)]
    out += ["", "_Outil d'aide à la réflexion : discutez de tout changement avec votre équipe soignante._"]
    return "\n".join(out)
