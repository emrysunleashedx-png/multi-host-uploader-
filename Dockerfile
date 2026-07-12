# Dockerfile for the NovaFlix Media Router bot (Pyrogram + Firebase + Doodstream + TMDB).
#
# python:3.11-slim is a minimal Debian-based image with Python preinstalled --
# small image size, faster builds, and includes apt so any future native
# dependency (unlike libtorrent's manylinux wheel, which doesn't need this)
# could still be added here if ever needed.
FROM python:3.11-slim

WORKDIR /app

# Copy just the dependency list first and install before copying the rest
# of the code. Docker caches each instruction as a layer -- as long as
# requirements.txt hasn't changed, this layer is reused on future builds
# even if main.py etc. changed, making rebuilds faster.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code.
COPY . .

# Northflank (like Render) sets a PORT env var for services that expose an
# HTTP endpoint. This bot's dummy HTTP server (see main.py's __main__ block)
# already reads PORT itself, so nothing extra is needed here beyond
# documenting it for anyone reading this file.
ENV PORT=8000

CMD ["python", "main.py"]
