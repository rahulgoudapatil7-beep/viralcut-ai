# ViralCut AI — Single-Container Production Dockerfile (Frontend + FastAPI + FFmpeg + Hindi Fonts)
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

# Install FFmpeg, Node.js (for yt-dlp JS runtime), and Hindi/Devanagari + Latin fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    fontconfig \
    fonts-noto-core \
    fonts-dejavu-core \
    ca-certificates \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Support either "backend/" or Windows "Backend/" folder name automatically
COPY . /tmp/repo
RUN if [ -d /tmp/repo/Backend ]; then cp -r /tmp/repo/Backend /app/backend; \
    else cp -r /tmp/repo/backend /app/backend; fi \
    && rm -rf /tmp/repo /app/backend/venv /app/backend/ffmpeg_bin /app/backend/clips /app/backend/node_modules

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    python-multipart \
    "yt-dlp[default]" \
    youtube-transcript-api \
    imageio-ffmpeg \
    httpx

# Copy built React app into /app/frontend/dist so FastAPI serves it at /
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

WORKDIR /app/backend
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-c", "import os, uvicorn; uvicorn.run('main:app', host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))"]
