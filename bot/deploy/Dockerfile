FROM python:3.11-slim

# Run as a non-root user
RUN useradd --create-home --uid 10001 polybot

WORKDIR /app

# Install Python deps first (better layer caching)
COPY bot/requirements.txt /app/bot/requirements.txt
RUN pip install --no-cache-dir -r /app/bot/requirements.txt

# Copy the bot package
COPY bot /app/bot

# Log directory (writable by the app user)
RUN mkdir -p /app/bot/logs && chown -R polybot:polybot /app

USER polybot

# Healthcheck endpoint must be enabled in the env / .env file
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request, sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/ready', timeout=3).status == 200 else 1)"

# Paper mode is the default — live trading requires three env gates
ENV TRADING_MODE=paper \
    ALLOW_LIVE_TRADING=false \
    HEALTHCHECK_ENABLED=true \
    HEALTHCHECK_HOST=0.0.0.0 \
    HEALTHCHECK_PORT=8787

CMD ["python", "-m", "bot.main", "run"]
