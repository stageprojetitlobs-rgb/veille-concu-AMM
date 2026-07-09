"""Source AMM régionale UEMOA — portail SIAR (siar.uemoa.int).

L'UEMOA délivre une **AMM régionale unique** valable dans ses 8 États membres
(Bénin, Burkina Faso, Côte d'Ivoire, Guinée-Bissau, Mali, Niger, Sénégal, Togo) —
le cœur du marché export de Lobs en Afrique de l'Ouest.

Le portail métier `infoveterinaire.uemoa.int` est injoignable, mais le **Système
d'Information Agricole Régional** (`siar.uemoa.int`, fiche « Disponibilité des
médicaments vétérinaires ») expose les données : pays, n° AMM régional, classe
thérapeutique, forme pharmaceutique. Données publiques → conformes.

Limite : ce flux ne contient ni nom de marque ni titulaire → `concurrent` reste
None ; le signal porte sur la présence d'AMM par pays / classe thérapeutique.

Conformité : portail public officiel, pagination respectant `download_delay_s`,
aucune donnée personnelle.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date

import httpx

from veille.schema import Record, RecordType
from veille.sources.base import Source

log = logging.getLogger(__name__)

# Noms de pays (tels qu'affichés) → code ISO.
_PAYS_ISO = {
    "senegal": "SN", "sénégal": "SN",
    "mali": "ML",
    "cote d'ivoire": "CI", "côte d'ivoire": "CI", "cote d ivoire": "CI",
    "burkina faso": "BF", "burkina": "BF",
    "niger": "NE",
    "togo": "TG",
    "benin": "BJ", "bénin": "BJ",
    "guinee bissau": "GW", "guinée bissau": "GW", "guinee-bissau": "GW",
}

_AMM_RE = re.compile(r"UEMOA/V", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_TAG_RE = re.compile(r"<[^>]+>")


def _cells(tr_html: str) -> list[str]:
    out = []
    for td in re.findall(r"<td[^>]*>(.*?)</td>", tr_html, re.S):
        out.append(re.sub(r"\s+", " ", _TAG_RE.sub("", td)).strip())
    return out


def _iso(pays: str) -> str:
    return _PAYS_ISO.get(pays.strip().lower(), pays.strip()[:20])


class UemoaSiarSource(Source):
    """AMM régionale UEMOA via SIAR. Config attendue (config.yaml) :

        sources:
          uemoa_siar:
            enabled: true
            base_url: "https://siar.uemoa.int/disponibilite-medicament.php"
            id_fiche: 163
            max_pages: 40
            verify_ssl: false
    """

    name = "uemoa_siar"

    def fetch(self) -> list[Record]:
        base = self.cfg.get("base_url")
        if not base:
            log.warning("uemoa_siar : base_url absente de la config")
            return []
        id_fiche = self.cfg.get("id_fiche", 163)
        max_pages = int(self.cfg.get("max_pages", 40))
        verify = self.cfg.get("verify_ssl", False)
        delay = self.settings.download_delay_s

        client = httpx.Client(
            headers={"User-Agent": self.settings.user_agent,
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=max(self.settings.http_timeout_s, 30),
            verify=verify,
            follow_redirects=True,
        )
        try:
            # 1) page de base → récupère le `code` de session.
            try:
                html = client.get(base).text
            except httpx.HTTPError as exc:
                log.error("uemoa_siar : page de base injoignable (%s)", exc)
                return []
            m = re.search(r"code=([A-Za-z0-9=]+)", html)
            if not m:
                log.error("uemoa_siar : paramètre `code` introuvable sur la page")
                return []
            code = m.group(1)

            records: list[Record] = []
            seen: set[str] = set()
            for page in range(1, max_pages + 1):
                if page == 1:
                    page_html = html
                else:
                    if delay > 0:
                        time.sleep(delay)
                    url = f"{base}?code={code}&action=action_data&paginate={page}&idFiche={id_fiche}"
                    try:
                        page_html = client.get(url).text
                    except httpx.HTTPError as exc:
                        log.warning("uemoa_siar : page %d échec (%s)", page, exc)
                        break

                rows = [tr for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S)
                        if _AMM_RE.search(tr)]
                if not rows:
                    break

                new_on_page = 0
                for tr in rows:
                    c = _cells(tr)
                    if not c:
                        continue
                    pays_name = c[0]
                    # Saut des lignes de filtre/en-tête qui fuient parfois du tableau.
                    if not pays_name or any(k in pays_name.lower() for k in ("choisir", "numéro", "numero")):
                        continue
                    amm = next((x for x in c if _AMM_RE.search(x)), "")
                    if not amm:
                        continue
                    amm = re.sub(r"\s+", " ", amm).strip()
                    uid = f"{_iso(pays_name)}|{amm}".lower()
                    if uid in seen:
                        continue
                    seen.add(uid)
                    new_on_page += 1

                    idx = c.index(next(x for x in c if _AMM_RE.search(x)))
                    classe = c[idx + 1] if idx + 1 < len(c) else ""
                    forme = c[idx + 2] if idx + 2 < len(c) else ""
                    ym = _YEAR_RE.search(" ".join(c))
                    d = date(int(ym.group(0)), 1, 1) if ym else None

                    libelle = " · ".join(p for p in (classe, forme) if p) or amm
                    rec = Record(
                        source=self.name,
                        source_uid=uid,
                        record_type=RecordType.NOUVELLE_AMM,
                        concurrent=None,  # pas de titulaire/marque dans ce flux
                        produit=f"{amm} — {libelle}",
                        molecules=[],
                        pays=_iso(pays_name),
                        url=base,
                        date_source=d,
                        tags=self.settings.keywords_in(f"{classe} {forme}"),
                        extra={
                            "numero_amm": amm,
                            "classe_therapeutique": classe,
                            "forme": forme,
                            "pays_nom": pays_name,
                            "amm_regionale": "UEMOA",
                        },
                    )
                    rec.compute_hashes()
                    records.append(rec)

                if new_on_page == 0:
                    break
        finally:
            client.close()

        log.info("uemoa_siar : %d AMM régionale(s) UEMOA retenue(s)", len(records))
        return records
