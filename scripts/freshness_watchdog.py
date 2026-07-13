"""Watchdog de fraîcheur — détecte qu'une source PDF/Excel déjà configurée a
changé de contenu à la MÊME URL (l'autorité a remplacé le fichier en place).

Ne détecte PAS une nouvelle édition publiée à une URL différente (ex. Ouganda
qui est passé de .../2018/... à .../2024/...) : ça reste du ressort d'une
recherche humaine/IA ponctuelle. Ce script couvre le cas gratuit, scriptable
sans IA : surveiller si le fichier déjà connu a bougé.

Respecte les règles de conformité du projet : un GET par URL (pas de boucle),
robots.txt vérifié avant toute requête, User-Agent identifié, timeout raisonnable.

Usage :
    python -m scripts.freshness_watchdog            # écrit data/freshness_state.json
    python -m scripts.freshness_watchdog --check-only  # n'écrit rien, code de sortie ≠0 si changement
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import yaml

CONFIG_PATH = Path("config/config.yaml")
STATE_PATH = Path("data/freshness_state.json")
USER_AGENT = "Mozilla/5.0 (compatible; VeilleLobsBot/1.0; +https://github.com/stageprojetitlobs-rgb/veille-concu-AMM)"


def _trackable_urls(cfg: dict) -> list[tuple[str, str]]:
    """(label, url) pour chaque fichier PDF/Excel déjà configuré.

    Liste explicite plutôt qu'auto-découverte : évite de suivre par erreur une
    API live (toujours "fraîche" par nature, pas de sens à la surveiller) ou
    un flux RSS. Ajouter une nouvelle source PDF/Excel ici en même temps que
    dans config.yaml (même piège que AMM_SOURCES dans dashboard.py).
    """
    s = cfg.get("sources", {})
    out: list[tuple[str, str]] = []

    def add(label: str, url: str | None) -> None:
        if url:
            out.append((label, url))

    add("onssa_maroc", s.get("onssa_maroc", {}).get("url_pdf"))
    add("nafdac_nigeria", s.get("nafdac_nigeria", {}).get("url_pdf"))
    for i, url in enumerate(s.get("cdsco_inde", {}).get("url_pdfs", [])):
        add(f"cdsco_inde[{i}]", url)
    add("bnvf_bangladesh", s.get("bnvf_bangladesh", {}).get("url_pdf"))
    add("minepia_cameroun", s.get("minepia_cameroun", {}).get("url_pdf"))
    add("cucthuy_vietnam", s.get("cucthuy_vietnam", {}).get("url_xlsx"))
    for reg in s.get("pdf_registry", {}).get("registries", []):
        add(f"pdf_registry[{reg.get('pays')}]", reg.get("url"))

    return out


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        resp = httpx.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=10, follow_redirects=True)
        if resp.status_code >= 400:
            return True  # pas de robots.txt = pas de restriction
        rp.parse(resp.text.splitlines())
    except httpx.HTTPError:
        return True  # injoignable : on ne bloque pas sur une erreur réseau ponctuelle
    return rp.can_fetch(USER_AGENT, url)


def _signature(url: str) -> dict | None:
    if not _robots_allows(url):
        print(f"  robots.txt interdit {url} → ignoré", file=sys.stderr)
        return None
    try:
        resp = httpx.head(url, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
        if resp.status_code >= 400 or "content-length" not in resp.headers:
            resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  échec requête {url} ({exc}) → ignoré cette fois", file=sys.stderr)
        return None
    return {
        "etag": resp.headers.get("etag", ""),
        "last_modified": resp.headers.get("last-modified", ""),
        "content_length": resp.headers.get("content-length", ""),
    }


def run(check_only: bool = False) -> list[str]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    urls = _trackable_urls(cfg)

    old_state: dict = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    new_state: dict = {}
    changed: list[str] = []

    for label, url in urls:
        sig = _signature(url)
        if sig is None:
            if url in old_state:
                new_state[url] = old_state[url]  # préserve l'état précédent, pas d'échec = changement
            continue
        new_state[url] = sig
        prev = old_state.get(url)
        if prev is not None and prev != sig:
            changed.append(f"- **{label}** : contenu modifié à la même URL — {url}")

    if not check_only:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(new_state, indent=2, ensure_ascii=False), encoding="utf-8")

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog de fraîcheur des sources PDF/Excel")
    parser.add_argument("--check-only", action="store_true", help="N'écrit pas l'état, code de sortie ≠0 si changement")
    args = parser.parse_args()

    changed = run(check_only=args.check_only)

    if changed:
        print("Changements détectés :")
        print("\n".join(changed))
    else:
        print("Aucun changement détecté.")

    if args.check_only and changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
