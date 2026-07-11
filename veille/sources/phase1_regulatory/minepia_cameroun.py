"""Source AMM Cameroun — MINEPIA, liste des AMM vétérinaires.

Le MINEPIA (Ministère de l'Élevage, des Pêches et des Industries Animales)
publie sa liste des AMM vétérinaires sous forme de **PDF public** (tableau
réglé, groupé par laboratoire titulaire). Document de consultation publique
→ conforme. robots.txt absent sur minepia.cm (404) → aucune restriction.

Le tableau est extrait proprement par pdfplumber mais avec un nombre de
colonnes brutes variable selon les pages (cellules fusionnées → colonnes
vides supplémentaires) : on filtre les cellules vides puis on retombe
systématiquement sur 7 valeurs dans l'ordre du document (N°, Produit,
Présentation, Classe thérapeutique, Substances actives, N° AMM, Statut).

Pas de date d'AMM en clair dans le document (contrairement à l'Ouganda ou
au Rwanda) : le numéro AMM encode bien un mois/année en interne (ex.
« DRL0630323 » → 03/23) mais on ne le décode PAS en date affichée pour ne
jamais montrer une précision (jour) que le document ne fournit pas — même
choix que pour le Maroc, où le numéro AMM brut est affiché tel quel comme
clé de vérification plutôt que transformé en date approximative.

Marché Lobs : Lobs International Health y est elle-même titulaire de
plusieurs AMM (utile pour suivre sa propre présence en plus des concurrents).
"""
from __future__ import annotations

import logging
import re
import tempfile

import httpx
import pdfplumber

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)


def _clean(cell: str | None) -> str:
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


class MinepiaCamerounSource(Source):
    """Liste AMM MINEPIA (Cameroun). Config attendue (config.yaml) :

        sources:
          minepia_cameroun:
            enabled: true
            url_pdf: "https://minepia.cm/.../Liste-2024-AMM-finalisee-1.pdf"
            inclure_tous_produits: true
    """

    name = "minepia_cameroun"

    def fetch(self) -> list[Record]:
        url = self.cfg.get("url_pdf")
        if not url:
            log.warning("minepia_cameroun : aucune url_pdf en config")
            return []

        try:
            pdf_path = self._download(url)
        except httpx.HTTPError as exc:
            log.error("minepia_cameroun : échec téléchargement %s (%s)", url, exc)
            return []

        rows = self._parse_pdf(pdf_path)
        log.info("minepia_cameroun : %d ligne(s) extraite(s)", len(rows))

        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        records: list[Record] = []
        seen: set[str] = set()

        for r in rows:
            produit = r["produit"]
            if not produit:
                continue
            titulaire = r["laboratoire"]
            concurrent = self.settings.matched_concurrent(titulaire) if titulaire else None
            if not concurrent and not inclure_tous:
                continue

            uid = (r["numero_amm"] or f"{titulaire}|{produit}").lower()
            if uid in seen:
                continue
            seen.add(uid)

            tags = self.settings.keywords_in(f"{produit} {r['classe']} {r['substances']}")

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=produit,
                molecules=[m.strip() for m in re.split(r"[;,]", r["substances"]) if m.strip()],
                pays="CM",
                url=url,
                date_source=None,
                tags=tags,
                extra={
                    "titulaire": titulaire,
                    "presentation": r["presentation"],
                    "classe_therapeutique": r["classe"],
                    "numero_amm": r["numero_amm"],
                    "statut": r["statut"],
                },
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("minepia_cameroun : %d enregistrement(s) retenu(s)", len(records))
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
        laboratoire = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row:
                            continue
                        cells = [_clean(c) for c in row if _clean(c)]
                        if not cells:
                            continue
                        if cells[0].upper().startswith("LABORATOIRE"):
                            laboratoire = re.sub(r"^LABORATOIRE\s+", "", cells[0], flags=re.I).strip()
                            continue
                        if cells[0] == "N°":
                            continue  # ligne d'en-tête répétée à chaque page/labo
                        if len(cells) != 7 or not cells[0].isdigit():
                            continue  # ligne inattendue (résumé par classe p.1, etc.)
                        out.append({
                            "laboratoire": laboratoire,
                            "produit": cells[1],
                            "presentation": cells[2],
                            "classe": cells[3],
                            "substances": cells[4],
                            "numero_amm": cells[5],
                            "statut": cells[6],
                        })
        return out
