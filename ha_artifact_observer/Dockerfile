# Artifact-only source context. Docker API stays disabled in public metadata.
# Supervisor may supply BUILD_FROM even without build.yaml. Do not consume it:
# BuildKit's actual Docker architecture selects a fixed Debian stage instead.
ARG TARGETARCH
FROM ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3 AS base-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian@sha256:4ecdb87bbf2e24c220140aedefdc521f7e3c499bc116c886f432261dc247d2f8 AS base-arm64
FROM base-${TARGETARCH} AS final
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i "s#http://deb.debian.org/debian#https://deb.debian.org/debian#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 update \
    && apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 install -y --no-install-recommends python3
COPY ha_artifact_observer /opt/ha-artifact-observer/ha_artifact_observer
COPY run.sh /run.sh
RUN chmod 0755 /run.sh
ENV PYTHONPATH=/opt/ha-artifact-observer PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["/run.sh"]
