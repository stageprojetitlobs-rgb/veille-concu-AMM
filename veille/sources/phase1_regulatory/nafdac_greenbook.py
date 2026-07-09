"""Source AMM Nigeria — NAFDAC Greenbook (base publique en ligne).

Le vieux PDF NAFDAC ("List of Registered Animal Health Products") n'est mis à
jour qu'épisodiquement (dernières entrées observées : 2018). Le Greenbook
(greenbook.nafdac.gov.ng) est la base de données OFFICIELLE et activement
maintenue de NAFDAC ; elle expose une API JSON publique derrière son tableau
DataTables (endpoint : la racine du site, appelée en AJAX par le site lui-même).

Conformité :
  - robots.txt de greenbook.nafdac.gov.ng : entièrement ouvert (`Disallow:` vide).
  - Aucune authentification, aucun contournement : c'est l'API publique que le
    site utilise pour afficher son propre tableau de recherche au public.
  - On complète le PDF historique (nafdac_nigeria), on ne le remplace pas : le
    Greenbook ne couvre que les produits qu'il a lui-même digitalisés.

Filtrage vétérinaire : `product_category_id == 6` ("Veterinary" dans
`/productCategories`). On paginate sur l'ensemble du catalogue (~9000 produits
toutes catégories) et on ne garde que cette catégorie.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_BASE_URL = "https://greenbook.nafdac.gov.ng"
_VETERINARY_CATEGORY_ID = 6
_PAGE_SIZE = 1000

_CLEAN_RE = re.compile(r"[#*]+")


def _clean_name(raw: str) -> str:
    """Le site lui-même retire les marqueurs '#'/'*' à l'affichage (search.js)."""
    return _CLEAN_RE.sub("", raw or "").strip()


def _parse_date(raw: str | None):
    from datetime import date
    if not raw:
        return None
    try:
        y, m, d = raw.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


class NafdacGreenbookSource(Source):
    """NAFDAC Greenbook — API JSON publique (produits vétérinaires). Config :

        sources:
          nafdac_greenbook:
            enabled: true
            inclure_tous_produits: true
    """

    name = "nafdac_greenbook"

    def fetch(self) -> list[Record]:
        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        delay = self.settings.download_delay_s

        client = httpx.Client(
            headers={
                "User-Agent": self.settings.user_agent,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            timeout=max(self.settings.http_timeout_s, 60),
        )

        records: list[Record] = []
        seen: set[str] = set()
        start = 0
        total = None

        while total is None or start < total:
            try:
                resp = client.get(_BASE_URL, params={
                    "draw": 1, "start": start, "length": _PAGE_SIZE,
                })
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.error("nafdac_greenbook : échec page start=%d (%s)", start, exc)
                break

            total = payload.get("recordsTotal", 0)
            batch = payload.get("data", [])
            if not batch:
                break

            for item in batch:
                if item.get("product_category_id") != _VETERINARY_CATEGORY_ID:
                    continue

                reg_no = (item.get("NAFDAC") or "").strip()
                produit = _clean_name(item.get("product_name", ""))
                if not reg_no or not produit:
                    continue
                uid = reg_no.lower()
                if uid in seen:
                    continue
                seen.add(uid)

                applicant = (item.get("applicant") or {}).get("name") or ""
                ingredient = (item.get("ingredient") or {}).get("ingredient_name") or ""
                molecules = [ingredient] if ingredient else []

                concurrent = self.settings.matched_concurrent(f"{applicant} {produit}")
                if not concurrent and not inclure_tous:
                    continue

                rec = Record(
                    source=self.name,
                    source_uid=uid,
                    record_type=RecordType.NOUVELLE_AMM,
                    concurrent=concurrent,
                    produit=produit,
                    molecules=molecules,
                    pays="NG",
                    url=f"{_BASE_URL}/products/details/{item.get('product_id', '')}",
                    date_source=_parse_date(item.get("approval_date")),
                    tags=self.settings.keywords_in(f"{produit} {ingredient}"),
                    extra={
                        "titulaire": applicant,
                        "numero_amm": reg_no,
                        "forme": (item.get("form") or {}).get("name") or "",
                        "voie": (item.get("route") or {}).get("name") or "",
                        "dosage": item.get("strength") or "",
                        "statut": item.get("status") or "",
                        "date_expiration": item.get("expiry_date") or "",
                        "registre": "NAFDAC Greenbook",
                    },
                )
                rec.compute_hashes()
                records.append(rec)

            start += _PAGE_SIZE
            if delay > 0:
                time.sleep(delay)

        client.close()
        log.info("nafdac_greenbook : %d AMM vétérinaire(s) retenue(s)", len(records))
        return records
