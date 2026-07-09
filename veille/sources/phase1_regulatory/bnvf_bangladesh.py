"""Source AMM Bangladesh — National Veterinary Formulary (DGDA).

Le Bangladesh ne publie pas de registre tabulaire, mais le « Bangladesh National
Veterinary Formulary » (BDNVF, DGDA) — PDF public officiel — liste, au fil des
monographies, les **produits enregistrés** sous la forme « Marque (Fabricant),
forme. dosage, prix ». On en extrait les couples marque / fabricant.

PDF de monographies (prose) → extraction par motif, pas par tableau. Pour écarter
les faux positifs (parenthèses chimiques « (Benzyl penicillin) » apparaissant une
seule fois), on ne retient que les fabricants vus au moins `min_occurrences` fois :
un vrai laboratoire récurre, un nom chimique parasite est unique.

Conformité : document public officiel, un GET par run, aucune donnée personnelle.
"""
from __future__ import annotations

import collections
import logging
import re
import tempfile

import httpx
import pdfplumber

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

# « Marque (Fabricant), » — fabricant entre parenthèses suivi d'une virgule.
_PAT = re.compile(r"([A-Z][A-Za-z0-9\-\+\. ]{2,30})\s*\(([A-Z][A-Za-z0-9\-\. ]{2,28})\)\s*,")

# Mots qui trahissent une parenthèse non-commerciale (chimie, anatomie, renvois).
_STOP = ("penicillin", "acid", "sodium", "see", "section", "vet)", "oral", "i.v",
         "i.m", "gi ", "sulph", "chloride", "vaccine", "injection", "tablet",
         "powder", "bolus", "solution", "suspension")


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class BnvfBangladeshSource(Source):
    """National Veterinary Formulary (Bangladesh). Config attendue :

        sources:
          bnvf_bangladesh:
            enabled: true
            url_pdf: "https://.../Bangladesh-National-Veterinary-Formulary-2023.pdf"
            min_occurrences: 3
            inclure_tous_produits: true
    """

    name = "bnvf_bangladesh"

    def fetch(self) -> list[Record]:
        url = self.cfg.get("url_pdf")
        if not url:
            log.warning("bnvf_bangladesh : url_pdf absente de la config")
            return []
        try:
            path = self._download(url)
        except httpx.HTTPError as exc:
            log.error("bnvf_bangladesh : échec téléchargement (%s)", exc)
            return []

        # Passe 1 : extraire tous les couples (marque, fabricant).
        pairs: list[tuple[str, str]] = []
        maker_counts: collections.Counter = collections.Counter()
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    for m in _PAT.finditer(text):
                        brand = _txt(m.group(1))
                        maker = _txt(m.group(2))
                        low = maker.lower()
                        if any(s in low for s in _STOP):
                            continue
                        if len(maker) < 3 or maker.isupper() and len(maker) <= 2:
                            continue
                        pairs.append((brand, maker))
                        maker_counts[maker] += 1
        except Exception as exc:
            log.error("bnvf_bangladesh : PDF illisible (%s)", exc)
            return []

        # Passe 2 : ne garder que les fabricants récurrents (vrais laboratoires).
        min_occ = int(self.cfg.get("min_occurrences", 3))
        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        records: list[Record] = []
        seen: set[str] = set()
        for brand, maker in pairs:
            if maker_counts[maker] < min_occ:
                continue
            uid = f"BD|{maker}|{brand}".lower()
            if uid in seen:
                continue
            seen.add(uid)

            concurrent = self.settings.matched_concurrent(maker)
            if not concurrent and not inclure_tous:
                continue

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=brand,
                molecules=[],
                pays="BD",
                url=url,
                date_source=None,
                tags=self.settings.keywords_in(brand),
                extra={"titulaire": maker, "registre": "BD National Veterinary Formulary"},
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("bnvf_bangladesh : %d produit(s) retenu(s) (%d fabricants)",
                 len(records), len({m for _, m in pairs if maker_counts[m] >= min_occ}))
        return records

    def _download(self, url: str) -> str:
        resp = httpx.get(
            url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=max(self.settings.http_timeout_s, 90),
            follow_redirects=True,
            verify=False,
        )
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name
