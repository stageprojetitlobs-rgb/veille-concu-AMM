# Image de déploiement du dashboard de veille Lobs (dashboard + scraper).
FROM python:3.11-slim

# Dépendances système minimales (pdfplumber/pillow ont besoin de zlib/jpeg).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installe les dépendances Python d'abord (cache Docker).
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Puis le code.
COPY . .

# La base vit sur un disque persistant monté ici (cf render.yaml).
ENV VEILLE_DB_PATH=/data/veille.db
ENV PORT=8765
EXPOSE 8765

# Démarre le scraper (boot + rafraîchissement périodique) puis le dashboard.
CMD ["bash", "scripts/start.sh"]
