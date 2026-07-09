"""Orchestrateur : lance les sources, diff vs historique, notifie les vrais changements."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from veille.notifier.base import Notifier
from veille.notifier.slack import ConsoleNotifier, SlackNotifier
from veille.settings import Settings, load_settings
from veille.sources.registry import available_sources, build_source
from veille.storage.base import Change, Store
from veille.storage.sqlite import SqliteStore

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    source: str
    collectes: int
    changements: list[Change]


def _build_store(settings: Settings) -> Store:
    if settings.storage_backend == "sqlite":
        return SqliteStore(settings.sqlite_path)
    raise ValueError(f"Backend stockage non supporté : {settings.storage_backend}")


def _build_notifier(settings: Settings, dry_run: bool) -> Notifier:
    # En --dry-run, on n'envoie jamais rien : console uniquement.
    if dry_run or settings.notifier_backend == "console":
        return ConsoleNotifier()
    if settings.notifier_backend == "slack":
        if not settings.slack_webhook_url:
            log.warning(
                "SLACK_WEBHOOK_URL absent → dégradation en mode console "
                "(les signaux sont logués mais rien n'est envoyé sur Slack). "
                "Renseigner .env pour activer les notifications."
            )
            return ConsoleNotifier()
        return SlackNotifier(settings.slack_webhook_url)
    raise ValueError(f"Notifier non supporté : {settings.notifier_backend}")


def run(
    sources: list[str] | None = None,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> list[RunResult]:
    settings = settings or load_settings()
    store = _build_store(settings)
    notifier = _build_notifier(settings, dry_run)

    targets = sources or available_sources()
    results: list[RunResult] = []

    try:
        for name in targets:
            src = build_source(name, settings)
            if not src.enabled:
                log.info("Source %s désactivée (config) — ignorée", name)
                continue

            log.info("→ Collecte source %s", name)
            records = src.fetch()
            changes = store.diff(records, source=name)
            log.info(
                "%s : %d collecté(s), %d changement(s) à notifier",
                name, len(records), len(changes),
            )

            if changes:
                notifier.send(changes)
                if not dry_run:
                    store.commit_changes(changes)
                else:
                    log.info("[dry-run] aucun envoi, aucune écriture historique")

            results.append(RunResult(name, len(records), changes))
    finally:
        if isinstance(store, SqliteStore):
            store.close()

    return results
