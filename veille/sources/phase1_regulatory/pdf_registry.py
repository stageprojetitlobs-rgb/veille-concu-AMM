"""Source générique — registres AMM nationaux publiés en PDF tabulaire.

Beaucoup d'autorités (Ouganda NDA, et d'autres à venir) publient leur registre
des médicaments vétérinaires sous forme d'un PDF à tableau réglé, mais avec des
intitulés et un ordre de colonnes DIFFÉRENTS à chaque pays.

Plutôt qu'un fichier Python par pays, cette source est **pilotée par la config** :
chaque registre déclare son URL, son pays, et un mappage `colonnes` (champ logique
→ fragment d'intitulé d'en-tête). Le parsing détecte la ligne d'en-tête, en déduit
les indices de colonnes, puis lit les lignes de données.

Conformité : documents publics officiels, un GET par fichier, aucune donnée
personnelle (titulaires/fabricants = personnes morales).

Config attendue (config.yaml) :

    sources:
      pdf_registry:
        enabled: true
        inclure_tous_produits: true
        registries:
          - pays: UG
            label: "NDA Ouganda"
            url: "https://www.nda.or.ug/.../...VETERINARY...2018...pdf"
            colonnes:
              produit: "name of drug"
              molecules: "generic name"
              titulaire: "license holder"
              fabricant: "manufacturer"
              reg_no: "registrati"
            # Optionnel : ne garder que les lignes dont une colonne contient un motif.
            filtre: {colonne: "reference", contient: ["/v"]}
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

# Champs logiques reconnus. `produit` est obligatoire.
_FIELDS = ("produit", "molecules", "titulaire", "fabricant", "especes", "reg_no", "reference")


def _clean(cell: str | None) -> str:
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


class PdfRegistrySource(Source):
    name = "pdf_registry"

    def fetch(self) -> list[Record]:
        registries = self.cfg.get("registries") or []
        if not registries:
            log.warning("pdf_registry : aucun registre configuré")
            return []

        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        records: list[Record] = []
        seen: set[str] = set()

        for reg in registries:
            url = reg.get("url")
            pays = reg.get("pays", "")
            colonnes = reg.get("colonnes") or {}
            colonnes_idx = reg.get("colonnes_idx") or {}
            if not url or not (colonnes.get("produit") or "produit" in colonnes_idx):
                log.warning("pdf_registry : registre %r incomplet (url/produit)", reg.get("label") or pays)
                continue

            try:
                pdf_path = self._download(url)
            except httpx.HTTPError as exc:
                log.error("pdf_registry : échec téléchargement %s (%s)", url, exc)
                continue

            if colonnes_idx:
                rows = self._parse_pdf_idx(pdf_path, colonnes_idx, reg.get("filtre"))
            else:
                rows = self._parse_pdf(pdf_path, colonnes, reg.get("filtre"))
            log.info("pdf_registry : %s (%s) → %d ligne(s)", reg.get("label") or pays, url, len(rows))

            for r in rows:
                produit = r.get("produit", "")
                if not produit:
                    continue
                titulaire = r.get("titulaire") or r.get("fabricant") or ""
                concurrent = self.settings.matched_concurrent(
                    f"{titulaire} {r.get('fabricant','')}") if titulaire else None
                if not concurrent and not inclure_tous:
                    continue

                uid = f"{pays}|{r.get('reg_no') or titulaire}|{produit}".lower()
                if uid in seen:
                    continue
                seen.add(uid)

                molecules = [r["molecules"]] if r.get("molecules") else []
                especes = r.get("especes", "")
                tags = self.settings.keywords_in(f"{produit} {r.get('molecules','')} {especes}")

                rec = Record(
                    source=self.name,
                    source_uid=uid,
                    record_type=RecordType.NOUVELLE_AMM,
                    concurrent=concurrent,
                    produit=produit,
                    molecules=molecules,
                    pays=pays,
                    url=url,
                    date_source=None,
                    tags=tags,
                    extra={
                        "titulaire": titulaire,
                        "fabricant": r.get("fabricant", ""),
                        "especes_cibles": especes,
                        "reg_no": r.get("reg_no", ""),
                        "registre": reg.get("label", ""),
                    },
                )
                rec.compute_hashes()
                records.append(rec)

        log.info("pdf_registry : %d enregistrement(s) retenu(s)", len(records))
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
    def _build_colmap(header_row: list[str], colonnes: dict) -> dict[str, int]:
        colmap: dict[str, int] = {}
        cleaned = [_clean(c).lower() for c in header_row]
        for field, needle in colonnes.items():
            if field not in _FIELDS:
                continue
            ndl = needle.lower()
            for idx, cell in enumerate(cleaned):
                if ndl in cell:
                    colmap[field] = idx
                    break
        return colmap

    def _parse_pdf(self, pdf_path: str, colonnes: dict, filtre: dict | None) -> list[dict]:
        out: list[dict] = []
        produit_needle = colonnes["produit"].lower()
        colmap: dict[str, int] = {}

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row:
                            continue
                        joined = " ".join(_clean(c).lower() for c in row)
                        # Ligne d'en-tête = contient l'intitulé de la colonne produit.
                        if produit_needle in joined and any(
                            (colonnes.get(f, "").lower() in joined)
                            for f in ("titulaire", "molecules", "reg_no") if colonnes.get(f)
                        ):
                            colmap = self._build_colmap(row, colonnes)
                            continue
                        if not colmap or "produit" not in colmap:
                            continue
                        rec = {field: _clean(row[idx]) if idx < len(row) else ""
                               for field, idx in colmap.items()}
                        if not rec.get("produit"):
                            continue
                        # Filtre optionnel (ex. ne garder que le vétérinaire).
                        if filtre:
                            col = filtre.get("colonne", "")
                            motifs = filtre.get("contient", [])
                            idx = colmap.get(col)
                            val = _clean(row[idx]).lower() if (idx is not None and idx < len(row)) else ""
                            if not any(m.lower() in val for m in motifs):
                                continue
                        out.append(rec)
        return out

    def _parse_pdf_idx(self, pdf_path: str, colonnes_idx: dict, filtre: dict | None) -> list[dict]:
        """Variante par indices fixes : pour les PDF dont l'en-tête est éclaté/mal
        aligné mais dont les lignes de données sont régulières. On retient toute
        ligne dont la 1re cellule est un numéro de série (S/N)."""
        out: list[dict] = []
        idx_produit = colonnes_idx["produit"]
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row:
                            continue
                        first = _clean(row[0])
                        if not first or not first.isdigit():
                            continue
                        if idx_produit >= len(row):
                            continue
                        rec = {field: _clean(row[idx]) if (isinstance(idx, int) and idx < len(row)) else ""
                               for field, idx in colonnes_idx.items()}
                        if not rec.get("produit"):
                            continue
                        if filtre:
                            idx = colonnes_idx.get(filtre.get("colonne_idx"))
                            motifs = filtre.get("contient", [])
                            val = _clean(row[idx]).lower() if (isinstance(idx, int) and idx < len(row)) else ""
                            if not any(m.lower() in val for m in motifs):
                                continue
                        out.append(rec)
        return out
