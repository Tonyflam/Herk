# HELM — Railway / always-on container image.
# Python 3.12 (agent) + Node 22 (Trust Wallet Agent Kit CLI) in one image so the
# live execution adapter can shell out to `twak` exactly as it does locally.
#
# NO secrets are baked in. The wallet keystore, TWAK credentials and all arming
# flags are injected at runtime as environment variables (see docker-entrypoint.sh).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=22

# --- system + Node 22 (for the TWAK CLI) -----------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl gnupg ca-certificates build-essential git \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @trustwallet/cli \
 && apt-get purge -y gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (core + live/on-chain) ------------------------------------
COPY requirements.txt requirements-live.txt ./
RUN pip install -r requirements.txt -r requirements-live.txt

# --- app source -------------------------------------------------------------
COPY . .
RUN chmod +x docker-entrypoint.sh scripts/*.sh 2>/dev/null || true

ENTRYPOINT ["./docker-entrypoint.sh"]
