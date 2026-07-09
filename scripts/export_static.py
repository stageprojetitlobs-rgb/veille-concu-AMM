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
        recs = d.records_filtered(con, None, None, None, None)
        _write(out, "signaux.html", d.render_signaux(
            recs, d.all_concurrents(con), d.all_sources(con), "", "", "", "", ""))
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
