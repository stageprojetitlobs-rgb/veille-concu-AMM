"""Agent hebdomadaire de recherche de sources AMM plus fraîches.

Rejoue, sans intervention humaine, le travail de recherche fait manuellement le
2026-07-09 : pour chaque registre AMM déjà intégré (PDF/API), vérifier si
l'autorité a publié une édition plus récente ou une base en ligne plus à jour
que celle configurée. Tourne dans GitHub Actions (cf .github/workflows/
weekly-research.yml), indépendamment de Claude Code — juste une clé API.

Ce script NE MODIFIE JAMAIS `main` directement. Il :
  1. demande à Claude (avec l'outil de recherche web) d'auditer chaque source ;
  2. écrit un rapport daté dans `research/` (toujours, même si rien de neuf) ;
  3. si Claude propose un changement d'URL simple (même format de fichier, même
     parseur), ouvre une Pull Request pour relecture humaine — jamais de merge
     automatique. Une source nécessitant du nouveau code (nouvelle API à
     intégrer) est seulement documentée dans le rapport, pas appliquée.

Conformité (rappel donné explicitement au modèle dans le prompt) :
  - Respecte toujours robots.txt et un rate-limiting raisonnable.
  - Privilégie une API/export officiel documenté au scraping HTML.
  - Aucun contournement anti-bot (pas de proxy résidentiel, pas de résolution
    de CAPTCHA). Si une source bloque, on le documente, on ne force jamais.
  - RGPD : signaler toute donnée à caractère personnel rencontrée.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"
RESEARCH_DIR = ROOT / "research"

_MODEL = "claude-sonnet-5"

_COMPLIANCE_RULES = """\
Respecte toujours robots.txt et applique un rate-limiting raisonnable. On ne
surcharge jamais un serveur. Avant de proposer une source, vérifie s'il existe
une API officielle ou un export de données documenté et privilégie-le au
scraping HTML. N'implémente/ne propose AUCUN contournement de protection
anti-bot (pas de rotation de proxies résidentiels, pas de résolution de
CAPTCHA). Si une source bloque, documente-le, ne force jamais. RGPD : si une
donnée à caractère personnel apparaît dans une source, signale-le et ne la
recommande pas telle quelle."""


def _sources_summary() -> str:
    """Résumé lisible des sources PDF/API actuellement configurées (pour le prompt)."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    lines = []
    for name, sc in (cfg.get("sources") or {}).items():
        if not sc.get("enabled"):
            continue
        url = sc.get("url_pdf") or sc.get("api_url") or sc.get("base_url")
        urls = sc.get("url_pdfs")
        if not url and not urls:
            continue
        lines.append(f"- `{name}` : {url or urls}")
    return "\n".join(lines)


def _build_prompt() -> str:
    return f"""Tu audites les sources de la veille AMM (autorisations de mise sur le
marché vétérinaires) d'un laboratoire pharmaceutique vétérinaire (marchés
export : Afrique, Moyen-Orient, Asie). Voici les sources PDF/API actuellement
configurées :

{_sources_summary()}

Pour CHAQUE source, vérifie via recherche web si l'autorité a publié :
  1. une édition PDF plus récente que celle utilisée (même format de tableau,
     juste une URL différente/datée plus récemment) ;
  2. une base de données en ligne / API publique plus à jour que le PDF actuel
     (comme trouvé pour le Nigeria : NAFDAC Greenbook derrière une API JSON
     publique, alors qu'on utilisait un vieux PDF 2016-2018).

Règles de conformité impératives :
{_COMPLIANCE_RULES}

Réponds en deux parties :

1. Un résumé en français, lisible par un humain, de ce que tu as trouvé pour
   chaque source (même "rien de nouveau" doit être dit explicitement).

2. Un bloc JSON (```json ... ```) listant UNIQUEMENT les changements sûrs et
   mécaniques : une source déjà configurée dont tu as trouvé une URL PDF plus
   récente, avec EXACTEMENT le même format de tableau (donc le parseur Python
   existant marchera sans modification). Format :

```json
{{"changements": [{{"source": "onssa_maroc", "url_actuelle": "...", "url_proposee": "...", "raison": "édition mise à jour trouvée sur le site officiel, publiée le ...", "date_nouvelle_edition": "YYYY-MM-DD"}}]}}
```

Si aucun changement sûr n'est trouvé, renvoie `{{"changements": []}}`. NE PROPOSE
JAMAIS de nouvelle source nécessitant un nouveau parseur (une API JSON inédite,
une structure de tableau différente) dans ce bloc JSON — décris-la seulement
dans le résumé texte, pour qu'un humain l'intègre à la main."""


def _call_claude(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 30}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _extract_json(text: str) -> dict:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        return {"changements": []}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {"changements": []}


def _write_report(text: str) -> Path:
    RESEARCH_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()
    path = RESEARCH_DIR / f"{today}.md"
    header = f"# Recherche de sources plus fraîches — {today}\n\n"
    path.write_text(header + text, encoding="utf-8")
    return path


def _apply_safe_changes(changements: list[dict]) -> list[dict]:
    """Applique dans config.yaml les changements d'URL simples. Retourne ceux appliqués."""
    if not changements:
        return []
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    applied = []
    for ch in changements:
        old, new = ch.get("url_actuelle"), ch.get("url_proposee")
        if not old or not new or old not in raw:
            continue
        raw = raw.replace(old, new)
        applied.append(ch)
    if applied:
        CONFIG_PATH.write_text(raw, encoding="utf-8")
    return applied


def _sh(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _open_pull_request(applied: list[dict], report_path: Path) -> None:
    branch = f"research/{date.today().isoformat()}"
    _sh("git", "checkout", "-b", branch)
    _sh("git", "add", str(report_path), "config/config.yaml")
    body_lines = [f"- **{c['source']}** : {c['raison']}" for c in applied]
    _sh("git", "commit", "-m",
        "Recherche hebdomadaire : URLs plus fraîches trouvées\n\n"
        + "\n".join(body_lines))
    _sh("git", "push", "-u", "origin", branch)
    _sh(
        "gh", "pr", "create",
        "--title", f"Sources plus fraîches trouvées ({date.today().isoformat()})",
        "--body",
        "Proposé automatiquement par l'agent de recherche hebdomadaire "
        "(scripts/research_sources.py). À relire avant fusion : vérifier que "
        "le format du nouveau PDF est bien identique, puis relancer la "
        "collecte pour la source concernée.\n\n" + "\n".join(body_lines),
        "--base", "main", "--head", branch,
    )


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY absent — rien à faire.", file=sys.stderr)
        return 1

    prompt = _build_prompt()
    text = _call_claude(prompt)
    report_path = _write_report(text)
    print(f"Rapport écrit : {report_path}")

    parsed = _extract_json(text)
    applied = _apply_safe_changes(parsed.get("changements", []))

    if applied:
        print(f"{len(applied)} changement(s) sûr(s) appliqué(s), ouverture d'une PR…")
        _open_pull_request(applied, report_path)
    else:
        # Toujours committer le rapport, même sans changement de config.
        _sh("git", "add", str(report_path))
        status = _sh("git", "status", "--porcelain")
        if status.strip():
            _sh("git", "commit", "-m",
                f"Recherche hebdomadaire {date.today().isoformat()} : rien à changer")
            _sh("git", "push", "origin", "main")
        print("Aucun changement sûr trouvé — rapport committé sur main.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
