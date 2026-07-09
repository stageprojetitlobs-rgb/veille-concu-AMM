"""Source AMM Zambie — ZAMRA (Zambia Medicines Regulatory Authority).

ZAMRA expose un **portail public** de produits enregistrés (app.zamra.co.zm) qui
s'appuie sur une API JSON publique `publicaccess/onSearchPublicRegisteredproducts`.
On consomme directement cette API publique (destinée à la consultation) — conforme,
plus fiable et plus léger qu'un scraping HTML, et aucun contournement d'auth.

L'API renvoie TOUS les produits (humains + vétérinaires) ; on filtre sur les
classifications vétérinaires (`classification_name`).

Conformité : API publique officielle, une requête par run, aucune donnée
personnelle (titulaires = personnes morales).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

# Classifications considérées comme « vétérinaires » (casefold, test par appartenance).
_VET_CLASSES = {
    "veterinary medicinal product",
    "feed additives",
    "feed supplements",
    "acaricide",
    "ruminants (livestock)",
    # Catégories mixtes mais pertinentes pour Lobs (vitamines/compléments,
    # désinfectants) — élargissement volontaire du périmètre.
    "nutritional supplements",
    "antiseptic/disinfectant",
    "other allied products",
}


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


class ZamraZambieSource(Source):
    """Produits vétérinaires enregistrés en Zambie (API publique ZAMRA).

    Config attendue (config.yaml) :

        sources:
          zamra_zambie:
            enabled: true
            api_url: "https://app.zamra.co.zm:42882/portal/publicaccess/onSearchPublicRegisteredproducts"
            take: 6000
            verify_ssl: true
            inclure_tous_produits: true
    """

    name = "zamra_zambie"

    def fetch(self) -> list[Record]:
        api_url = self.cfg.get("api_url")
        if not api_url:
            log.warning("zamra_zambie : api_url absente de la config")
            return []

        take = int(self.cfg.get("take", 6000))
        params = {
            "skip": 0, "take": take,
            "section_id": "", "sub_modulesin": "", "extra_paramsdata": "{}",
        }
        try:
            resp = httpx.get(
                api_url,
                params=params,
                headers={"User-Agent": self.settings.user_agent, "Accept": "application/json"},
                timeout=max(self.settings.http_timeout_s, 90),
                verify=self.cfg.get("verify_ssl", True),
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("zamra_zambie : échec API (%s)", exc)
            return []

        data = payload.get("data") or []
        log.info("zamra_zambie : %d produit(s) reçus (total déclaré %s)",
                 len(data), payload.get("totalCount"))

        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        records: list[Record] = []
        seen: set[str] = set()

        for r in data:
            classification = (r.get("classification_name") or "").strip().lower()
            section = (r.get("section_name") or "").strip().lower()
            if "veterin" not in classification and classification not in _VET_CLASSES and "veterin" not in section:
                continue

            produit = (r.get("brand_name") or "").strip()
            if not produit:
                continue

            registrant = (r.get("registrant") or "").strip()
            manufacturer = (r.get("manufacturer") or "").strip()
            concurrent = self.settings.matched_concurrent(f"{registrant} {manufacturer}") \
                if (registrant or manufacturer) else None
            if not concurrent and not inclure_tous:
                continue

            cert = (r.get("certificate_no") or "").strip()
            uid = (cert or f"{registrant}|{produit}").lower()
            if uid in seen:
                continue
            seen.add(uid)

            generic = (r.get("generic_name") or "").strip()
            molecules = [generic] if generic else []
            tags = self.settings.keywords_in(f"{produit} {generic}")

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=produit,
                molecules=molecules,
                pays="ZM",
                url="https://app.zamra.co.zm:42882/portal/#/public/registered-medicines",
                date_source=_parse_date(r.get("certificate_issue_date", "")),
                tags=tags,
                extra={
                    "titulaire": registrant,
                    "fabricant": manufacturer,
                    "registrant_country": r.get("registrant_country"),
                    "classification": r.get("classification_name"),
                    "certificate_no": cert,
                    "dosage_form": r.get("dosage_form"),
                },
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("zamra_zambie : %d enregistrement(s) vétérinaire(s) retenu(s)", len(records))
        return records
