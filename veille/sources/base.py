"""Interface commune à toutes les sources de veille."""
from __future__ import annotations

from abc import ABC, abstractmethod

from veille.schema import Record
from veille.settings import Settings


class Source(ABC):
    #: identifiant court, doit correspondre à la clé sous `sources:` dans config.yaml
    name: str

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.source_cfg(self.name)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @abstractmethod
    def fetch(self) -> list[Record]:
        """Collecte la source et renvoie des enregistrements normalisés.

        Doit respecter les contraintes de conformité (robots.txt, rate-limiting,
        canaux officiels). Ne déclenche aucune notification : c'est le rôle de
        l'orchestrateur après diff.
        """
