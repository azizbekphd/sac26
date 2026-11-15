FROM node:22-bookworm-slim AS viewer
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src/ ./src/
RUN npm run build

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY server/ ./server/
COPY deploy/render-index-smoke.py /app/render-index-smoke.py
RUN python /app/render-index-smoke.py
COPY --from=viewer /build/dist/ ./dist/
USER 65532:65532
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn server.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
