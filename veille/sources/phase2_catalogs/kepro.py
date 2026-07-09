"""Source — Kepro (kepro.nl).

Qualification (cf SOURCES.md) :
  - robots.txt : Disallow: (tout autorisé) ✅
  - Sitemap    : /products-sitemap.xml — 61 produits (root) + variantes fr/es ✅
  - Pages      : login requis (WP B2B portal) → pages individuelles inaccessibles

Approche retenue : parsing du sitemap public uniquement. Aucune page produit
n'est chargée. Les URLs racines (sans préfixe langue) contiennent le nom du
produit sous forme de slug — signal d'existence/nouveauté fiable.

Limites assumées :
  - Pas de molécules (non disponibles sans login)
  - Pas de PDF (idem)
  - Diff = apparition / disparition d'un produit dans le sitemap

Signal utile malgré tout : Kepro possède 1 AMM française (GLUCADEX) déjà dans
l'ANSES. Son catalogue complet (vaccins, antibiotiques, antiparasitaires) visible
ici complète la vue régulatoire par la vue commerciale.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_SITEMAP_URL = "https://www.kepro.nl/products-sitemap.xml"
# Regex : on ne retient que les URLs racines (sans préfixe langue /fr/ /es/)
_ROOT_PRODUCT = re.compile(r"https://www\.kepro\.nl/products/([\w\-]+)/?$")
# Nettoyage des slugs : supprime le suffixe numérique WordPress (-2, -3…)
_SLUG_SUFFIX = re.compile(r"-\d+$")


def _slug_to_name(slug: str) -> str:
    """'chlor-200-wsp-2' → 'CHLOR 200 WSP'"""
    slug = _SLUG_SUFFIX.sub("", slug)
    return slug.replace("-", " ").upper()


class KeprosSource(Source):
    """Kepro — catalogue via sitemap public (login requis pour les pages).

    Config attendue (config.yaml) :

        sources:
          kepro:
            enabled: true
            sitemap_url: "https://www.kepro.nl/products-sitemap.xml"  # optionnel
    """

    name = "kepro"

    def fetch(self) -> list[Record]:
        sitemap_url = self.cfg.get("sitemap_url", _SITEMAP_URL)
        xml_bytes = self._download(sitemap_url)
        slugs = _parse_sitemap(xml_bytes)
        log.info("kepro sitemap : %d produits racines", len(slugs))

        records = []
        for slug in slugs:
            rec = self._to_record(slug)
            if rec is not None:
                records.append(rec)
        return records

    def _download(self, url: str) -> bytes:
        resp = httpx.get(
            url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def _to_record(self, slug: str) -> Record | None:
        name = _slug_to_name(slug)
        concurrent = self.settings.matched_concurrent("kepro")
        if not concurrent and not self.cfg.get("inclure_tous_produits", False):
            # kepro est dans la config concurrents — si non matché, config manquante
            log.warning("kepro : concurrent non résolu (vérifier config concurrents)")
            return None

        tags = self.settings.keywords_in(name)
        rec = Record(
            source=self.name,
            source_uid=slug,                      # slug stable = natural key
            record_type=RecordType.PRODUIT,
            concurrent=concurrent,
            produit=name,
            molecules=[],                          # non disponible sans login
            pays="NL",
            url=f"https://www.kepro.nl/products/{slug}/",
            date_source=None,
            tags=tags,
            extra={
                "titulaire": "Kepro",
                "slug": slug,
                "note": "page non accessible (login) — signal d'existence depuis sitemap",
            },
        )
        rec.compute_hashes()
        return rec


def _parse_sitemap(xml_bytes: bytes) -> list[str]:
    """Renvoie les slugs des produits racines (sans préfixe langue)."""
    root = ET.fromstring(xml_bytes)
    slugs = []
    for loc_el in root.findall(".//sm:loc", _NS):
        url = (loc_el.text or "").strip()
        m = _ROOT_PRODUCT.match(url)
        if m:
            slugs.append(m.group(1))
    return slugs
