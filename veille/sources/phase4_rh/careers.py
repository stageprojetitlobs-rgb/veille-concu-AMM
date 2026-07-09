"""Source Phase 4 — Pages carrières des concurrents.

Canal complémentaire à France Travail : les concurrents publient souvent leurs offres
sur leur propre site bien avant (ou sans) passer par un jobboard.

Approche : crawl direct de la page carrières configurée pour chaque concurrent.
  - `robots.txt` vérifié en amont à chaque run (via urllib.robotparser — stdlib).
  - Extraction des offres via lxml (HTML) : titre + lien + date si dispo.
  - rate-limiting : délai entre chaque hôte.
  - Aucun login, aucun contournement.

Si robots.txt interdit le crawl → la source logge INFO et saute le concurrent.

Conformité :
  - robots.txt respecté.
  - On ne scrape QUE les pages explicitement listées dans config.yaml.
  - RGPD : aucune donnée personnelle (offres publiques) ; contacts non stockés.
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lxml_html

from veille.schema import Record, RecordType, normalize_text
from veille.sources.base import Source

log = logging.getLogger(__name__)

# Sélecteurs CSS / XPath utilisés pour détecter les offres d'emploi dans la page HTML.
# Ordre : du plus spécifique au plus générique. Premier match gagne.
_JOB_SELECTORS = [
    # Balises structurées schema.org/JobPosting
    ".//div[@itemtype='http://schema.org/JobPosting']",
    ".//article[@itemtype='http://schema.org/JobPosting']",
    # Classes fréquentes dans les ATS / sites WordPress emploi
    ".//div[contains(@class,'job-listing')]",
    ".//div[contains(@class,'job-offer')]",
    ".//div[contains(@class,'offre-emploi')]",
    ".//div[contains(@class,'career-item')]",
    ".//li[contains(@class,'job-item')]",
    ".//li[contains(@class,'job-listing')]",
    # Liens directs vers des offres (heuristique URL)
    ".//a[contains(@href,'emploi') or contains(@href,'job') or contains(@href,'offre') "
    "or contains(@href,'career') or contains(@href,'recrutement') "
    "or contains(@href,'poste')]",
]

# Mots-clés indiquant un poste à fort signal pour Lobs.
_MOTS_CLES_SIGNAL = [
    "export", "international", "zone",
    "afrique", "africa", "mena", "moyen-orient",
    "vétérinaire", "veterinaire", "santé animale", "sante animale",
    "chef de produit", "responsable commercial", "directeur",
    "productions animales", "elevage",
]


def _robots_allows(page_url: str, user_agent: str, timeout_s: float) -> bool:
    """Vérifie que robots.txt autorise le crawl de `page_url`."""
    parsed = urlparse(page_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as exc:
        # Robots.txt absent ou erreur réseau : on autorise (comportement standard).
        log.debug("robots.txt inaccessible pour %s (%s) → autorisé", robots_url, exc)
        return True
    allowed = rp.can_fetch(user_agent, page_url)
    if not allowed:
        log.info("robots.txt interdit le crawl de %s → saut", page_url)
    return allowed


def _extract_jobs(html_bytes: bytes, base_url: str) -> list[dict]:
    """Extrait les offres d'une page HTML. Retourne [{titre, url, date_raw}]."""
    try:
        tree = lxml_html.fromstring(html_bytes, base_url=base_url)
    except Exception as exc:
        log.warning("Parsing HTML échoué pour %s : %s", base_url, exc)
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for selector in _JOB_SELECTORS:
        elements = tree.xpath(selector)
        if not elements:
            continue

        for el in elements:
            # Titre : text_content() de l'élément ou attribut title/aria-label.
            titre = (el.text_content() or "").strip()
            if not titre:
                titre = el.get("title") or el.get("aria-label") or ""
            titre = " ".join(titre.split())[:200]

            # Lien.
            href = el.get("href") or ""
            if not href and el.tag != "a":
                a_els = el.xpath(".//a[@href]")
                href = a_els[0].get("href") if a_els else ""
            absolute_url = urljoin(base_url, href) if href else base_url

            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)

            # Date (optionnelle).
            date_raw = ""
            for date_el in el.xpath(".//*[@datetime]"):
                date_raw = date_el.get("datetime", "")
                break

            if titre or href:
                jobs.append({"titre": titre, "url": absolute_url, "date_raw": date_raw})

        if jobs:
            break  # on s'arrête au premier sélecteur qui donne des résultats

    return jobs


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(raw[:10])
        except (ValueError, TypeError):
            return None


class CareersSource(Source):
    """Scraper des pages carrières des concurrents. Config attendue :

        sources:
          careers:
            enabled: true
            pages:
              - concurrent: Axience
                url: "https://www.axience.com/recrutement/"
              - concurrent: Laprovet
                url: "https://www.laprovet.com/nous-rejoindre/"
              - concurrent: Osalia
                url: "https://www.osalia.fr/recrutement/"
              - concurrent: Kepro
                url: "https://www.kepro.nl/careers/"
    """

    name = "careers"

    def fetch(self) -> list[Record]:
        pages = self.cfg.get("pages") or []
        delay = self.settings.download_delay_s
        records: list[Record] = []

        for i, page_cfg in enumerate(pages):
            url = page_cfg.get("url")
            concurrent = page_cfg.get("concurrent")
            if not url or not concurrent:
                log.warning("careers : entrée invalide %r, ignorée", page_cfg)
                continue

            # robots.txt check.
            if not _robots_allows(url, self.settings.user_agent, self.settings.http_timeout_s):
                continue

            if i > 0 and delay > 0:
                time.sleep(delay)

            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=self.settings.http_timeout_s,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("careers %s : échec HTTP (%s)", url, exc)
                continue

            jobs = _extract_jobs(resp.content, base_url=str(resp.url))
            log.info("careers %s (%s) : %d offre(s) détectée(s)", concurrent, url, len(jobs))

            for job in jobs:
                rec = self._job_to_record(job, concurrent, url)
                if rec:
                    records.append(rec)

        return records

    def _job_to_record(self, job: dict, concurrent: str, page_url: str) -> Record | None:
        titre = job.get("titre") or ""
        job_url = job.get("url") or page_url
        date_src = _parse_date(job.get("date_raw"))

        # Déduplique sur (concurrent, URL normalisée).
        uid = f"{normalize_text(concurrent)}:{job_url}"

        # Tags : zones stratégiques + mots-clés métier.
        text = normalize_text(titre)
        zones = [m for m in _MOTS_CLES_SIGNAL if normalize_text(m) in text]
        tags = list(set(self.settings.keywords_in(titre)) | set(zones))

        # Un poste sans titre ET sur la page mère (pas de lien propre) = bruit.
        if not titre and job_url == page_url:
            return None

        rec = Record(
            source=self.name,
            source_uid=uid,
            record_type=RecordType.OFFRE_EMPLOI,
            concurrent=self.settings.matched_concurrent(concurrent) or concurrent,
            produit=titre or None,
            molecules=[],
            pays=page_cfg_pays(self.cfg, concurrent),
            url=job_url,
            date_source=date_src,
            tags=tags,
            extra={
                "page_source": page_url,
                "titre_brut": titre[:200],
            },
        )
        rec.compute_hashes()
        return rec


def page_cfg_pays(cfg: dict, concurrent: str) -> str | None:
    """Retourne le pays configuré pour un concurrent donné, ou None."""
    for p in cfg.get("pages") or []:
        if p.get("concurrent") == concurrent:
            return p.get("pays") or None
    return None
