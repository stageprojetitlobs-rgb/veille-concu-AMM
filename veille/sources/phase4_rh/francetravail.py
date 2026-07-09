"""Source Phase 4 — France Travail (ex-Pôle emploi) : API officielle des offres d'emploi.

Canal officiel, OAuth2 client_credentials, gratuit pour les partenaires développeurs.
Inscription : https://francetravail.io/data/api/offres-emploi

Signal clé : un concurrent qui recrute sur une zone géographique cible = il prépare une
offensive. Ex. « Responsable Export Afrique subsaharienne chez Axience » = Axience attaque
l'Afrique, zone prioritaire Lobs.

Stratégie de recherche (deux axes cumulables) :
  1. `entreprise=<nom_concurrent>` : toutes les offres d'un concurrent (précis, mais
     dépend du fait que l'entreprise déclare son offre avec sa raison sociale exacte).
  2. `motsCles=<mots_metier>` : offres matchant des termes sectoriels (« vétérinaire »,
     « export afrique », etc.) — plus large, filtrage concurrent côté collecteur.

La source tourne en mode stub (sans API key) tant que les credentials ne sont pas renseignés
dans `.env`. Elle ne lève pas d'exception : elle logge un WARNING et renvoie [].

Conformité :
  - API officielle documentée : aucun scraping HTML.
  - rate-limiting : délai entre les appels par concurrent.
  - RGPD : les offres d'emploi sont des données publiques ; les noms des recruteurs
    (contact) NE SONT PAS stockés dans le pivot — uniquement l'intitulé, l'entreprise,
    la localisation et le lien canonique de l'offre.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from typing import Any

import httpx

from veille.schema import Record, RecordType, normalize_text
from veille.sources.base import Source

log = logging.getLogger(__name__)

# ── Endpoints France Travail ──────────────────────────────────────────────────
_TOKEN_URL = (
    "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
    "?realm=%2Fpartenaire"
)
_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"

# Scope requis pour l'API Offres d'emploi v2.
_SCOPE = "api_offresdemploiv2 o2dsoffre"

# Codes NAF (secteurs d'activité INSEE) des industries cibles.
# On filtre les offres pour ne retenir que celles issues de ces secteurs.
# Cela élimine les homonymes (ex. "Osalia" maison de retraite vs labo vétérinaire).
_SECTEURS_CIBLES = {
    "2120Z",  # Fabrication de préparations pharmaceutiques
    "4646Z",  # Commerce de gros de produits pharmaceutiques
    "7211Z",  # Recherche-développement en biotechnologie
    "7219Z",  # Recherche-développement en autres sciences
    "2100Z",  # Fabrication de produits pharmaceutiques de base
    "7120B",  # Analyses, essais et inspections techniques
}

# Zones géographiques stratégiques Lobs — matchées dans l'intitulé + description.
_ZONES_STRATEGIQUES = [
    "afrique", "africa",
    "moyen-orient", "middle east", "mena",
    "maghreb", "maroc", "algerie", "tunisie",
    "export", "international",
    "zone subsaharienne", "subsaharienne",
]

# Mots-clés métier vétérinaire/santé animale pour la recherche par motsCles.
_MOTS_CLES_METIER = [
    "vétérinaire", "veterinaire",
    "médicament vétérinaire", "medicament veterinaire",
    "santé animale", "sante animale",
    "productions animales", "elevage",
    "pharmacie vétérinaire",
    # Volaille / vaccins aviaires
    "vaccin aviaire", "vaccin volaille",
    "aviculture", "avicole",
    "poulet", "dinde", "reproducteur",
]


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        # Format API : "2026-05-01T00:00:00.000Z" ou "2026-05-01"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.date()
    except (ValueError, TypeError):
        return None


def _detect_zone(text: str) -> list[str]:
    """Retourne les zones stratégiques trouvées dans le texte (intitulé + description)."""
    normed = normalize_text(text)
    return [z for z in _ZONES_STRATEGIQUES if z in normed]


class FranceTravailSource(Source):
    """Source France Travail (API officielle). Config attendue :

        sources:
          france_travail:
            enabled: true
            # Axes de recherche (au moins un doit être actif).
            # 1) Par nom d'entreprise : chaque concurrent est interrogé séparément.
            recherche_par_entreprise: true
            # 2) Par mots-clés métier (vétérinaire, santé animale…).
            #    Plus large, filtré côté collecteur sur les concurrents connus.
            recherche_par_mots_cles: true
            # Nombre max de résultats par requête (API max = 150).
            nb_resultats: 50
            # Filtre géographique optionnel (codes département/région INSEE, ou laisser vide).
            # Ex. [] = toute la France (offres export souvent basées à Paris/Lyon).
            departements: []

    Credentials (.env) :
        FRANCE_TRAVAIL_CLIENT_ID=<id>
        FRANCE_TRAVAIL_CLIENT_SECRET=<secret>
    """

    name = "france_travail"

    # ── Authentification OAuth2 ───────────────────────────────────────────────

    def _get_token(self, client_id: str, client_secret: str) -> str | None:
        """Obtient un access_token via client_credentials grant."""
        try:
            resp = httpx.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": _SCOPE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.settings.http_timeout_s,
            )
            resp.raise_for_status()
            return resp.json().get("access_token")
        except httpx.HTTPError as exc:
            log.error("France Travail : échec authentification OAuth2 (%s)", exc)
            return None

    # ── Recherche ─────────────────────────────────────────────────────────────

    def _search(
        self,
        token: str,
        params: dict[str, Any],
    ) -> list[dict]:
        """Appel à l'endpoint de recherche. Retourne la liste brute des offres."""
        base_params: dict[str, Any] = {
            "range": f"0-{self.cfg.get('nb_resultats', 50) - 1}",
            "sort": "1",  # tri par date de création décroissante
        }
        dept = self.cfg.get("departements") or []
        if dept:
            base_params["departement"] = ",".join(str(d) for d in dept)

        base_params.update(params)
        try:
            resp = httpx.get(
                _SEARCH_URL,
                params=base_params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.settings.user_agent,
                },
                timeout=self.settings.http_timeout_s,
            )
            resp.raise_for_status()
            # 204 No Content (ou corps vide) = aucun résultat : pas de JSON à parser.
            if resp.status_code == 204 or not resp.content:
                return []
            data = resp.json()
            return data.get("resultats", []) or []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 204:
                return []  # 204 = aucun résultat
            log.error("France Travail search params=%r : %s", params, exc)
            return []
        except httpx.HTTPError as exc:
            log.error("France Travail search params=%r : %s", params, exc)
            return []

    # ── Conversion vers Record pivot ──────────────────────────────────────────

    def _offre_to_record(self, offre: dict, concurrent_hint: str | None = None) -> Record | None:
        """Convertit une offre API en Record pivot OFFRE_EMPLOI.

        RGPD : on stocke UNIQUEMENT les données publiques de l'offre (intitulé,
        entreprise, lieu, URL). Les coordonnées du contact ne sont pas stockées.
        """
        uid = offre.get("id")
        if not uid:
            return None

        intitule = offre.get("intitule") or ""
        entreprise = (offre.get("entreprise") or {}).get("nom") or ""
        description = offre.get("description") or ""
        lieu = (offre.get("lieuTravail") or {}).get("libelle") or ""
        url = offre.get("origineOffre", {}).get("urlOrigine") or (
            f"https://candidat.francetravail.fr/offres/recherche/detail/{uid}"
        )
        date_creation = _parse_date(offre.get("dateCreation"))

        # Filtre sectoriel : on ne garde que les offres issues des secteurs pharma/véto.
        # Élimine les homonymes (ex. "Osalia" maison de retraite, "Kepro" TP...).
        # Si secteurActivite absent de l'offre ET qu'on est en mode recherche_par_mots_cles,
        # on laisse passer (les mots-clés métier garantissent déjà le secteur).
        naf = (offre.get("secteurActivite") or "").strip()
        if concurrent_hint and naf and naf not in _SECTEURS_CIBLES:
            log.debug(
                "Offre %s ignorée : secteur %r hors cible (entreprise=%r, intitulé=%r)",
                uid, naf, entreprise, intitule,
            )
            return None

        # Matching concurrent : le hint (requête par entreprise) n'est retenu que si
        # le nom d'entreprise de l'offre le confirme — l'API matche large (recherche
        # « act » → agences ACTUAL…), on ne lui fait pas confiance aveuglément.
        concurrent = None
        if concurrent_hint:
            conc_obj = next(
                (c for c in self.settings.concurrents if c.nom == concurrent_hint), None
            )
            if conc_obj and conc_obj.matches(entreprise):
                concurrent = concurrent_hint
        if concurrent is None:
            concurrent = self.settings.matched_concurrent(f"{intitule} {entreprise} {description}")
        if concurrent is None:
            return None  # offre hors scope concurrents suivis

        # Tags zones stratégiques.
        zones = _detect_zone(f"{intitule} {lieu} {description}")
        tags = self.settings.keywords_in(f"{intitule} {description}")
        if zones:
            tags = list(set(tags) | {f"zone:{z}" for z in zones})

        # Qualification du poste (ex-post sur l'intitulé).
        qualification = offre.get("qualificationCode") or ""
        contrat = offre.get("typeContratLibelle") or ""

        rec = Record(
            source=self.name,
            source_uid=uid,
            record_type=RecordType.OFFRE_EMPLOI,
            concurrent=concurrent,
            produit=intitule or None,       # intitulé du poste = libellé du signal
            molecules=[],
            pays="FR",                      # France Travail = offres France (siège)
            url=url,
            date_source=date_creation,
            tags=tags,
            extra={
                "entreprise": entreprise,
                "lieu": lieu,
                "contrat": contrat,
                "qualification": qualification,
                "zones_detectees": zones,
                "description_extrait": description[:300],
            },
        )
        rec.compute_hashes()
        return rec

    # ── fetch() principal ─────────────────────────────────────────────────────

    def fetch(self) -> list[Record]:
        client_id = os.getenv("FRANCE_TRAVAIL_CLIENT_ID", "")
        client_secret = os.getenv("FRANCE_TRAVAIL_CLIENT_SECRET", "")

        if not client_id or not client_secret:
            log.warning(
                "France Travail : FRANCE_TRAVAIL_CLIENT_ID / CLIENT_SECRET absents "
                "→ source désactivée. Inscription : https://francetravail.io/data/api"
            )
            return []

        token = self._get_token(client_id, client_secret)
        if not token:
            return []

        delay = self.settings.download_delay_s
        records: list[Record] = []
        seen_uids: set[str] = set()

        def _add(offre: dict, hint: str | None = None) -> None:
            uid = offre.get("id")
            if uid and uid not in seen_uids:
                seen_uids.add(uid)
                rec = self._offre_to_record(offre, hint)
                if rec:
                    records.append(rec)

        # ── Axe 1 : recherche par entreprise ─────────────────────────────────
        if self.cfg.get("recherche_par_entreprise", True):
            for concurrent_cfg in self.settings.raw.get("concurrents", []):
                nom = concurrent_cfg.get("nom", "")
                aliases = concurrent_cfg.get("aliases", [nom])
                # On prend le premier alias (raison sociale courte).
                terme = aliases[0] if aliases else nom
                log.info("France Travail : recherche entreprise=%r", terme)
                offres = self._search(token, {"entreprise": terme})
                log.info("France Travail : %d offre(s) pour %r", len(offres), terme)
                for offre in offres:
                    _add(offre, hint=nom)
                if delay > 0:
                    time.sleep(delay)

        # ── Axe 2 : recherche par mots-clés métier ───────────────────────────
        if self.cfg.get("recherche_par_mots_cles", True):
            for mc in _MOTS_CLES_METIER:
                log.info("France Travail : recherche motsCles=%r", mc)
                offres = self._search(token, {"motsCles": mc})
                log.info("France Travail : %d offre(s) pour motsCles=%r", len(offres), mc)
                for offre in offres:
                    _add(offre, hint=None)
                if delay > 0:
                    time.sleep(delay)

        log.info("France Travail : %d offre(s) concurrentes retenues", len(records))
        return records
