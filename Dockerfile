# Pinned to an exact image, like requirements.txt; Dependabot proposes updates. Same base as Kettle.
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

ARG VERSION=dev
LABEL org.opencontainers.image.title="ASO Hub" \
      org.opencontainers.image.description="App Store and Google Play numbers, keyword rankings and to-dos in one place" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/data/aso-hub.db \
    HOST=0.0.0.0 \
    PORT=8690

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY config.toml ./
COPY aso_hub ./aso_hub

# The server's ubuntu user (1001:1001) owns /opt/appdata, so the container runs as it.
USER 1001:1001
VOLUME ["/data"]
EXPOSE 8690
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8690/healthz', timeout=4)"

# One process. The scheduled pull runs in this container through `docker exec` (see deploy/).
CMD ["python", "-m", "aso_hub", "serve"]
