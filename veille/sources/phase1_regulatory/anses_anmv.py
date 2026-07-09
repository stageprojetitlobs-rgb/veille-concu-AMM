"""Source 1 — ANSES/ANMV : médicaments vétérinaires autorisés en France.

Accès = open data officiel (cf SOURCES.md), AUCUN scraping :
  - archive .7z contenant le XML complet des AMM (un `medicinal-product` par AMM)
  - XML de référence résolvant les codes `term-*` (titulaires, substances, espèces…)

Champs stratégiques exploités :
  - prod-id (UUID UPD)  → clé de jointure future avec l'API EMA (Phase 2)
  - composition         → substances actives (molécules génériquées)
  - voie-administration → espèce, denrée (viande/lait) et `qte-ta` = temps d'attente
  - atcvet-code, paragraphes-rcp (scan mots-clés stratégiques)
"""
from __future__ import annotations

import io
import logging
import tempfile
from datetime import date, datetime
from pathlib import Path

import httpx
import py7zr
from lxml import etree

from veille.schema import Record, RecordType, normalize_text
from veille.settings import ROOT
from veille.sources.base import Source

log = logging.getLogger(__name__)

CACHE_DIR = ROOT / "data" / "cache" / "anses"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _classify_rcp_title(titre: str) -> str | None:
    """Mappe un titre de paragraphe RCP (tous gabarits ANSES) vers une rubrique
    business, ou None si la rubrique est administrative/non suivie.

    Le matching se fait sur le titre normalisé (insensible à la numérotation et aux
    variantes de formulation entre les 3-4 gabarits du référentiel term-titre).
    """
    t = normalize_text(titre)
    # Ordre important : "contre-indications" contient "indication".
    if "contre" in t and "indication" in t:
        return "contre_indications"
    if "especes cibles" in t:
        return "especes_cibles"
    if "indication" in t:
        return "indications"
    if "composition qualitative" in t:
        return "composition"
    if "posologie" in t:
        return "posologie"
    return None


class AnsesAnmvSource(Source):
    name = "anses_anmv"

    def fetch(self) -> list[Record]:
        ref = self._load_reference()
        xml_bytes = self._load_archive_xml()
        return list(self._parse_products(xml_bytes, ref))

    # ------------------------------------------------------------------ I/O
    def _http_get(self, url: str) -> bytes:
        headers = {"User-Agent": self.settings.user_agent}
        log.info("Téléchargement %s", url)
        resp = httpx.get(
            url, headers=headers, timeout=self.settings.http_timeout_s, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.content

    def _load_reference(self) -> dict[str, dict[str, str]]:
        """{term_name: {code: libellé}} depuis le XML de référence."""
        local = self.cfg.get("local_reference_path")
        if local:
            data = Path(local).read_bytes()
        else:
            data = self._http_get(self.cfg["url_reference"])
        root = etree.fromstring(data)
        ref: dict[str, dict[str, str]] = {}
        for term in root:
            if not isinstance(term.tag, str):
                continue  # commentaires
            mapping: dict[str, str] = {}
            for entry in term.findall("entry"):
                code = entry.findtext("source-code")
                desc = entry.findtext("source-desc")
                if code is not None and desc is not None:
                    mapping[code] = desc
            if mapping:
                ref[term.tag] = mapping
        log.info("Référentiel chargé : %d tables", len(ref))
        return ref

    def _load_archive_xml(self) -> bytes:
        """Renvoie le XML complet (décompressé depuis l'archive .7z)."""
        local = self.cfg.get("local_archive_path")
        if local:
            archive = Path(local).read_bytes()
        else:
            archive = self._http_get(self.cfg["url_archive"])
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / "amm-vet-fr-v2-v.7z").write_bytes(archive)

        with tempfile.TemporaryDirectory() as tmp:
            with py7zr.SevenZipFile(io.BytesIO(archive), "r") as z:
                # L'archive contient AUSSI le fichier de référence (...-d.xml). On
                # veut le fichier des produits, qui se termine par "-v.xml".
                names = [n for n in z.getnames() if n.lower().endswith(".xml")]
                products = [n for n in names if n.lower().endswith("-v.xml")]
                target = (products or [n for n in names if not n.lower().endswith("-d.xml")])
                if not target:
                    raise RuntimeError("Fichier produits introuvable dans l'archive ANSES")
                z.extract(path=tmp, targets=target[:1])
            return (Path(tmp) / target[0]).read_bytes()

    # ------------------------------------------------------------------ parsing
    def _parse_products(self, xml_bytes: bytes, ref: dict[str, dict[str, str]]):
        tit = ref.get("term-tit", {})
        sa = ref.get("term-sa", {})
        esp = ref.get("term-esp", {})
        denr = ref.get("term-denr", {})
        unite = ref.get("term-unite", {})
        titres = ref.get("term-titre", {})
        inclure_rcp = bool(self.cfg.get("inclure_rcp", False))

        context = etree.iterparse(io.BytesIO(xml_bytes), events=("end",), tag="medicinal-product")
        for _, el in context:
            rec = self._build_record(el, tit, sa, esp, denr, unite, titres, inclure_rcp)
            # Mémoire : on libère l'élément traité (gros fichier).
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
            if rec is not None:
                yield rec

    def _extract_temps_attente(self, el, esp, denr, unite) -> list[dict]:
        """Temps d'attente par espèce/denrée (qte-ta) — donnée structurée fiable."""
        out = []
        for va in el.findall("voie-administration/voie-admin"):
            qte = va.findtext("qte-ta")
            if not qte:
                continue
            out.append({
                "espece": esp.get(va.findtext("term-esp"), va.findtext("term-esp")),
                "denree": denr.get(va.findtext("term-denr"), va.findtext("term-denr")),
                "valeur": qte,
                "unite": unite.get(va.findtext("term-unite"), va.findtext("term-unite")),
            })
        return out

    def _extract_rcp_content(self, el, titres, esp, denr, unite) -> dict:
        """Sous-ensemble business du RCP (cf SOURCES.md 1b). Rubriques administratives
        ignorées. Données issues du même XML V2 — aucun appel à l'IRCP."""
        buckets: dict[str, list[str]] = {}
        for p in el.findall("paragraphes-rcp/para-rcp"):
            cat = _classify_rcp_title(titres.get(p.findtext("term-titre"), ""))
            if cat is None:
                continue
            contenu = (p.findtext("contenu") or "").strip()
            if contenu:
                buckets.setdefault(cat, []).append(contenu)
        rcp = {cat: "\n".join(parts) for cat, parts in buckets.items()}
        # Temps d'attente : on privilégie la donnée structurée (qte-ta).
        ta = self._extract_temps_attente(el, esp, denr, unite)
        if ta:
            rcp["temps_attente"] = ta
        return rcp

    def _build_record(self, el, tit, sa, esp, denr, unite, titres, inclure_rcp) -> Record | None:
        titulaire = tit.get(el.findtext("term-tit"), "")
        concurrent = self.settings.matched_concurrent(titulaire) if titulaire else None

        # Filtre : par défaut on ne garde que les concurrents suivis.
        if not concurrent and not self.cfg.get("inclure_tous_produits", False):
            return None

        nom = el.findtext("nom")
        molecules = []
        for c in el.findall("composition/compo"):
            code = c.findtext("sa/term-sa")
            if code is None:
                continue
            libelle = sa.get(code)
            if libelle:
                molecules.append(libelle)
            else:
                log.warning(
                    "term-sa non résolu : code %r (produit %r) — stocké comme [INCONNU:%s]",
                    code, nom, code,
                )
                molecules.append(f"[INCONNU:{code}]")
        molecules = sorted(set(molecules))

        atcvet = [c.text for c in el.findall("atcvet-code/code-atcvet") if c.text]

        # Scan mots-clés stratégiques sur nom + RCP (sur tout le RCP, peu coûteux).
        rcp_text = " ".join(
            (p.findtext("contenu") or "") for p in el.findall("paragraphes-rcp/para-rcp")
        )
        tags = self.settings.keywords_in(f"{nom} {rcp_text}")

        extra = {
            "num_amm_fr": el.findtext("num"),
            "num_amm_eu": el.findtext("num-amm"),
            "perm_id": el.findtext("perm-id"),
            "prod_id_upd": el.findtext("prod-id"),  # clé jointure EMA
            "titulaire": titulaire,
            "atcvet": atcvet,
        }

        # RCP business uniquement si demandé (garde la source légère par défaut).
        rcp_content = (
            self._extract_rcp_content(el, titres, esp, denr, unite) if inclure_rcp else {}
        )

        rec = Record(
            source=self.name,
            source_uid=el.findtext("src-id"),
            record_type=RecordType.NOUVELLE_AMM,
            concurrent=concurrent,
            produit=nom,
            molecules=molecules,
            pays="FR",
            url=el.findtext("lien-rcp"),
            date_source=_parse_date(el.findtext("date-amm")),
            tags=tags,
            extra=extra,
            rcp_content=rcp_content,
        )
        rec.compute_hashes()
        return rec
