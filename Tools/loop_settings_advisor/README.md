# Loop Settings Advisor — prototype

Outil **de recommandation seulement** : il analyse les données Loop stockées dans
Nightscout et propose des ajustements bornés des réglages. Il n'écrit jamais
rien dans Nightscout ni dans Loop.

## Accès à Nightscout

Le client lit `NIGHTSCOUT_URL` (défaut `https://nightscout.domainecullens.ca`) et
`NIGHTSCOUT_TOKEN`, un jeton d'accès en **lecture seule**. Ne collez jamais ce jeton
dans un fichier versionné.

Nightscout 15 n'accepte dans l'en-tête `Authorization: Bearer` qu'un **JWT**.
Un jeton d'accès brut (`readable-…`) doit donc être passé en paramètre `?token=`,
et c'est ce que fait le client. Après un 401, il ne réessaie pas, car Nightscout
pénalise les échecs répétés.

```bash
pip install -r requirements.txt
python -m advisor check                    # verifyauth + dernière glycémie
python -m advisor inventory --days 90 --out rapports/
python -m advisor night-basal --days 28 --out rapports/
python -m unittest discover -s tests -t .  # tests sur données synthétiques
```

## Structure

| Module | Rôle |
|---|---|
| `advisor/nightscout.py` | Client API v1 en lecture, pagination par fenêtres, recherche de la donnée la plus ancienne |
| `advisor/inventory.py` | Étape b : historique disponible et complétude (CGM, traitements, devicestatus, profils) |
| `advisor/profile.py` | Historique des profils Loop (basales, ISF, ratios, cibles, fuseau), conversion mmol/L ↔ mg/dL |
| `advisor/data.py` | Normalisation : glycémies, basales temporaires et suspensions, bolus, glucides, overrides, changements de site et de capteur |
| `advisor/insulin.py` | Modèle exponentiel de LoopKit (préréglages adulte, enfant, Fiasp, Lyumjev, Afrezza) |
| `advisor/basal_night.py` | **Module 1** : basale de nuit |
| `advisor/report.py` | Rapport Markdown, rempli uniquement avec les valeurs calculées |
| `tests/` | Simulateur synthétique et tests |

Modules prévus : `isf.py` (corrections hors repas), `carb_ratio.py` (repas
annoncés, absorption), `day_basal.py` (créneaux de jeûne en journée),
`devicestatus.py` (comparaison avec l'IOB et le COB calculés par Loop), puis
`explain.py` (l'IA explique le JSON du rapport sans produire de chiffres).

## Méthode : basale de nuit

1. Pour chaque nuit (00:00 à 06:00 par défaut, dans le fuseau du profil Loop),
   la glycémie est placée sur une grille de 5 minutes.
2. L'insuline **nette** est reconstruite comme dans Loop : basale temporaire moins
   basale programmée en vigueur à cet instant, plus les bolus, avec 6 h
   d'historique. Le modèle d'insuline dépend du champ `insulinType` de chaque dose.
3. Pour chaque intervalle : `BGI = −ISF × insuline nette absorbée`, puis
   `déviation = ΔBG − BGI`.
4. Écart de basale : `déviation × 12 / ISF` (U/h). Il est attribué aux 3 heures
   précédentes, comme dans Autotune. Une fenêtre de 00:00 à 06:00 renseigne donc
   surtout les basales de 22 h à 3 h environ.
5. On calcule le besoin estimé par nuit et par heure, puis la médiane des nuits
   valides, regroupée par segment de la basale actuelle.
6. Proposition = actuelle + 50 % de l'écart, plafonnée à ±15 % et arrondie à
   0,05 U/h. Sous 5 % d'écart, aucun changement n'est proposé.
7. Le niveau de confiance dépend du nombre de nuits valides (moins de 5 : aucune
   recommandation ; 7 : moyenne ; 14 : élevée). Il baisse si les nuits ne vont pas
   dans le même sens, ou si le sens contredit le rapport entre l'insuline livrée
   par Loop et la basale programmée.

Nuits exclues : override ou cible temporaire, changement de site (dans la nuit
ou les 12 h avant), démarrage de capteur (12 h), glucides pendant la nuit,
réglages modifiés, CGM couvrant moins de 75 % de la fenêtre, moins de 50 %
d'intervalles utilisables. Les intervalles où les glucides du soir sont encore
actifs (absorption × 1,5) et les sauts de plus de 15 mg/dL en 5 min (bruit ou
compression) sont aussi exclus.

Limites connues : le résultat dépend de l'ISF programmé (une erreur d'ISF fausse
l'écart de basale) ; la DIA et le modèle choisi dans Loop ne sont pas dans
Nightscout (on utilise donc `insulinType`, et `--insulin` pour
Humalog/Novolog/Apidra) ; les repas non annoncés ne sont pas détectés
explicitement, la médiane des nuits en limite l'effet.
