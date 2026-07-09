"""Tests du cœur anti-spam : hashs registre/RCP + diff vs historique."""
from __future__ import annotations

import tempfile
from pathlib import Path

from veille.schema import Record, RecordType, normalize_text
from veille.storage.base import ChangeType
from veille.storage.sqlite import SqliteStore


def _rec(uid: str, produit: str, molecules=None, rcp=None) -> Record:
    r = Record(
        source="test",
        source_uid=uid,
        record_type=RecordType.NOUVELLE_AMM,
        concurrent="Bimeda",
        produit=produit,
        molecules=molecules or ["Ivermectine"],
        pays="FR",
        rcp_content=rcp or {},
    )
    r.compute_hashes()
    return r


def test_normalize_text_neutralise_le_formatage():
    assert normalize_text("  Posologie :  5 mg/kg.\n") == normalize_text("posologie 5 mg kg")
    assert normalize_text("Été") == "ete"


def test_hash_registre_stable_et_sensible():
    a = _rec("1", "BIMECTIN")
    b = _rec("1", "BIMECTIN")
    assert a.hash_registre == b.hash_registre
    c = _rec("1", "BIMECTIN", molecules=["Moxidectine"])
    assert a.hash_registre != c.hash_registre


def test_hash_rcp_ignore_reformatage_mais_voit_le_fond():
    a = _rec("1", "BIMECTIN", rcp={"posologie": "5 mg/kg en une prise"})
    b = _rec("1", "BIMECTIN", rcp={"posologie": "  5 MG/KG  en une prise.  "})
    assert a.hash_rcp == b.hash_rcp                       # reformatage ignoré
    c = _rec("1", "BIMECTIN", rcp={"posologie": "10 mg/kg en une prise"})
    assert a.hash_rcp != c.hash_rcp                       # vrai changement vu
    # Le contenu RCP ne doit PAS polluer le hash registre.
    assert a.hash_registre == c.hash_registre


def test_diff_distingue_nouvelle_amm_et_maj_rcp():
    db = Path(tempfile.mktemp(suffix=".db"))
    store = SqliteStore(db)
    try:
        # 1er passage : nouveau
        changes = store.diff([_rec("1", "BIMECTIN", rcp={"posologie": "5 mg/kg"})], "test")
        assert changes[0].type == ChangeType.NOUVEAU
        store.commit_changes(changes)

        # Identique → rien
        assert store.diff([_rec("1", "BIMECTIN", rcp={"posologie": "5 mg/kg"})], "test") == []

        # Seul le RCP change → MODIFIE, aspect "rcp" uniquement
        changes = store.diff([_rec("1", "BIMECTIN", rcp={"posologie": "8 mg/kg"})], "test")
        assert changes[0].type == ChangeType.MODIFIE
        assert changes[0].aspects == ("rcp",)
        store.commit_changes(changes)

        # Seul le registre change (molécule) → aspect "registre" uniquement
        changes = store.diff(
            [_rec("1", "BIMECTIN", molecules=["Moxidectine"], rcp={"posologie": "8 mg/kg"})],
            "test",
        )
        assert changes[0].aspects == ("registre",)
    finally:
        store.close()
        db.unlink(missing_ok=True)
