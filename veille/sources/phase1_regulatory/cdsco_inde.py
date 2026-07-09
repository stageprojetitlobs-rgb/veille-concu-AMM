"""Source AMM Inde — CDSCO « List of Veterinary Drugs & Vaccines Approved ».

Le CDSCO (Central Drugs Standard Control Organisation) publie les autorisations
de mise sur le marché vétérinaires (Form-45/46) sous forme de **PDF publics**
(tableaux réglés), par tranches temporelles. Documents de consultation publique
→ conformes.

Les éditions successives n'ont PAS le même ordre de colonnes (8 vs 10) : on parse
donc en **mappant les en-têtes** (nom de colonne → indice), pas par position fixe.
On accepte une LISTE d'URLs (url_pdfs) pour cumuler plusieurs tranches.

Marché Lobs : l'Inde est un grand fournisseur de génériques vétérinaires export ;
plusieurs concurrents y déposent (Intervet India, Ceva Polchem…).

Conformité : documents publics officiels, un GET par fichier, aucune donnée
personnelle (firmes = personnes morales).
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


def _clean(cell: str | None) -> str:
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


def _parse_date(raw: str) -> date | None:
    raw = re.sub(r"\s+", "", raw or "")  # « 21-01- 2021 » → « 21-01-2021 »
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# Mots-clés d'en-tête → champ logique. On teste par « contient » (insensible casse).
_HEADER_MAP = [
    ("firm", "firm"),
    ("product", "product"),
    ("permission no", "permission_no"),
    ("permission date", "date"),
    ("species", "species"),
    ("indication", "indication"),
]


def _build_colmap(header_row: list[str]) -> dict[str, int]:
    colmap: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        low = _clean(cell).lower()
        if not low:
            continue
        for needle, field in _HEADER_MAP:
            if needle in low and field not in colmap:
                colmap[field] = idx
    # « date » seule (édition sans « permission date ») en repli.
    if "date" not in colmap:
        for idx, cell in enumerate(header_row):
            if _clean(cell).lower() == "date":
                colmap["date"] = idx
                break
    return colmap


class CdscoIndeSource(Source):
    """Liste CDSCO (Inde). Config attendue (config.yaml) :

        sources:
          cdsco_inde:
            enabled: true
            url_pdfs:
              - "https://cdsco.gov.in/.../vet-data-2020-to-2022.pdf"
              - "https://cdsco.gov.in/.../vddrug1.pdf"
            inclure_tous_produits: true
    """

    name = "cdsco_inde"

    def fetch(self) -> list[Record]:
        urls = self.cfg.get("url_pdfs") or ([self.cfg["url_pdf"]] if self.cfg.get("url_pdf") else [])
        if not urls:
            log.warning("cdsco_inde : aucune url_pdfs en config")
            return []

        records: list[Record] = []
        seen: set[str] = set()
        inclure_tous = self.cfg.get("inclure_tous_produits", True)

        for url in urls:
            try:
                pdf_path = self._download(url)
            except httpx.HTTPError as exc:
                log.error("cdsco_inde : échec téléchargement %s (%s)", url, exc)
                continue

            rows = self._parse_pdf(pdf_path)
            log.info("cdsco_inde : %d ligne(s) depuis %s", len(rows), url)

            for r in rows:
                produit = r.get("product", "")
                if not produit:
                    continue
                firm = re.sub(r"^M/s\.?\s*", "", r.get("firm", "")).strip()
                concurrent = self.settings.matched_concurrent(firm) if firm else None
                if not concurrent and not inclure_tous:
                    continue

                uid = (r.get("permission_no") or f"{firm}|{produit}").lower()
                if uid in seen:
                    continue
                seen.add(uid)

                especes = r.get("species", "")
                indication = r.get("indication", "")
                tags = self.settings.keywords_in(f"{produit} {indication} {especes}")

                rec = Record(
                    source=self.name,
                    source_uid=uid,
                    record_type=RecordType.NOUVELLE_AMM,
                    concurrent=concurrent,
                    produit=produit,
                    molecules=[],
                    pays="IN",
                    url=url,
                    date_source=_parse_date(r.get("date", "")),
                    tags=tags,
                    extra={
                        "titulaire": firm,
                        "especes_cibles": especes,
                        "indication": indication[:300],
                        "permission_no": r.get("permission_no", ""),
                    },
                )
                rec.compute_hashes()
                records.append(rec)

        log.info("cdsco_inde : %d enregistrement(s) retenu(s)", len(records))
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
        colmap: dict[str, int] = {}
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row:
                            continue
                        # Ligne d'en-tête : contient « Permission No. » → (re)cale le mapping.
                        joined = " ".join(_clean(c).lower() for c in row)
                        if "permission no" in joined or ("firm" in joined and "product" in joined):
                            colmap = _build_colmap(row)
                            continue
                        if not colmap:
                            continue
                        # Ligne de données : doit avoir un n° de série numérique en tête.
                        first = _clean(row[0]) if row else ""
                        if not first or not first[0].isdigit():
                            continue
                        rec = {field: _clean(row[idx]) if idx < len(row) else ""
                               for field, idx in colmap.items()}
                        out.append(rec)
        return out
