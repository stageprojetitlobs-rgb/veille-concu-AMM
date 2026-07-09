"""Interface de stockage abstraite.

Sépare la logique de veille du backend de persistance. Pour basculer vers Airtable
plus tard, il suffira d'écrire un `AirtableStore(Store)` et de changer
`storage.backend` dans config.yaml — aucun autre code à toucher.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from veille.schema import Record


class ChangeType(str, Enum):
    NOUVEAU = "nouveau"      # natural_key jamais vue → nouvelle AMM
    MODIFIE = "modifie"      # registre et/ou RCP modifié → à notifier
    INCHANGE = "inchange"    # rien de neuf → ignoré


@dataclass
class Change:
    type: ChangeType
    record: Record
    # Aspects modifiés : sous-ensemble de {"registre", "rcp"}. Permet au notifier
    # de distinguer « nouvelle/MAJ AMM » d'une « mise à jour de RCP ».
    aspects: tuple[str, ...] = ()
    ancien_registre: str | None = None
    ancien_rcp: str | None = None


class Store(ABC):
    """Contrat minimal pour persister et diffraîchir les enregistrements."""

    @abstractmethod
    def known_states(self, source: str) -> dict[str, tuple[str, str]]:
        """Renvoie {natural_key: (hash_registre, hash_rcp)} déjà connus."""

    @abstractmethod
    def upsert(self, record: Record) -> None:
        """Insère ou met à jour l'état courant d'un enregistrement."""

    @abstractmethod
    def append_history(self, change: Change) -> None:
        """Journalise un changement (nouveau/modifié) dans l'historique."""

    def diff(self, records: list[Record], source: str) -> list[Change]:
        """Compare les enregistrements collectés à l'historique.

        Ne renvoie QUE les vrais changements. On compare séparément le hash registre
        et le hash RCP pour qualifier la nature du changement.
        """
        known = self.known_states(source)
        changes: list[Change] = []
        for rec in records:
            if not rec.hash_registre:
                rec.compute_hashes()
            prev = known.get(rec.natural_key)
            if prev is None:
                changes.append(Change(ChangeType.NOUVEAU, rec, aspects=("registre",)))
                continue
            old_reg, old_rcp = prev
            aspects = []
            if old_reg != rec.hash_registre:
                aspects.append("registre")
            if old_rcp != rec.hash_rcp:
                aspects.append("rcp")
            if aspects:
                changes.append(Change(
                    ChangeType.MODIFIE, rec, aspects=tuple(aspects),
                    ancien_registre=old_reg, ancien_rcp=old_rcp,
                ))
            # sinon INCHANGE → on n'émet rien
        return changes

    def commit_changes(self, changes: list[Change]) -> None:
        """Persiste les changements (état courant + historique)."""
        for ch in changes:
            self.upsert(ch.record)
            self.append_history(ch)
