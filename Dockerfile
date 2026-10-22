# Image du SAE Project Bot (lot 4). Construction : docker compose build
# Guide : docs/deployment-linux.md
FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Utilisateur sans privilèges : le bot ne tourne jamais en root.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code et contenus. Aucun secret : .env est exclu par .dockerignore et fourni au lancement.
COPY src ./src
COPY config ./config
COPY content ./content
COPY knowledge ./knowledge
COPY docs ./docs
COPY README.md ./
RUN mkdir -p data logs && chown bot:bot data logs

USER bot
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["python", "-m", "src.healthcheck"]
CMD ["python", "-m", "src.main"]
