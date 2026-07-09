"""Tests SPACE — PDF extraction + parsing, offline."""
from __future__ import annotations

import io, re
import pytest

from veille.sources.phase3_salons.space import (
    _extract_pdf_text, _pdf_uid, _find_excerpt,
)
from veille.schema import normalize_text


# ── helpers ──────────────────────────────────────────────────────────────────

def test_pdf_uid_stable():
    url = "https://s3.amazonaws.com/bucket/SPACE2026-DOSSIER_abc123.pdf?X-Amz=tok"
    assert _pdf_uid(url) == "SPACE2026-DOSSIER_abc123.pdf"


def test_pdf_uid_no_query():
    url = "https://example.com/path/to/liste-exposants.pdf"
    assert _pdf_uid(url) == "liste-exposants.pdf"


def test_find_excerpt_found():
    text = "lorem ipsum axience dolor sit amet consectetur"
    exc = _find_excerpt(text, "axience")
    assert "axience" in exc


def test_find_excerpt_not_found():
    assert _find_excerpt("lorem ipsum", "axience") == ""


# ── PDF extraction (with a real minimal PDF built in-memory) ─────────────────
# We use reportlab if available, otherwise skip.
try:
    from reportlab.pdfgen import canvas as rl_canvas
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


@pytest.mark.skipif(not HAS_REPORTLAB, reason="reportlab not installed")
def test_extract_pdf_text_with_reportlab():
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    c.drawString(72, 720, "AXIENCE expose au SPACE 2026")
    c.drawString(72, 700, "Bimeda présente ses vaccins aviaires")
    c.save()
    buf.seek(0)
    text = _extract_pdf_text(buf.read())
    assert "AXIENCE" in text
    assert "Bimeda" in text


def test_normalize_covers_pdf_noise():
    """Vérifie que normalize_text absorbe le bruit typique d'un PDF."""
    raw = "  AXIENCE   SAS\n\nVaccin  Aviaire  2026  "
    normed = normalize_text(raw)
    assert "axience" in normed
    assert "vaccin" in normed
    assert "  " not in normed  # pas de double espace
