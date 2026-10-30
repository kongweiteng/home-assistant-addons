# Artifact-only source context. Docker API stays disabled in public metadata.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3
FROM ${BUILD_FROM}
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
