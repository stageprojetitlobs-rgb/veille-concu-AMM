"""Génère une version STATIQUE du dashboard (fichiers HTML) pour GitHub Pages.

Le dashboard normal est un serveur : chaque page est produite à la volée. Ici on
appelle les mêmes fonctions de rendu, on écrit le résultat dans des fichiers .html,
et on réécrit les liens internes (`/amm?pays=ZM` → `amm-ZM.html`, etc.) pour qu'ils
marchent sans serveur.

La recherche et les filtres du dashboard tournent déjà en JavaScript côté
navigateur : ils continuent de fonctionner en statique.

Usage :
    python -m scripts.export_static           # écrit dans ./site
    python -m scripts.export_static --out dist
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from scripts import dashboard as d


# Filtres de la page Signaux : côté serveur en local, côté navigateur en statique.
# Les lignes .sig-row portent déjà data-conc / data-src / data-type → on filtre en JS.
_SIGNAUX_FILTER_JS = """<script>
(function(){
  const form = document.querySelector('form[action="signaux.html"]');
  if (!form) return;
  const rows = Array.from(document.querySelectorAll('.sig-row'));
  const selC = form.querySelector('select[name="concurrent"]');
  const selS = form.querySelector('select[name="source"]');
  const selT = form.querySelector('select[name="rtype"]');
  const cnt = Array.from(document.querySelectorAll('div'))
    .find(d => d.children.length === 0 && d.textContent.includes('résultat(s) affiché(s)'));
  function apply(e){
    if (e) e.preventDefault();
    const c = selC.value;
    const s = selS.value ? selS.options[selS.selectedIndex].text : '';
    const t = selT.value ? selT.options[selT.selectedIndex].text : '';
    let n = 0;
    for (const r of rows){
      const ok = (!c || r.dataset.conc === c)
              && (!s || r.dataset.src === s)
              && (!t || r.dataset.type === t);
      r.style.display = ok ? '' : 'none';
      if (ok) n++;
    }
    if (cnt) cnt.textContent = n + ' résultat(s) affiché(s)';
  }
  form.addEventListener('submit', apply);
  [selC, selS, selT].forEach(sel => sel.addEventListener('change', () => apply()));
})();
</script>"""


def _rewrite_links(html: str) -> str:
    """Réécrit les liens absolus du serveur en chemins de fichiers statiques."""
    # AMM par pays : /amm?pays=XX → amm-XX.html
    html = re.sub(r'(href=")/amm\?pays=([A-Za-z]{2})(")',
                  lambda m: f'{m.group(1)}amm-{m.group(2)}.html{m.group(3)}', html)
    # Signaux filtrés (filtres serveur inopérants en statique) → page complète.
    html = re.sub(r'(href=")/signaux\?[^"]*(")',
                  lambda m: f'{m.group(1)}signaux.html{m.group(2)}', html)
    # Formulaire de filtres signaux → neutralisé (pointe vers la page complète).
    html = html.replace('action="/signaux"', 'action="signaux.html"')
    # Pages simples : /xxx → xxx.html, et / → index.html
    for path, file in (
        ("/amm", "amm.html"), ("/concurrents", "concurrents.html"),
        ("/afrique", "afrique.html"), ("/asie", "asie.html"),
        ("/signaux", "signaux.html"), ("/aide", "aide.html"),
    ):
        html = html.replace(f'href="{path}"', f'href="{file}"')
    html = html.replace('href="/"', 'href="index.html"')
    return html


def _write(out: Path, name: str, html: str) -> None:
    (out / name).write_text(_rewrite_links(html), encoding="utf-8")


def build(out_dir: str = "site") -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = d.get_db()
    n = 0
    try:
        # Pages principales (mêmes appels que le routeur do_GET).
        _write(out, "index.html", d.render_accueil(
            d.stats(con), d.by_concurrent(con), d.by_source(con), d.recent_history(con)))
        _write(out, "concurrents.html", d.render_concurrents(d.by_concurrent(con)))
        _write(out, "afrique.html", d.render_afrique(con))
        _write(out, "asie.html", d.render_asie_mo(con))
        _write(out, "aide.html", d.render_aide(con))
        _write(out, "amm.html", d.render_amm(con, ""))
        # Signaux équilibrés PAR SOURCE (150 plus récents chacune) : un « top 2000
        # global » serait écrasé par les grosses sources (Vietnam ~4700) et
        # UEMOA/Maroc/Nigeria n'apparaîtraient jamais. Les filtres tournent en JS.
        recs = []
        for s in d.all_sources(con):
            recs.extend(d.records_filtered(con, None, s, None, None, limit=150))
        # Vraie date si connue, sinon estimation statistique par source (jamais
        # la date de collecte : ferait remonter les dates inconnues en tête).
        estimates = d.date_estimates_by_source(con)
        recs.sort(key=lambda r: d._sort_key(r, estimates), reverse=True)
        html_sig = d.render_signaux(
            recs, d.all_concurrents(con), d.all_sources(con), "", "", "", "", "")
        html_sig = html_sig.replace(" · max 200", " · filtres instantanés")
        html_sig = html_sig.replace("</body>", _SIGNAUX_FILTER_JS + "</body>")
        _write(out, "signaux.html", html_sig)
        n += 7

        # Une page par pays ayant des AMM (grille cliquable → amm-XX.html).
        for pays, _count in d.amm_counts_by_pays(con):
            if not re.fullmatch(r"[A-Za-z]{2}", pays or ""):
                continue  # on ne génère que les codes ISO propres
            _write(out, f"amm-{pays}.html", d.render_amm(con, pays))
            n += 1

        # Empêche GitHub Pages de passer le site dans Jekyll (inutile ici).
        (out / ".nojekyll").write_text("", encoding="utf-8")
    finally:
        con.close()
    return n


def main():
    parser = argparse.ArgumentParser(description="Export statique du dashboard veille")
    parser.add_argument("--out", default="site", help="dossier de sortie (défaut: site)")
    args = parser.parse_args()
    n = build(args.out)
    print(f"  {n} page(s) générée(s) dans ./{args.out}")


if __name__ == "__main__":
    main()
