ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        jq \
        python3 \
        python3-aiohttp \
        python3-cryptography \
        python3-pil \
        python3-qrcode

COPY run.sh /
COPY weixin_gateway /opt/weixin-gateway/weixin_gateway

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/weixin-gateway \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
