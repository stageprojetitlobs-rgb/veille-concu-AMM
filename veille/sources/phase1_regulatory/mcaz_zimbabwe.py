"""Source AMM Zimbabwe — MCAZ (Medicines Control Authority of Zimbabwe).

MCAZ publie un registre public en ligne des médicaments approuvés. La page
« Approved Veterinary Medicines Register » s'alimente via un endpoint JSON public
(grille Kendo) : `Medicines/GetMedicinesByCategory?category=2` (2 = vétérinaire).
On consomme directement cet endpoint public — conforme, sans contournement d'auth.

Conformité : registre public officiel, une requête par run, aucune donnée
personnelle (titulaires/fabricants = personnes morales).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).date()
    except ValueError:
        return None


def _clean(s: str | None) -> str:
    return (s or "").replace("&amp;", "&").strip()


class McazZimbabweSource(Source):
    """Registre vétérinaire MCAZ (Zimbabwe). Config attendue (config.yaml) :

        sources:
          mcaz_zimbabwe:
            enabled: true
            api_url: "https://onlineservices.mcaz.co.zw/onlineregister/Medicines/GetMedicinesByCategory?category=2"
            verify_ssl: false
            inclure_tous_produits: true
    """

    name = "mcaz_zimbabwe"

    def fetch(self) -> list[Record]:
        api_url = self.cfg.get("api_url")
        if not api_url:
            log.warning("mcaz_zimbabwe : api_url absente de la config")
            return []

        try:
            resp = httpx.get(
                api_url,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=max(self.settings.http_timeout_s, 60),
                verify=self.cfg.get("verify_ssl", True),
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("mcaz_zimbabwe : échec API (%s)", exc)
            return []

        rows = payload.get("Data") or []
        log.info("mcaz_zimbabwe : %d produit(s) vétérinaire(s) reçus", len(rows))

        inclure_tous = self.cfg.get("inclure_tous_produits", True)
        records: list[Record] = []
        seen: set[str] = set()

        for r in rows:
            produit = _clean(r.get("Trade_Name"))
            if not produit:
                continue

            applicant = _clean(r.get("ApplicantName"))
            principal = _clean(r.get("PrincipalName"))
            manufacturer = _clean(r.get("Manufacturers"))
            concurrent = self.settings.matched_concurrent(
                f"{applicant} {principal} {manufacturer}")
            if not concurrent and not inclure_tous:
                continue

            reg_no = _clean(r.get("Registration_No"))
            uid = (reg_no or f"{applicant}|{produit}").lower()
            if uid in seen:
                continue
            seen.add(uid)

            generic = _clean(r.get("Generic_Name"))
            molecules = [generic] if generic else []
            tags = self.settings.keywords_in(f"{produit} {generic}")

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=produit,
                molecules=molecules,
                pays="ZW",
                url="https://onlineservices.mcaz.co.zw/onlineregister/Medicines?category=2",
                date_source=_parse_date(r.get("Date_Registered", "")),
                tags=tags,
                extra={
                    "titulaire": applicant,
                    "principal": principal,
                    "fabricant": manufacturer,
                    "categorie": _clean(r.get("Category")),
                    "forme": _clean(r.get("Forms")),
                    "dosage": _clean(r.get("Strength")),
                    "reg_no": reg_no,
                },
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("mcaz_zimbabwe : %d enregistrement(s) retenu(s)", len(records))
        return records
