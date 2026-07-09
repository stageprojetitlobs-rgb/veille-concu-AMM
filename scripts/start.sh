#!/usr/bin/env bash
# Démarrage cloud : peuple la base au 1er boot si elle est vide, lance un
# rafraîchissement périodique en tâche de fond, puis démarre le dashboard.
#
# Le dashboard doit rester le processus principal (c'est lui que l'hébergeur
# surveille via la sonde /healthz). Le scraper tourne à côté, sans bloquer.
set -euo pipefail

DB="${VEILLE_DB_PATH:-/data/veille.db}"
REFRESH_HOURS="${SCRAPE_EVERY_HOURS:-24}"   # 1 fois par jour par défaut

run_scrape() {
  echo "[start] scrape en cours…"
  python -m scripts.run || echo "[start] scrape échoué (on continue)"
  echo "[start] scrape terminé."
}

# Scraping en arrière-plan (ne bloque JAMAIS le démarrage du dashboard, sinon la
# sonde de santé de l'hébergeur expire) : collecte initiale si base vide, puis
# rafraîchissement périodique.
(
  if [ ! -s "$DB" ]; then
    echo "[start] base absente → collecte initiale (en tâche de fond)."
    run_scrape
  fi
  while true; do
    sleep "$(( REFRESH_HOURS * 3600 ))"
    run_scrape
  done
) &

# Dashboard au premier plan (processus principal surveillé par l'hébergeur).
exec python -m scripts.dashboard --no-browser
