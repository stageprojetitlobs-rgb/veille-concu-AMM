"""Source AMM Nigeria — NAFDAC « List of Registered Animal Health Products ».

Le Nigeria (NAFDAC — National Agency for Food and Drug Administration and
Control) publie la liste officielle des produits de santé animale enregistrés
sous forme de **PDF public** (tableau réglé). Document de consultation publique
→ conforme (export officiel privilégié au scraping).

Contrairement à l'ONSSA (Maroc), le PDF NAFDAC a des cellules de tableau ruled :
`pdfplumber.extract_tables()` reconstruit les colonnes directement, sans calage
de positions.

Colonnes : S/N | NOM PRODUIT | N° NAFDAC | COMPOSITION | FORME |
           DEMANDEUR | FABRICANT | DATE APPROBATION | EXPIRATION

Conformité : document public officiel, un seul GET par run, aucune donnée
personnelle (demandeurs = personnes morales).
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import date, datetime

import httpx
import pdfplumber

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def _clean(cell: str | None) -> str:
    """Aplati les retours-ligne internes aux cellules du PDF."""
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


def _parse_date(raw: str) -> date | None:
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


class NafdacNigeriaSource(Source):
    """Liste NAFDAC (Nigeria). Config attendue (config.yaml) :

        sources:
          nafdac_nigeria:
            enabled: true
            url_pdf: "https://www.nafdac.gov.ng/.../List-of-Registered-Animal-Health-Products-...pdf"
            inclure_tous_produits: true
    """

    name = "nafdac_nigeria"

    def fetch(self) -> list[Record]:
        url = self.cfg.get("url_pdf")
        if not url:
            log.warning("nafdac_nigeria : url_pdf absente de la config")
            return []

        try:
            pdf_path = self._download(url)
        except httpx.HTTPError as exc:
            log.error("nafdac_nigeria : échec téléchargement PDF (%s)", exc)
            return []

        rows = self._parse_pdf(pdf_path)
        log.info("nafdac_nigeria : %d ligne(s) extraite(s) du PDF", len(rows))

        records: list[Record] = []
        seen: set[str] = set()
        inclure_tous = self.cfg.get("inclure_tous_produits", True)

        for r in rows:
            produit = r["produit"]
            if not produit:
                continue

            # Le demandeur (applicant) est le titulaire commercial le plus pertinent ;
            # à défaut on tente le fabricant.
            titulaire = r["demandeur"] or r["fabricant"]
            concurrent = self.settings.matched_concurrent(titulaire) if titulaire else None
            if not concurrent and not inclure_tous:
                continue

            uid = (r["reg_no"] or f"{titulaire}|{produit}").lower()
            if uid in seen:
                continue
            seen.add(uid)

            tags = self.settings.keywords_in(f"{produit} {r['composition']}")

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=produit,
                molecules=[],
                pays="NG",
                url=url,
                date_source=_parse_date(r["date"]),
                tags=tags,
                extra={
                    "titulaire": titulaire,
                    "fabricant": r["fabricant"],
                    "composition": r["composition"][:500],
                    "reg_no": r["reg_no"],
                    "forme": r["forme"],
                },
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("nafdac_nigeria : %d enregistrement(s) retenu(s)", len(records))
        return records

    def _download(self, url: str) -> str:
        resp = httpx.get(
            url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
        )
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name

    @staticmethod
    def _parse_pdf(pdf_path: str) -> list[dict]:
        out: list[dict] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row or len(row) < 8:
                            continue
                        # Saut des en-têtes répétés sur chaque page.
                        first = _clean(row[0])
                        if first.upper() in ("S/N", "") and not _clean(row[1]):
                            continue
                        if first.upper() == "S/N":
                            continue
                        out.append({
                            "produit": _clean(row[1]),
                            "reg_no": _clean(row[2]),
                            "composition": _clean(row[3]),
                            "forme": _clean(row[4]),
                            "demandeur": _clean(row[5]),
                            "fabricant": _clean(row[6]),
                            "date": _clean(row[7]),
                        })
        return out
