# Supervisor rejects digest-form build.yaml values and can silently fall back
# to Alpine. Bind both architectures here and ignore its BUILD_FROM argument.
ARG TARGETARCH
FROM ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3 AS base-amd64
FROM ghcr.io/home-assistant/aarch64-base-debian@sha256:4ecdb87bbf2e24c220140aedefdc521f7e3c499bc116c886f432261dc247d2f8 AS base-arm64
FROM base-${TARGETARCH} AS final
LABEL org.opencontainers.image.title="Codex Delivery" \
      org.opencontainers.image.description="Bounded durable work metadata service; execution disabled by default"

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
RUN sed -i "s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && timeout --signal=TERM --kill-after=5 240 apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 update \
    && timeout --signal=TERM --kill-after=5 360 apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=15 install -y --no-install-recommends ca-certificates python3 python3-cryptography git \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY codex_delivery /opt/codex-delivery/codex_delivery
COPY codex_work_runtime /opt/codex-delivery/codex_work_runtime
# Only fixed nonsecret policies and pinned assets enter this directory.
# Private release-authority registration is provisioned outside the image.
COPY runtime-assets/ /opt/codex-delivery/
RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/codex-delivery \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
CMD ["/run.sh"]
