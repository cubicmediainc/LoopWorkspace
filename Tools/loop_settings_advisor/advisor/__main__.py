"""Ligne de commande.

    python -m advisor check                 # teste l'accès (verifyauth + 1 glycémie)
    python -m advisor inventory --days 90   # évalue l'historique disponible
    python -m advisor night-basal --days 28 --out rapports/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .basal_night import analyze
from .config import NightBasalConfig
from .data import parse_entries, parse_treatments
from .inventory import format_inventory, run_inventory
from .nightscout import NightscoutAuthError, NightscoutClient
from .profile import ProfileTimeline
from .report import format_night_basal


def cmd_check(client: NightscoutClient, _args) -> int:
    auth = client.verify_auth()
    print(f"verifyauth : canRead={auth.get('canRead')} canWrite={auth.get('canWrite')} "
          f"isAdmin={auth.get('isAdmin')} rolefound={auth.get('rolefound')}")
    if not auth.get("canRead"):
        print("Lecture refusée : définissez NIGHTSCOUT_TOKEN (jeton « readable »).")
        return 1
    e = client.get("/api/v1/entries.json", {"count": 1})
    if e:
        print(f"Dernière glycémie : {e[0].get('sgv')} mg/dL à {e[0].get('dateString')}")
    return 0


def cmd_inventory(client: NightscoutClient, args) -> int:
    inv = run_inventory(client, args.days)
    text = format_inventory(inv)
    print(text)
    _save(args.out, "inventaire", text, inv)
    return 0


def cmd_night_basal(client: NightscoutClient, args) -> int:
    cfg = NightBasalConfig(days=args.days, night_start_hour=args.start, night_end_hour=args.end,
                           max_change_fraction=args.max_change, default_insulin_preset=args.insulin)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=cfg.days + 2)
    profiles = ProfileTimeline(client.profiles())
    ds = parse_treatments(client.treatments(start, now))
    ds.glucose = parse_entries(client.entries(start, now))
    report = analyze(ds, profiles, cfg)
    text = format_night_basal(report)
    print(text)
    _save(args.out, "basale_nuit", text, report.to_dict())
    return 0


def _save(out_dir: str | None, stem: str, text: str, data: dict) -> None:
    if not out_dir:
        return
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    (d / f"{stem}-{ts}.md").write_text(text, encoding="utf-8")
    (d / f"{stem}-{ts}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nRapport enregistré dans {d}/{stem}-{ts}.md (+ .json)", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="advisor", description="Analyse des réglages Loop depuis Nightscout")
    ap.add_argument("--url", help="URL Nightscout (défaut : $NIGHTSCOUT_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("inventory")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out")
    p = sub.add_parser("night-basal")
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--start", type=int, default=0, help="heure de début de la nuit (locale)")
    p.add_argument("--end", type=int, default=6, help="heure de fin de la nuit (locale)")
    p.add_argument("--max-change", type=float, default=0.15, help="variation maximale par itération (≤ 0,20)")
    p.add_argument("--insulin", default="rapidActingAdult",
                   choices=["rapidActingAdult", "rapidActingChild", "fiasp", "lyumjev", "afrezza"],
                   help="modèle pour Humalog/Novolog/Apidra (non transmis par Loop)")
    p.add_argument("--out")
    args = ap.parse_args(argv)
    client = NightscoutClient(base_url=args.url)
    try:
        return {"check": cmd_check, "inventory": cmd_inventory, "night-basal": cmd_night_basal}[args.cmd](client, args)
    except NightscoutAuthError as e:
        print(f"Erreur d'authentification : {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
