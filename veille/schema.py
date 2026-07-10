"""Schéma pivot commun à toutes les sources.

Toutes les sources `fetch()` renvoient des `Record` normalisés. L'orchestrateur ne
manipule que ce type, ce qui découple totalement la logique de diff/notification du
format propre à chaque source.

Deux hashs distincts (cf compute_hashes) :
  - `hash_registre` : existence / identité de l'AMM (produit, molécules, n° AMM…).
    Un changement = nouvelle AMM ou modification du registre.
  - `hash_rcp`      : contenu clinique business du RCP (posologie, temps d'attente,
    indications, composition/dosages, contre-indications). Un changement = mise à
    jour de RCP. Vide tant que la source ne remonte pas le RCP (flag inclure_rcp).
Cette séparation permet de notifier différemment « nouvelle AMM » et « MAJ RCP ».
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum


class RecordType(str, Enum):
    """Nature du signal — sert à formuler la notification."""
    NOUVELLE_AMM = "nouvelle_amm"          # Phase 1 (ANSES/EMA)
    PRODUIT = "produit"                     # Phase 2 (catalogues)
    EXPOSANT = "exposant"                   # Phase 3 (salons)
    OFFRE_EMPLOI = "offre_emploi"           # Phase 4 (RH)
    ACTUALITE = "actualite"                 # Flux RSS officiels (ex. Laprovet)


_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_text(s: str) -> str:
    """Normalise un texte AVANT hachage : accents, casse, ponctuation, espaces.

    On ne hashe jamais le texte brut : un simple reformatage (espace, majuscule,
    ponctuation) ne doit pas être vu comme une modification de RCP.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = _NON_ALNUM.sub(" ", s)
    return _WS.sub(" ", s).strip()


@dataclass
class Record:
    # --- Identité / dédup ---
    source: str                 # ex. "anses_anmv"
    source_uid: str             # identifiant STABLE de l'enregistrement dans la source
                                # (ex. src-id ANSES). Clé de dédup avec `source`.
    record_type: RecordType

    # --- Données pivot métier ---
    concurrent: str | None      # nom canonique du concurrent (None si non identifié)
    produit: str | None         # nom du produit / médicament
    molecules: list[str] = field(default_factory=list)  # substances actives
    pays: str | None = None
    url: str | None = None

    # --- Métadonnées ---
    date_source: date | None = None     # date métier (ex. date-amm)
    date_detection: datetime = field(default_factory=datetime.utcnow)
    tags: list[str] = field(default_factory=list)       # mots-clés stratégiques détectés
    extra: dict = field(default_factory=dict)           # champs spécifiques source (registre)
    # Sous-ensemble ciblé du RCP (vide si la source ne le remonte pas). Clés métier :
    # composition, especes_cibles, indications, contre_indications, posologie, temps_attente.
    rcp_content: dict = field(default_factory=dict)

    # --- Hashs (remplis par compute_hashes) ---
    hash_registre: str = ""
    hash_rcp: str = ""

    # Champs définissant l'existence/identité de l'AMM. On EXCLUT url (navigation),
    # date_detection (volatile) et tags (dérivés).
    _REGISTRE_FIELDS = ("produit", "molecules", "pays", "date_source")
    # Clés de `extra` qui font partie de l'identité registre.
    _REGISTRE_EXTRA = (
        "num_amm_fr", "num_amm_eu", "titulaire", "atcvet",
        # Numéro d'AMM / d'enregistrement — clé de vérification sur le site
        # officiel. Les sources utilisent des noms différents selon le pays.
        "numero_amm", "reg_no",
    )

    def _registre_payload(self) -> dict:
        payload: dict = {}
        for k in self._REGISTRE_FIELDS:
            v = getattr(self, k)
            if isinstance(v, (date, datetime)):
                v = v.isoformat()
            payload[k] = v
        for k in self._REGISTRE_EXTRA:
            payload[k] = self.extra.get(k)
        return payload

    def _rcp_payload(self) -> dict:
        """RCP normalisé pour hachage stable. Le texte libre passe par normalize_text ;
        les données structurées (temps d'attente) sont sérialisées telles quelles."""
        payload: dict = {}
        for key, val in self.rcp_content.items():
            if isinstance(val, str):
                payload[key] = normalize_text(val)
            else:
                payload[key] = val  # ex. temps_attente : liste de dicts structurés
        return payload

    def compute_hashes(self) -> tuple[str, str]:
        self.hash_registre = self._sha(self._registre_payload())
        self.hash_rcp = self._sha(self._rcp_payload())
        return self.hash_registre, self.hash_rcp

    @staticmethod
    def _sha(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def natural_key(self) -> str:
        """Clé naturelle pour la dédup : (source, source_uid)."""
        return f"{self.source}:{self.source_uid}"

    def to_row(self) -> dict:
        """Sérialisation plate pour le stockage (JSON sur les champs complexes)."""
        d = asdict(self)
        d["record_type"] = self.record_type.value
        d["molecules"] = json.dumps(self.molecules, ensure_ascii=False)
        d["tags"] = json.dumps(self.tags, ensure_ascii=False)
        d["extra"] = json.dumps(self.extra, ensure_ascii=False)
        d["rcp_content"] = json.dumps(self.rcp_content, ensure_ascii=False)
        d["date_source"] = self.date_source.isoformat() if self.date_source else None
        d["date_detection"] = self.date_detection.isoformat()
        return d
