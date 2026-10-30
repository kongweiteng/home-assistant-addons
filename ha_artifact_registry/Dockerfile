# Do not consume Supervisor's BUILD_FROM fallback. Select the reviewed base with
# BuildKit's actual Docker architecture (HA's aarch64 maps to Docker's arm64).
ARG TARGETARCH
FROM ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3 AS base-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian@sha256:4ecdb87bbf2e24c220140aedefdc521f7e3c499bc116c886f432261dc247d2f8 AS base-arm64
FROM base-${TARGETARCH} AS final
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && timeout --signal=TERM --kill-after=5 240 apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 update \
    && timeout --signal=TERM --kill-after=5 360 apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 install -y --no-install-recommends python3 python3-cryptography \
    && rm -rf /var/lib/apt/lists/*
COPY ha_artifact_registry/__init__.py ha_artifact_registry/image.py ha_artifact_registry/service.py ha_artifact_registry/http.py ha_artifact_registry/main.py ha_artifact_registry/tls.py /opt/registry/ha_artifact_registry/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
ENV PYTHONPATH=/opt/registry PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
USER 0:0
CMD ["/run.sh"]
