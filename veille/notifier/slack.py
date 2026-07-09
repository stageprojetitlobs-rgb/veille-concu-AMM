"""Notifier Slack via webhook entrant, et un ConsoleNotifier pour le local."""
from __future__ import annotations

import logging

import httpx

from veille.notifier.base import Notifier
from veille.storage.base import Change

log = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    """N'envoie rien : affiche dans les logs. Utile en local / sans webhook."""

    def send(self, changes: list[Change]) -> None:
        for ch in changes:
            log.info("[NOTIF] %s", self.format_line(ch))


class SlackNotifier(Notifier):
    def __init__(self, webhook_url: str, timeout_s: float = 15.0):
        if not webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL manquant : renseigner .env ou passer en --dry-run")
        self.webhook_url = webhook_url
        self.timeout_s = timeout_s

    def send(self, changes: list[Change]) -> None:
        if not changes:
            return
        header = f"*Veille concurrentielle — {len(changes)} signal(aux)*"
        body = "\n\n".join(self.format_line(ch) for ch in changes)
        payload = {"text": f"{header}\n\n{body}"}
        resp = httpx.post(self.webhook_url, json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        log.info("Slack: %d changement(s) notifié(s)", len(changes))
