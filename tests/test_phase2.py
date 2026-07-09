"""Tests Phase 2 — kepro + inovet (parsing offline, sans réseau)."""
from __future__ import annotations

from veille.sources.phase2_catalogs.kepro import _parse_sitemap as kepro_parse_sitemap, _slug_to_name
from veille.sources.phase2_catalogs.inovet import _parse_sitemap as inovet_parse_sitemap

KEPRO_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.kepro.nl/products/chlor-200-wsp-2/</loc></url>
  <url><loc>https://www.kepro.nl/products/doxy-gen-2020-wsp-2/</loc></url>
  <url><loc>https://www.kepro.nl/fr/products/chlor-200-wsp-2/</loc></url>
  <url><loc>https://www.kepro.nl/es/products/chlor-200-wsp-2/</loc></url>
  <url><loc>https://www.kepro.nl/page-sitemap.xml</loc></url>
</urlset>"""

INOVET_SITEMAP = b"""<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.inovet.eu/fr-fr/afrique/mali/doxyveto-100-mg-g-premix-p000123</loc></url>
  <url><loc>https://www.inovet.eu/fr-fr/moyen-orient/egypte/doxyveto-100-mg-g-premix-p000123</loc></url>
  <url><loc>https://www.inovet.eu/fr-fr/europe/france/autre-produit-p000999</loc></url>
  <url><loc>https://www.inovet.eu/fr-fr/afrique/senegal/vaccin-aviaire-p000456</loc></url>
  <url><loc>https://www.inovet.eu/fr-fr/categories/</loc></url>
</urlset>"""


# --- kepro ---

def test_kepro_slug_to_name():
    assert _slug_to_name("chlor-200-wsp-2") == "CHLOR 200 WSP"
    assert _slug_to_name("doxy-gen-2020-wsp-2") == "DOXY GEN 2020 WSP"
    assert _slug_to_name("mastoline-2") == "MASTOLINE"
    assert _slug_to_name("mastoline") == "MASTOLINE"


def test_kepro_parse_sitemap_root_only():
    """Seules les URLs racines (sans /fr/ /es/) sont retenues."""
    slugs = kepro_parse_sitemap(KEPRO_SITEMAP)
    assert len(slugs) == 2
    assert "chlor-200-wsp-2" in slugs
    assert "doxy-gen-2020-wsp-2" in slugs


# --- inovet ---

def test_inovet_parse_sitemap_groups_by_pid():
    products = inovet_parse_sitemap(INOVET_SITEMAP)
    # p000123 apparaît dans 2 pays (mali + egypte)
    assert "000123" in products
    p = products["000123"]
    assert p["name"] == "DOXYVETO 100 MG G PREMIX"
    assert set(p["zones"]) == {"afrique", "moyen-orient"}
    assert len(p["pays"]) == 2

    # p000999 (europe/france) présent
    assert "000999" in products

    # p000456 (afrique/senegal)
    assert "000456" in products
    assert "afrique" in products["000456"]["zones"]


def test_inovet_zones_strategiques():
    """Seuls les produits avec zone afrique/moyen-orient/maghreb sont retenus."""
    products = inovet_parse_sitemap(INOVET_SITEMAP)
    zones_cibles = {"afrique", "moyen-orient", "maghreb"}
    strategiques = {
        pid for pid, info in products.items()
        if set(info["zones"]).intersection(zones_cibles)
    }
    # p000123 (afrique+mo), p000456 (afrique) = 2 produits stratégiques
    assert "000123" in strategiques
    assert "000456" in strategiques
    # p000999 (europe seulement) = non stratégique
    assert "000999" not in strategiques
