"""Source AMM Kenya — Veterinary Medicines Directorate (vmd.go.ke).

Le VMD publie son registre complet directement en HTML (tableau rendu côté
serveur, pas de PDF ni de JS requis), réparti sur 3 pages par catégorie :
`/pharmaceuticals`, `/biologicals`, `/feed-additives`. Chaque page contient
la totalité de sa catégorie sur une seule page (pas de pagination observée).

Marché est-africain jusque-là non couvert par la veille — Kenya est un débouché
export important pour Lobs. Premier passage : titulaires connus déjà repérés
dans les données (ex. Laprovet), confirmant la pertinence concurrentielle.

Conformité : robots.txt de vmd.go.ke autorise ces chemins (aucune règle
Disallow ne les couvre), pages HTML publiques destinées à la consultation par
le public (éleveurs, vétérinaires, importateurs) — aucune authentification,
aucun contournement.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import date

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_BASE = "https://www.vmd.go.ke"
_CATEGORIES = ("pharmaceuticals", "biologicals", "feed-additives")

_TAG_RE = re.compile(r"<[^>]+>")
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# Format observé : "Tue, 06/04/2019 - 12:00"
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _clean(cell: str) -> str:
    text = _TAG_RE.sub("", cell)
    # Le site échappe parfois deux fois (&amp;amp; -> &amp; -> &) : déséchapper 2x.
    text = html.unescape(html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> date | None:
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _parse_table(html: str) -> list[dict]:
    headers = [_clean(h).lower() for h in _TH_RE.findall(html)]
    if not headers:
        return []
    rows = []
    for tr in _TR_RE.findall(html):
        cells = [_clean(td) for td in _TD_RE.findall(tr)]
        if len(cells) < 4:
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


class VmdKenyaSource(Source):
    """VMD Kenya — registre HTML public (3 catégories). Config :

        sources:
          vmd_kenya:
            enabled: true
            inclure_tous_produits: true
    """

    name = "vmd_kenya"

    def fetch(self) -> list[Record]:
        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        delay = self.settings.download_delay_s

        records: list[Record] = []
        seen: set[str] = set()

        for cat in _CATEGORIES:
            url = f"{_BASE}/{cat}"
            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": self.settings.user_agent},
                    timeout=self.settings.http_timeout_s,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.error("vmd_kenya : échec %s (%s)", cat, exc)
                continue

            rows = _parse_table(resp.text)
            log.info("vmd_kenya : %d ligne(s) pour %s", len(rows), cat)

            for r in rows:
                produit = r.get("trade name", "")
                if not produit:
                    continue
                reg_no = r.get("reg. no.") or r.get("registration no.") or ""
                uid = f"{cat}|{reg_no or produit}".lower()
                if uid in seen:
                    continue
                seen.add(uid)

                ingredient = r.get("ingredients & strength", "")
                mah = r.get("mah", "")
                manufacturer = r.get("manufacturer", "")
                concurrent = self.settings.matched_concurrent(f"{mah} {manufacturer} {produit}")
                if not concurrent and not inclure_tous:
                    continue

                rec = Record(
                    source=self.name,
                    source_uid=uid,
                    record_type=RecordType.NOUVELLE_AMM,
                    concurrent=concurrent,
                    produit=produit,
                    molecules=[ingredient] if ingredient else [],
                    pays="KE",
                    url=url,
                    date_source=_parse_date(r.get("date of reg.", "")),
                    tags=self.settings.keywords_in(f"{produit} {ingredient}"),
                    extra={
                        "titulaire": mah,
                        "fabricant": manufacturer,
                        "numero_amm": reg_no,
                        "categorie": cat,
                        "classe": r.get("class/category", ""),
                        "forme": r.get("dosage", ""),
                        "registre": "VMD Kenya",
                    },
                )
                rec.compute_hashes()
                records.append(rec)

            if delay > 0:
                time.sleep(delay)

        log.info("vmd_kenya : %d AMM retenue(s)", len(records))
        return records
