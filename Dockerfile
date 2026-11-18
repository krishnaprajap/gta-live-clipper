# Ubuntu 24.04: same OS as the recommended VPS; ships FFmpeg 6.1 (tested) and Python 3.12.
FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt anthropic
COPY clipper ./clipper
COPY config/config.example.yaml ./config/config.example.yaml
COPY scripts ./scripts
# unprivileged runtime user; data/ is the only writable volume
RUN useradd --create-home --uid 10001 clipper && mkdir -p /app/data && chown -R clipper /app/data
USER clipper
ENV HF_HOME=/app/data/hf DASHBOARD_HOST=0.0.0.0 DASHBOARD_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "clipper"]
CMD ["serve"]
