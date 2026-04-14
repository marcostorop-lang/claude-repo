# Polymarket bot — reproducible container image.
#
# Scope: packages the *bot process* (src.main) for running a paper-
# trading loop inside a container.  The dashboard runs separately
# (Node + better-sqlite3) — this image intentionally does NOT bundle it
# so the two concerns can be scaled and restarted independently.
#
# Paper-mode by default: TRADING_MODE defaults to ``paper`` and the
# live-trading double gate (ALLOW_LIVE_TRADING=true AND
# I_UNDERSTAND_REAL_MONEY='YES_TRADE_REAL_FUNDS') is not set in this
# image.  Flipping either requires an explicit ``-e`` at runtime.
#
# Build:   docker build -t polybot .
# Run:     docker run --rm -v $PWD/data:/data polybot
# Compose: see ``docker-compose.yml`` in the repo root.

FROM python:3.12-slim AS runtime

# Non-interactive apt + deterministic pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# curl is only needed for the HEALTHCHECK probe below.  Everything
# else is pure-Python, so we keep the base layer tiny.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user — defence in depth.  The ``appuser`` UID
# matches a typical host account so bind-mounts under ``/data`` don't
# end up root-owned on the host filesystem.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Install deps first so the dep layer caches independently of source.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Source.  Keep the copy narrow — the .dockerignore drops tests,
# the SQLite files, and the dashboard so the image stays slim.
COPY src ./src
COPY docs ./docs
COPY README.md ./README.md

# Data dir for the SQLite DB, log file, alert JSONL, metrics JSONL,
# and DB backups.  Always a volume — we never bake state into the
# image.
RUN mkdir -p /data /data/backups && chown -R appuser:appuser /data /app
VOLUME ["/data"]

USER appuser

# Defaults: paper mode, DB + logs in /data, reasonable polling.
# Override with ``docker run -e KEY=value`` or compose env_file.
ENV TRADING_MODE=paper \
    LOG_LEVEL=INFO \
    SQLITE_DB_PATH=/data/polymarket_bot.db \
    LOG_FILE=/data/bot.log \
    ALERT_LOG_FILE=/data/alerts.jsonl \
    METRICS_FILE=/data/metrics.jsonl \
    DB_BACKUP_DIR=/data/backups \
    POLL_INTERVAL_SECONDS=60 \
    KILL_SWITCH_FILE=/data/KILL_SWITCH

# Healthcheck: the bot doesn't serve HTTP, so we probe the DB file's
# mtime — if the bot hasn't touched it in >3 × poll_interval it is
# either stuck or crashed.  Cheap and filesystem-only.
HEALTHCHECK --interval=120s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, sys, time; \
p=os.environ.get('SQLITE_DB_PATH','/data/polymarket_bot.db'); \
sys.exit(0 if os.path.exists(p) and (time.time()-os.path.getmtime(p))<300 else 1)"

CMD ["python", "-m", "src.main", "run"]
