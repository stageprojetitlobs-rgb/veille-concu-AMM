"""Dashboard veille concurrentielle Lobs — serveur local.

Usage :
    python -m scripts.dashboard          # ouvre sur http://localhost:8765
    python -m scripts.dashboard --port 9000

Zéro dépendance externe : stdlib uniquement (http.server, sqlite3, json).
Les assets CSS/JS (Tailwind, Chart.js) sont chargés depuis CDN.

Architecture (3 pages, navigation commune via layout()) :
    /              Accueil    — vue d'ensemble : chiffres clés, graphiques, derniers évènements
    /concurrents   Concurrents — qui surveille-t-on, organisés par segment thérapeutique
    /signaux       Signaux    — tous les signaux détectés, filtrables
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import hashlib
import hmac
import html as _html
import json
import os
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
# En cloud, la base vit sur un disque persistant : chemin surchargé par VEILLE_DB_PATH.
DB_PATH = Path(os.environ.get("VEILLE_DB_PATH", ROOT / "data" / "db" / "veille.db"))
CONFIG_PATH = ROOT / "config" / "config.yaml"

# Authentification : si DASH_PASSWORD est défini (déploiement en ligne), le
# dashboard exige un identifiant/mot de passe (HTTP Basic Auth). En local, sans
# cette variable, aucun mot de passe n'est demandé.
DASH_USER = os.environ.get("DASH_USER", "lobs")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")


def load_competitors_config():
    """Charge la liste des concurrents avec leurs segments depuis config.yaml."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("concurrents", [])


# ── Palette couleurs ──────────────────────────────────────────────────────────
# Couleurs « marque » fixes pour les concurrents historiques, le reste est
# généré de façon déterministe à partir du nom (même nom → même couleur, stable
# entre deux rafraîchissements). Couvre donc les 47 concurrents sans table à jour.
_BRAND_COLORS = {
    "Bimeda":   "#3B82F6",
    "Axience":  "#10B981",
    "Laprovet": "#F59E0B",
    "Kepro":    "#8B5CF6",
    "Inovet":   "#EF4444",
    "Osalia":   "#EC4899",
}
DEFAULT_COLOR = "#6B7280"


def color_for(nom: str | None) -> str:
    """Couleur stable pour un concurrent (marque fixe ou dérivée du nom)."""
    if not nom:
        return DEFAULT_COLOR
    if nom in _BRAND_COLORS:
        return _BRAND_COLORS[nom]
    # Hash → teinte ; saturation/luminosité fixes pour un rendu homogène et lisible.
    h = int(hashlib.md5(nom.encode("utf-8")).hexdigest(), 16)
    hue = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.50, 0.55)
    return f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"


RECORD_TYPE_LABELS = {
    "nouvelle_amm": "AMM",
    "produit": "Produit",
    "exposant": "Exposant",
    "offre_emploi": "Offre emploi",
    "actualite": "Actu",
}

SOURCE_LABELS = {
    "anses_anmv": "ANSES/ANMV (FR)",
    "onssa_maroc": "ONSSA (MA)",
    "nafdac_nigeria": "NAFDAC (NG, historique)",
    "nafdac_greenbook": "NAFDAC Greenbook (NG)",
    "cdsco_inde": "CDSCO (IN)",
    "cucthuy_vietnam": "Cục Thú y (VN)",
    "bnvf_bangladesh": "BNVF (BD)",
    "vmd_kenya": "VMD (KE)",
    "pdf_registry": "Registre national",
    "zamra_zambie": "ZAMRA (ZM)",
    "mcaz_zimbabwe": "MCAZ (ZW)",
    "uemoa_siar": "UEMOA (Afrique Ouest)",
    "ema_upd": "EMA UPD",
    "kepro": "Kepro",
    "inovet": "Inovet",
    "rss": "RSS",
    "news_pages": "Pages actu",
    "space": "SPACE",
    "france_travail": "France Travail",
    "careers": "Carrières",
}

SEGMENT_META = {
    "volaille":            {"label": "Vaccins Volaille",      "color": "#F59E0B"},
    "chiens":              {"label": "Vaccins Chiens",        "color": "#3B82F6"},
    "ruminants":           {"label": "Vaccins Ruminants",     "color": "#10B981"},
    "anti-infectieux":     {"label": "Anti-infectieux",       "color": "#8B5CF6"},
    "trypanocides":        {"label": "Trypanocides",          "color": "#EF4444"},
    "api":                 {"label": "API (mat. active)",     "color": "#6366F1"},
    "ape":                 {"label": "Antiparasitaires ext.", "color": "#14B8A6"},
    "anti-inflammatoires": {"label": "Anti-inflammatoires",   "color": "#F97316"},
    "reproduction":        {"label": "Reproduction",          "color": "#EC4899"},
    "vitamines":           {"label": "Vitamines",             "color": "#84CC16"},
    "general":             {"label": "Général",               "color": "#6B7280"},
}
SEGMENT_ORDER = ["volaille", "chiens", "ruminants", "anti-infectieux",
                 "trypanocides", "api", "ape", "anti-inflammatoires",
                 "reproduction", "vitamines", "general"]


# ── Requêtes DB ───────────────────────────────────────────────────────────────

def get_db():
    # Sur un disque cloud vierge, la base n'existe pas encore (scraper pas encore
    # passé) : on crée le dossier + le schéma vide pour afficher un dashboard à 0
    # plutôt que planter. Import local pour ne pas alourdir le démarrage local.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if not con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
    ).fetchone():
        from veille.storage.sqlite import _SCHEMA
        con.executescript(_SCHEMA)
        con.commit()
    return con


def stats(con):
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM records")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM history")
    history = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT concurrent) FROM records WHERE concurrent IS NOT NULL")
    nb_conc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT source) FROM records")
    nb_sources = cur.fetchone()[0]
    return {"total": total, "history": history, "concurrents": nb_conc, "sources": nb_sources}


def by_concurrent(con):
    cur = con.cursor()
    cur.execute("""
        SELECT concurrent, COUNT(*) as n
        FROM records WHERE concurrent IS NOT NULL
        GROUP BY concurrent ORDER BY n DESC
    """)
    return [dict(r) for r in cur.fetchall()]


def by_source(con):
    cur = con.cursor()
    cur.execute("""
        SELECT source, record_type, COUNT(*) as n
        FROM records GROUP BY source, record_type ORDER BY source
    """)
    return [dict(r) for r in cur.fetchall()]


def recent_history(con, limit=50):
    cur = con.cursor()
    cur.execute("""
        SELECT h.change_type, h.detected_at, h.source,
               h.concurrent, h.produit
        FROM history h
        ORDER BY h.detected_at DESC LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def records_filtered(con, concurrent=None, source=None, rtype=None, pays=None, limit=200):
    where, params = [], []
    if concurrent:
        where.append("concurrent = ?"); params.append(concurrent)
    if source:
        where.append("source = ?"); params.append(source)
    if rtype:
        where.append("record_type = ?"); params.append(rtype)
    if pays:
        where.append("pays = ?"); params.append(pays)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cur = con.cursor()
    cur.execute(f"""
        SELECT source, source_uid, record_type, concurrent, produit,
               molecules, pays, url, date_source, date_detection, tags, extra
        FROM records {clause}
        ORDER BY COALESCE(date_source, date_detection) DESC
        LIMIT ?
    """, params + [limit])
    return [dict(r) for r in cur.fetchall()]


# Sources qui sont de vrais registres d'AMM (pas de la présence/actu).
AMM_SOURCES = (
    "anses_anmv", "onssa_maroc", "nafdac_nigeria", "nafdac_greenbook", "cdsco_inde",
    "pdf_registry", "zamra_zambie", "mcaz_zimbabwe", "uemoa_siar", "cucthuy_vietnam",
    "bnvf_bangladesh", "vmd_kenya",
)


def amm_counts_by_pays(con):
    """Nombre d'AMM par pays (registres officiels uniquement)."""
    q = "SELECT pays, COUNT(*) FROM records WHERE source IN (%s) AND pays IS NOT NULL GROUP BY pays ORDER BY COUNT(*) DESC" % (
        ",".join("?" * len(AMM_SOURCES)))
    cur = con.cursor()
    cur.execute(q, AMM_SOURCES)
    return [(p, n) for p, n in cur.fetchall()]


def amm_for_pays(con, pays, limit=10000):
    """Toutes les AMM d'un pays donné (registres officiels)."""
    # Tri : AMM la plus récente d'abord (date registre, sinon date de collecte).
    q = "SELECT concurrent, produit, molecules, source, url, date_source, date_detection, extra FROM records WHERE source IN (%s) AND pays = ? ORDER BY COALESCE(date_source, date_detection) DESC, produit LIMIT ?" % (
        ",".join("?" * len(AMM_SOURCES)))
    cur = con.cursor()
    cur.execute(q, (*AMM_SOURCES, pays, limit))
    return [dict(r) for r in cur.fetchall()]


def date_affichee(r) -> str:
    """Date du registre si connue, sinon date de 1ʳᵉ collecte préfixée « ≈ »
    (beaucoup de registres PDF ne datent pas leurs lignes)."""
    if r.get("date_source"):
        return r["date_source"][:10]
    det = (r.get("date_detection") or "")[:10]
    return f"≈ {det}" if det else "—"


def official_urls_by_pays(con):
    """URL officielle (registre/source) représentative pour chaque code pays."""
    cur = con.cursor()
    cur.execute("""
        SELECT pays, url FROM records
        WHERE pays IS NOT NULL AND url IS NOT NULL AND url != ''
        GROUP BY pays
    """)
    return {p: u for p, u in cur.fetchall()}


def all_concurrents(con):
    cur = con.cursor()
    cur.execute("SELECT DISTINCT concurrent FROM records WHERE concurrent IS NOT NULL ORDER BY concurrent")
    return [r[0] for r in cur.fetchall()]


def all_sources(con):
    cur = con.cursor()
    cur.execute("SELECT DISTINCT source FROM records ORDER BY source")
    return [r[0] for r in cur.fetchall()]


# ── Layout commun ─────────────────────────────────────────────────────────────

def layout(active: str, title: str, body: str, extra_head: str = "",
           extra_script: str = "") -> str:
    """Gabarit HTML partagé : en-tête, navigation, pied de page identiques
    sur toutes les pages. `active` ∈ {accueil, concurrents, signaux}.
    """
    def nav_link(href, key, label, icon):
        # Style identique sur toutes les pages : seule la pastille de fond change
        # pour l'onglet actif → aucun décalage de mise en page entre les pages.
        base = "flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
        if key == active:
            cls = f"{base} bg-indigo-600 text-white shadow-sm"
        else:
            cls = f"{base} text-gray-600 hover:bg-white hover:text-indigo-600"
        return f'<a href="{href}" class="{cls}">{label}</a>'

    nav = (
        nav_link("/", "accueil", "Accueil", "")
        + nav_link("/amm", "amm", "AMM par pays", "")
        + nav_link("/concurrents", "concurrents", "Concurrents", "")
        + nav_link("/afrique", "afrique", "Afrique", "")
        + nav_link("/asie", "asie", "Asie & MO", "")
        + nav_link("/signaux", "signaux", "Signaux", "")
        + nav_link("/aide", "aide", "Infos", "")
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Veille Lobs</title>
  <script src="https://cdn.tailwindcss.com"></script>
  {extra_head}
</head>
<body class="bg-gray-50 text-gray-800 font-sans min-h-screen flex flex-col">

<header class="bg-white border-b shadow-sm sticky top-0 z-10">
  <div class="max-w-7xl mx-auto px-6 py-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
    <div class="flex items-center gap-3">
      <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white font-bold text-lg">L</div>
      <div>
        <p class="font-bold text-gray-900 leading-tight text-base">Veille Concurrentielle Lobs</p>
        <p class="text-xs text-gray-400 leading-tight">Surveillance du marché vétérinaire</p>
      </div>
    </div>
    <nav class="flex items-center gap-1 bg-gray-100 rounded-xl p-1">{nav}</nav>
  </div>
</header>

<main class="flex-1 w-full max-w-7xl mx-auto px-6 py-8">
{body}
</main>

<footer class="border-t bg-white">
  <div class="max-w-7xl mx-auto px-6 py-4 text-xs text-gray-400 flex justify-between">
    <span>Veille automatisée · sources publiques · robots.txt respecté</span>
    <a href="/" class="hover:text-gray-600">Rafraîchir</a>
  </div>
</footer>

{extra_script}
</body>
</html>"""


# ── Page Accueil ──────────────────────────────────────────────────────────────

def render_accueil(stats_d, by_conc, by_src, history) -> str:
    # Top 12 concurrents pour le graphique (le reste regroupé en « Autres »)
    top = by_conc[:12]
    rest = sum(r["n"] for r in by_conc[12:])
    conc_labels = [r["concurrent"] for r in top] + (["Autres"] if rest else [])
    conc_data = [r["n"] for r in top] + ([rest] if rest else [])
    conc_colors = [color_for(r["concurrent"]) for r in top] + (["#CBD5E1"] if rest else [])

    src_agg: dict = {}
    for r in by_src:
        lbl = SOURCE_LABELS.get(r["source"], r["source"])
        src_agg[lbl] = src_agg.get(lbl, 0) + r["n"]

    # Historique
    hist_html = ""
    for h in history:
        conc = h["concurrent"] or "—"
        color = color_for(h["concurrent"])
        ctype = "Nouveau" if h["change_type"] == "nouveau" else "Modifié"
        produit = (h["produit"] or "—")[:70]
        src_lbl = SOURCE_LABELS.get(h["source"], h["source"])
        dt = (h["detected_at"] or "")[:16].replace("T", " ")
        hist_html += f"""
        <tr class="border-b hover:bg-gray-50">
          <td class="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{dt}</td>
          <td class="px-3 py-2 text-xs font-semibold" style="color:{color}">{conc}</td>
          <td class="px-3 py-2 text-xs">{produit}</td>
          <td class="px-3 py-2 text-xs">{ctype}</td>
          <td class="px-3 py-2 text-xs text-gray-500">{src_lbl}</td>
        </tr>"""
    if not hist_html:
        hist_html = '<tr><td colspan="5" class="px-3 py-6 text-center text-sm text-gray-400">Aucun évènement détecté pour l\'instant.</td></tr>'

    cards = [
        ("Signaux en base", stats_d["total"], "text-indigo-600",
         "Toutes les informations collectées (produits, AMM, actualités…)"),
        ("Concurrents actifs", stats_d["concurrents"], "text-emerald-600",
         "Concurrents pour lesquels au moins un signal existe"),
        ("Sources actives", stats_d["sources"], "text-violet-600",
         "Canaux surveillés (registres, RSS, pages actu…)"),
        ("Évènements suivis", stats_d["history"], "text-amber-600",
         "Nouveautés et modifications historisées dans le temps"),
    ]
    cards_html = ""
    for label, val, cls, desc in cards:
        cards_html += f"""
        <div class="bg-white rounded-xl shadow-sm p-5 border">
          <p class="text-3xl font-bold {cls}">{val}</p>
          <p class="text-sm font-medium text-gray-700 mt-1">{label}</p>
          <p class="text-xs text-gray-400 mt-1 leading-snug">{desc}</p>
        </div>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Vue d'ensemble</h1>
    <p class="text-sm text-gray-500 mt-1">
      Suivi automatique de l'activité de nos concurrents vétérinaires.
      Cette page résume ce que le système a collecté ; détaillez par
      <a href="/concurrents" class="text-indigo-600 hover:underline">concurrent</a> ou explorez les
      <a href="/signaux" class="text-indigo-600 hover:underline">signaux</a>.
    </p>
  </div>

  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">{cards_html}</div>

  <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-8">
    <div class="bg-white rounded-xl shadow-sm p-5 border">
      <h2 class="font-semibold text-sm text-gray-700 mb-1">Qui génère le plus de signaux ?</h2>
      <p class="text-xs text-gray-400 mb-4">Répartition des signaux par concurrent (top 12).</p>
      <canvas id="concChart" height="200"></canvas>
    </div>
    <div class="bg-white rounded-xl shadow-sm p-5 border">
      <h2 class="font-semibold text-sm text-gray-700 mb-1">D'où viennent les informations ?</h2>
      <p class="text-xs text-gray-400 mb-4">Nombre de signaux par canal de collecte.</p>
      <canvas id="srcChart" height="200"></canvas>
    </div>
  </div>

  <div class="bg-white rounded-xl shadow-sm border">
    <div class="px-5 py-4 border-b flex items-center justify-between">
      <div>
        <h2 class="font-semibold text-sm text-gray-700">Derniers évènements détectés</h2>
        <p class="text-xs text-gray-400">Nouveautés et modifications récentes chez les concurrents.</p>
      </div>
      <span class="text-xs text-gray-400">50 derniers</span>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
          <tr>
            <th class="px-3 py-2 text-left">Date</th>
            <th class="px-3 py-2 text-left">Concurrent</th>
            <th class="px-3 py-2 text-left">Signal</th>
            <th class="px-3 py-2 text-left">Changement</th>
            <th class="px-3 py-2 text-left">Source</th>
          </tr>
        </thead>
        <tbody>{hist_html}</tbody>
      </table>
    </div>
  </div>
"""

    extra_head = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>'
    extra_script = f"""<script>
new Chart(document.getElementById('concChart'), {{
  type: 'doughnut',
  data: {{ labels: {json.dumps(conc_labels)}, datasets: [{{ data: {json.dumps(conc_data)}, backgroundColor: {json.dumps(conc_colors)}, borderWidth: 2 }}] }},
  options: {{ plugins: {{ legend: {{ position: 'right', labels: {{ font: {{ size: 11 }}, boxWidth: 12 }} }} }}, cutout: '58%' }}
}});
new Chart(document.getElementById('srcChart'), {{
  type: 'bar',
  data: {{ labels: {json.dumps(list(src_agg.keys()))}, datasets: [{{ data: {json.dumps(list(src_agg.values()))}, backgroundColor: '#6366F1', borderRadius: 6 }}] }},
  options: {{ indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ beginAtZero: true, ticks: {{ font: {{ size: 10 }} }} }}, y: {{ ticks: {{ font: {{ size: 11 }} }} }} }} }}
}});
</script>"""

    return layout("accueil", "Accueil", body, extra_head, extra_script)


# ── Page Concurrents ──────────────────────────────────────────────────────────

def render_concurrents(by_conc) -> str:
    competitors = load_competitors_config()
    counts = {r["concurrent"]: r["n"] for r in by_conc}

    by_seg: dict[str, list[str]] = {}
    for c in competitors:
        for seg in c.get("segments", ["general"]):
            by_seg.setdefault(seg, []).append(c["nom"])

    cards_html = ""
    for seg in SEGMENT_ORDER:
        if seg not in by_seg:
            continue
        meta = SEGMENT_META.get(seg, {"label": seg, "color": DEFAULT_COLOR})
        color, label = meta["color"], meta["label"]
        noms = sorted(by_seg[seg])
        badges = "".join(
            f'<a href="/signaux?concurrent={n}" class="inline-block px-2 py-1 rounded text-xs '
            f'font-medium text-white mr-1 mb-1 hover:opacity-80" '
            f'style="background:{color_for(n)}">{n}'
            + (f' <span class="opacity-70">·{counts[n]}</span>' if counts.get(n) else "")
            + '</a>'
            for n in noms
        )
        cards_html += f"""
        <div class="bg-white rounded-xl shadow-sm p-4 border-l-4" style="border-color:{color}">
          <h2 class="font-bold text-sm mb-3" style="color:{color}">{label}
            <span class="text-gray-400 font-normal ml-1">({len(noms)})</span>
          </h2>
          <div>{badges}</div>
        </div>"""

    rows = ""
    for c in sorted(competitors, key=lambda x: x["nom"]):
        nom = c["nom"]
        n = counts.get(nom, 0)
        n_badge = (f'<span class="text-xs bg-indigo-50 text-indigo-600 rounded px-2 py-0.5">{n}</span>'
                   if n else '<span class="text-xs text-gray-300">—</span>')
        seg_badges = "".join(
            f'<span class="inline-block px-1.5 py-0.5 rounded text-xs mr-1 mb-1" '
            f'style="background:{SEGMENT_META.get(s,{}).get("color",DEFAULT_COLOR)}1a;'
            f'color:{SEGMENT_META.get(s,{}).get("color",DEFAULT_COLOR)}">'
            f'{SEGMENT_META.get(s,{}).get("label",s)}</span>'
            for s in c.get("segments", ["general"])
        )
        rows += f"""<tr class="border-b hover:bg-gray-50">
          <td class="py-2 px-3 font-medium text-sm" style="color:{color_for(nom)}">{nom}</td>
          <td class="py-2 px-3 text-center">{n_badge}</td>
          <td class="py-2 px-3 text-xs text-gray-400">{', '.join(c.get('aliases', [])[:2])}</td>
          <td class="py-2 px-3">{seg_badges}</td>
        </tr>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Concurrents surveillés</h1>
    <p class="text-sm text-gray-500 mt-1">
      {len(competitors)} concurrents répartis sur {len(by_seg)} segments thérapeutiques.
      Le chiffre à côté de chaque nom = nombre de signaux collectés. Cliquez sur un nom pour filtrer ses signaux.
    </p>
  </div>

  <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">Par segment thérapeutique</h2>
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-10">{cards_html}</div>

  <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">Liste complète</h2>
  <div class="bg-white rounded-xl shadow-sm border overflow-hidden">
    <table class="w-full text-left">
      <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
        <tr>
          <th class="py-2 px-3">Concurrent</th>
          <th class="py-2 px-3 text-center">Signaux</th>
          <th class="py-2 px-3">Aliases</th>
          <th class="py-2 px-3">Segments</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
"""
    return layout("concurrents", "Concurrents", body)


# ── Page Afrique : couverture des 54 pays ─────────────────────────────────────

# (iso, nom, membre_UEMOA). Les 54 pays du continent : aucun n'est omis.
AFRICA_COUNTRIES = [
    ("DZ", "Algérie", False), ("AO", "Angola", False), ("BJ", "Bénin", True),
    ("BW", "Botswana", False), ("BF", "Burkina Faso", True), ("BI", "Burundi", False),
    ("CM", "Cameroun", False), ("CV", "Cap-Vert", False), ("CF", "Centrafrique", False),
    ("TD", "Tchad", False), ("KM", "Comores", False), ("CG", "Congo", False),
    ("CD", "RD Congo", False), ("CI", "Côte d'Ivoire", True), ("DJ", "Djibouti", False),
    ("EG", "Égypte", False), ("GQ", "Guinée équatoriale", False), ("ER", "Érythrée", False),
    ("SZ", "Eswatini", False), ("ET", "Éthiopie", False), ("GA", "Gabon", False),
    ("GM", "Gambie", False), ("GH", "Ghana", False), ("GN", "Guinée", False),
    ("GW", "Guinée-Bissau", True), ("KE", "Kenya", False), ("LS", "Lesotho", False),
    ("LR", "Libéria", False), ("LY", "Libye", False), ("MG", "Madagascar", False),
    ("MW", "Malawi", False), ("ML", "Mali", True), ("MR", "Mauritanie", False),
    ("MU", "Maurice", False), ("MA", "Maroc", False), ("MZ", "Mozambique", False),
    ("NA", "Namibie", False), ("NE", "Niger", True), ("NG", "Nigeria", False),
    ("UG", "Ouganda", False), ("RW", "Rwanda", False), ("ST", "Sao Tomé-et-Principe", False),
    ("SN", "Sénégal", True), ("SC", "Seychelles", False), ("SL", "Sierra Leone", False),
    ("SO", "Somalie", False), ("SD", "Soudan", False), ("SS", "Soudan du Sud", False),
    ("ZA", "Afrique du Sud", False), ("TZ", "Tanzanie", False), ("TG", "Togo", True),
    ("TN", "Tunisie", False), ("ZM", "Zambie", False), ("ZW", "Zimbabwe", False),
]

# Libellé de la source de registre national par ISO (pour l'affichage).
_REGISTRY_BY_ISO = {
    "MA": "ONSSA", "NG": "NAFDAC", "RW": "Rwanda FDA", "UG": "NDA",
    "ZM": "ZAMRA", "ZW": "MCAZ",
}


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().replace("-", " ").replace("'", " ").strip()


def render_afrique(con) -> str:
    cur = con.cursor()
    cur.execute("SELECT pays, COUNT(*) FROM records WHERE pays IS NOT NULL GROUP BY pays")
    counts = {p: n for p, n in cur.fetchall()}
    urls = official_urls_by_pays(con)
    # Index normalisé des clés DB (pour rattacher les noms inovet « Senegal », etc.).
    norm_index: dict[str, int] = {}
    for p, n in counts.items():
        norm_index[_norm(p)] = norm_index.get(_norm(p), 0) + n

    n_registre = n_regional = n_indirect = n_aucun = 0
    rows = ""
    for iso, nom, uemoa in sorted(AFRICA_COUNTRIES, key=lambda x: x[1]):
        amm = counts.get(iso, 0)                       # registres (codés ISO)
        indirect = norm_index.get(_norm(nom), 0)       # présence inovet (noms)
        if iso in _REGISTRY_BY_ISO and amm:
            statut, couleur, detail = "Registre national", "#10B981", f"{_REGISTRY_BY_ISO[iso]} · {amm} AMM"
            n_registre += 1
        elif uemoa:
            extra = f" · {amm} AMM régionales" if amm else " (membre)"
            statut, couleur, detail = "AMM régionale UEMOA", "#3B82F6", f"UEMOA{extra}"
            n_regional += 1
        elif indirect:
            statut, couleur, detail = "Couverture indirecte", "#F59E0B", f"Présence produits · {indirect} signaux"
            n_indirect += 1
        else:
            statut, couleur, detail = "Aucune donnée publique", "#9CA3AF", "Pas de registre en ligne"
            n_aucun += 1
        total = amm + (indirect if not amm else 0)
        # Lien « voir les AMM » dans le dashboard (filtre pays) + lien registre officiel.
        nom_html = (f'<a href="/signaux?pays={iso}" class="text-indigo-600 hover:underline">{nom}</a>'
                    if total else f'<span class="text-gray-400">{nom}</span>')
        off = urls.get(iso, "")
        off_html = (f' · <a href="{off}" target="_blank" rel="noopener" class="text-indigo-500 hover:underline">registre officiel ↗</a>'
                    if off else "")
        rows += f"""<tr class="border-b hover:bg-gray-50">
          <td class="py-2 px-3 text-xs text-gray-400 font-mono">{iso}</td>
          <td class="py-2 px-3 text-sm font-medium">{nom_html}</td>
          <td class="py-2 px-3"><span class="text-xs px-2 py-0.5 rounded-full text-white" style="background:{couleur}">{statut}</span></td>
          <td class="py-2 px-3 text-xs text-gray-500">{detail}{off_html}</td>
          <td class="py-2 px-3 text-right text-xs font-semibold text-gray-700">{total or '—'}</td>
        </tr>"""

    cartes = [
        ("Registres nationaux", n_registre, "#10B981", "AMM officielles collectées"),
        ("AMM régionale UEMOA", n_regional, "#3B82F6", "8 pays d'Afrique de l'Ouest"),
        ("Couverture indirecte", n_indirect, "#F59E0B", "Présence produits concurrents"),
        ("Sans donnée publique", n_aucun, "#9CA3AF", "Aucun registre en ligne"),
    ]
    cartes_html = ""
    for label, val, col, desc in cartes:
        cartes_html += f"""
        <div class="bg-white rounded-xl shadow-sm p-5 border-l-4" style="border-color:{col}">
          <p class="text-3xl font-bold" style="color:{col}">{val}</p>
          <p class="text-sm font-medium text-gray-700 mt-1">{label}</p>
          <p class="text-xs text-gray-400 mt-1">{desc}</p>
        </div>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Couverture Afrique — 54 pays</h1>
    <p class="text-sm text-gray-500 mt-1">
      Chaque pays du continent est pris en compte. Statut = meilleure source disponible.
      « Aucune donnée publique » = ce pays ne publie pas de registre AMM en ligne (donnée inexistante,
      pas une omission).
    </p>
  </div>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">{cartes_html}</div>
  <div class="bg-white rounded-xl shadow-sm border overflow-hidden">
    <table class="w-full text-left">
      <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
        <tr>
          <th class="py-2 px-3">ISO</th><th class="py-2 px-3">Pays</th>
          <th class="py-2 px-3">Statut</th><th class="py-2 px-3">Source</th>
          <th class="py-2 px-3 text-right">Signaux</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
"""
    return layout("afrique", "Afrique", body)


# ── Page Asie & Moyen-Orient : couverture ─────────────────────────────────────

# (iso, nom). Pays d'Asie et du Moyen-Orient + océan Indien (Maurice).
ASIE_MO_COUNTRIES = [
    # Asie
    ("IN", "Inde"), ("VN", "Vietnam"), ("BD", "Bangladesh"), ("CN", "Chine"),
    ("PK", "Pakistan"), ("ID", "Indonésie"), ("PH", "Philippines"), ("TH", "Thaïlande"),
    ("MY", "Malaisie"), ("NP", "Népal"), ("LK", "Sri Lanka"), ("KH", "Cambodge"),
    ("MM", "Birmanie"), ("LA", "Laos"), ("JP", "Japon"), ("KR", "Corée du Sud"),
    ("TW", "Taïwan"), ("MN", "Mongolie"), ("SG", "Singapour"),
    # Moyen-Orient
    ("SA", "Arabie saoudite"), ("AE", "Émirats arabes unis"), ("KW", "Koweït"),
    ("QA", "Qatar"), ("OM", "Oman"), ("BH", "Bahreïn"), ("IQ", "Irak"),
    ("IR", "Iran"), ("JO", "Jordanie"), ("YE", "Yémen"), ("IL", "Israël"),
    ("LB", "Liban"), ("SY", "Syrie"), ("TR", "Turquie"),
    # Océan Indien
    ("MU", "Maurice"),
]

# Registres intégrés pour cette zone (ISO -> libellé source).
_REGISTRY_ASIE = {
    "IN": "CDSCO", "VN": "Cục Thú y", "BD": "BNVF", "CN": "IVDC (annonces)",
}


def render_asie_mo(con) -> str:
    cur = con.cursor()
    cur.execute("SELECT pays, COUNT(*) FROM records WHERE pays IS NOT NULL GROUP BY pays")
    counts = {p: n for p, n in cur.fetchall()}
    urls = official_urls_by_pays(con)
    norm_index: dict[str, int] = {}
    for p, n in counts.items():
        k = _norm(p)
        norm_index[k] = norm_index.get(k, 0) + n

    n_reg = n_indirect = n_aucun = 0
    rows = ""
    for iso, nom in sorted(ASIE_MO_COUNTRIES, key=lambda x: x[1]):
        amm = counts.get(iso, 0)
        indirect = norm_index.get(_norm(nom), 0)
        if iso in _REGISTRY_ASIE and (amm or iso == "CN"):
            statut, couleur, detail = "Registre national", "#10B981", f"{_REGISTRY_ASIE[iso]} · {amm} signaux"
            n_reg += 1
            total = amm
        elif indirect:
            statut, couleur, detail = "Couverture indirecte", "#F59E0B", f"Présence produits · {indirect} signaux"
            n_indirect += 1
            total = indirect
        else:
            statut, couleur, detail = "Aucune donnée publique", "#9CA3AF", "Pas de registre en ligne accessible"
            n_aucun += 1
            total = 0
        nom_html = (f'<a href="/signaux?pays={iso}" class="text-indigo-600 hover:underline">{nom}</a>'
                    if total else f'<span class="text-gray-400">{nom}</span>')
        off = urls.get(iso, "")
        off_html = (f' · <a href="{off}" target="_blank" rel="noopener" class="text-indigo-500 hover:underline">registre officiel ↗</a>'
                    if off else "")
        rows += f"""<tr class="border-b hover:bg-gray-50">
          <td class="py-2 px-3 text-xs text-gray-400 font-mono">{iso}</td>
          <td class="py-2 px-3 text-sm font-medium">{nom_html}</td>
          <td class="py-2 px-3"><span class="text-xs px-2 py-0.5 rounded-full text-white" style="background:{couleur}">{statut}</span></td>
          <td class="py-2 px-3 text-xs text-gray-500">{detail}{off_html}</td>
          <td class="py-2 px-3 text-right text-xs font-semibold text-gray-700">{total or '—'}</td>
        </tr>"""

    cartes = [
        ("Registres nationaux", n_reg, "#10B981", "AMM officielles collectées"),
        ("Couverture indirecte", n_indirect, "#F59E0B", "Présence produits concurrents"),
        ("Sans donnée publique", n_aucun, "#9CA3AF", "Aucun registre accessible"),
    ]
    cartes_html = ""
    for label, val, col, desc in cartes:
        cartes_html += f"""
        <div class="bg-white rounded-xl shadow-sm p-5 border-l-4" style="border-color:{col}">
          <p class="text-3xl font-bold" style="color:{col}">{val}</p>
          <p class="text-sm font-medium text-gray-700 mt-1">{label}</p>
          <p class="text-xs text-gray-400 mt-1">{desc}</p>
        </div>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Couverture Asie & Moyen-Orient</h1>
    <p class="text-sm text-gray-500 mt-1">
      {len(ASIE_MO_COUNTRIES)} pays pris en compte. « Aucune donnée publique » = pas de registre
      AMM accessible en ligne (donnée inexistante ou serveur géo-bloqué), pas une omission.
    </p>
  </div>
  <div class="grid grid-cols-3 gap-4 mb-8">{cartes_html}</div>
  <div class="bg-white rounded-xl shadow-sm border overflow-hidden">
    <table class="w-full text-left">
      <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
        <tr>
          <th class="py-2 px-3">ISO</th><th class="py-2 px-3">Pays</th>
          <th class="py-2 px-3">Statut</th><th class="py-2 px-3">Source</th>
          <th class="py-2 px-3 text-right">Signaux</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
"""
    return layout("asie", "Asie & Moyen-Orient", body)


# ── Page AMM : toutes les AMM par pays (vue simple) ───────────────────────────

# Nom lisible + drapeau pour chaque code pays connu.
_ISO_NOM = {iso: nom for iso, nom, *_ in AFRICA_COUNTRIES}
_ISO_NOM.update({iso: nom for iso, nom in ASIE_MO_COUNTRIES})
_ISO_NOM.update({"FR": "France", "NL": "Pays-Bas", "US": "États-Unis", "ES": "Espagne",
                 "DE": "Allemagne", "GB": "Royaume-Uni", "IT": "Italie"})


def _flag(iso: str) -> str:
    if len(iso) != 2 or not iso.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in iso)


def render_amm(con, pays: str = "") -> str:
    if not pays:
        # Vue 1 : grille des pays, triée par nombre d'AMM.
        cards = ""
        total = 0
        for iso, n in amm_counts_by_pays(con):
            total += n
            nom = _ISO_NOM.get(iso, iso)
            cards += f"""
            <a href="/amm?pays={iso}" class="bg-white rounded-xl shadow-sm border p-4 hover:shadow-md hover:border-indigo-300 transition flex items-center justify-between">
              <span class="flex items-center gap-3">
                <span class="text-2xl">{_flag(iso)}</span>
                <span class="font-medium text-gray-800">{nom}</span>
              </span>
              <span class="text-lg font-bold text-indigo-600">{n}</span>
            </a>"""
        body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">AMM par pays</h1>
    <p class="text-sm text-gray-500 mt-1">
      {total} AMM officielles collectées. Cliquez sur un pays pour voir <b>toutes</b> ses AMM.
    </p>
  </div>
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">{cards}</div>
"""
        return layout("amm", "AMM", body)

    # Vue 2 : toutes les AMM du pays choisi.
    rows = amm_for_pays(con, pays)
    official = official_urls_by_pays(con).get(pays, "")
    nom = _ISO_NOM.get(pays, pays)
    trs = ""
    for r in rows:
        conc = r["concurrent"] or "—"
        color = color_for(r["concurrent"])
        produit = r["produit"] or "—"
        mols = ", ".join(json.loads(r["molecules"] or "[]"))
        date = date_affichee(r)
        src = SOURCE_LABELS.get(r["source"], r["source"])
        url = r["url"] or ""
        try:
            extra = json.loads(r["extra"] or "{}")
        except (ValueError, TypeError):
            extra = {}
        numero_amm = extra.get("numero_amm") or extra.get("reg_no") or ""
        # Données portées par la ligne → la fiche détail s'ouvre SANS quitter la page.
        attrs = (f'data-produit="{_html.escape(produit, quote=True)}" '
                 f'data-mols="{_html.escape(mols, quote=True)}" '
                 f'data-conc="{_html.escape(conc, quote=True)}" '
                 f'data-numamm="{_html.escape(numero_amm, quote=True)}" '
                 f'data-date="{date or "—"}" data-src="{_html.escape(src, quote=True)}" '
                 f'data-url="{_html.escape(url, quote=True)}"')
        trs += f"""<tr class="amm-row border-b hover:bg-indigo-50 cursor-pointer" {attrs}>
          <td class="px-3 py-2 text-sm font-medium">{produit}</td>
          <td class="px-3 py-2 text-xs text-gray-500">{mols}</td>
          <td class="px-3 py-2 text-xs font-semibold" style="color:{color}">{conc}</td>
          <td class="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{date}</td>
          <td class="px-3 py-2 text-xs text-gray-400">{src}</td>
          <td class="px-3 py-2 text-indigo-400 text-xs">détail ›</td>
        </tr>"""
    if not trs:
        trs = '<tr><td colspan="6" class="px-3 py-6 text-center text-sm text-gray-400">Aucune AMM pour ce pays.</td></tr>'

    off_btn = (f'<a href="{official}" target="_blank" rel="noopener" class="text-sm bg-indigo-600 text-white rounded-lg px-4 py-2 hover:bg-indigo-700">Ouvrir le registre officiel ↗</a>'
               if official else "")
    body = f"""
  <div class="mb-5 flex items-center justify-between flex-wrap gap-3">
    <div>
      <a href="/amm" class="text-xs text-gray-400 hover:text-gray-600">← tous les pays</a>
      <h1 class="text-2xl font-bold text-gray-900 mt-1">{_flag(pays)} {nom} — <span id="amm-count">{len(rows)}</span> AMM</h1>
    </div>
    {off_btn}
  </div>

  <div class="mb-4">
    <input id="amm-search" type="search" autofocus autocomplete="off"
      placeholder="🔍 Rechercher un produit, une molécule, un concurrent…"
      class="w-full border rounded-xl px-4 py-3 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-400">
    <p class="text-xs text-gray-400 mt-1">Tapez pour filtrer instantanément · <span id="amm-shown">{len(rows)}</span> résultat(s)</p>
  </div>

  <div class="bg-white rounded-xl shadow-sm border overflow-hidden">
    <table class="w-full text-left">
      <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
        <tr>
          <th class="px-3 py-2">Produit</th><th class="px-3 py-2">Molécules</th>
          <th class="px-3 py-2">Titulaire</th><th class="px-3 py-2">Date AMM</th>
          <th class="px-3 py-2">Source</th><th class="px-3 py-2"></th>
        </tr>
      </thead>
      <tbody id="amm-tbody">{trs}</tbody>
    </table>
  </div>

  <!-- Fiche détail (cachée par défaut) -->
  <div id="amm-modal" class="fixed inset-0 bg-black/40 hidden items-center justify-center z-50 p-4">
    <div class="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 relative">
      <button id="amm-close" class="absolute top-3 right-4 text-gray-400 hover:text-gray-700 text-xl">×</button>
      <p class="text-xs text-gray-400 mb-1">AMM · {_flag(pays)} {nom}</p>
      <h2 id="m-produit" class="text-xl font-bold text-gray-900 mb-1"></h2>
      <p id="m-numamm-wrap" class="mb-4">
        <span class="text-xs text-gray-400">N° AMM (vérifiable sur le registre officiel) </span>
        <code id="m-numamm" class="text-xs bg-gray-100 rounded px-1.5 py-0.5 font-mono text-gray-700"></code>
      </p>
      <dl class="space-y-2 text-sm">
        <div class="flex"><dt class="w-32 text-gray-400">Molécules</dt><dd id="m-mols" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Titulaire</dt><dd id="m-conc" class="flex-1 font-semibold"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Date AMM</dt><dd id="m-date" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Source</dt><dd id="m-src" class="flex-1 text-gray-800"></dd></div>
      </dl>
      <a id="m-url" href="#" target="_blank" rel="noopener"
         class="mt-5 inline-block text-xs text-gray-400 hover:text-indigo-600">
        Voir le document officiel complet ↗ <span class="text-gray-300">(tout le registre)</span>
      </a>
    </div>
  </div>
"""
    script = """<script>
(function(){
  const inp = document.getElementById('amm-search');
  const rows = Array.from(document.querySelectorAll('#amm-tbody tr'));
  const shown = document.getElementById('amm-shown');
  inp.addEventListener('input', function(){
    const q = inp.value.trim().toLowerCase();
    let n = 0;
    for (const r of rows){
      const hit = !q || r.textContent.toLowerCase().includes(q);
      r.style.display = hit ? '' : 'none';
      if (hit) n++;
    }
    shown.textContent = n;
  });
  // Fiche détail au clic sur une ligne
  const modal = document.getElementById('amm-modal');
  const show = (k) => document.getElementById('m-'+k);
  function open(r){
    show('produit').textContent = r.dataset.produit;
    show('mols').textContent = r.dataset.mols || '—';
    show('conc').textContent = r.dataset.conc;
    show('date').textContent = r.dataset.date;
    show('src').textContent = r.dataset.src;
    const numamm = document.getElementById('m-numamm-wrap');
    if (r.dataset.numamm){ show('numamm').textContent = r.dataset.numamm; numamm.style.display=''; }
    else { numamm.style.display='none'; }
    const u = document.getElementById('m-url');
    if (r.dataset.url){ u.href = r.dataset.url; u.style.display=''; } else { u.style.display='none'; }
    modal.classList.remove('hidden'); modal.classList.add('flex');
  }
  function close(){ modal.classList.add('hidden'); modal.classList.remove('flex'); }
  rows.forEach(r => r.addEventListener('click', () => open(r)));
  document.getElementById('amm-close').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
})();
</script>"""
    return layout("amm", f"AMM {nom}", body, extra_script=script)


# ── Page Infos : définitions + sources ────────────────────────────────────────

GLOSSAIRE = [
    ("AMM", "Autorisation de Mise sur le Marché : l'agrément officiel d'un pays "
            "qui permet de vendre un médicament vétérinaire sur son territoire. "
            "C'est le cœur de la veille : qui a le droit de vendre quoi, et où."),
    ("Date « ≈ »", "Quand un registre n'indique pas la date d'octroi de l'AMM "
                   "(fréquent dans les PDF officiels), on affiche la date de première "
                   "collecte par la veille, précédée du signe ≈."),
    ("Titulaire", "L'entreprise qui détient l'AMM (le laboratoire qui commercialise "
                  "le produit). C'est sur ce nom qu'on reconnaît un concurrent."),
    ("Molécule / substance active", "Le principe actif du médicament (ex. ivermectine, "
                                    "amoxicilline). Permet de comparer les produits au-delà du nom commercial."),
    ("Signal", "Une information collectée automatiquement : une AMM, un nouveau produit, "
               "une actualité, une offre d'emploi… Chaque ligne du dashboard est un signal."),
    ("Registre national", "La base officielle d'un pays listant ses AMM vétérinaires "
                          "(ex. ONSSA au Maroc). Source la plus fiable."),
    ("AMM régionale (UEMOA)", "Une seule autorisation valable dans 8 pays d'Afrique de "
                              "l'Ouest à la fois (Sénégal, Mali, Côte d'Ivoire…)."),
    ("Couverture indirecte", "Pour les pays sans registre public, on déduit la présence "
                             "d'un concurrent via ses catalogues / annonces, faute de base officielle."),
    ("Concurrent", "Un laboratoire suivi par Lobs. Le système le reconnaît automatiquement "
                   "dans les sources grâce à ses noms et variantes (aliases)."),
]

# (clé source, drapeau, autorité, type, ce que ça couvre)
SOURCE_INFO = [
    ("anses_anmv", "🇫🇷", "ANSES / ANMV", "Open data (XML)", "Toutes les AMM vétérinaires françaises."),
    ("onssa_maroc", "🇲🇦", "ONSSA", "PDF officiel", "Liste positive des médicaments vétérinaires du Maroc."),
    ("nafdac_nigeria", "🇳🇬", "NAFDAC", "PDF officiel (historique)", "Produits de santé animale enregistrés au Nigeria — liste 2016-2018."),
    ("nafdac_greenbook", "🇳🇬", "NAFDAC Greenbook", "API publique officielle", "Base en ligne activement maintenue de NAFDAC — AMM plus récentes (jusqu'à fin 2024)."),
    ("cdsco_inde", "🇮🇳", "CDSCO", "PDF officiels", "Autorisations vétérinaires (Form-45/46) en Inde."),
    ("cucthuy_vietnam", "🇻🇳", "Cục Thú y", "Excel officiel", "Médicaments vét autorisés au Vietnam (fabriqués / importés / aquaculture)."),
    ("bnvf_bangladesh", "🇧🇩", "DGDA", "PDF (formulaire national)", "Produits vét enregistrés au Bangladesh (extraits du formulaire)."),
    ("vmd_kenya", "🇰🇪", "Veterinary Medicines Directorate", "Registre HTML public", "Produits vét enregistrés au Kenya (pharmaceutiques, biologiques, additifs alimentaires)."),
    ("zamra_zambie", "🇿🇲", "ZAMRA", "API publique", "Registre des médicaments vétérinaires de Zambie."),
    ("mcaz_zimbabwe", "🇿🇼", "MCAZ", "Registre en ligne", "Médicaments vétérinaires approuvés au Zimbabwe."),
    ("uemoa_siar", "🌍", "UEMOA / CRMV", "Portail régional", "AMM régionales valables dans 8 pays d'Afrique de l'Ouest."),
    ("pdf_registry", "🌍", "NDA / Rwanda FDA", "PDF officiels", "Registres nationaux Ouganda et Rwanda."),
    ("news_pages", "🇨🇳", "IVDC + presse", "Pages officielles", "Annonces d'approbations (Chine) + actus de concurrents."),
    ("inovet", "🌐", "Catalogue Inovet", "Présence produits", "Pays de présence des produits (couverture indirecte ~90 pays)."),
    ("rss", "🌐", "Flux officiels", "RSS / Atom", "Communiqués des concurrents (Ceva, MSD, Zoetis…)."),
    ("france_travail", "🇫🇷", "France Travail", "API officielle", "Offres d'emploi du secteur vétérinaire (signal d'expansion)."),
    ("space", "🇫🇷", "Salon SPACE", "Veille salon", "Exposants & dossiers de presse du salon SPACE Rennes."),
    ("kepro", "🇳🇱", "Kepro", "Sitemap", "Produits du concurrent Kepro."),
    ("careers", "🌐", "Pages carrières", "Sites concurrents", "Recrutements publiés directement par les concurrents."),
]


def render_aide(con) -> str:
    # Compte d'AMM réel par source pour montrer le volume.
    cur = con.cursor()
    cur.execute("SELECT source, COUNT(*) FROM records GROUP BY source")
    counts = {s: n for s, n in cur.fetchall()}

    glos = ""
    for terme, defi in GLOSSAIRE:
        glos += f"""
        <div class="bg-white rounded-xl shadow-sm border p-4">
          <p class="font-semibold text-indigo-700 text-sm">{terme}</p>
          <p class="text-sm text-gray-600 mt-1 leading-snug">{defi}</p>
        </div>"""

    src_rows = ""
    for key, flag, autorite, typ, desc in SOURCE_INFO:
        n = counts.get(key, 0)
        label = SOURCE_LABELS.get(key, key)
        src_rows += f"""<tr class="border-b hover:bg-gray-50">
          <td class="px-3 py-2 text-lg">{flag}</td>
          <td class="px-3 py-2 text-sm font-medium">{label}</td>
          <td class="px-3 py-2 text-xs text-gray-600">{autorite}</td>
          <td class="px-3 py-2 text-xs"><span class="bg-gray-100 rounded px-2 py-0.5">{typ}</span></td>
          <td class="px-3 py-2 text-xs text-gray-500">{desc}</td>
          <td class="px-3 py-2 text-right text-xs font-semibold text-gray-700">{n or '—'}</td>
        </tr>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Infos & définitions</h1>
    <p class="text-sm text-gray-500 mt-1">
      À quoi servent les termes du dashboard, et d'où viennent les données.
    </p>
  </div>

  <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">Définitions</h2>
  <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-10">{glos}</div>

  <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">Les sources de données</h2>
  <p class="text-sm text-gray-500 mb-3">
    Chaque source est un canal <b>officiel et public</b> (registre, API, flux). Aucune protection
    n'est contournée : si un site bloque l'accès automatisé, on s'en tient à son canal officiel.
  </p>
  <div class="bg-white rounded-xl shadow-sm border overflow-hidden">
    <table class="w-full text-left">
      <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
        <tr>
          <th class="px-3 py-2"></th><th class="px-3 py-2">Source</th>
          <th class="px-3 py-2">Autorité</th><th class="px-3 py-2">Type</th>
          <th class="px-3 py-2">Ce que ça couvre</th><th class="px-3 py-2 text-right">Signaux</th>
        </tr>
      </thead>
      <tbody>{src_rows}</tbody>
    </table>
  </div>
"""
    return layout("aide", "Infos", body)


# ── Page Signaux ──────────────────────────────────────────────────────────────

def render_signaux(records, concurrents, sources,
                   filter_concurrent="", filter_source="", filter_rtype="",
                   filter_pays="", official_url="") -> str:
    rows_html = ""
    for r in records:
        conc = r["concurrent"] or "—"
        color = color_for(r["concurrent"])
        rtype_lbl = RECORD_TYPE_LABELS.get(r["record_type"], r["record_type"])
        produit = (r["produit"] or "—")[:70]
        pays = r["pays"] or "—"
        date = date_affichee(r)
        src_lbl = SOURCE_LABELS.get(r["source"], r["source"])
        tags = ", ".join(json.loads(r["tags"] or "[]"))
        url = r["url"] or ""
        mols = ", ".join(json.loads(r["molecules"] or "[]"))
        try:
            extra = json.loads(r["extra"] or "{}")
        except (ValueError, TypeError):
            extra = {}
        titulaire = extra.get("titulaire") or ""
        registre = extra.get("registre") or ""
        numero_amm = extra.get("numero_amm") or extra.get("reg_no") or ""
        # La fiche détail s'ouvre au clic (SANS quitter la page) : les registres
        # n'ont pas d'URL par produit, donc « voir » = fiche + lien registre clair.
        attrs = (f'data-produit="{_html.escape(produit, quote=True)}" '
                 f'data-mols="{_html.escape(mols, quote=True)}" '
                 f'data-conc="{_html.escape(conc, quote=True)}" '
                 f'data-type="{_html.escape(rtype_lbl, quote=True)}" '
                 f'data-src="{_html.escape(src_lbl, quote=True)}" '
                 f'data-pays="{_html.escape(pays, quote=True)}" '
                 f'data-date="{date}" data-tit="{_html.escape(titulaire, quote=True)}" '
                 f'data-reg="{_html.escape(registre, quote=True)}" '
                 f'data-numamm="{_html.escape(numero_amm, quote=True)}" '
                 f'data-url="{_html.escape(url, quote=True)}"')
        rows_html += f"""
        <tr class="sig-row border-b hover:bg-indigo-50 cursor-pointer" {attrs}>
          <td class="px-3 py-2 text-xs font-semibold" style="color:{color}">{conc}</td>
          <td class="px-3 py-2 text-xs">{produit}</td>
          <td class="px-3 py-2"><span class="text-xs bg-gray-100 rounded px-1.5 py-0.5">{rtype_lbl}</span></td>
          <td class="px-3 py-2 text-xs text-gray-500">{src_lbl}</td>
          <td class="px-3 py-2 text-xs text-gray-500">{pays}</td>
          <td class="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{date}</td>
          <td class="px-3 py-2 text-xs text-gray-400">{tags}</td>
          <td class="px-3 py-2 whitespace-nowrap text-indigo-400 text-xs">détail ›</td>
        </tr>"""
    if not rows_html:
        rows_html = '<tr><td colspan="8" class="px-3 py-6 text-center text-sm text-gray-400">Aucun signal ne correspond à ces filtres.</td></tr>'

    conc_opts = "".join(
        f'<option value="{c}" {"selected" if c == filter_concurrent else ""}>{c}</option>'
        for c in concurrents)
    src_opts = "".join(
        f'<option value="{s}" {"selected" if s == filter_source else ""}>{SOURCE_LABELS.get(s,s)}</option>'
        for s in sources)
    rtype_opts = "".join(
        f'<option value="{k}" {"selected" if k == filter_rtype else ""}>{v}</option>'
        for k, v in RECORD_TYPE_LABELS.items())

    active_filter = filter_concurrent or filter_source or filter_rtype or filter_pays
    reset = ('<a href="/signaux" class="text-xs text-gray-400 hover:text-gray-600 px-2 py-1.5">Réinitialiser</a>'
             if active_filter else "")
    pays_field = (f'<input type="hidden" name="pays" value="{filter_pays}">' if filter_pays else "")

    # Bandeau « registre officiel » quand on filtre un pays dont on connaît la source.
    bandeau = ""
    if filter_pays:
        lien = (f'<a href="{official_url}" target="_blank" rel="noopener" '
                f'class="underline font-medium">ouvrir le registre officiel ↗</a>'
                if official_url else "source officielle non disponible")
        bandeau = f"""
  <div class="mb-4 bg-indigo-50 border border-indigo-200 rounded-xl px-4 py-3 text-sm text-indigo-800">
    AMM filtrées sur le pays <b>{filter_pays}</b> · {lien}
  </div>"""

    body = f"""
  <div class="mb-6">
    <h1 class="text-2xl font-bold text-gray-900">Signaux détectés</h1>
    <p class="text-sm text-gray-500 mt-1">
      Chaque ligne = une information collectée automatiquement (nouveau produit, AMM, actualité…).
      Utilisez les filtres pour cibler un concurrent, une source, un type ou un pays.
    </p>
  </div>
  {bandeau}

  <div class="bg-white rounded-xl shadow-sm border">
    <div class="px-5 py-4 border-b">
      <form method="GET" action="/signaux" class="flex flex-wrap gap-2 items-center">
        <select name="concurrent" class="text-sm border rounded-lg px-3 py-1.5 bg-white">
          <option value="">Tous les concurrents</option>{conc_opts}
        </select>
        <select name="source" class="text-sm border rounded-lg px-3 py-1.5 bg-white">
          <option value="">Toutes les sources</option>{src_opts}
        </select>
        <select name="rtype" class="text-sm border rounded-lg px-3 py-1.5 bg-white">
          <option value="">Tous les types</option>{rtype_opts}
        </select>
        {pays_field}
        <button type="submit" class="text-sm bg-indigo-600 text-white rounded-lg px-4 py-1.5 hover:bg-indigo-700">Filtrer</button>
        {reset}
      </form>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
          <tr>
            <th class="px-3 py-2 text-left">Concurrent</th>
            <th class="px-3 py-2 text-left">Signal / Produit</th>
            <th class="px-3 py-2 text-left">Type</th>
            <th class="px-3 py-2 text-left">Source</th>
            <th class="px-3 py-2 text-left">Pays</th>
            <th class="px-3 py-2 text-left">Date</th>
            <th class="px-3 py-2 text-left">Tags</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div class="px-5 py-3 border-t text-xs text-gray-400">{len(records)} résultat(s) affiché(s) · max 200</div>
  </div>

  <!-- Fiche détail du signal (cachée par défaut) -->
  <div id="sig-modal" class="fixed inset-0 bg-black/40 hidden items-center justify-center z-50 p-4">
    <div class="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 relative">
      <button id="sig-close" class="absolute top-3 right-4 text-gray-400 hover:text-gray-700 text-xl">×</button>
      <p class="text-xs text-gray-400 mb-1"><span id="s-type"></span> · <span id="s-pays"></span></p>
      <h2 id="s-produit" class="text-xl font-bold text-gray-900 mb-4"></h2>
      <dl class="space-y-2 text-sm">
        <div class="flex"><dt class="w-32 text-gray-400">N° AMM</dt><dd id="s-numamm" class="flex-1 text-gray-800 font-mono text-xs"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Concurrent</dt><dd id="s-conc" class="flex-1 font-semibold"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Molécules</dt><dd id="s-mols" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Titulaire</dt><dd id="s-tit" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Date</dt><dd id="s-date" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Source</dt><dd id="s-src" class="flex-1 text-gray-800"></dd></div>
        <div class="flex"><dt class="w-32 text-gray-400">Registre</dt><dd id="s-reg" class="flex-1 text-gray-800"></dd></div>
      </dl>
      <a id="s-url" href="#" target="_blank" rel="noopener"
         class="mt-5 block text-center text-sm bg-indigo-600 text-white rounded-lg px-4 py-2.5 hover:bg-indigo-700 font-medium">
        Copier le nom + ouvrir le portail ↗
      </a>
      <p id="s-hint" class="text-xs text-gray-400 mt-2 text-center">Le nom du produit sera copié : collez-le (⌘V) dans la recherche du portail.</p>
    </div>
  </div>
"""
    script = """<script>
(function(){
  const modal = document.getElementById('sig-modal');
  if (!modal) return;
  const rows = Array.from(document.querySelectorAll('.sig-row'));
  const set = (k, v) => { const e = document.getElementById('s-'+k); if (e) e.textContent = v || '—'; };
  function open(r){
    set('produit', r.dataset.produit);
    set('type', r.dataset.type);
    set('pays', r.dataset.pays);
    set('conc', r.dataset.conc);
    set('mols', r.dataset.mols);
    set('tit', r.dataset.tit);
    set('date', r.dataset.date);
    set('src', r.dataset.src);
    set('reg', r.dataset.reg);
    set('numamm', r.dataset.numamm);
    const u = document.getElementById('s-url');
    const hint = document.getElementById('s-hint');
    if (r.dataset.url){
      u.href = r.dataset.url; u.style.display='';
      if (hint) hint.style.display='';
      // Copie le N° AMM si on l'a (clé de recherche précise), sinon le nom du produit.
      u.dataset.produit = r.dataset.numamm || r.dataset.produit || '';
    } else {
      u.style.display='none';
      if (hint) hint.style.display='none';
    }
    modal.classList.remove('hidden'); modal.classList.add('flex');
  }
  // Copie le nom du produit dans le presse-papier au moment d'ouvrir le portail.
  const openBtn = document.getElementById('s-url');
  if (openBtn){
    openBtn.addEventListener('click', function(){
      const nom = openBtn.dataset.produit || '';
      if (nom && navigator.clipboard){ navigator.clipboard.writeText(nom).catch(()=>{}); }
      const hint = document.getElementById('s-hint');
      if (hint && nom){ hint.textContent = '« ' + nom +' » copié — collez-le (⌘V) dans la recherche du portail.'; }
    });
  }
  function close(){ modal.classList.add('hidden'); modal.classList.remove('flex'); }
  rows.forEach(r => r.addEventListener('click', () => open(r)));
  document.getElementById('sig-close').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
})();
</script>"""
    return layout("signaux", "Signaux", body, extra_script=script)


# ── Serveur HTTP ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silencieux

    def _authorized(self) -> bool:
        """True si l'accès est autorisé. Sans DASH_PASSWORD (local) : toujours vrai.
        Sinon exige un Basic Auth correct (comparaison à temps constant)."""
        if not DASH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (hmac.compare_digest(user, DASH_USER)
                and hmac.compare_digest(pwd, DASH_PASSWORD))

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Veille Lobs"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h1>401 — authentification requise</h1>".encode("utf-8"))

    def _send(self, body: str, status: int = 200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path

        # Sonde de santé pour l'hébergeur — répond sans authentification.
        if path == "/healthz":
            self._send("ok")
            return

        if not self._authorized():
            self._require_auth()
            return

        # Redirections de compatibilité (anciennes URL)
        if path in ("/records",):
            path = "/signaux"
        if path in ("/competitors",):
            path = "/concurrents"

        con = get_db()
        try:
            if path == "/":
                html = render_accueil(
                    stats(con), by_concurrent(con), by_source(con), recent_history(con))
            elif path == "/concurrents":
                html = render_concurrents(by_concurrent(con))
            elif path == "/amm":
                html = render_amm(con, qs.get("pays", [""])[0])
            elif path == "/aide":
                html = render_aide(con)
            elif path == "/afrique":
                html = render_afrique(con)
            elif path == "/asie":
                html = render_asie_mo(con)
            elif path == "/signaux":
                fc = qs.get("concurrent", [""])[0]
                fs = qs.get("source", [""])[0]
                fr = qs.get("rtype", [""])[0]
                fp = qs.get("pays", [""])[0]
                recs = records_filtered(con, fc or None, fs or None, fr or None, fp or None)
                off = official_urls_by_pays(con).get(fp, "") if fp else ""
                html = render_signaux(recs, all_concurrents(con), all_sources(con),
                                      fc, fs, fr, fp, off)
            else:
                self._send("<h1>404 — page introuvable</h1>", 404)
                return
            self._send(html)
        finally:
            con.close()


def main():
    # En cloud, l'hébergeur impose le port via $PORT et il faut écouter sur
    # 0.0.0.0 (toutes interfaces). En local, on garde localhost + navigateur.
    env_port = os.environ.get("PORT")
    default_host = "0.0.0.0" if env_port else "localhost"

    parser = argparse.ArgumentParser(description="Dashboard veille Lobs")
    parser.add_argument("--port", type=int, default=int(env_port or 8765))
    parser.add_argument("--host", default=os.environ.get("HOST", default_host))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    is_cloud = bool(env_port) or args.host == "0.0.0.0"
    shown_host = "localhost" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{shown_host}:{args.port}"
    print(f"  Dashboard Lobs → {url}")
    if DASH_PASSWORD:
        print(f"  Authentification ACTIVE (utilisateur : {DASH_USER})")
    else:
        print("  Authentification désactivée (DASH_PASSWORD non défini)")
    print("  Ctrl+C pour arrêter\n")

    if not args.no_browser and not is_cloud:
        webbrowser.open(url)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno == 48:  # Address already in use
            print(f"  Le dashboard tourne déjà sur le port {args.port} → {url}")
            print(f"  Pour le relancer : lsof -ti:{args.port} | xargs kill")
            return
        raise
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Arrêt du serveur.")


if __name__ == "__main__":
    main()
