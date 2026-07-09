# Veille concurrentielle — Lobs (laboratoire vétérinaire)

Veille automatisée sur les concurrents (Osalia, Axience, Laprovet, Kepro, Bimeda,
Inovet…), zones France / Afrique / Moyen-Orient / Europe. Le système collecte des
sources fiables, déduplique par hash de contenu et ne notifie que les **vrais**
changements (nouvelle AMM, nouveau produit, nouvel exposant, nouvelle offre).

La qualification des sources (accès retenu, risque, conformité) est dans
[`SOURCES.md`](SOURCES.md). À lire avant d'ajouter une source.

## État

| Phase | Source | Statut |
|-------|--------|--------|
| 1 | **ANSES/ANMV** (open data XML V2) | ✅ Implémentée |
| 2 | EMA UPD (API OAuth2 MAH) | ⏳ En attente d'accès interne |
| 2 | Catalogues concurrents (Scrapy) | ⬜ À faire |
| 3 | Salons pro (`pd.read_html`) | ⬜ À faire |
| 4 | Veille RH | ⬜ Voie officielle à cadrer (cf `veille/sources/phase4_rh/README.md`) |

## Architecture

```
veille/
  schema.py            Record pivot (concurrent, produit, molecules, hash_contenu…)
  settings.py          Config .env + config.yaml, matching concurrents/mots-clés
  storage/             Store abstrait + SqliteStore (tables records + history)
  notifier/            Notifier abstrait + Slack + Console
  sources/             Source abstraite + registre
    phase1_regulatory/anses_anmv.py
  orchestrator.py      Lance sources → diff vs historique → notifie
scripts/run.py         CLI
```

Chaque source expose `fetch() -> list[Record]`. L'orchestrateur compare au hash
historisé (`storage.diff`) et ne transmet au notifier que `NOUVEAU` / `MODIFIE`.

## Installation

Python ≥ 3.11. Avec [uv](https://github.com/astral-sh/uv) :

```bash
uv venv && source .venv/bin/activate
uv pip install -e .              # phase 1 (ANSES)
uv pip install -e ".[scraping]"  # ajoute Scrapy/pdfplumber/pandas (phases 2-3)
uv pip install -e ".[dev]"       # pytest, ruff
```

Ou en venv standard :

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configuration

```bash
cp .env.example .env             # renseigner SLACK_WEBHOOK_URL (ou laisser vide + --dry-run)
```

- `.env` : secrets uniquement (webhook Slack, futurs identifiants OAuth2 EMA). Jamais commité.
- `config/config.yaml` : concurrents suivis, mots-clés stratégiques, réglages par source.
  - `sources.anses_anmv.inclure_tous_produits: false` → ne remonte que les concurrents suivis.
  - `sources.anses_anmv.inclure_rcp: false` → si `true`, extrait aussi le sous-ensemble
    business du RCP (posologie, temps d'attente, indications, composition/dosages,
    contre-indications) et suit ses MAJ via un hash RCP distinct. Même source (XML V2).

## Lancement

```bash
python -m scripts.run --list-sources                 # liste les sources
python -m scripts.run --source anses_anmv --dry-run  # affiche sans rien envoyer
python -m scripts.run --source anses_anmv            # envoi réel + écriture historique
python -m scripts.run                                # toutes les sources actives
```

Le mode `--dry-run` n'envoie aucune notification et n'écrit pas l'historique :
idéal pour valider ce qui *serait* remonté. Exemple de sortie complet dans
[`docs/exemple_sortie_anses.txt`](docs/exemple_sortie_anses.txt).

## Déduplication / diff

- Chaque `Record` porte **deux** hashs SHA-256 (cf `veille/schema.py`) :
  - `hash_registre` : existence/identité de l'AMM (produit, molécules, n° AMM, date…).
  - `hash_rcp` : contenu clinique business du RCP, **après normalisation** (casse,
    accents, ponctuation, espaces) — on ne hashe jamais le texte brut. Vide si
    `inclure_rcp: false`.
- Le diff compare les deux séparément → on notifie différemment **« nouvelle AMM »**,
  **« registre modifié »** et **« RCP modifié »** (cf `Notifier._label`).
- `SqliteStore` conserve l'état courant (`records`) et un journal (`history`) avec
  l'aspect modifié. Au run suivant, seuls les hashs changés (ou inconnus) sont notifiés.

## Planification (cron)

Le dataset ANSES est publié chaque mardi. Exemple — chaque mardi à 8h :

```cron
0 8 * * 2 cd /chemin/veille && .venv/bin/python -m scripts.run --source anses_anmv >> logs/cron.log 2>&1
```

## Conformité

- Respect de `robots.txt` et rate-limiting (`DOWNLOAD_DELAY` + AutoThrottle côté Scrapy).
- Priorité aux API/exports officiels avant tout scraping (cf `SOURCES.md`).
- **Aucun** contournement anti-bot (pas de proxies résidentiels, pas de CAPTCHA forcé).
  Si une source bloque → canal officiel ou alerte à l'équipe.
- RGPD : la Phase 4 (RH) ne touche que des données pro via canaux officiels.

## Tests

```bash
pytest -q
```
