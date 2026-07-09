"""Interface de notification découplée.

Premier backend : Slack (webhook). Extensible à Make/Airtable en ajoutant une
classe `Notifier` ici. L'orchestrateur ne connaît que cette interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from veille.storage.base import Change


class Notifier(ABC):
    @abstractmethod
    def send(self, changes: list[Change]) -> None:
        """Émet une notification pour la liste de changements (non vide)."""

    # Libellé du « nouveau » selon le type d'enregistrement (l'AMM n'est qu'un cas).
    _NOUVEAU_LABELS = {
        "nouvelle_amm": "🆕 NOUVELLE AMM",
        "produit": "🆕 NOUVEAU PRODUIT",
        "exposant": "🆕 NOUVEL EXPOSANT",
        "offre_emploi": "🆕 NOUVELLE OFFRE",
        "actualite": "📰 NOUVELLE ACTU",
    }

    @staticmethod
    def _label(change: Change) -> str:
        """Qualifie le changement selon son type et les aspects modifiés."""
        rtype = change.record.record_type.value
        if change.type.value == "nouveau":
            return Notifier._NOUVEAU_LABELS.get(rtype, "🆕 NOUVEAU")
        # Modification : le double hash registre/RCP ne concerne que les AMM.
        if rtype == "nouvelle_amm":
            has_reg = "registre" in change.aspects
            has_rcp = "rcp" in change.aspects
            if has_reg and has_rcp:
                return "✏️ AMM + RCP MODIFIÉS"
            if has_rcp:
                return "✏️ RCP MODIFIÉ"
            return "✏️ REGISTRE MODIFIÉ"
        return "✏️ MISE À JOUR"

    @staticmethod
    def format_line(change: Change) -> str:
        """Rendu texte d'un changement, adapté au record_type."""
        rec = change.record
        label = Notifier._label(change)
        concurrent = rec.concurrent or "concurrent ?"
        tags_str = f"  [tags: {', '.join(rec.tags)}]" if rec.tags else ""
        date_str = rec.date_source.isoformat() if rec.date_source else "?"
        url_str = f"\n   {rec.url}" if rec.url else ""
        rtype = rec.record_type.value

        if rtype in ("nouvelle_amm", "produit"):
            # Ligne détail : molécules + pays.
            molecules = ", ".join(rec.molecules) if rec.molecules else "—"
            pays_str = f" · {rec.pays}" if rec.pays else ""
            detail = f"   molécules: {molecules}{pays_str}"

        elif rtype == "exposant":
            # Ligne détail : salon (via extra) + pays.
            salon = rec.extra.get("salon") or rec.source
            pays_str = f" · {rec.pays}" if rec.pays else ""
            detail = f"   salon: {salon}{pays_str}"

        elif rtype == "offre_emploi":
            # Ligne détail : lieu + type de contrat + zones détectées.
            lieu = rec.extra.get("lieu") or "—"
            contrat = rec.extra.get("contrat") or ""
            zones = rec.extra.get("zones_detectees") or []
            zones_str = f" · zones: {', '.join(zones)}" if zones else ""
            contrat_str = f" · {contrat}" if contrat else ""
            detail = f"   lieu: {lieu}{contrat_str}{zones_str}"

        elif rtype == "actualite":
            # Ligne détail : résumé tronqué.
            resume = rec.extra.get("resume") or ""
            snippet = resume[:120].replace("\n", " ")
            detail = f"   {snippet}…" if len(resume) > 120 else f"   {snippet}"

        else:
            # Fallback générique.
            detail = f"   pays: {rec.pays or '—'}"

        return (
            f"{label} · {concurrent} · {rec.produit or '?'} ({date_str})\n"
            f"{detail}{tags_str}{url_str}"
        )
