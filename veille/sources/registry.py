"""Registre des sources disponibles.

Pour ajouter une source : l'implémenter sous `sources/` puis l'enregistrer ici.
"""
from __future__ import annotations

from veille.settings import Settings
from veille.sources.base import Source
from veille.sources.generic.rss import RssSource
from veille.sources.generic.news_pages import NewsPagesSource
from veille.sources.phase1_regulatory.anses_anmv import AnsesAnmvSource
from veille.sources.phase1_regulatory.ema_upd import EmaUpdSource
from veille.sources.phase1_regulatory.onssa_maroc import OnssaMarocSource
from veille.sources.phase1_regulatory.nafdac_nigeria import NafdacNigeriaSource
from veille.sources.phase1_regulatory.nafdac_greenbook import NafdacGreenbookSource
from veille.sources.phase1_regulatory.cdsco_inde import CdscoIndeSource
from veille.sources.phase1_regulatory.pdf_registry import PdfRegistrySource
from veille.sources.phase1_regulatory.zamra_zambie import ZamraZambieSource
from veille.sources.phase1_regulatory.mcaz_zimbabwe import McazZimbabweSource
from veille.sources.phase1_regulatory.uemoa_siar import UemoaSiarSource
from veille.sources.phase1_regulatory.cucthuy_vietnam import CucthuyVietnamSource
from veille.sources.phase1_regulatory.bnvf_bangladesh import BnvfBangladeshSource
from veille.sources.phase2_catalogs.kepro import KeprosSource
from veille.sources.phase2_catalogs.inovet import InovetSource
from veille.sources.phase3_salons.space import SpaceSource
from veille.sources.phase4_rh.francetravail import FranceTravailSource
from veille.sources.phase4_rh.careers import CareersSource

# nom (clé config) -> classe Source
_SOURCES: dict[str, type[Source]] = {
    AnsesAnmvSource.name: AnsesAnmvSource,
    EmaUpdSource.name: EmaUpdSource,       # désactivée tant que l'accès OAuth2 n'est pas accordé
    OnssaMarocSource.name: OnssaMarocSource,  # AMM Maroc — PDF officiel ONSSA (liste positive)
    NafdacNigeriaSource.name: NafdacNigeriaSource,  # AMM Nigeria — PDF officiel NAFDAC (historique, 2016-2018)
    NafdacGreenbookSource.name: NafdacGreenbookSource,  # AMM Nigeria — Greenbook, API publique (2020-2024, plus frais)
    CdscoIndeSource.name: CdscoIndeSource,  # AMM Inde — PDF officiels CDSCO (Form-45/46)
    PdfRegistrySource.name: PdfRegistrySource,  # registres AMM nationaux en PDF (générique, multi-pays)
    ZamraZambieSource.name: ZamraZambieSource,  # AMM Zambie — API publique ZAMRA (JSON)
    McazZimbabweSource.name: McazZimbabweSource,  # AMM Zimbabwe — registre public MCAZ (JSON)
    UemoaSiarSource.name: UemoaSiarSource,  # AMM régionale UEMOA — portail SIAR (8 pays Afrique Ouest)
    CucthuyVietnamSource.name: CucthuyVietnamSource,  # AMM Vietnam — Excel officiel Cục Thú y
    BnvfBangladeshSource.name: BnvfBangladeshSource,  # AMM Bangladesh — National Veterinary Formulary (PDF)
    RssSource.name: RssSource,             # flux officiels de syndication (ex. Laprovet)
    NewsPagesSource.name: NewsPagesSource, # pages actualités HTML sans RSS (Hipra, Dechra)
    KeprosSource.name: KeprosSource,       # kepro.nl — sitemap (pages derrière login)
    InovetSource.name: InovetSource,       # inovet.eu — sitemap fr-fr (pages JS-only)
    SpaceSource.name: SpaceSource,         # SPACE Rennes — PDF watcher + probe eventmaker API
    FranceTravailSource.name: FranceTravailSource,  # Phase 4 — API officielle offres d'emploi
    CareersSource.name: CareersSource,     # Phase 4 — pages carrières concurrents
}


def available_sources() -> list[str]:
    return sorted(_SOURCES)


def build_source(name: str, settings: Settings) -> Source:
    if name not in _SOURCES:
        raise KeyError(f"Source inconnue : {name!r}. Dispo : {available_sources()}")
    return _SOURCES[name](settings)
