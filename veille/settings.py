"""Chargement centralisé de la configuration (.env + config.yaml).

Aucun secret n'est lu depuis le YAML : tout ce qui est sensible vient du .env via
les variables d'environnement. Le YAML porte uniquement des réglages non secrets.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Racine du projet = parent du package `veille`.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"


def _norm(s: str) -> str:
    """casefold + suppression des accents — pour un matching robuste des noms."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold().strip()


@dataclass(frozen=True)
class Concurrent:
    nom: str
    aliases: tuple[str, ...]

    def matches(self, text: str) -> bool:
        """True si le texte (raison sociale, titulaire…) désigne ce concurrent.

        Matching par MOT ENTIER (\\b) et non par sous-chaîne : l'alias court
        « act » ne doit pas matcher « activité », « contact » ou
        « Actiengesellschaft ».
        """
        t = _norm(text)
        return any(
            re.search(rf"\b{re.escape(_norm(a))}\b", t)
            for a in (self.nom, *self.aliases)
        )


@dataclass(frozen=True)
class Settings:
    raw: dict
    user_agent: str
    slack_webhook_url: str | None
    concurrents: tuple[Concurrent, ...]
    mots_cles: tuple[str, ...] = field(default=())

    # --- Accès pratiques ---
    def source_cfg(self, name: str) -> dict:
        return self.raw.get("sources", {}).get(name, {})

    @property
    def sqlite_path(self) -> Path:
        # En cloud, dashboard et scraper partagent la base sur un disque persistant
        # via VEILLE_DB_PATH ; sinon chemin du config.yaml (relatif à la racine).
        env_path = os.environ.get("VEILLE_DB_PATH")
        if env_path:
            return Path(env_path)
        rel = self.raw.get("storage", {}).get("sqlite_path", "data/db/veille.db")
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    @property
    def storage_backend(self) -> str:
        return self.raw.get("storage", {}).get("backend", "sqlite")

    @property
    def notifier_backend(self) -> str:
        return self.raw.get("notifier", {}).get("backend", "console")

    @property
    def http_timeout_s(self) -> float:
        return float(self.raw.get("http", {}).get("timeout_s", 60))

    @property
    def download_delay_s(self) -> float:
        return float(self.raw.get("http", {}).get("download_delay_s", 1.0))

    def matched_concurrent(self, text: str) -> str | None:
        """Renvoie le nom canonique du concurrent désigné par `text`, sinon None."""
        for c in self.concurrents:
            if c.matches(text):
                return c.nom
        return None

    def keywords_in(self, text: str) -> list[str]:
        """Mots-clés stratégiques présents dans `text` (normalisé)."""
        t = _norm(text)
        return [kw for kw in self.mots_cles if _norm(kw) in t]


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")  # silencieux si absent
    cfg_path = config_path or DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    concurrents = tuple(
        Concurrent(nom=c["nom"], aliases=tuple(c.get("aliases", [])))
        for c in raw.get("concurrents", [])
    )

    return Settings(
        raw=raw,
        user_agent=os.getenv("VEILLE_USER_AGENT", "veille-lobs/0.1"),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
        concurrents=concurrents,
        mots_cles=tuple(raw.get("mots_cles_strategiques", [])),
    )
