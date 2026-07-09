"""Collecteur RSS/Atom générique — canaux officiels de syndication.

Cas d'usage initial : Laprovet. Le site catalogue laprovet.fr **bloque** le scraping
externe (connexions reset/refused), mais le site corporate laprovet.com (WordPress)
expose un **flux RSS officiel** `https://www.laprovet.com/wp/feed/` — un flux de
syndication est explicitement destiné à être consommé, donc 100 % conforme
(cf CLAUDE.md : « si une source bloque, on bascule sur son canal officiel »).

Ce collecteur est volontairement **générique** : il lit une liste de flux depuis
config.yaml (`sources.rss.feeds`) et les mappe vers le schéma pivot. Réutilisable pour
tout concurrent disposant d'un flux officiel (RSS 2.0 ou Atom).

Conformité :
  - on ne lit QUE des flux de syndication publics et officiels ;
  - rate-limiting : `download_delay_s` respecté entre deux requêtes ;
  - aucun contournement, aucun scraping de pages protégées.

Le flux éditorial complète (sans remplacer) la source 1 ANSES : l'ANSES porte le
régulatoire « dur » (AMM), le RSS porte les signaux qualitatifs (campagnes, zones).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

# Espaces de noms rencontrés dans les flux Atom.
_ATOM = "{http://www.w3.org/2005/Atom}"


import re as _re
_HTML_TAG = _re.compile(r"<[^>]+>")
_WHITESPACE = _re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    """Supprime les balises HTML et normalise les espaces."""
    text = _HTML_TAG.sub(" ", raw or "")
    return _WHITESPACE.sub(" ", text).strip()


def _text(el: ET.Element | None) -> str:
    return _strip_html(el.text or "") if el is not None else ""


def _parse_date(raw: str) -> datetime | None:
    """RSS = RFC822 (pubDate) ; Atom = ISO8601 (updated/published)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)  # RFC822
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))  # ISO8601
    except ValueError:
        return None


def _parse_items(xml_bytes: bytes) -> list[dict]:
    """Extrait les entrées d'un flux RSS 2.0 ou Atom en dicts homogènes.

    Renvoie : [{uid, titre, lien, date, resume}]. `uid` est l'identifiant stable
    (guid/id, sinon lien) — base de la dédup.
    """
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []

    # --- RSS 2.0 : <rss><channel><item>... ---
    for it in root.iter("item"):
        link = _text(it.find("link"))
        guid = _text(it.find("guid")) or link
        items.append({
            "uid": guid,
            "titre": _text(it.find("title")),
            "lien": link,
            "date": _parse_date(_text(it.find("pubDate"))),
            "resume": _text(it.find("description")),
        })
    if items:
        return items

    # --- Atom : <feed><entry>... ---
    for entry in root.iter(f"{_ATOM}entry"):
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        uid = _text(entry.find(f"{_ATOM}id")) or link
        date = _parse_date(_text(entry.find(f"{_ATOM}updated"))
                           or _text(entry.find(f"{_ATOM}published")))
        items.append({
            "uid": uid,
            "titre": _text(entry.find(f"{_ATOM}title")),
            "lien": link,
            "date": date,
            "resume": _text(entry.find(f"{_ATOM}summary")),
        })
    return items


class RssSource(Source):
    """Source RSS/Atom multi-flux. Config attendue (config.yaml) :

        sources:
          rss:
            enabled: true
            # Si false (défaut), on ne retient que les items mentionnant un concurrent
            # connu (matching titre+résumé) OU rattachés explicitement via `concurrent`.
            inclure_tous_items: false
            feeds:
              - concurrent: Laprovet          # nom canonique (cf concurrents:)
                url: https://www.laprovet.com/wp/feed/
                pays: FR
    """

    name = "rss"

    def fetch(self) -> list[Record]:
        feeds = self.cfg.get("feeds", []) or []
        delay = self.settings.download_delay_s
        records: list[Record] = []

        for i, feed in enumerate(feeds):
            url = feed.get("url")
            if not url:
                log.warning("Flux RSS sans url, ignoré : %r", feed)
                continue
            if i > 0 and delay > 0:
                time.sleep(delay)  # rate-limiting entre hôtes
            try:
                xml_bytes = self._download(url)
            except httpx.HTTPError as e:
                log.error("RSS %s : échec téléchargement (%s)", url, e)
                continue
            try:
                items = _parse_items(xml_bytes)
            except ET.ParseError as e:
                log.error("RSS %s : flux illisible (%s)", url, e)
                continue

            log.info("RSS %s : %d entrées", url, len(items))
            for item in items:
                rec = self._to_record(item, feed)
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

    def _to_record(self, item: dict, feed: dict) -> Record | None:
        titre = item.get("titre") or ""
        resume = item.get("resume") or ""
        uid = item.get("uid")
        if not uid:
            return None  # pas d'identifiant stable → dédup impossible

        # Concurrent : rattachement explicite via le flux, sinon matching sur le texte.
        concurrent = feed.get("concurrent")
        if concurrent:
            concurrent = self.settings.matched_concurrent(concurrent) or concurrent
        else:
            concurrent = self.settings.matched_concurrent(f"{titre} {resume}")

        if not concurrent and not self.cfg.get("inclure_tous_items", False):
            return None

        date_dt = item.get("date")
        rec = Record(
            source=self.name,
            source_uid=uid,
            record_type=RecordType.ACTUALITE,
            concurrent=concurrent,
            produit=titre or None,   # titre de l'article = libellé du signal
            pays=feed.get("pays"),
            url=item.get("lien") or None,
            date_source=date_dt.date() if date_dt else None,
            tags=self.settings.keywords_in(f"{titre} {resume}"),
            extra={
                "titulaire": concurrent,
                "flux": feed.get("url"),
                "resume": resume[:500],
            },
        )
        rec.compute_hashes()
        return rec
