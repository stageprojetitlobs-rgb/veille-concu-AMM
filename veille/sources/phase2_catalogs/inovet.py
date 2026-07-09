"""Source — Inovet (inovet.eu).

Qualification (cf SOURCES.md) :
  - robots.txt : autorise tout sauf /admin /shop/basket /profile /checkout /productcompare ✅
  - Sitemap    : /fr-fr/sitemap_0.xml — 1161 produits (entrées product URL) ✅
  - Pages      : JS obligatoire (SPA) → pages individuelles inaccessibles sans
                 moteur headless

Approche retenue : parsing du sitemap fr-fr public uniquement. Les URLs portent
une structure riche :
    /fr-fr/{zone}/{pays}/{slug-produit}-p{id}

Exemples :
    /fr-fr/moyen-orient/egypte/doxyveto-100-mg-g-premix-p000123
    /fr-fr/afrique/mali/vaccin-nd-p000456

Ce que l'on en extrait :
  - `source_uid` = pID (stable, identifiant produit dans l'URL)
  - `produit`    = nom humanisé depuis le slug (ex. "DOXYVETO 100 MG G PREMIX")
  - `pays`       = liste dédupliquée de pays/zones où le produit est présent
  - `zones`      = sous-ensemble ciblé (afrique, moyen-orient, maghreb) → extra
  - `tags`       = mots-clés stratégiques détectés dans le nom

Valeur veille : Inovet est un distributeur international présent en Afrique et
au Moyen-Orient — zones cibles de Lobs. Un nouveau pID = nouveau produit dans
leur catalogue. Un changement de pays = expansion géographique.

Zones stratégiques configurées sous `zones_cibles` (config.yaml).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

import httpx

from veille.countries import to_iso
from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_SITEMAP_URL = "https://www.inovet.eu/fr-fr/sitemap_0.xml"

# /fr-fr/{zone}/{pays}/{slug}-p{id}
_PRODUCT_URL = re.compile(
    r"https://www\.inovet\.eu/fr-fr/([^/]+)/([^/]+)/([\w\-]+)-p(\d+)/?$"
)

# Zones stratégiques par défaut (alignées sur la cible géo Lobs)
_DEFAULT_ZONES = ["afrique", "moyen-orient", "maghreb"]


def _slug_to_name(slug: str) -> str:
    """'doxyveto-100-mg-g-premix' → 'DOXYVETO 100 MG G PREMIX'"""
    return slug.replace("-", " ").upper()


def _normalise_country(raw: str) -> str:
    """Segment d'URL → code ISO ('cote-d-ivoire' → 'CI').

    On passe par le nom lisible puis `to_iso` (veille.countries) pour rester
    cohérent avec les registres AMM qui émettent déjà des codes ISO.
    """
    return to_iso(raw.replace("-", " ").replace("é", "e").title())


class InovetSource(Source):
    """Inovet — catalogue via sitemap fr-fr (JS requis pour les pages).

    Config attendue (config.yaml) :

        sources:
          inovet:
            enabled: true
            sitemap_url: "https://www.inovet.eu/fr-fr/sitemap_0.xml"  # optionnel
            # Zones conservées (toutes si absent ou vide)
            zones_cibles: [afrique, moyen-orient, maghreb]
            # Si true, conserve aussi les produits hors zones cibles
            inclure_toutes_zones: false
    """

    name = "inovet"

    def fetch(self) -> list[Record]:
        sitemap_url = self.cfg.get("sitemap_url", _SITEMAP_URL)
        xml_bytes = self._download(sitemap_url)
        products = _parse_sitemap(xml_bytes)

        zones_cibles = set(self.cfg.get("zones_cibles", _DEFAULT_ZONES))
        inclure_toutes = self.cfg.get("inclure_toutes_zones", False)

        log.info("inovet sitemap : %d produits uniques (pID)", len(products))

        records = []
        for pid, info in products.items():
            # Filtrage zone si demandé
            zones_presentes = set(info["zones"])
            if not inclure_toutes and not zones_presentes.intersection(zones_cibles):
                continue
            rec = self._to_record(pid, info, zones_cibles)
            if rec is not None:
                records.append(rec)

        log.info("inovet : %d produits après filtre zones", len(records))
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

    def _to_record(self, pid: str, info: dict, zones_cibles: set) -> Record | None:
        name = info["name"]
        concurrent = self.settings.matched_concurrent("inovet")
        if not concurrent and not self.cfg.get("inclure_tous_produits", False):
            log.warning("inovet : concurrent non résolu (vérifier config concurrents)")
            return None

        tags = self.settings.keywords_in(name)
        pays_strategiques = sorted(
            {p for z, p in info["pays_par_zone"] if z in zones_cibles}
        )
        tous_pays = sorted(set(info["pays"]))
        toutes_zones = sorted(set(info["zones"]))

        # On prend une URL représentative (premier pays stratégique sinon première)
        url = info.get("url_sample")

        rec = Record(
            source=self.name,
            source_uid=pid,
            record_type=RecordType.PRODUIT,
            concurrent=concurrent,
            produit=name,
            molecules=[],           # non disponible sans JS
            pays=", ".join(pays_strategiques) or ", ".join(tous_pays[:3]),
            url=url,
            date_source=None,
            tags=tags,
            extra={
                "titulaire": "Inovet",
                "pid": pid,
                "slug": info["slug"],
                "zones": toutes_zones,
                "pays_strategiques": pays_strategiques,
                "tous_pays": tous_pays,
                "nb_pays": len(tous_pays),
                "note": "pages non accessibles (JS requis) — signal de présence depuis sitemap",
            },
        )
        rec.compute_hashes()
        return rec


def _parse_sitemap(xml_bytes: bytes) -> dict[str, dict]:
    """Parse le sitemap inovet fr-fr et regroupe par pID.

    Retourne {pid: {name, slug, zones, pays, pays_par_zone, url_sample}}.
    Les doublons de pays (même produit, même pays, pIDs différents) sont tracés.
    """
    root = ET.fromstring(xml_bytes)
    by_pid: dict[str, dict] = {}

    for loc_el in root.findall(".//sm:loc", _NS):
        url = (loc_el.text or "").strip()
        m = _PRODUCT_URL.match(url)
        if not m:
            continue
        zone, pays_raw, slug, pid = m.group(1), m.group(2), m.group(3), m.group(4)

        if pid not in by_pid:
            by_pid[pid] = {
                "name": _slug_to_name(slug),
                "slug": slug,
                "zones": [],
                "pays": [],
                "pays_par_zone": [],
                "url_sample": url,
            }

        entry = by_pid[pid]
        pays = _normalise_country(pays_raw)
        if zone not in entry["zones"]:
            entry["zones"].append(zone)
        if pays not in entry["pays"]:
            entry["pays"].append(pays)
        entry["pays_par_zone"].append((zone, pays))

    return by_pid
