FROM node:22-bookworm-slim AS viewer
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src/ ./src/
RUN npm run build

FROM python:3.13-slim-bookworm AS detector
RUN --mount=type=bind,target=/context --mount=type=secret,id=motion-detector_b64 \
    set -eu; \
    mkdir /detector; \
    if [ -f /run/secrets/motion-detector_b64 ]; then \
      base64 -d < /run/secrets/motion-detector_b64 > /tmp/detector.tar.gz; \
      tar -xzf /tmp/detector.tar.gz -C /detector; \
      rm /tmp/detector.tar.gz; \
    else \
      tar -xzf /context/motion-detector.tar.gz -C /detector; \
    fi

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    SPHEREX_MOTION_EPHEMERAL_CACHE=1

COPY requirements.txt ./
COPY --from=detector /detector/ /usr/local/share/spherex-motion-detector/
RUN pip install --no-cache-dir -r requirements.txt -r /usr/local/share/spherex-motion-detector/requirements.txt \
    && pip check
COPY server/ ./server/
COPY --from=viewer /build/dist/ ./dist/
RUN python -c 'from server.motion_adapter import verify_frozen; verify_frozen()'

USER 65532:65532
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn server.app:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
