"""Source — SPACE Rennes (salon des productions animales).

Qualification (cf SOURCES.md) :
  - robots.txt    : `Sitemap: …` + aucun Disallow pertinent ✅
  - Liste exposants : chargée via eventmaker.io (WebSocket + JS) → inaccessible
                     statiquement SAUF si le guestApiToken est configuré.
  - Presse        : /fr/dossier-de-presse expose des PDFs publics sur S3.
                    Le dossier de presse pré-salon = éditorial.
                    Un annuaire exposants PDF peut apparaître 2–6 semaines avant.

Deux modes complémentaires (tous deux sans JS) :

1. `pdf_watcher` (par défaut, actif en permanence)
   Surveille les pages presse pour de nouveaux PDFs. Quand un nouveau document
   apparaît, il est téléchargé, parsé avec pdfplumber et cherché pour :
   - mots-clés stratégiques (trypanocide, vaccin aviaire…)
   - noms de concurrents
   Si un nom de concurrent est trouvé dans un PDF, un Record EXPOSANT est émis.
   signal : apparition d'un nouveau PDF OU changement de contenu d'un PDF existant.

2. `eventmaker_api` (activé automatiquement si guestApiToken présent)
   Quand SPACE configure l'accès public à l'API eventmaker (typiquement 4–8 semaines
   avant le salon), le guestApiToken devient non vide dans la page. On appelle alors
   l'API JSON et on extrait la liste d'exposants proprement.
   Pattern API eventmaker : GET /v2/events/{slug}/companies?token={token}

SPACE 2026 : 15–17 septembre 2026 (40ème édition, ~900 exposants, 280 internationaux).
"""
from __future__ import annotations

import io
import logging
import re
import time
from datetime import date

import httpx
import pdfplumber

from veille.schema import Record, RecordType, normalize_text
from veille.sources.base import Source

log = logging.getLogger(__name__)

_PRESS_PAGES = [
    "https://www.space.fr/fr/dossier-de-presse",
    "https://www.space.fr/fr/espace-presse",
]
_EM_PAGE = "https://www.space.fr/fr/liste-des-exposants"
_PDF_PATTERN = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)
_EM_TOKEN = re.compile(r'guestApiToken:\s*["\']([^"\']{8,})["\']')
_EM_SLUG  = re.compile(r'eventSlug:\s*["\']([^"\']+)["\']')
_EM_EVENT_ID = re.compile(r'"eventId"\s*:\s*"?(\d+)"?')


class SpaceSource(Source):
    """SPACE Rennes — PDF watcher + sonde API eventmaker.

    Config attendue (config.yaml) :

        sources:
          space:
            enabled: true
            # Pages presse à surveiller pour de nouveaux PDFs
            press_pages:
              - "https://www.space.fr/fr/dossier-de-presse"
              - "https://www.space.fr/fr/espace-presse"
            # Page liste exposants (pour sonder le token eventmaker)
            exhibitors_page: "https://www.space.fr/fr/liste-des-exposants"
    """

    name = "space"

    def fetch(self) -> list[Record]:
        records: list[Record] = []
        delay = self.settings.download_delay_s

        # ── 1. Sonde eventmaker API ──────────────────────────────────────────
        em_records = self._probe_eventmaker()
        if em_records:
            log.info("SPACE eventmaker API : %d exposants extraits", len(em_records))
            return em_records  # API disponible → source de vérité, on skip les PDFs

        # ── 2. PDF watcher ───────────────────────────────────────────────────
        press_pages = self.cfg.get("press_pages", _PRESS_PAGES)
        seen_pdfs: set[str] = set()

        for i, page_url in enumerate(press_pages):
            if i > 0:
                time.sleep(delay)
            pdf_urls = self._get_pdf_links(page_url)
            for pdf_url in pdf_urls:
                if pdf_url in seen_pdfs:
                    continue
                seen_pdfs.add(pdf_url)
                time.sleep(delay)
                recs = self._process_pdf(pdf_url)
                records.extend(recs)

        log.info("SPACE PDF watcher : %d signal(s) dans %d PDF(s)", len(records), len(seen_pdfs))
        return records

    # ── Eventmaker probe ──────────────────────────────────────────────────────

    def _probe_eventmaker(self) -> list[Record]:
        """Si le guestApiToken est configuré sur la page exposants, appelle l'API."""
        em_page = self.cfg.get("exhibitors_page", _EM_PAGE)
        try:
            r = self._get(em_page)
        except httpx.HTTPError as e:
            log.warning("SPACE eventmaker page inaccessible : %s", e)
            return []

        token_m = _EM_TOKEN.search(r.text)
        if not token_m:
            log.info("SPACE eventmaker : guestApiToken absent ou vide — API non encore configurée")
            return []

        token = token_m.group(1)
        slug_m = _EM_SLUG.search(r.text)
        slug = slug_m.group(1) if slug_m else None
        log.info("SPACE eventmaker token trouvé ! slug=%s", slug)

        return self._fetch_eventmaker_companies(token, slug)

    def _fetch_eventmaker_companies(self, token: str, slug: str | None) -> list[Record]:
        """Appelle l'API eventmaker et mappe les entreprises en Records EXPOSANT."""
        # Essai des endpoints connus (v2 + fallback v1)
        slugs_to_try = [s for s in [slug, "space", "space-2026", "space2026"] if s]
        endpoints = [
            f"https://api.eventmaker.io/v2/events/{s}/companies" for s in slugs_to_try
        ] + [
            f"https://api.eventmaker.io/v2/events/{s}/exhibitors" for s in slugs_to_try
        ]

        for url in endpoints:
            try:
                r = httpx.get(
                    url,
                    params={"token": token},
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=self.settings.http_timeout_s,
                    follow_redirects=True,
                )
                if r.status_code == 200:
                    data = r.json()
                    companies = data if isinstance(data, list) else data.get("companies", data.get("exhibitors", []))
                    log.info("eventmaker API OK : %s → %d entreprises", url, len(companies))
                    return [rec for c in companies for rec in [self._company_to_record(c)] if rec]
            except Exception as e:
                log.debug("eventmaker endpoint %s : %s", url, e)
        log.warning("SPACE eventmaker token trouvé mais aucun endpoint API n'a répondu")
        return []

    def _company_to_record(self, company: dict) -> Record | None:
        nom = company.get("name") or company.get("company_name") or ""
        if not nom:
            return None
        concurrent = self.settings.matched_concurrent(nom)
        tags = self.settings.keywords_in(nom + " " + (company.get("description") or ""))
        uid = str(company.get("id") or company.get("uid") or nom)

        rec = Record(
            source=self.name,
            source_uid=f"em:{uid}",
            record_type=RecordType.EXPOSANT,
            concurrent=concurrent,
            produit=nom,
            molecules=[],
            pays="FR",
            url=company.get("website") or company.get("url"),
            date_source=date.today(),
            tags=tags,
            extra={
                "nom": nom,
                "pays_entreprise": company.get("country"),
                "categories": company.get("categories", []),
                "source_mode": "eventmaker_api",
            },
        )
        rec.compute_hashes()
        return rec

    # ── PDF watcher ───────────────────────────────────────────────────────────

    def _get_pdf_links(self, page_url: str) -> list[str]:
        try:
            r = self._get(page_url)
            return list(dict.fromkeys(  # dédupliqué, ordre préservé
                m.group(1) for m in _PDF_PATTERN.finditer(r.text)
            ))
        except httpx.HTTPError as e:
            log.warning("SPACE presse page %s inaccessible : %s", page_url, e)
            return []

    def _process_pdf(self, pdf_url: str) -> list[Record]:
        """Télécharge un PDF, cherche concurrents + mots-clés, émet un Record par hit."""
        try:
            r = self._get(pdf_url)
        except httpx.HTTPError as e:
            log.warning("SPACE PDF %s inaccessible : %s", pdf_url, e)
            return []

        content_type = r.headers.get("content-type", "")
        if "pdf" not in content_type and not pdf_url.lower().endswith(".pdf"):
            log.debug("SPACE : %s n'est pas un PDF (%s)", pdf_url, content_type)
            return []

        try:
            text = _extract_pdf_text(r.content)
        except Exception as e:
            log.warning("SPACE PDF parse error %s : %s", pdf_url, e)
            return []

        if not text.strip():
            return []

        log.info("SPACE PDF extrait : %s (%d chars)", pdf_url.split("/")[-1][:60], len(text))
        return self._pdf_to_records(text, pdf_url)

    def _pdf_to_records(self, text: str, pdf_url: str) -> list[Record]:
        """Crée un Record par concurrent trouvé dans le texte du PDF."""
        records = []
        norm_text = normalize_text(text)
        tags_global = self.settings.keywords_in(norm_text)

        # Cherche chaque concurrent dans le texte
        for concurrent_obj in self.settings.concurrents:
            if any(normalize_text(a) in norm_text for a in (concurrent_obj.nom, *concurrent_obj.aliases)):
                uid = f"pdf:{_pdf_uid(pdf_url)}:{normalize_text(concurrent_obj.nom)}"
                rec = Record(
                    source=self.name,
                    source_uid=uid,
                    record_type=RecordType.EXPOSANT,
                    concurrent=concurrent_obj.nom,
                    produit=f"Mention dans {pdf_url.split('/')[-1][:50]}",
                    molecules=[],
                    pays="FR",
                    url=pdf_url,
                    date_source=date.today(),
                    tags=tags_global,
                    extra={
                        "pdf_url": pdf_url,
                        "source_mode": "pdf_watcher",
                        "extrait": _find_excerpt(norm_text, normalize_text(concurrent_obj.nom)),
                    },
                )
                rec.compute_hashes()
                records.append(rec)

        # Si aucun concurrent mais mots-clés stratégiques → signal générique
        if not records and tags_global:
            uid = f"pdf:{_pdf_uid(pdf_url)}:keywords"
            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.EXPOSANT,
                concurrent=None,
                produit=f"Document SPACE : {pdf_url.split('/')[-1][:50]}",
                molecules=[],
                pays="FR",
                url=pdf_url,
                date_source=date.today(),
                tags=tags_global,
                extra={"pdf_url": pdf_url, "source_mode": "pdf_watcher"},
            )
            rec.compute_hashes()
            records.append(rec)

        return records

    def _get(self, url: str) -> httpx.Response:
        r = httpx.get(
            url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_pdf_text(content: bytes) -> str:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        parts = []
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n".join(parts)


def _pdf_uid(url: str) -> str:
    """UID stable depuis l'URL du PDF (dernier segment sans query)."""
    path = url.split("?")[0].rstrip("/")
    return path.split("/")[-1][:80]


def _find_excerpt(text: str, term: str, window: int = 120) -> str:
    """Extrait ~120 chars autour de la première occurrence du terme."""
    i = text.find(term)
    if i < 0:
        return ""
    start = max(0, i - window // 2)
    end = min(len(text), i + len(term) + window // 2)
    return f"…{text[start:end]}…"
