FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The pre-fetched DOT sign inventory. Without this the address lookup silently
# returns zero block faces in the container while working perfectly on a laptop --
# which is exactly what happened on the first deploy. It is data the app cannot
# function without, not an optional asset.
COPY data/sheepshead_signs.json ./data/sheepshead_signs.json

# 311 illegal-parking complaints, snapped to block faces. Without it every block
# reports "no complaint history" -- which looks like the neighborhood is quiet
# rather than like the file is missing. Same silent-empty failure as the sign data.
COPY data/illegal_parking_311.json ./data/illegal_parking_311.json

# Readings taken before this deploy, so the trend chart shows the whole evening
# rather than restarting from zero every time we ship. Optional -- the app works
# without it, it just has a shorter history.
COPY data/history.jsonl ./data/history.jsonl

# Cloud Run injects $PORT (8080 by default) and requires binding 0.0.0.0.
# Cloud Run containers default to UTC. Every time in this app is compared against
# posted NYC parking signs, which are New York wall-clock, so the container runs on
# New York time and any naive datetime is correct by construction. tzdata is already
# a dependency because python:3.12-slim ships no IANA database.
ENV TZ=America/New_York

ENV PORT=8080
EXPOSE 8080

# sh -c so $PORT expands at runtime, not build time.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
