"""Collecteur générique — pages actualités / communiqués de presse HTML.

Pour les concurrents qui n'exposent pas de flux RSS mais dont le site est
server-rendered (HTML statique). Scraping minimal, ciblé sur la liste d'articles.

Sources initiales :
  - Hipra  : https://www.hipra.com/en/press/press-releases
             https://www.hipra.com/en/news
  - Dechra : https://www.dechra.com/news

Conformité :
  - robots.txt vérifié avant chaque requête (urllib.robotparser — stdlib).
  - rate-limiting : download_delay_s entre chaque hôte.
  - On ne scrape QUE les pages listées dans config.yaml.
  - On stocke uniquement titre + URL + date (données publiques).
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lxml_html

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)


# ── Sélecteurs par site ───────────────────────────────────────────────────────
# Chaque entrée : liste de tuples (xpath_titre, xpath_lien, xpath_date)
# On essaie dans l'ordre jusqu'au premier qui retourne des résultats.
_SITE_SELECTORS: list[dict[str, Any]] = [
    {
        # Hipra — /en/press/press-releases
        # Articles : <a href="/en/press/press-releases/{slug}" hreflang="en">
        # Les liens de nav n'ont PAS l'attribut hreflang → filtre fiable.
        # La date est dans le div.field--name-node-post-date suivant le h3.
        "host_contains": "hipra.com",
        "items": ".//a[contains(@href,'/en/press/press-releases/') and @hreflang]"
                 " | .//a[contains(@href,'/en/corporate-news/') and @hreflang]",
        "titre": ".",
        "lien": "@href",
        "date": None,
    },
    {
        # Dechra — /news
        # Chaque article = <a class="js-dvp-news-list" href="/news/...">
        # Titre = <p class="h3 mb-1..."> dans le col-md-10
        # Date  = <p class="my-0 font-weight-bold"> dans col-md-1 (desktop, ex "01 Apr")
        "host_contains": "dechra.com",
        "items": ".//a[contains(@class,'js-dvp-news-list') and contains(@href,'/news/')]",
        "titre": ".//div[contains(@class,'col-md-10')]/p[1]",
        "lien": "@href",
        "date": ".//div[contains(@class,'d-none d-md-block')]//p[contains(@class,'font-weight-bold')]",
    },
    {
        # Virbac — communiqués financiers + actualités presse
        # Structure : <div class="group"><p/><p/><div class="wysiwyg"><h3>Titre</h3></div><p><a href="/pagecontent/...">Read more</a></p></div>
        "host_contains": "virbac.com",
        "items": ".//div[contains(@class,'group') and .//a[contains(@href,'/pagecontent/')]]",
        "titre": ".//div[contains(@class,'wysiwyg')]//h3",
        "lien": ".//a[contains(@href,'/pagecontent/')]/@href",
        "date": None,
    },
    {
        # FATRO — /blog/news/
        # Structure PrestaShop : .ph_simpleblog h3 a
        "host_contains": "fatro.it",
        "items": ".//h3/a[contains(@href,'/blog/')]",
        "titre": ".",
        "lien": "@href",
        "date": None,
    },
    {
        # Calier — /es/media/
        # Structure : h4 a dans liste articles
        "host_contains": "calier.com",
        "items": ".//h4/a[@href] | .//h3/a[@href]",
        "titre": ".",
        "lien": "@href",
        "date": None,
    },
    {
        # IVDC Chine — 农业农村部公告 (annonces officielles d'AMM vétérinaires).
        # Canal officiel conforme : la base vdts.ivdc.org.cn:8099 est protégée
        # anti-bot (WAF + session) → on s'en tient aux annonces HTML publiées.
        # Filtre sur 兽药 (médicament vét) / 批准 (approbation).
        "host_contains": "ivdc.org.cn",
        "items": ".//li/a[contains(@href,'.htm') and (contains(.,'兽药') or contains(.,'批准'))]",
        "titre": ".",
        "lien": "@href",
        "date": None,
    },
]

_GENERIC_SELECTORS = [
    ".//article//h2/a[@href]",
    ".//article//h3/a[@href]",
    ".//div[contains(@class,'news')]//h2/a[@href]",
    ".//div[contains(@class,'news')]//h3/a[@href]",
    ".//div[contains(@class,'press')]//h3/a[@href]",
    ".//h3/a[@href]",
    ".//h2/a[@href]",
]


def _robots_allows(page_url: str, user_agent: str, timeout_s: float) -> bool:
    parsed = urlparse(page_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as exc:
        log.debug("robots.txt inaccessible pour %s (%s) → autorisé", robots_url, exc)
        return True
    allowed = rp.can_fetch(user_agent, page_url)
    if not allowed:
        log.info("robots.txt interdit %s → saut", page_url)
    return allowed


def _el_text(el: "lxml_html.HtmlElement | None", xpath: str | None) -> str:
    if el is None or xpath is None:
        return ""
    if xpath == ".":
        parts = el.itertext()
        return " ".join(p.strip() for p in parts if p.strip())
    found = el.xpath(xpath)
    if not found:
        return ""
    target = found[0]
    if isinstance(target, str):
        return target.strip()
    parts = target.itertext()
    return " ".join(p.strip() for p in parts if p.strip())


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d %B %Y", "%d-%m-%Y", "%Y-%m-%d", "%B %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # Tente d'extraire l'année seule comme fallback
    import re
    m = re.search(r"\b(20\d{2})\b", raw)
    if m:
        try:
            return date(int(m.group(1)), 1, 1)
        except ValueError:
            pass
    return None


def _selector_for_host(url: str) -> dict[str, Any] | None:
    host = urlparse(url).netloc
    for s in _SITE_SELECTORS:
        if s["host_contains"] in host:
            return s
    return None


def _extract_articles(
    page_url: str,
    html_bytes: bytes,
) -> list[dict[str, str]]:
    """Parse la page et retourne [{titre, url, date_raw}]."""
    try:
        tree = lxml_html.fromstring(html_bytes, base_url=page_url)
    except Exception as exc:
        log.warning("news_pages : parse HTML échoué pour %s : %s", page_url, exc)
        return []

    sel = _selector_for_host(page_url)
    results: list[dict] = []

    if sel:
        # Le champ "items" peut contenir plusieurs XPath séparés par " | "
        # lxml supporte les unions XPath nativement dans un seul appel.
        items = tree.xpath(sel["items"])
        for el in items:
            titre = _el_text(el, sel["titre"])
            lien_raw = _el_text(el, sel["lien"])
            date_raw = _el_text(el, sel.get("date"))

            if not titre or not lien_raw:
                continue
            url_abs = urljoin(page_url, lien_raw)
            # Filtre : même domaine uniquement
            if urlparse(url_abs).netloc != urlparse(page_url).netloc:
                continue
            results.append({"titre": titre, "url": url_abs, "date_raw": date_raw})
    else:
        # Fallback générique
        for xpath in _GENERIC_SELECTORS:
            items = tree.xpath(xpath)
            if not items:
                continue
            for el in items:
                titre_parts = list(el.itertext())
                titre = " ".join(p.strip() for p in titre_parts if p.strip())
                href = el.get("href", "")
                if not titre or not href:
                    continue
                url_abs = urljoin(page_url, href)
                if urlparse(url_abs).netloc != urlparse(page_url).netloc:
                    continue
                results.append({"titre": titre, "url": url_abs, "date_raw": ""})
            if results:
                break

    # Déduplique par URL
    seen: set[str] = set()
    deduped = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    return deduped


class NewsPagesSource(Source):
    """Scrape les pages actualités / press-release des concurrents sans RSS.

    Config attendue :
        sources:
          news_pages:
            enabled: true
            pages:
              - concurrent: Hipra
                url: "https://www.hipra.com/en/press/press-releases"
                pays: ES
              - concurrent: Hipra
                url: "https://www.hipra.com/en/news"
                pays: ES
              - concurrent: Dechra
                url: "https://www.dechra.com/news"
                pays: GB
    """

    name = "news_pages"

    def fetch(self) -> list[Record]:
        pages = self.cfg.get("pages") or []
        if not pages:
            log.warning("news_pages : aucune page configurée")
            return []

        delay = self.settings.download_delay_s
        records: list[Record] = []
        seen_urls: set[str] = set()

        for page_cfg in pages:
            page_url = page_cfg.get("url", "")
            concurrent = page_cfg.get("concurrent", "")
            pays = page_cfg.get("pays", "")

            # `concurrent` est optionnel : certaines pages (ex. annonces d'un
            # régulateur) couvrent tout le marché, sans concurrent unique.
            if not page_url:
                continue

            # Vérification robots.txt
            if not _robots_allows(page_url, self.settings.user_agent, self.settings.http_timeout_s):
                continue

            log.info("news_pages : collecte %s (%s)", concurrent or pays or "marché", page_url)
            try:
                resp = httpx.get(
                    page_url,
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=self.settings.http_timeout_s,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("news_pages : erreur HTTP %s : %s", page_url, exc)
                if delay > 0:
                    time.sleep(delay)
                continue

            articles = _extract_articles(page_url, resp.content)
            log.info("news_pages : %d article(s) extrait(s) depuis %s", len(articles), page_url)

            for art in articles:
                art_url = art["url"]
                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)

                titre = art["titre"]
                date_src = _parse_date(art["date_raw"])

                # source_uid = URL canonique de l'article
                tags = self.settings.keywords_in(titre)

                rec = Record(
                    source=self.name,
                    source_uid=art_url,
                    record_type=RecordType.ACTUALITE,
                    concurrent=concurrent or self.settings.matched_concurrent(titre) or None,
                    produit=titre or None,
                    molecules=[],
                    pays=pays,
                    url=art_url,
                    date_source=date_src,
                    tags=tags,
                    extra={
                        "page_source": page_url,
                        "resume": "",
                    },
                )
                rec.compute_hashes()
                records.append(rec)

            if delay > 0:
                time.sleep(delay)

        log.info("news_pages : %d article(s) concurrents retenus", len(records))
        return records
