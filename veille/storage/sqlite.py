"""Backend SQLite.

Deux tables :
  - `records`  : état COURANT, une ligne par (source, source_uid). Sert au diff.
  - `history`  : journal append-only des changements (nouveau/modifié) avec horodatage.

NB : on utilise une table générique partitionnée par colonne `source` plutôt qu'une
table physique par source. C'est plus simple à requêter et l'abstraction `Store`
suffit déjà à isoler les sources ; le swap Airtable reste trivial.

Deux hashs sont conservés par enregistrement (`hash_registre`, `hash_rcp`) pour
distinguer une nouvelle/MAJ d'AMM d'une mise à jour de RCP.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from veille.schema import Record
from veille.storage.base import Change, Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    natural_key    TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    source_uid     TEXT NOT NULL,
    record_type    TEXT NOT NULL,
    concurrent     TEXT,
    produit        TEXT,
    molecules      TEXT,
    pays           TEXT,
    url            TEXT,
    date_source    TEXT,
    date_detection TEXT NOT NULL,
    tags           TEXT,
    extra          TEXT,
    rcp_content    TEXT,
    hash_registre  TEXT NOT NULL,
    hash_rcp       TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source);

CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key     TEXT NOT NULL,
    source          TEXT NOT NULL,
    change_type     TEXT NOT NULL,
    aspects         TEXT NOT NULL,
    ancien_registre TEXT,
    ancien_rcp      TEXT,
    hash_registre   TEXT NOT NULL,
    hash_rcp        TEXT NOT NULL,
    produit         TEXT,
    concurrent      TEXT,
    detected_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_source ON history(source);
"""


class SqliteStore(Store):
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def known_states(self, source: str) -> dict[str, tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT natural_key, hash_registre, hash_rcp FROM records WHERE source = ?",
            (source,),
        )
        return {
            row["natural_key"]: (row["hash_registre"], row["hash_rcp"])
            for row in cur.fetchall()
        }

    def upsert(self, record: Record) -> None:
        row = record.to_row()
        self._conn.execute(
            """
            INSERT INTO records (
                natural_key, source, source_uid, record_type, concurrent, produit,
                molecules, pays, url, date_source, date_detection, tags, extra,
                rcp_content, hash_registre, hash_rcp, updated_at
            ) VALUES (
                :natural_key, :source, :source_uid, :record_type, :concurrent, :produit,
                :molecules, :pays, :url, :date_source, :date_detection, :tags, :extra,
                :rcp_content, :hash_registre, :hash_rcp, :updated_at
            )
            ON CONFLICT(natural_key) DO UPDATE SET
                record_type=excluded.record_type,
                concurrent=excluded.concurrent,
                produit=excluded.produit,
                molecules=excluded.molecules,
                pays=excluded.pays,
                url=excluded.url,
                date_source=excluded.date_source,
                tags=excluded.tags,
                extra=excluded.extra,
                rcp_content=excluded.rcp_content,
                hash_registre=excluded.hash_registre,
                hash_rcp=excluded.hash_rcp,
                updated_at=excluded.updated_at
            """,
            {**row, "natural_key": record.natural_key, "updated_at": datetime.utcnow().isoformat()},
        )
        self._conn.commit()

    def append_history(self, change: Change) -> None:
        rec = change.record
        self._conn.execute(
            """
            INSERT INTO history (
                natural_key, source, change_type, aspects, ancien_registre, ancien_rcp,
                hash_registre, hash_rcp, produit, concurrent, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.natural_key, rec.source, change.type.value, ",".join(change.aspects),
                change.ancien_registre, change.ancien_rcp,
                rec.hash_registre, rec.hash_rcp, rec.produit, rec.concurrent,
                datetime.utcnow().isoformat(),
            ),
        )
        self._conn.commit()
