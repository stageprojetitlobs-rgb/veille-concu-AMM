"""Source 2 — EMA UPD (Union Product Database). STUB / EN ATTENTE D'ACCÈS.

L'accès se fait par API REST réservée aux titulaires d'AMM (MAH), authentifiée en
OAuth2 (release 31/01/2025). Tant que les identifiants OAuth2 ne sont pas accordés
par l'EMA (cf docs/ema_upd_cadrage.md), ce module :
  - NE fait AUCUN appel réseau réel et NE scrape AUCUN endpoint interne ;
  - fonctionne en mode `fixtures` sur des données d'exemple (tests/fixtures/) ;
  - expose déjà le mapping vers le schéma pivot, prêt à brancher.

Pour passer en réel : renseigner les identifiants dans .env, les URLs réelles
(Chapitre 5 Vet EU IG) dans config.yaml, puis `mode: live` + `enabled: true`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from veille.schema import Record, RecordType
from veille.settings import ROOT
from veille.sources.base import Source

log = logging.getLogger(__name__)

FIXTURE = ROOT / "tests" / "fixtures" / "ema_upd_products.json"


class EmaAccessNotConfigured(RuntimeError):
    """Levé si on tente un appel réel sans identifiants OAuth2."""


class EmaUpdClient:
    """Client httpx + OAuth2 client-credentials. Le flow réel est prêt mais ne part
    jamais tant que les identifiants ne sont pas présents (garde-fou explicite)."""

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        token_url: str | None,
        api_base: str | None,
        timeout_s: float = 30.0,
        user_agent: str = "veille-lobs/0.1",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.api_base = api_base
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self._token: str | None = None
        self._token_exp: float = 0.0

    @property
    def configured(self) -> bool:
        return all([self.client_id, self.client_secret, self.token_url, self.api_base])

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise EmaAccessNotConfigured(
                "Accès EMA UPD non configuré (OAuth2). Renseigner EMA_UPD_* dans .env "
                "et les URLs dans config.yaml. Cf docs/ema_upd_cadrage.md."
            )

    def _get_token(self) -> str:
        """OAuth2 client-credentials avec cache jusqu'à expiration.

        ⚠️ Flow type, à confirmer/ajuster avec le Chapitre 5 (scope/audience exacts).
        """
        self._ensure_configured()
        if self._token and time.monotonic() < self._token_exp - 30:
            return self._token
        resp = httpx.post(
            self.token_url,  # type: ignore[arg-type]
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                # "scope": "...",  # à renseigner depuis les specs EMA
            },
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_exp = time.monotonic() + float(payload.get("expires_in", 3600))
        return self._token

    def search_products(self, **params) -> list[dict]:
        """Liste/recherche de produits vétérinaires (route réelle à brancher).

        ⚠️ Endpoint et pagination à compléter depuis le Chapitre 5 Vet EU IG.
        """
        self._ensure_configured()
        token = self._get_token()
        resp = httpx.get(
            f"{self.api_base}/products",  # placeholder — route réelle à confirmer
            params=params,
            headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        return resp.json().get("products", [])


def _load_fixture() -> list[dict]:
    data = json.loads(Path(FIXTURE).read_text(encoding="utf-8"))
    return data.get("products", [])


class EmaUpdSource(Source):
    name = "ema_upd"

    def _client(self) -> EmaUpdClient:
        return EmaUpdClient(
            client_id=os.getenv("EMA_UPD_CLIENT_ID"),
            client_secret=os.getenv("EMA_UPD_CLIENT_SECRET"),
            token_url=os.getenv("EMA_UPD_TOKEN_URL"),
            api_base=self.cfg.get("api_base"),
            timeout_s=self.settings.http_timeout_s,
            user_agent=self.settings.user_agent,
        )

    def fetch(self) -> list[Record]:
        mode = self.cfg.get("mode", "fixtures")
        if mode == "live":
            raw = self._client().search_products(**self.cfg.get("query", {}))
        else:
            log.warning("EMA UPD en mode FIXTURES (accès OAuth2 non accordé) — données d'exemple")
            raw = _load_fixture()

        records = []
        for item in raw:
            rec = self._to_record(item)
            if rec is not None:
                records.append(rec)
        return records

    def _to_record(self, item: dict) -> Record | None:
        """Mapping EMA → schéma pivot. Aligné sur la fixture ; à réajuster aux noms
        de champs réels du Chapitre 5 lors du branchement live."""
        holder = (item.get("marketingAuthorisationHolder") or {}).get("name", "")
        concurrent = self.settings.matched_concurrent(holder) if holder else None
        if not concurrent and not self.cfg.get("inclure_tous_produits", False):
            return None

        from datetime import datetime
        date_amm = item.get("authorisationDate")
        date_source = None
        if date_amm:
            try:
                date_source = datetime.strptime(date_amm[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

        nom = item.get("name")
        tags = self.settings.keywords_in(nom or "")

        rec = Record(
            source=self.name,
            source_uid=item.get("productIdentifier") or item.get("permanentIdentifier"),
            record_type=RecordType.NOUVELLE_AMM,
            concurrent=concurrent,
            produit=nom,
            molecules=sorted(item.get("activeSubstances", [])),
            pays=item.get("authorisationCountry", "EU"),
            url=item.get("productUrl"),
            date_source=date_source,
            tags=tags,
            extra={
                "prod_id_upd": item.get("productIdentifier"),   # clé jointure ANSES
                "perm_id": item.get("permanentIdentifier"),
                "titulaire": holder,
                "atcvet": item.get("atcVetCodes", []),
                "especes": item.get("targetSpecies", []),
                "statut": item.get("authorisationStatus"),
            },
        )
        rec.compute_hashes()
        return rec
