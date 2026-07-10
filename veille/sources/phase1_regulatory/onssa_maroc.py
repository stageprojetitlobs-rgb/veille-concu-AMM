"""Source AMM Maroc — ONSSA « Liste positive des Médicaments Vétérinaires ».

Le Maroc (ONSSA — Office National de Sécurité Sanitaire des produits
Alimentaires) publie la liste officielle des médicaments vétérinaires autorisés
sous forme d'un **PDF public unique**, mis à jour périodiquement. Document
explicitement destiné à la consultation publique → 100 % conforme (on privilégie
l'export officiel au scraping de pages, cf CLAUDE.md).

Le PDF est un tableau multi-colonnes (société, produit, date AMM, principe actif,
espèces cibles, validité, présentation, n° AMM). On reconstruit les colonnes par
position horizontale (x0) des mots — pdfplumber — puis on regroupe par produit
(une ligne = un produit, ancrée sur sa date d'AMM).

Le périmètre Maroc est pertinent pour Lobs : marché export direct, et les labos
qui y déposent une AMM sont souvent les mêmes que ceux qui exportent en Afrique.

Conformité :
  - document public officiel, téléchargé une fois par run ;
  - rate-limiting : un seul GET (pas de crawl) ;
  - aucune donnée personnelle (titulaires = personnes morales).
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import date, datetime

import httpx
import pdfplumber

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# Bornes de colonnes (position x0 du mot), calées sur la mise en page du PDF ONSSA.
# Validité / présentation (506 ≤ x0 < 700) sont ignorés : hors identité registre.
# N° AMM (x0 ≥ 700, dernière colonne, ex. "AMM N° 1706.4/19/V2") EST capté :
# c'est la clé de vérification sur le site officiel, indispensable.
def _col_of(x0: float) -> str | None:
    if x0 < 110:
        return "societe"
    if 110 <= x0 < 210:
        return "produit"
    if 210 <= x0 < 283:
        return "date"
    if 283 <= x0 < 415:
        return "principe"
    if 415 <= x0 < 506:
        return "especes"
    if x0 >= 700:
        return "reg_no"
    return None


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


def _join_band(cells: dict, col: str, lo: float, hi: float) -> str:
    """Concatène les mots d'une colonne dont le `top` est dans la bande [lo, hi[."""
    parts = [t for (tp, x, t) in sorted(cells[col]) if lo <= tp < hi]
    return " ".join(parts).strip()


def _split_molecules(principe: str) -> list[str]:
    """Découpe un champ principe actif en substances. Heuristique simple :
    nouvelle molécule = mot commençant par une majuscule après un mot en
    minuscules (ex. « Colistine sulfate Enrofloxacine » → 2 molécules).
    En cas de doute on garde la chaîne entière (1 élément).
    """
    principe = principe.strip()
    if not principe:
        return []
    tokens = principe.split()
    mols: list[str] = []
    cur: list[str] = []
    for i, tok in enumerate(tokens):
        starts_upper = tok[:1].isupper()
        prev_lower = bool(cur) and cur[-1][:1].islower()
        if starts_upper and prev_lower:
            mols.append(" ".join(cur))
            cur = [tok]
        else:
            cur.append(tok)
    if cur:
        mols.append(" ".join(cur))
    return [m.strip() for m in mols if m.strip()]


class OnssaMarocSource(Source):
    """Liste positive ONSSA (Maroc). Config attendue (config.yaml) :

        sources:
          onssa_maroc:
            enabled: true
            url_pdf: "https://www.onssa.gov.ma/.../Liste-positive-...pdf"
            # Si false, ne garde que les produits des concurrents suivis.
            inclure_tous_produits: true
    """

    name = "onssa_maroc"

    def fetch(self) -> list[Record]:
        url = self.cfg.get("url_pdf")
        if not url:
            log.warning("onssa_maroc : url_pdf absente de la config")
            return []

        try:
            pdf_path = self._download(url)
        except httpx.HTTPError as exc:
            log.error("onssa_maroc : échec téléchargement PDF (%s)", exc)
            return []

        produits = self._parse_pdf(pdf_path)
        log.info("onssa_maroc : %d produit(s) extrait(s) du PDF", len(produits))

        records: list[Record] = []
        seen: set[str] = set()
        inclure_tous = self.cfg.get("inclure_tous_produits", True)

        for p in produits:
            societe = p["societe"]
            produit = p["produit"]
            if not produit:
                continue

            concurrent = self.settings.matched_concurrent(societe) if societe else None
            if not concurrent and not inclure_tous:
                continue

            uid = f"{societe}|{produit}".lower()
            if uid in seen:
                continue
            seen.add(uid)

            molecules = _split_molecules(p["principe"])
            especes = p["especes"]
            tags = self.settings.keywords_in(f"{produit} {p['principe']} {especes}")

            rec = Record(
                source=self.name,
                source_uid=uid,
                record_type=RecordType.NOUVELLE_AMM,
                concurrent=concurrent,
                produit=produit,
                molecules=molecules,
                pays="MA",
                url=url,
                date_source=_parse_date(p["date"]),
                tags=tags,
                extra={
                    "titulaire": societe,
                    "principe_actif": p["principe"],
                    "especes_cibles": especes,
                    "numero_amm": p.get("reg_no", ""),
                },
            )
            rec.compute_hashes()
            records.append(rec)

        log.info("onssa_maroc : %d enregistrement(s) retenu(s)", len(records))
        return records

    def _download(self, url: str) -> str:
        resp = httpx.get(
            url,
            headers={"User-Agent": self.settings.user_agent},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
        )
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name

    def _parse_pdf(self, pdf_path: str) -> list[dict]:
        """Parse le PDF en liste de produits. La société, imprimée une seule fois
        par groupe (et pouvant courir sur plusieurs pages), est reportée."""
        out: list[dict] = []
        current_societe = ""

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                rows, current_societe = self._parse_page(page, current_societe)
                out.extend(rows)
        return out

    @staticmethod
    def _parse_page(page, current_societe: str) -> tuple[list[dict], str]:
        words = [w for w in page.extract_words() if 125 < w["top"] < 538]
        cells: dict[str, list] = {
            "societe": [], "produit": [], "date": [], "principe": [], "especes": [],
            "reg_no": [],
        }
        for w in words:
            c = _col_of(w["x0"])
            if c:
                cells[c].append((w["top"], w["x0"], w["text"]))

        # Lignes « société » regroupées par position verticale (top arrondi).
        soc_by_top: dict[int, list[str]] = {}
        for (tp, x, t) in sorted(cells["societe"]):
            soc_by_top.setdefault(round(tp), []).append(t)
        soc_lines = sorted((tp, " ".join(ws)) for tp, ws in soc_by_top.items())

        dates = sorted((t, txt) for (t, x, txt) in cells["date"] if _DATE_RE.match(txt))
        rows: list[dict] = []
        for i, (top, d) in enumerate(dates):
            lo = top - 6
            hi = dates[i + 1][0] - 6 if i + 1 < len(dates) else 10**6

            # Société applicable = dernière imprimée au-dessus (ou sur) ce produit.
            soc_here = current_societe
            for tp, txt in soc_lines:
                if tp <= top + 3:
                    soc_here = txt
            current_societe = soc_here

            # Une même ligne « produit » peut porter plusieurs numéros d'AMM
            # (une par présentation/dosage) : "AMM N° X AMM N° Y ...". On les
            # sépare proprement plutôt que de garder un seul bloc concaténé.
            reg_no_raw = _join_band(cells, "reg_no", lo, hi)
            # Un vrai numéro contient au moins un chiffre et un "/" (ex. "1706.4/19/V2") ;
            # écarte les fragments parasites (mots de la colonne voisine mal bornés).
            reg_nos = [n.strip() for n in re.split(r"AMM\s*N°\s*", reg_no_raw)
                       if re.search(r"\d", n) and "/" in n]
            rows.append({
                "societe": soc_here,
                "produit": _join_band(cells, "produit", lo, hi),
                "date": d,
                "principe": _join_band(cells, "principe", lo, hi),
                "especes": _join_band(cells, "especes", lo, hi),
                "reg_no": "; ".join(reg_nos),
            })
        return rows, current_societe
