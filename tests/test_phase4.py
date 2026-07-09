"""Tests Phase 4 — Veille RH : France Travail + pages carrières, offline."""
from __future__ import annotations

import json
import pytest

from veille.sources.phase4_rh.francetravail import _parse_date, _detect_zone, FranceTravailSource
from veille.sources.phase4_rh.careers import _extract_jobs, _robots_allows


# ── France Travail helpers ────────────────────────────────────────────────────

def test_parse_date_iso():
    assert _parse_date("2026-05-01T00:00:00.000Z").isoformat() == "2026-05-01"


def test_parse_date_none():
    assert _parse_date(None) is None
    assert _parse_date("") is None


def test_detect_zone_afrique():
    zones = _detect_zone("Responsable Export Afrique subsaharienne")
    assert "afrique" in zones or "export" in zones


def test_detect_zone_mena():
    zones = _detect_zone("Chef de zone MENA - Santé animale")
    assert "mena" in zones


def test_detect_zone_no_match():
    zones = _detect_zone("Développeur Python junior")
    assert zones == []


def test_no_credentials_returns_empty(tmp_path, monkeypatch):
    """Sans credentials, fetch() ne plante pas et renvoie []."""
    import yaml
    from veille.settings import load_settings
    cfg = {
        "concurrents": [{"nom": "Axience", "aliases": ["axience"]}],
        "mots_cles_strategiques": [],
        "sources": {"france_travail": {"enabled": True}},
        "http": {"timeout_s": 10, "download_delay_s": 0},
        "storage": {"backend": "sqlite", "sqlite_path": str(tmp_path / "v.db")},
        "notifier": {"backend": "console"},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    # Pas de credentials dans l'environnement.
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
    settings = load_settings(cfg_path)
    from veille.sources.phase4_rh.francetravail import FranceTravailSource
    src = FranceTravailSource(settings)
    assert src.fetch() == []


# ── Careers HTML extraction ───────────────────────────────────────────────────

_HTML_WITH_JOBS = (
    "<html><body>"
    '<div class="job-listing">'
    '<a href="/jobs/responsable-export-afrique">Responsable Export Afrique - CDI</a>'
    '<time datetime="2026-05-15">15 mai 2026</time>'
    "</div>"
    '<div class="job-listing">'
    '<a href="/jobs/chef-produit-veterinaire">Chef de produit</a>'
    "</div>"
    "</body></html>"
).encode("utf-8")

def test_extract_jobs_finds_listings():
    jobs = _extract_jobs(_HTML_WITH_JOBS, "https://www.axience.com/recrutement/")
    assert len(jobs) >= 2
    titres = [j["titre"] for j in jobs]
    assert any("Responsable" in t or "Export" in t for t in titres)


def test_extract_jobs_absolute_url():
    jobs = _extract_jobs(_HTML_WITH_JOBS, "https://www.axience.com/recrutement/")
    urls = [j["url"] for j in jobs]
    assert all(u.startswith("https://") for u in urls)


def test_extract_jobs_date():
    jobs = _extract_jobs(_HTML_WITH_JOBS, "https://www.axience.com/recrutement/")
    # Au moins un job devrait avoir une date
    dates = [j["date_raw"] for j in jobs if j.get("date_raw")]
    assert dates  # "2026-05-15" présent


_HTML_EMPTY = b"<html><body><p>Aucune offre.</p></body></html>"

def test_extract_jobs_empty_page():
    jobs = _extract_jobs(_HTML_EMPTY, "https://example.com/careers/")
    assert jobs == []


# ── robots.txt (mock) — test logique permissive ───────────────────────────────

def test_robots_allows_no_robots(monkeypatch):
    """Si robots.txt est inaccessible → autorisé (comportement standard)."""
    import urllib.robotparser

    class FailingParser(urllib.robotparser.RobotFileParser):
        def read(self):
            raise OSError("connexion refusée")

    monkeypatch.setattr(urllib.robotparser, "RobotFileParser", FailingParser)
    # Doit retourner True (fail-open)
    result = _robots_allows("https://example.com/jobs/", "veille-lobs/0.1", 5.0)
    assert result is True
